"""Bandcamp : retrouver les morceaux d'une playlist sur Bandcamp pour les acheter.

Bandcamp n'a pas d'API publique pour la wishlist : on se contente de trouver la
page de chaque morceau (la recherche du site, comme dans un navigateur) et c'est
l'utilisateur qui l'ouvre pour l'ajouter à sa wishlist ou l'acheter.
"""
from __future__ import annotations

import html as htmllib
import re
import threading
import time
from urllib.parse import quote_plus

import requests

from discogs import _norm, clean_title

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"}
GAP = 0.6
_net = threading.Lock()
_last = [0.0]


def _wait() -> None:
    with _net:
        d = _last[0] + GAP - time.time()
        if d > 0:
            time.sleep(d)
        _last[0] = time.time()


def search_url(artist: str, title: str) -> str:
    return "https://bandcamp.com/search?item_type=t&q=" + quote_plus(f"{artist} {title}".strip())


def _clean_url(u: str) -> str:
    return (u or "").split("?")[0]


def _api(text: str) -> list:
    """Recherche « autocomplete » qu'utilise la barre de recherche du site (réponse JSON)."""
    _wait()
    r = requests.post("https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic",
                      json={"search_text": text, "search_filter": "t", "full_page": False, "fan_id": None},
                      headers=UA, timeout=15)
    r.raise_for_status()
    out = []
    for it in ((r.json().get("auto") or {}).get("results") or []):
        if it.get("type") != "t":
            continue
        out.append({"title": it.get("name", ""), "artist": it.get("band_name", ""), "album": it.get("album_name") or "",
                    "url": _clean_url(it.get("item_url_path") or ""), "cover": it.get("img") or ""})
    return out


_LI = re.compile(r'<li class="searchresult[^"]*".*?</li>', re.S)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _page(artist: str, title: str) -> list:
    """Page de résultats classique, si la recherche JSON ne répond pas."""
    _wait()
    r = requests.get(search_url(artist, title), headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for li in _LI.findall(r.text):
        if not re.search(r'class="itemtype">\s*track', li, re.I):
            continue
        head = re.search(r'class="heading">\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.S)
        sub = _text((re.search(r'class="subhead">(.*?)</div>', li, re.S) or [None, ""])[1])
        img = re.search(r'<img src="([^"]+)"', li)
        by = re.search(r"\bby (.+)$", sub)
        frm = re.search(r"^from (.+?) by ", sub)
        if head:
            out.append({"title": _text(head.group(2)), "artist": by.group(1) if by else "", "album": frm.group(1) if frm else "",
                        "url": _clean_url(htmllib.unescape(head.group(1))), "cover": img.group(1) if img else ""})
    return out


def _score(r: dict, artist: str, title: str) -> int:
    a, ra = _norm(artist), _norm(r["artist"])
    t, rt = _norm(clean_title(title)), _norm(clean_title(r["title"]))
    s = 0
    if a and ra and (a == ra or a in ra or ra in a):
        s += 2
    if t and rt and (t == rt or t in rt or rt in t):
        s += 2
    return s


def search_track(artist: str, title: str) -> tuple[list, str]:
    """(résultats classés, état) — état : sure (artiste et titre concordent), maybe ou none."""
    res: list = []
    for fn in (lambda: _api(f"{artist} {clean_title(title)}"), lambda: _page(artist, clean_title(title))):
        try:
            res = fn()
        except (requests.RequestException, ValueError):
            res = []
        if res:
            break
    ranked = sorted(res, key=lambda r: -_score(r, artist, title))[:4]
    if not ranked:
        return [], "none"
    best = _score(ranked[0], artist, title)
    if best >= 4:
        return ranked, "sure"
    if best >= 2:
        return ranked, "maybe"
    return [], "none"


class Finder:
    def __init__(self):
        self.lock = threading.Lock()
        self.load_id = -1
        self.running = False
        self.items: dict[int, dict] = {}
        self.total = 0
        self.gen = 0

    def start(self, load_id: int, tracks: list) -> dict:
        with self.lock:
            if self.load_id == load_id and (self.running or self.items):
                return {"ok": True}
            self.gen += 1
            gen = self.gen
            self.load_id, self.running, self.total = load_id, True, len(tracks)
            self.items = {t.position: {"state": "wait", "search": search_url(t.artist, t.title)} for t in tracks}
        threading.Thread(target=self._run, args=(gen, tracks), daemon=True).start()
        return {"ok": True}

    def _run(self, gen: int, tracks: list) -> None:
        try:
            for t in tracks:
                if gen != self.gen:
                    return
                link = t.uri if re.match(r"https://[\w.-]+\.bandcamp\.com/", t.uri or "") else ""
                if link:   # playlist venue de Bandcamp : on a déjà la page
                    item = {"state": "sure", "results": [{"title": t.title, "artist": t.artist, "album": t.album,
                                                          "url": _clean_url(link), "cover": t.cover_url or t.thumb or ""}]}
                else:
                    with self.lock:
                        self.items[t.position]["state"] = "search"
                    res, state = search_track(t.artist, t.title)
                    item = {"state": state, "results": res}
                item["search"] = search_url(t.artist, t.title)
                with self.lock:
                    if gen == self.gen:
                        self.items[t.position] = item
        finally:
            with self.lock:
                if gen == self.gen:
                    self.running = False

    def status(self) -> dict:
        with self.lock:
            done = sum(1 for i in self.items.values() if i["state"] in ("sure", "maybe", "none"))
            return {"load_id": self.load_id, "running": self.running, "done": done, "total": self.total,
                    "items": {n: dict(i) for n, i in self.items.items()}}
