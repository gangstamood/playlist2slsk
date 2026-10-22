"""Discogs : retrouver les disques d'une playlist et les ajouter à sa wantlist.

Rien à voir avec Soulseek : on cherche chaque morceau dans la base Discogs avec le
jeton personnel de l'utilisateur (Réglages › Développeurs sur discogs.com), on
propose la sortie la plus probable (vinyle d'abord, pressage le plus ancien) et
on ajoute à la wantlist ce que l'utilisateur a coché.
"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from pathlib import Path

import requests

API = "https://api.discogs.com"
UA = "Playlist2Slsk/1.0 +https://github.com/gangstamood/playlist2slsk"
GAP = 1.05          # Discogs autorise 60 requêtes par minute avec un jeton
MAX_ALT = 6         # sorties proposées par morceau

_net = threading.Lock()
_last = [0.0]


class DiscogsError(Exception):
    pass


def _call(method: str, path: str, token: str, **kw) -> requests.Response:
    """Une requête à l'API, en respectant la limite de débit (et en patientant sur un 429)."""
    for attempt in range(4):
        with _net:
            wait = _last[0] + GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.request(method, API + path, timeout=20,
                                 headers={"Authorization": f"Discogs token={token}", "User-Agent": UA}, **kw)
        except requests.RequestException as e:
            if attempt == 3:
                raise DiscogsError("Discogs ne répond pas. Vérifie ta connexion.") from e
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(10 + 5 * attempt)
            continue
        if r.status_code == 401:
            raise DiscogsError("Jeton Discogs refusé. Génère-en un nouveau et colle-le à nouveau.")
        return r
    raise DiscogsError("Discogs limite les requêtes : réessaie dans une minute.")


# --------------------------------------------------------------------------- #
# Compte
# --------------------------------------------------------------------------- #
class Account:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def connect(self, token: str) -> dict:
        token = (token or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{20,80}", token):
            return {"error": "Ce n'est pas un jeton Discogs : copie la longue suite de lettres et de chiffres."}
        try:
            r = _call("GET", "/oauth/identity", token)
        except DiscogsError as e:
            return {"error": str(e)}
        if r.status_code != 200:
            return {"error": f"Discogs a répondu {r.status_code}."}
        user = r.json().get("username", "")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"token": token, "user": user}), "utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return {"ok": True, "user": user}

    def logout(self) -> dict:
        try:
            self.path.unlink()
        except OSError:
            pass
        return {"ok": True}

    def public(self) -> dict:
        a = self.load()
        return {"connected": bool(a.get("token")), "user": a.get("user", "")}


# --------------------------------------------------------------------------- #
# Recherche
# --------------------------------------------------------------------------- #
_NOISE = re.compile(r"\s*[\(\[][^\)\]]*\b(original mix|extended mix|radio edit|remaster(ed)?|feat\.?|ft\.?|with|version|mono|stereo|live)\b[^\)\]]*[\)\]]", re.I)
_DASH = re.compile(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mix|live)\b.*$", re.I)


def clean_title(title: str) -> str:
    t = _DASH.sub("", _NOISE.sub("", title or "")).strip()
    return t or (title or "").strip()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(\d+\)", "", s)          # « Artiste (2) » : homonymes chez Discogs
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _format_rank(formats: list) -> int:
    f = " ".join(formats or []).lower()
    if "vinyl" in f:
        return 0
    if "cd" in f:
        return 1
    if "cassette" in f:
        return 2
    if "file" in f:
        return 4
    return 3


def _release(it: dict) -> dict:
    title = it.get("title", "")
    artist, _, rel = title.partition(" - ")
    fmts = it.get("format") or []
    labels = it.get("label") or []
    return {
        "id": it.get("id"),
        "master": it.get("master_id") or 0,
        "artist": re.sub(r"\s*\(\d+\)", "", artist) if rel else "",
        "title": rel or title,
        "year": str(it.get("year") or ""),
        "label": labels[0] if labels else "",
        "catno": it.get("catno") or "",
        "format": ", ".join(dict.fromkeys(fmts[:3])),
        "country": it.get("country") or "",
        "thumb": it.get("cover_image") or it.get("thumb") or "",
        "url": "https://www.discogs.com" + (it.get("uri") or f"/release/{it.get('id')}"),
        "vinyl": _format_rank(fmts) == 0,
    }


def _rank(items: list, artist: str) -> list:
    a = _norm(artist)
    out, seen = [], set()
    def key(it):
        r = _release(it)
        same_artist = bool(a) and (_norm(r["artist"]) == a or a in _norm(r["artist"]))
        year = int(r["year"]) if r["year"].isdigit() else 9999
        return (0 if same_artist else 1, _format_rank(it.get("format")), year)
    for it in sorted(items, key=key):
        r = _release(it)
        group = r["master"] or ("r", r["id"])
        if group in seen or not r["id"]:
            continue
        seen.add(group)
        out.append(r)
        if len(out) >= MAX_ALT:
            break
    return out


def search_track(token: str, artist: str, title: str) -> list:
    """Sorties Discogs contenant ce morceau, la plus probable en premier."""
    tries = [{"artist": artist, "track": title}]
    clean = clean_title(title)
    if clean != title:
        tries.append({"artist": artist, "track": clean})
    tries.append({"q": f"{artist} {clean}"})
    for params in tries:
        r = _call("GET", "/database/search", token, params={**params, "type": "release", "per_page": 25})
        if r.status_code != 200:
            raise DiscogsError(f"Recherche Discogs impossible ({r.status_code}).")
        res = _rank(r.json().get("results") or [], artist)
        if res:
            return res
    return []


def wants(token: str, user: str, pages: int = 10) -> set:
    """Identifiants des sorties déjà dans la wantlist (jusqu'à 1 000)."""
    ids: set = set()
    for page in range(1, pages + 1):
        r = _call("GET", f"/users/{user}/wants", token, params={"per_page": 100, "page": page})
        if r.status_code != 200:
            break
        data = r.json()
        ids.update(w.get("id") for w in data.get("wants") or [])
        if page >= (data.get("pagination") or {}).get("pages", 1):
            break
    return ids


def add_want(token: str, user: str, release_id: int) -> bool:
    r = _call("PUT", f"/users/{user}/wants/{int(release_id)}", token)
    return r.status_code in (200, 201)


# --------------------------------------------------------------------------- #
# Tâche de fond : rapprocher toute la playlist
# --------------------------------------------------------------------------- #
class Matcher:
    def __init__(self, account: Account):
        self.account = account
        self.lock = threading.Lock()
        self.load_id = -1
        self.running = False
        self.error = ""
        self.items: dict[int, dict] = {}     # position → {state, results, choice}
        self.total = 0
        self.wanted: set = set()
        self.gen = 0

    def start(self, load_id: int, tracks: list) -> dict:
        a = self.account.load()
        if not a.get("token"):
            return {"error": "Connecte d'abord ton compte Discogs."}
        with self.lock:
            if self.load_id == load_id and (self.running or self.items):
                return {"ok": True}
            self.gen += 1
            self.load_id, self.running, self.error = load_id, True, ""
            self.items = {t.position: {"state": "wait"} for t in tracks}
            self.total = len(tracks)
            gen = self.gen
        threading.Thread(target=self._run, args=(gen, a, tracks), daemon=True).start()
        return {"ok": True}

    def _run(self, gen: int, a: dict, tracks: list) -> None:
        token, user = a["token"], a.get("user", "")
        try:
            wanted = wants(token, user) if user else set()
            with self.lock:
                self.wanted = wanted
            for t in tracks:
                if gen != self.gen:
                    return
                with self.lock:
                    self.items[t.position] = {"state": "search"}
                try:
                    res = search_track(token, t.artist, t.title)
                    item = {"state": "found" if res else "none", "results": res, "choice": 0}
                except DiscogsError as e:
                    item = {"state": "error", "error": str(e)}
                with self.lock:
                    if gen == self.gen:
                        self.items[t.position] = item
        except DiscogsError as e:
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                if gen == self.gen:
                    self.running = False

    def status(self) -> dict:
        with self.lock:
            done = sum(1 for i in self.items.values() if i["state"] in ("found", "none", "error"))
            items = {}
            for n, it in self.items.items():
                it = dict(it)
                if it.get("results"):
                    it["results"] = [dict(r, wanted=r["id"] in self.wanted) for r in it["results"]]
                items[n] = it
            return {"load_id": self.load_id, "running": self.running, "error": self.error,
                    "done": done, "total": self.total, "items": items}

    def add(self, ids: list) -> dict:
        a = self.account.load()
        if not a.get("token") or not a.get("user"):
            return {"error": "Connecte d'abord ton compte Discogs."}
        ok, failed = [], []
        for rid in ids[:500]:
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                continue
            try:
                (ok if add_want(a["token"], a["user"], rid) else failed).append(rid)
            except DiscogsError as e:
                return {"error": str(e), "added": ok}
        with self.lock:
            self.wanted.update(ok)
        return {"ok": True, "added": ok, "failed": failed, "user": a["user"]}
