#!/usr/bin/env python3
"""
playlist2slsk — interface graphique locale.

Lance un petit serveur sur 127.0.0.1 (rien n'est exposé sur le réseau) et ouvre
la page dans le navigateur. Le téléchargement lui-même est fait par
playlist2slsk.py, lancé en sous-processus : l'interface ne fait que préparer la
commande, suivre la progression et afficher le résultat.

    python3 app.py            # ou double-clic sur « Lancer.command »
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))   # _MEIPASS : appli empaquetée
FROZEN = bool(getattr(sys, "frozen", False))
try:
    VERSION = (HERE / "VERSION").read_text("utf-8").strip()
except OSError:
    VERSION = "dev"
UPDATE_REPO = os.environ.get("P2S_UPDATE_REPO", "gangstamood/playlist2slsk")   # où sont publiés les installateurs (Releases)
SOURCE_REPO = os.environ.get("P2S_SOURCE_REPO", "gangstamood/playlist2slsk")         # code source et signalements
WIN = sys.platform == "win32"
# Sous Windows, les programmes lancés en arrière-plan n'ouvrent pas de fenêtre noire.
NOWIN = {"creationflags": 0x08000000} if WIN else {}   # CREATE_NO_WINDOW
sys.path.insert(0, str(HERE))
import playlist2slsk as p2s  # noqa: E402
import discogs  # noqa: E402
import bcfind  # noqa: E402
import updater  # noqa: E402

SCRIPT = HERE / "playlist2slsk.py"
UI = HERE / "ui.html"
APP_DIR = Path.home() / ".playlist2slsk"
IMPORTS = APP_DIR / "imports"
SETTINGS = APP_DIR / "settings.json"
EDITS = APP_DIR / "edits.json"           # corrections manuelles {source: {clé: {artist, title}}}
FOLLOWS = APP_DIR / "follows.json"       # playlists suivies, mises à jour à la demande
HISTORY = APP_DIR / "history.json"       # playlists déjà chargées, les plus récentes d'abord
DIRS = APP_DIR / "dirs.json"             # dossier choisi pour une playlist précise (source → chemin)
DG_ACCOUNT = discogs.Account(APP_DIR / "discogs.json")   # jeton personnel Discogs, lisible par toi seul
DG = discogs.Matcher(DG_ACCOUNT)
BC = bcfind.Finder()                    # pages Bandcamp des morceaux de la playlist
SPOTIFY_TOKEN = APP_DIR / "spotify.json"  # connexion Spotify (jetons OAuth), lisible par toi seul
PORT = int(os.environ.get("P2S_PORT", "8765"))
DOWNLOADS = Path(os.environ.get("P2S_DOWNLOADS", str(Path.home() / "Downloads")))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

DEFAULTS = {
    "out": "~/Music/playlist2slsk",
    "quality": "lossless",          # any | lossless | only_lossless | mp3
    "min_bitrate": 0,               # kbps, 0 = pas de minimum
    "strict_conditions": False,     # refuser les fichiers au débit inconnu
    "cdj": False,                   # 16 bits / 48 kHz max
    "check_lossless": True,         # repérer les MP3 réencodés en FLAC
    "export_traktor": False,        # <playlist>.nml à importer dans Traktor
    "export_rekordbox": False,      # playlist ajoutée à <dossier racine>/rekordbox.xml
    "stale_s": 15,                  # secondes sans recevoir d'octet avant de changer de source (sockseek : 30)
    "legal_ok": False,              # avertissement d'usage accepté au premier lancement
    "check_updates": True,          # vérifier au lancement s'il existe une nouvelle version
    "rekordbox_ready": False,       # le mode d'emploi Rekordbox a déjà été montré une fois
    "pattern": "{artist} - {title}",
    "match": "normal",              # normal | strict | wide
    "fast_search": False,
    "remove_ft": False,
    "banned_users": "",
    "cover": True,
    "tags": True,
    "skip_library": False,          # ne pas retélécharger ce qui est déjà dans la bibliothèque
    "library_dirs": "",             # un dossier par ligne
    "sync_on_launch": False,        # mettre à jour les playlists suivies à l'ouverture
    "bandcamp": True,               # bouton « acheter sur Bandcamp » à côté de chaque morceau
    "extra": "",
    "spotify_id": "",
    "spotify_secret": "",
}
MATCH_ARGS = {
    "normal": [],
    "strict": ["--strict-title", "--strict-artist"],
    "wide": ["--length-tol", "10", "--desperate"],
}


# --------------------------------------------------------------------------- #
# Réglages
# --------------------------------------------------------------------------- #
CONF = Path.home() / ".config" / "sockseek" / "sockseek.conf"


def read_conf() -> dict:
    """Clés « clé = valeur » du sockseek.conf (les sections [profil] sont ignorées)."""
    out = {}
    try:
        for line in CONF.read_text("utf-8").splitlines():
            if line.strip().startswith("["):
                break
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_conf(updates: dict) -> None:
    """Met à jour quelques clés du sockseek.conf en gardant le reste du fichier intact."""
    try:
        lines = CONF.read_text("utf-8").splitlines()
    except OSError:
        lines = []
    head_end = next((i for i, l in enumerate(lines) if l.strip().startswith("[")), len(lines))
    head, tail = lines[:head_end], lines[head_end:]
    done = set()
    for i, line in enumerate(head):
        if "=" in line and not line.lstrip().startswith("#"):
            k = line.split("=", 1)[0].strip()
            if k in updates:
                head[i] = f"{k} = {updates[k]}"
                done.add(k)
    head = [l for l in head if not ("=" in l and l.split("=", 1)[0].strip() in updates and updates[l.split("=", 1)[0].strip()] == "")]
    while head and not head[-1].strip():
        head.pop()
    head += [f"{k} = {v}" for k, v in updates.items() if k not in done and v != ""]
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text("\n".join(head + ([""] + tail if tail else [])).strip("\n") + "\n", "utf-8")
    CONF.chmod(0o600)


SLSK_SERVER = (os.environ.get("P2S_SLSK_HOST", "server.slsknet.org"), int(os.environ.get("P2S_SLSK_PORT", "2242")))
LOGIN_ERRORS = {
    "INVALIDPASS": "Mot de passe incorrect pour ce pseudo.",
    "INVALIDUSERNAME": "Ce pseudo est refusé par Soulseek (trop long ou caractères interdits).",
    "INVALIDVERSION": "Soulseek refuse cette version du protocole : préviens-moi.",
    "SVRFULL": "Le serveur Soulseek est plein, réessaie dans un moment.",
    "SVRPRIVATE": "Le serveur Soulseek n'accepte pas de nouveaux comptes en ce moment.",
}


def _slsk_str(v: str) -> bytes:
    b = v.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def check_login(username: str, password: str, timeout: float = 12) -> dict:
    """Ouvre une connexion au serveur Soulseek, envoie le message Login et lit la réponse.

    Renvoie {"ok": True} si le serveur accepte, {"error": …} sinon ; {"network": True}
    quand le serveur est injoignable (on ne sait alors pas si le mot de passe est bon).
    Attention : un pseudo inconnu est créé par Soulseek avec ce mot de passe.
    """
    digest = hashlib.md5((username + password).encode("utf-8")).hexdigest()
    body = struct.pack("<I", 1) + _slsk_str(username) + _slsk_str(password) + struct.pack("<I", 160) \
        + _slsk_str(digest) + struct.pack("<I", 1)
    try:
        with socket.create_connection(SLSK_SERVER, timeout=timeout) as sock:
            sock.sendall(struct.pack("<I", len(body)) + body)
            buf = b""
            deadline = time.time() + timeout
            while time.time() < deadline:
                while len(buf) >= 8:
                    size, code = struct.unpack_from("<II", buf)
                    if len(buf) < 4 + size:
                        break
                    payload, buf = buf[8:4 + size], buf[4 + size:]
                    if code != 1:
                        continue
                    if payload[:1] == b"\x01":
                        return {"ok": True}
                    n = struct.unpack_from("<I", payload, 1)[0] if len(payload) >= 5 else 0
                    reason = payload[5:5 + n].decode("utf-8", "replace")
                    return {"error": LOGIN_ERRORS.get(reason, f"Connexion refusée par Soulseek ({reason or 'sans raison'}).")}
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        pass
    return {"error": "Impossible de joindre le serveur Soulseek. Vérifie ta connexion internet.", "network": True}


def save_account(body: dict) -> dict:
    updates = {"username": (body.get("username") or "").strip()}
    if body.get("password"):
        updates["password"] = body["password"]
    port = str(body.get("listen_port") or "").strip()
    if port and not port.isdigit():
        return {"error": "Le port doit être un nombre."}
    updates["listen-port"] = port
    if not updates["username"]:
        return {"error": "Indique ton identifiant Soulseek."}
    if any(c in v for v in updates.values() for c in "\r\n"):
        return {"error": "Retour à la ligne interdit dans l'identifiant ou le mot de passe."}
    if body.get("verify"):
        if S.running:
            return {"error": "Attends la fin du téléchargement pour tester le compte."}
        password = body.get("password") or read_conf().get("password", "")
        if not password:
            return {"error": "Indique ton mot de passe."}
        r = check_login(updates["username"], password)
        if not r.get("ok"):
            return r
    write_conf(updates)
    return {"ok": True}


def load_edits() -> dict:
    try:
        return json.loads(EDITS.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def apply_edits(tracks: list, source: str) -> None:
    edits = load_edits().get(source, {})
    for t in tracks:
        t.orig_key = t.key          # identité avant correction : c'est elle que le script retrouve
        e = edits.get(t.orig_key)
        if e:
            t.artists = p2s._split_artists(e.get("artist") or t.artist)
            t.title = e.get("title") or t.title


def replace_tracks(body: dict) -> dict:
    """Supprime les faux FLAC choisis et relance ces morceaux en écartant leurs sources."""
    with S.lock:
        if S.running:
            return {"error": "Attends la fin du téléchargement en cours."}
        dest = Path(S.dest)
        positions, ban = [], set()
        for n in body.get("positions") or []:
            d = S.details.get(int(n)) or {}
            p = Path(d.get("path") or "")
            if p.is_file() and dest in p.parents:     # uniquement nos propres téléchargements
                p.unlink()
                positions.append(int(n))
                if d.get("user"):
                    ban.add(d["user"])
    if not positions:
        return {"error": "Aucun fichier à remplacer."}
    return start_job({**(body.get("settings") or {}), "only": positions, "ban": sorted(ban),
                      "name": body.get("name"), "limit": body.get("limit")})


def save_edit(body: dict) -> dict:
    with S.lock:
        if S.running:
            return {"error": "Attends la fin du téléchargement en cours."}
        t = next((t for t in S.tracks if t.position == int(body.get("n") or 0)), None)
        if not t:
            return {"error": "Morceau introuvable."}
        artist, title = (body.get("artist") or "").strip(), (body.get("title") or "").strip()
        if not title:
            return {"error": "Le titre ne peut pas être vide."}
        edits = load_edits()
        edits.setdefault(S.source, {})[t.orig_key] = {"artist": artist, "title": title}
        APP_DIR.mkdir(parents=True, exist_ok=True)
        EDITS.write_text(json.dumps(edits, indent=2, ensure_ascii=False), "utf-8")
        t.artists = p2s._split_artists(artist)
        t.title = title
        return {"ok": True, "track": track_dict(t)}


def update_settings(body: dict) -> dict:
    s = load_settings()
    s.update({k: body[k] for k in DEFAULTS if k in body and k != "spotify_secret"})
    if body.get("spotify_secret"):
        s["spotify_secret"] = body["spotify_secret"]
    save_settings(s)
    return {"ok": True}

def _bundled_spotify_id() -> str:
    """Client ID Spotify fourni avec l'appli partagée (pas secret : connexion PKCE)."""
    try:
        return (HERE / "spotify-client-id.txt").read_text("utf-8").strip()
    except OSError:
        return ""


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text("utf-8")))
    except (OSError, ValueError):
        pass
    if not s["spotify_id"]:
        s["spotify_id"] = _bundled_spotify_id()
    return s


def save_settings(s: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    keep = {k: s[k] for k in DEFAULTS if k in s}
    SETTINGS.write_text(json.dumps(keep, indent=2, ensure_ascii=False), "utf-8")
    try:
        SETTINGS.chmod(0o600)   # contient éventuellement le secret Spotify
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# État : une playlist chargée, un téléchargement à la fois
# --------------------------------------------------------------------------- #
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.source = ""            # chemin du CSV importé ou URL Spotify
        self.name = ""
        self.tracks: list[p2s.Track] = []
        self.folder = ""            # nom du dossier de destination (historique, suivi ou nom de la source)
        self.load_id = 0            # change à chaque playlist chargée : l'interface se recale dessus
        self.job_id = 0
        self.reset_job()

    def reset_job(self):
        self.proc: subprocess.Popen | None = None
        self.running = False
        self.lines: list[str] = []
        self.status: dict[int, str] = {}     # position → skip|queued|search|dl|ok|miss|stopped
        self.details: dict[int, dict] = {}   # position → infos sockseek (format, débit, raison…)
        self.keys: dict[int, tuple] = {}     # position → (artiste, titre) tels qu'envoyés à sockseek
        self.todo: list[int] = []            # positions envoyées à sockseek, dans l'ordre
        self.post_i = 0
        self.dest = ""
        self.rc: int | None = None
        self.stopped = False
        self.started = 0.0
        self.exports: dict[str, str] = {}   # traktor|rekordbox → fichier écrit
        self.jobs: dict[str, int] = {}      # jobId sockseek → position (download_progress ne donne que le jobId)
        self.events: list[dict] = []        # derniers évènements sockseek marquants, pour le diagnostic


S = State()


def track_dict(t: p2s.Track) -> dict:
    return {"n": t.position, "artist": t.artist, "title": t.title, "album": t.album,
            "duration": t.duration_s, "cover": p2s._upgrade_scdn(t.cover_url) if t.cover_url else t.thumb,
            "bandcamp": t.uri if re.match(r"https://[\w.-]+/", t.uri) and "bandcamp.com/" in t.uri else ""}


def load_source(body: dict) -> dict:
    settings = load_settings()
    if body.get("source"):                      # réouverture depuis l'historique
        src = body["source"]
        if followable(src):
            body = {**body, "url": src}
        else:
            path = Path(src)
            if path.parent != IMPORTS or not path.is_file():
                return {"error": "Ce CSV n'est plus disponible : dépose-le à nouveau."}
            try:
                tracks = p2s.read_csv_tracks(path)
            except SystemExit as e:
                return {"error": str(e.code)}
            return _finish_load(tracks, path.stem, str(path))
    if body.get("download"):                    # CSV qu'Exportify vient de déposer dans Téléchargements
        path = DOWNLOADS / Path(body["download"]).name
        if path.suffix.lower() != ".csv" or not path.is_file():
            return {"error": "Ce fichier n'est plus dans Téléchargements."}
        body = {"filename": path.stem.replace("_", " ") + ".csv", "text": path.read_bytes().decode("utf-8-sig", "replace")}
    p2s.NOTICES.clear()
    if body.get("url"):
        url = body["url"].strip()
        cid = body.get("spotify_id") or settings["spotify_id"] or os.environ.get("SPOTIFY_CLIENT_ID", "")
        sec = body.get("spotify_secret") or settings["spotify_secret"] or os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        try:
            if p2s.YTDLP_RE.match(url):
                tracks, name = p2s.read_ytdlp_url(url)
            elif p2s.DEEZER_RE.match(url):
                tracks, name = p2s.read_deezer_url(url)
            elif p2s.BANDCAMP_RE.match(url):
                tracks, name = p2s.read_bandcamp_url(url)
            else:
                tracks, name = p2s.read_spotify_url(url, cid, sec, spotify_user_token())
        except SystemExit as e:
            return {"error": str(e.code)}
        except Exception as e:  # réseau, etc.
            return {"error": f"Impossible de lire ce lien : {e}"}
        source = url
    else:
        filename = body.get("filename") or "playlist.csv"
        stem = p2s.safe_name(Path(filename).stem)
        IMPORTS.mkdir(parents=True, exist_ok=True)
        path = IMPORTS / f"{stem}.csv"
        path.write_text(body.get("text", ""), "utf-8")
        try:
            tracks = p2s.read_csv_tracks(path)
        except SystemExit as e:
            return {"error": str(e.code)}
        except Exception as e:
            return {"error": f"CSV illisible : {e}"}
        name, source = stem, str(path)
    return _finish_load(tracks, name, source)


def _finish_load(tracks: list, name: str, source: str) -> dict:
    if not tracks:
        return {"error": "Aucun morceau trouvé dans cette playlist."}
    apply_edits(tracks, source)
    for i, t in enumerate(tracks, 1):
        t.position = i
    h = find_history(source) or {}
    folder = (find_follow(source) or {}).get("name") or h.get("folder") or p2s.safe_name(name)
    present = scan_present(tracks, folder, source)
    with S.lock:
        if S.running:
            return {"error": "Un téléchargement est déjà en cours."}
        S.source, S.name, S.tracks, S.folder = source, name, tracks, folder
        S.load_id += 1
        S.reset_job()
        for pos, path in present.items():      # déjà téléchargés : affichés cochés et écoutables
            S.status[pos] = "skip"
            S.details[pos] = {"path": str(path)}
    record_history(source, name, tracks, have=len(present))
    out = current()
    if p2s.NOTICES:
        out["notice"] = " ".join(p2s.NOTICES)
    return out


def scan_present(tracks: list, folder: str, source: str | None = None) -> dict:
    """Morceaux déjà présents dans le dossier de la playlist (même règle que le script)."""
    s = load_settings()
    dest = playlist_dest(S.source if source is None else source, folder, s)
    if not dest.is_dir():
        return {}
    width = max(2, len(str(len(tracks))))
    out = {}
    for t in tracks:
        try:
            f = p2s.existing_file(dest, p2s.render_name(s["pattern"] or DEFAULTS["pattern"], t, width))
        except Exception:
            f = None
        if f:
            out[t.position] = f
    return out


# --------------------------------------------------------------------------- #
# Historique des playlists chargées
# --------------------------------------------------------------------------- #
def load_dirs() -> dict:
    """Dossier choisi pour chaque playlist (source → chemin), s'il y en a un."""
    try:
        return json.loads(DIRS.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def custom_dir(source: str) -> str:
    return load_dirs().get(source, "") if source else ""


def playlist_dest(source: str, folder: str, settings: dict | None = None) -> Path:
    """Où va une playlist : son dossier à elle si on l'a choisi, sinon un sous-dossier du dossier commun."""
    d = custom_dir(source)
    if d:
        return Path(d).expanduser()
    s = settings or load_settings()
    return Path(s["out"] or DEFAULTS["out"]).expanduser() / p2s.safe_name(folder)


def set_dir(body: dict) -> dict:
    """Lie un dossier de l'ordinateur à une playlist (chemin vide : retour au dossier commun)."""
    source = body.get("source") or S.source
    if not source:
        return {"error": "Charge d'abord une playlist."}
    path = (body.get("path") or "").strip()
    dirs = load_dirs()
    if path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            return {"error": "Choisis un dossier complet (ex. ~/Music/Techno)."}
        if p.exists() and not p.is_dir():
            return {"error": "Ce chemin est un fichier, pas un dossier."}
        dirs[source] = path
    else:
        dirs.pop(source, None)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    DIRS.write_text(json.dumps(dirs, indent=2, ensure_ascii=False), "utf-8")
    if source == S.source and not S.running:     # recoche ce qui est déjà dans le nouveau dossier
        present = scan_present(S.tracks, S.folder)
        with S.lock:
            for pos in [p for p, st in S.status.items() if st == "skip"]:
                S.status.pop(pos, None)
                S.details.pop(pos, None)
            for pos, f in present.items():
                S.status[pos] = "skip"
                S.details[pos] = {"path": str(f)}
    return {"ok": True, "dir": path, "current": current() if source == S.source else None}


def load_history() -> list:
    try:
        return json.loads(HISTORY.read_text("utf-8"))
    except (OSError, ValueError):
        return []


def save_history(items: list) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(items[:60], indent=2, ensure_ascii=False), "utf-8")


def find_history(source: str) -> dict | None:
    return next((h for h in load_history() if h["source"] == source), None)


def record_history(source: str, name: str, tracks: list, **fields) -> None:
    items = load_history()
    old = next((h for h in items if h["source"] == source), {})
    items = [h for h in items if h["source"] != source]
    cover = next((track_dict(t)["cover"] for t in tracks if t.cover_url or t.thumb), "")
    entry = {**old, "source": source, "name": name, "kind": source_kind(source) if followable(source) else "csv",
             "count": len(tracks), "cover": cover or old.get("cover", ""), "opened": int(time.time()), **fields}
    save_history([entry] + items)


def update_history(source: str, **fields) -> None:
    items = load_history()
    for h in items:
        if h["source"] == source:
            h.update(fields)
    save_history(items)


def _rekordbox_app() -> Path | None:
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            apps = sorted(root.glob("rekordbox*/rekordbox*.app")) + sorted(root.glob("rekordbox*.app"))
            if apps:
                return apps[-1]                 # la version la plus récente
    elif WIN:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
            exes = sorted(Path(root).glob("Pioneer/rekordbox*/rekordbox.exe")) + sorted(Path(root).glob("AlphaTheta/rekordbox*/rekordbox.exe"))
            if exes:
                return exes[-1]
    return None


def send_rekordbox(body: dict) -> dict:
    """Ajoute la playlist chargée au rekordbox.xml (fichiers déjà téléchargés) et ouvre Rekordbox."""
    with S.lock:
        if S.running:
            return {"error": "Attends la fin du téléchargement en cours."}
        tracks = []
        for t in S.tracks:
            p = Path((S.details.get(t.position) or {}).get("path") or "")
            if p.is_file():
                t.final_path = p
                tracks.append(t)
        name = p2s.safe_name(S.folder or S.name or "playlist")
    if not tracks:
        return {"error": "Aucun morceau de cette playlist n'est encore téléchargé."}
    settings = load_settings()
    xml = Path(settings["out"] or DEFAULTS["out"]).expanduser() / "rekordbox.xml"
    p2s.update_rekordbox_xml(xml, name, tracks)
    first = not settings.get("rekordbox_ready")
    if first:
        settings["rekordbox_ready"] = True
        save_settings(settings)
    app = _rekordbox_app()
    if app and not body.get("no_open"):
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", str(app)])
        else:
            subprocess.Popen([str(app)], **NOWIN)
    return {"ok": True, "count": len(tracks), "name": name, "xml": str(xml), "xml_display": _tilde(xml),
            "opened": bool(app), "first": first}


def history() -> dict:
    """Toutes les playlists connues (déjà chargées ou suivies), pour la mosaïque de l'onglet Playlists."""
    follows = {f["source"]: f for f in load_follows()}
    dirs = load_dirs()
    items = load_history()
    known = {h["source"] for h in items}
    items += [{**f, "opened": f.get("added", 0)} for src, f in follows.items() if src not in known]
    for h in items:
        f = follows.get(h["source"])
        h["followable"] = followable(h["source"])
        h["followed"] = bool(f)
        if f:
            h.update({k: f.get(k) for k in ("synced", "new", "missing", "error")})
            h["cover"] = h.get("cover") or f.get("cover", "")
        h["dir"] = dirs.get(h["source"], "")
    return {"history": items, "sync": {k: SYNC[k] for k in ("active", "index", "total", "current", "added")}}


def forget_history(body: dict) -> dict:
    save_history([h for h in load_history() if h["source"] != body.get("source")] if body.get("source") else [])
    return {"ok": True}


def newest_download_csv(since: float) -> dict:
    """Le CSV le plus récent arrivé dans Téléchargements après `since` (secondes epoch)."""
    best = None
    try:
        for f in DOWNLOADS.iterdir():
            if f.suffix.lower() != ".csv" or not f.is_file():
                continue
            st = f.stat()
            if st.st_mtime > since and st.st_size > 0 and (best is None or st.st_mtime > best[0]):
                best = (st.st_mtime, f)
    except OSError as e:
        return {"error": f"Impossible de lire le dossier Téléchargements ({e.strerror}). "
                         "Autorise l'accès dans Réglages Système › Confidentialité › Fichiers et dossiers."}
    if best and time.time() - best[0] > 1.5:    # laisse au navigateur le temps de finir l'écriture
        return {"file": best[1].name}
    return {}


def current() -> dict:
    """La playlist chargée, telle que l'interface l'affiche."""
    f = find_follow(S.source)
    return {"name": S.folder or (f or {}).get("name") or S.name, "source": S.source, "load_id": S.load_id,
            "status": S.status, "details": S.details,
            "tracks": [track_dict(t) for t in S.tracks],
            "followable": followable(S.source), "followed": bool(f), "dir": custom_dir(S.source),
            "dest_display": _tilde(playlist_dest(S.source, S.folder or S.name)) if S.source else ""}


def _tilde(p: Path) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


# --------------------------------------------------------------------------- #
# Lancement et suivi
# --------------------------------------------------------------------------- #
def start_job(body: dict) -> dict:
    # Les réglages sont enregistrés par /api/settings ; ici on applique seulement
    # ceux envoyés pour ce lancement (ex. « Réessayer » en recherche élargie).
    settings = load_settings()
    settings.update({k: body[k] for k in DEFAULTS if k in body and k != "spotify_secret"})

    with S.lock:
        if S.running:
            return {"error": "Un téléchargement est déjà en cours."}
        if not S.tracks:
            return {"error": "Charge d'abord une playlist."}
        limit = int(body.get("limit") or 0)
        tracks = S.tracks[:limit] if limit > 0 else S.tracks
        name = p2s.safe_name(body.get("name") or S.name)
        pattern = settings["pattern"] or DEFAULTS["pattern"]
        dest = playlist_dest(S.source, name, settings)
        only = [int(x) for x in body.get("only") or []]

        bin_path = p2s.find_sockseek(None)
        if not bin_path:
            return {"error": "sockseek est introuvable. Installe-le d'abord (voir le README)."}

        # Relance de quelques morceaux : on garde l'état des autres pour le bilan.
        keep = (dict(S.status), dict(S.details)) if only else ({}, {})
        S.reset_job()
        S.status, S.details = keep
        for t in tracks:
            if only and t.position not in only:
                continue
            S.status[t.position] = "queued"     # le script précise ensuite : skip, lib, todo…
            S.details.pop(t.position, None)
            S.keys[t.position] = (_k(t.search_artist), _k(t.search_title))

        # Appli empaquetée : le même exécutable fait tourner le script (desktop.py --p2s-script).
        runner = [sys.executable, "--p2s-script"] if FROZEN else [sys.executable, "-u", str(SCRIPT)]
        cmd = [*runner, S.source, "-o", settings["out"] or DEFAULTS["out"],
               "--name", name, "--pattern", pattern]
        if custom_dir(S.source):
            cmd += ["--dest", str(dest)]
        if settings["quality"] == "only_lossless":
            cmd.append("--only-lossless")
        elif settings["quality"] == "lossless":
            cmd.append("--lossless")
        if limit > 0:
            cmd += ["-n", str(limit)]
        if not settings["cover"]:
            cmd.append("--no-cover")
        if not settings["tags"]:
            cmd.append("--no-tags")
        if only:
            cmd += ["--only", ",".join(map(str, only))]
        if settings["check_lossless"]:
            cmd.append("--check-lossless")
        if settings["export_traktor"]:
            cmd.append("--traktor")
        if settings["export_rekordbox"]:
            cmd += ["--rekordbox-xml", str(Path(settings["out"] or DEFAULTS["out"]).expanduser() / "rekordbox.xml")]
        ban = [u for u in body.get("ban") or [] if u]
        if ban:   # « Chercher une autre version » : on écarte les sources des faux FLAC
            settings["banned_users"] = ",".join(filter(None, [settings["banned_users"], *ban]))
        edits = load_edits().get(S.source)
        if edits:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            (APP_DIR / "edits_run.json").write_text(json.dumps(edits, ensure_ascii=False), "utf-8")
            cmd += ["--edits", str(APP_DIR / "edits_run.json")]
        if settings["skip_library"]:
            for d in settings["library_dirs"].splitlines():
                if d.strip():
                    cmd += ["--library", d.strip()]
        extra = sockseek_args(settings)
        if progress_json_supported(bin_path):
            extra.append("--progress-json")
        if extra:
            cmd += ["--", *extra]

        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        if CONF.exists() and option_supported(bin_path, "--config"):
            env["P2S_SOCKSEEK_CONF"] = str(CONF)
        if settings["spotify_id"]:
            env["SPOTIFY_CLIENT_ID"] = settings["spotify_id"]
        if settings["spotify_secret"]:
            env["SPOTIFY_CLIENT_SECRET"] = settings["spotify_secret"]
        if S.source.startswith(("spotify:", "https://open.spotify.com")):
            env["SPOTIFY_USER_TOKEN"] = spotify_user_token()

        S.dest = str(dest)
        S.folder = name
        threading.Thread(target=update_history, args=(S.source,), kwargs={"folder": name}, daemon=True).start()
        S.job_id += 1
        S.started = time.time()
        S.running = True
        group = NOWIN if WIN else {"start_new_session": True}
        S.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL, env=env, **group)
    threading.Thread(target=_read_output, args=(S.proc,), daemon=True).start()
    threading.Thread(target=_watch_staging, args=(dest.parent,), daemon=True).start()
    return {"ok": True, "status": S.status, "dest": S.dest}


def sockseek_args(s: dict) -> list[str]:
    """Traduit les réglages de l'interface en options sockseek (passées après « -- »)."""
    a: list[str] = []
    if s["quality"] == "mp3":
        a += ["--format", "mp3"]
    if int(s["min_bitrate"] or 0) > 0:
        a += ["--min-bitrate", str(int(s["min_bitrate"]))]
    if s["strict_conditions"]:
        a.append("--strict-conditions")
    if s["cdj"]:
        a += ["--max-bitdepth", "16", "--max-samplerate", "48000"]
    a += MATCH_ARGS.get(s["match"], [])
    if s["fast_search"]:
        a.append("--fast-search")
    stale = int(s.get("stale_s") or 0)
    if stale > 0:                       # source muette : on passe plus vite à une autre
        a += ["--max-stale-time", str(stale * 1000)]
    if s["remove_ft"]:
        a.append("--remove-ft")
    banned = ",".join(u.strip() for u in s["banned_users"].replace("\n", ",").split(",") if u.strip())
    if banned:
        a += ["--banned-users", banned]
    return a + s["extra"].split()


_PJ_CACHE: dict[str, str] = {}


def _help_text(bin_path: str) -> str:
    if bin_path not in _PJ_CACHE:
        text = ""
        for args in (["--help"], ["--help", "all"]):
            try:
                r = subprocess.run([bin_path, *args], capture_output=True, text=True, timeout=15,
                                   encoding="utf-8", errors="replace", **NOWIN)
                text += (r.stdout or "") + (r.stderr or "")
            except (OSError, subprocess.SubprocessError):
                pass
        _PJ_CACHE[bin_path] = text
    return _PJ_CACHE[bin_path]


def option_supported(bin_path: str, opt: str) -> bool:
    return opt in _help_text(bin_path)


def progress_json_supported(bin_path: str) -> bool:
    """--progress-json donne le détail de chaque morceau ; on vérifie que cette version le connaît."""
    return option_supported(bin_path, "progress-json")


def _k(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def _find_pos(data: dict) -> int | None:
    """Retrouve le morceau d'un évènement sockseek : par le fichier N.ext, sinon par artiste/titre."""
    path = data.get("downloadPath") or ""
    m = re.search(r"(?:^|[\\/])(\d+)\.\w+$", path)
    if m and 0 < int(m.group(1)) <= len(S.todo):
        return S.todo[int(m.group(1)) - 1]
    a, t = _k(data.get("artist")), _k(data.get("title"))
    for pos, (ka, kt) in S.keys.items():
        if (ka, kt) == (a, t):
            return pos
    for pos, (ka, kt) in S.keys.items():   # titre retouché par sockseek (--remove-ft, regex…)
        if ka == a and t and (kt.startswith(t) or t.startswith(kt)):
            return pos
    return None


def _num(data: dict, *keys: str) -> float | None:
    """Premier champ numérique trouvé parmi `keys` (les noms exacts varient selon la version de sockseek)."""
    low = {k.lower(): v for k, v in data.items()}
    for k in keys:
        v = low.get(k.lower())
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


_SAMPLES: dict[str, int] = {}


def _sample(ev: dict) -> None:
    """Garde quelques évènements sockseek de chaque type (aide à affiner la progression)."""
    kind = str(ev.get("type"))
    if _SAMPLES.get(kind, 0) >= 3:
        return
    _SAMPLES[kind] = _SAMPLES.get(kind, 0) + 1
    try:
        with (APP_DIR / "sockseek-events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass


_ROUTINE = ("download_progress", "search_start", "download_start")


def _on_event(ev: dict) -> None:
    _sample(ev)
    kind, data = ev.get("type"), ev.get("data") or {}
    if kind not in _ROUTINE and not (kind == "track_state" and data.get("terminalOutcome") in (None, "None")):
        S.events.append(ev)
        del S.events[:-25]
    pos = _find_pos(data) if data.get("artist") or data.get("downloadPath") else S.jobs.get(str(data.get("jobId")))
    if pos is None and kind == "download_progress":
        # 1er message d'un transfert : on le rattache au morceau dont le fichier a exactement cette taille
        size = _num(data, "totalBytes")
        cands = [p for p, d in S.details.items() if S.status.get(p) == "dl" and size and d.get("size") == size]
        if len(cands) == 1:
            pos = cands[0]
            S.jobs[str(data.get("jobId"))] = pos
    if pos is None:
        return
    d = S.details.setdefault(pos, {})
    if kind == "search_start":
        if S.status.get(pos) == "queued":
            S.status[pos] = "search"
    elif kind == "download_start":
        if S.status.get(pos) in ("queued", "search"):
            S.status[pos] = "dl"
        if d.get("user") and d.get("user") != data.get("username"):
            d["tries"] = d.get("tries", 1) + 1        # la source précédente a lâché : sockseek en essaie une autre
        d.update(user=data.get("username"), ext=data.get("extension"), size=data.get("size"),
                 got=0, started=time.time(), moved=time.time())
    elif kind == "track_state" and data.get("terminalOutcome") in (None, "None"):
        if data.get("activityPhase") not in (None, "None"):
            d["phase"] = data["activityPhase"]
    elif kind == "download_progress":
        if S.status.get(pos) in ("queued", "search"):
            S.status[pos] = "dl"
        got = _num(data, "bytesTransferred", "bytesDownloaded", "transferred", "downloaded", "bytes")
        size = _num(data, "totalBytes", "size", "fileSize", "total")
        pct = _num(data, "percent", "percentage", "progress", "progressPercent", "progressPercentage")
        if got is not None:
            if got > d.get("got", 0):
                d["moved"] = time.time()      # dernier octet reçu : sert à repérer une source bloquée
            d["got"] = max(got, d.get("got", 0))
        if size:
            d["size"] = size
        if pct is not None and not (got is not None and size):
            d["pct"] = pct / 100 if pct > 1 else pct
    elif kind == "track_state" and data.get("terminalOutcome") not in (None, "None"):
        d.update({k: v for k, v in {
            "outcome": data.get("terminalOutcome"), "reason": data.get("failureReason"),
            "user": data.get("username"), "ext": data.get("extension"), "size": data.get("size"),
            "bitrate": data.get("bitRate"), "results": data.get("rawResultCount"),
            "locked": data.get("lockedCount"),
        }.items() if v is not None})


def _add_line(line: str) -> None:
    line = ANSI.sub("", line).rstrip()
    if not line.strip():
        return
    if line.startswith("@@P2S "):   # évènements du script : état de chaque morceau
        try:
            ev = json.loads(line[6:])
        except ValueError:
            return
        with S.lock:
            if "todo" in ev:
                S.todo = ev["todo"]
            elif "export" in ev:
                S.exports[ev["export"]] = ev["path"]
            elif "pos" in ev:
                S.status[ev["pos"]] = ev["state"]
                d = S.details.setdefault(ev["pos"], {})
                for k in ("path", "quality"):
                    if ev.get(k):
                        d[k] = ev[k]
        return
    if line.startswith('{"type"'):
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if ev:
            with S.lock:
                _on_event(ev)
            return
    with S.lock:
        S.lines.append(line)
        if len(S.lines) > 5000:
            del S.lines[:1000]


def _read_output(proc: subprocess.Popen) -> None:
    buf = b""
    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"\r\n|\n|\r", buf)
        buf = parts.pop()
        for p in parts:
            _add_line(p.decode("utf-8", "replace"))
    if buf:
        _add_line(buf.decode("utf-8", "replace"))
    rc = proc.wait()
    with S.lock:
        S.rc = rc
        S.running = False
        for pos, st in S.status.items():
            if st in ("queued", "search", "dl"):
                S.status[pos] = "stopped" if S.stopped else "miss"
        have = sum(1 for st in S.status.values() if st in ("ok", "skip", "lib"))
        source, folder = S.source, S.folder
    update_history(source, have=have, downloaded=int(time.time()), folder=folder)


def _watch_staging(parent: Path) -> None:
    """sockseek nomme chaque fichier d'après son n° de ligne : 3.flac → 3e morceau de la liste."""
    while S.running:
        try:
            for d in parent.glob(".p2s_tmp_*"):
                if d.stat().st_mtime < S.started - 5:
                    continue
                for f in d.rglob("*"):
                    m = re.match(r"(\d+)\.", f.name)      # 3.flac, ou 3.flac.incomplete pendant le transfert
                    if not m or not f.is_file():
                        continue
                    i = int(m.group(1)) - 1
                    size = f.stat().st_size
                    with S.lock:
                        if 0 <= i < len(S.todo):
                            pos = S.todo[i]
                            if S.status.get(pos) in ("queued", "search"):
                                S.status[pos] = "dl"
                            det = S.details.setdefault(pos, {})
                            if size > det.get("got", 0):
                                det["moved"] = time.time()
                            det["got"] = max(size, det.get("got", 0))
        except OSError:
            pass
        time.sleep(0.7)


def stop_job() -> dict:
    SYNC["cancel"] = True
    with S.lock:
        if S.proc and S.running:
            S.stopped = True
            if WIN:   # arrête le script et sockseek d'un coup
                subprocess.run(["taskkill", "/PID", str(S.proc.pid), "/T", "/F"], capture_output=True, **NOWIN)
                return {"ok": True}
            try:
                os.killpg(S.proc.pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                S.proc.terminate()
    return {"ok": True}


def job_progress() -> dict:
    """Deux avancements : la recherche (morceaux déjà cherchés) et le global (recherche + transferts + rangement)."""
    todo = S.todo or [p for p, st in S.status.items() if st not in ("skip", "lib")]
    if not todo:
        return {"search": 0, "searching": 0, "searched": 0, "total": 0, "global": 0, "got": 0, "size": 0, "downloaded": 0}
    searched = searching = downloaded = 0
    got_b = size_b = 0.0
    score = 0.0
    for pos in todo:
        st, d = S.status.get(pos, "queued"), S.details.get(pos) or {}
        if st in ("ok", "miss", "stopped", "skip", "lib"):        # rangé (ou abandonné) : terminé
            searched += 1
            score += 1
            downloaded += st == "ok"
            continue
        if d.get("outcome"):                                      # sockseek a fini ce morceau
            searched += 1
            done = d["outcome"] in ("Succeeded", "Success", "Completed", "Downloaded")
            downloaded += done
            score += 0.25 + 0.6 + (0 if done else 0.15)
            if done and d.get("size"):
                got_b += d["size"]; size_b += d["size"]
            continue
        if st == "dl" or d.get("user"):                           # recherche finie, transfert en cours
            searched += 1
            size = d.get("size") or 0
            frac = min(d.get("got", 0) / size, 1) if size else d.get("pct", 0.1)
            if size:
                got_b += min(d.get("got", 0), size); size_b += size
            score += 0.25 + 0.6 * frac
        elif st == "search":
            searching += 1
            score += 0.08
    n = len(todo)
    return {"search": searched / n, "searching": searching, "searched": searched, "total": n,
            "global": score / n, "got": got_b, "size": size_b, "downloaded": downloaded}


def job_status(since: int) -> dict:
    with S.lock:
        counts = {}
        for st in S.status.values():
            counts[st] = counts.get(st, 0) + 1
        missing = Path(S.dest) / "missing.csv" if S.dest else None
        return {
            "running": S.running, "rc": S.rc, "stopped": S.stopped, "dest": S.dest,
            "dest_display": S.dest.replace(str(Path.home()), "~", 1),
            "lines": S.lines[since:], "next": len(S.lines), "exports": S.exports,
            "load_id": S.load_id, "job_id": S.job_id, "name": S.name,
            "sync": {k: SYNC[k] for k in ("active", "index", "total", "current", "added")},
            "status": S.status, "counts": counts, "details": S.details, "progress": job_progress(), "now": time.time(),
            "missing_csv": str(missing) if missing and missing.exists() and not S.running else "",
        }


# --------------------------------------------------------------------------- #
# Playlists suivies : on relance le téléchargement, seuls les nouveaux morceaux
# partent (le script saute ceux déjà présents dans le dossier).
# --------------------------------------------------------------------------- #
SYNC = {"active": False, "index": 0, "total": 0, "current": "", "cancel": False, "added": 0}


def followable(source: str) -> bool:
    return source.startswith(("http://", "https://", "spotify:"))


def load_follows() -> list:
    try:
        return json.loads(FOLLOWS.read_text("utf-8"))
    except (OSError, ValueError):
        return []


def save_follows(items: list) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    FOLLOWS.write_text(json.dumps(items, indent=2, ensure_ascii=False), "utf-8")


def find_follow(source: str) -> dict | None:
    return next((f for f in load_follows() if f["source"] == source), None)


def source_kind(url: str) -> str:
    u = url.lower()
    return "youtube" if "youtu" in u else "soundcloud" if "soundcloud" in u else "spotify" if "spotify" in u \
        else "deezer" if "deezer" in u or "dzr." in u else "bandcamp" if "bandcamp.com" in u else "web"


def follow(body: dict) -> dict:
    """Suit (ou ne suit plus) une playlist ; par défaut celle qui est chargée."""
    source = body.get("source") or S.source
    items = [f for f in load_follows() if f["source"] != source]
    if body.get("follow", True):
        if not followable(source):
            return {"error": "Seuls les liens Spotify, Deezer, YouTube ou SoundCloud peuvent être suivis : un CSV ne change pas tout seul."}
        with S.lock:
            mine = source == S.source
            name = p2s.safe_name(body.get("name") or (S.name if mine else "") or "playlist")
            cover = next((track_dict(t)["cover"] for t in S.tracks if t.cover_url or t.thumb), "") if mine else ""
            count = len(S.tracks) if mine else 0
        items.append({"source": source, "name": name, "kind": source_kind(source), "cover": cover,
                      "count": count, "added": int(time.time()), "synced": 0, "new": 0, "missing": 0, "error": ""})
    save_follows(items)
    return {"ok": True, "follows": items}


def list_follows() -> dict:
    dirs = load_dirs()
    items = [{**f, "dir": dirs.get(f["source"], "")} for f in load_follows()]
    return {"follows": items, "sync": {k: SYNC[k] for k in ("active", "index", "total", "current", "added")}}


def _record(source: str, **fields) -> None:
    items = load_follows()
    for f in items:
        if f["source"] == source:
            f.update(fields)
    save_follows(items)


def sync_follows(body: dict) -> dict:
    """Met à jour une playlist suivie (body.source) ou toutes, l'une après l'autre."""
    with S.lock:
        if S.running or SYNC["active"]:
            return {"error": "Attends la fin du téléchargement en cours."}
        items = load_follows()
        if body.get("source"):
            src = body["source"]
            items = [f for f in items if f["source"] == src]
            if not items and followable(src):    # playlist déjà chargée mais pas suivie : on la rescanne quand même
                h = find_history(src) or {}
                items = [{"source": src, "name": h.get("folder") or h.get("name") or "playlist"}]
        if not items:
            return {"error": "Aucune playlist suivie."}
        SYNC.update(active=True, index=0, total=len(items), current="", cancel=False, added=0)
    threading.Thread(target=_sync_worker, args=(items,), daemon=True).start()
    return {"ok": True, "total": len(items)}


def _sync_worker(items: list) -> None:
    try:
        for i, f in enumerate(items, 1):
            if SYNC["cancel"]:
                break
            SYNC.update(index=i, current=f["name"])
            r = load_source({"url": f["source"]})
            if r.get("error"):
                _record(f["source"], synced=int(time.time()), error=r["error"])
                continue
            r = start_job({"name": f["name"]})
            if r.get("error"):
                _record(f["source"], synced=int(time.time()), error=r["error"])
                continue
            while S.running:
                time.sleep(0.5)
            with S.lock:
                st = list(S.status.values())
                cover = next((track_dict(t)["cover"] for t in S.tracks if t.cover_url or t.thumb), "")
                count = len(S.tracks)
            new = st.count("ok")
            SYNC["added"] += new
            _record(f["source"], synced=int(time.time()), new=new, missing=st.count("miss"),
                    count=count, cover=cover or f.get("cover", ""), error="")
    finally:
        SYNC.update(active=False, current="")


# --------------------------------------------------------------------------- #
# Connexion Spotify (comme Exportify) : OAuth « PKCE », sans secret. Il faut une app
# sur developer.spotify.com avec l'URI de redirection affichée dans les réglages.
# --------------------------------------------------------------------------- #
SP_ACCOUNTS = os.environ.get("P2S_SPOTIFY_ACCOUNTS", "https://accounts.spotify.com")
SP_AUTH = SP_ACCOUNTS + "/authorize"
SP_TOKEN = SP_ACCOUNTS + "/api/token"
SP_API = os.environ.get("P2S_SPOTIFY_API", "https://api.spotify.com/v1")
SP_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
_SP_PENDING: dict[str, str] = {}      # state → code_verifier
LISTEN_PORT = PORT                    # remplacé par le port réellement ouvert


def spotify_redirect() -> str:
    return f"http://127.0.0.1:{LISTEN_PORT}/spotify/callback"


def _save_sp_token(data: dict, old: dict | None = None) -> dict:
    tok = {"access_token": data["access_token"],
           "refresh_token": data.get("refresh_token") or (old or {}).get("refresh_token", ""),
           "expires_at": int(time.time()) + int(data.get("expires_in", 3600)) - 60,
           "user": (old or {}).get("user", "")}
    APP_DIR.mkdir(parents=True, exist_ok=True)
    SPOTIFY_TOKEN.write_text(json.dumps(tok), "utf-8")
    SPOTIFY_TOKEN.chmod(0o600)
    return tok


def _load_sp_token() -> dict:
    try:
        return json.loads(SPOTIFY_TOKEN.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def spotify_user_token() -> str:
    """Jeton valide du compte connecté (rafraîchi si besoin), ou "" si pas connecté."""
    tok = _load_sp_token()
    if not tok.get("access_token"):
        return ""
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    cid = load_settings()["spotify_id"]
    if not (cid and tok.get("refresh_token")):
        return ""
    try:
        r = requests.post(SP_TOKEN, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                          "client_id": cid}, timeout=20)
        r.raise_for_status()
        return _save_sp_token(r.json(), tok)["access_token"]
    except requests.RequestException:
        return ""


def spotify_login() -> dict:
    cid = load_settings()["spotify_id"].strip()
    if not cid:
        return {"error": "Colle d'abord le Client ID de ton app Spotify."}
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _SP_PENDING.clear()
    _SP_PENDING[state] = verifier
    return {"url": SP_AUTH + "?" + urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": spotify_redirect(),
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "scope": SP_SCOPES})}


def spotify_callback(query: dict) -> str:
    """Retour de Spotify après « Accepter » : échange le code contre les jetons."""
    q = {k: v[0] for k, v in query.items()}
    verifier = _SP_PENDING.pop(q.get("state", ""), None)
    if q.get("error"):
        return "refus" if q["error"] == "access_denied" else q["error"]
    if not verifier or not q.get("code"):
        return "session"
    try:
        r = requests.post(SP_TOKEN, data={"grant_type": "authorization_code", "code": q["code"],
                                          "redirect_uri": spotify_redirect(), "client_id": load_settings()["spotify_id"],
                                          "code_verifier": verifier}, timeout=20)
        r.raise_for_status()
        tok = _save_sp_token(r.json())
        me = requests.get(f"{SP_API}/me", headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20)
        if me.ok:
            tok["user"] = me.json().get("display_name") or me.json().get("id", "")
            tok["user_id"] = me.json().get("id", "")
            SPOTIFY_TOKEN.write_text(json.dumps(tok), "utf-8")
        return ""
    except requests.RequestException as e:
        detail = ""
        if getattr(e, "response", None) is not None:
            try:
                detail = e.response.json().get("error_description") or e.response.json().get("error", "")
            except ValueError:
                detail = str(e.response.status_code)
        return detail or "réseau"


def spotify_logout() -> dict:
    try:
        SPOTIFY_TOKEN.unlink()
    except OSError:
        pass
    return {"ok": True}


def spotify_playlists() -> dict:
    """Comme l'écran d'accueil d'Exportify : tes playlists, avec leur nombre de titres."""
    token = spotify_user_token()
    if not token:
        return {"connected": False}
    hdr = {"Authorization": f"Bearer {token}"}
    tok = _load_sp_token()
    out, nxt = [], f"{SP_API}/me/playlists?limit=50"
    try:
        while nxt:
            r = requests.get(nxt, headers=hdr, timeout=20)
            if r.status_code == 401:
                return {"connected": False, "error": "Connexion Spotify expirée, reconnecte-toi."}
            r.raise_for_status()
            data = r.json()
            for p in data.get("items") or []:
                if not p:
                    continue
                meta = p.get("items") or p.get("tracks") or {}
                imgs = p.get("images") or []
                owner = (p.get("owner") or {})
                out.append({"id": p["id"], "name": p.get("name", ""), "count": meta.get("total", 0),
                            "image": imgs[-1]["url"] if imgs else "", "owner": owner.get("display_name", ""),
                            "mine": owner.get("id") == tok.get("user_id") or bool(p.get("collaborative")) or not tok.get("user_id")})
            nxt = data.get("next")
    except requests.RequestException as e:
        return {"connected": True, "user": tok.get("user", ""), "playlists": out, "error": f"Lecture des playlists impossible : {e}"}
    return {"connected": True, "user": tok.get("user", ""), "playlists": out}


# --------------------------------------------------------------------------- #
# Petits services macOS
# --------------------------------------------------------------------------- #
def pick_folder() -> dict:
    if sys.platform != "darwin":
        return {"error": "Sélecteur disponible sur macOS uniquement : saisis le chemin."}
    r = subprocess.run(["osascript", "-e",
                        'POSIX path of (choose folder with prompt "Dossier de destination")'],
                       capture_output=True, text=True)
    path = r.stdout.strip()
    if not path:
        return {"cancelled": True}
    home = str(Path.home())
    return {"path": "~" + path[len(home):].rstrip("/") if path.startswith(home) else path.rstrip("/")}


def reveal(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        return {"error": "Ce fichier n'existe plus."}
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    elif WIN:
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent)])
    return {"ok": True}


def open_path(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_dir():   # seulement des dossiers : jamais lancer un programme ou un fichier
        return {"error": "Ce dossier n'existe pas encore."}
    if WIN:
        os.startfile(str(p))  # type: ignore[attr-defined]
        return {"ok": True}
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(p)])
    return {"ok": True}


def open_url(url: str) -> dict:
    """Ouvre un lien web dans le navigateur de l'ordinateur (la fenêtre de l'appli n'a pas d'onglets)."""
    url = (url or "").strip()
    if not re.match(r"https?://[^\s]+$", url) or len(url) > 8000:
        return {"error": "Lien invalide."}
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    elif not webbrowser.open(url):
        return {"error": "Impossible d'ouvrir le navigateur."}
    return {"ok": True}


def discogs_match() -> dict:
    with S.lock:
        tracks, load_id = list(S.tracks), S.load_id
    if not tracks:
        return {"error": "Charge d'abord une playlist."}
    return DG.start(load_id, tracks)


def bandcamp_match() -> dict:
    with S.lock:
        tracks, load_id = list(S.tracks), S.load_id
    if not tracks:
        return {"error": "Charge d'abord une playlist."}
    return BC.start(load_id, tracks)


def info() -> dict:
    s = load_settings()
    conf = read_conf()
    return {
        "settings": {k: v for k, v in s.items() if k != "spotify_secret"},
        "has_secret": bool(s.get("spotify_secret")),
        "spotify": {"connected": bool(_load_sp_token().get("access_token")), "user": _load_sp_token().get("user", ""),
                    "redirect": spotify_redirect()},
        "sockseek": p2s.find_sockseek(None) or "",
        "account": {"username": conf.get("username", ""), "has_password": bool(conf.get("password")),
                    "listen_port": conf.get("listen-port", "")},
        "conf": bool(conf.get("username") and conf.get("password")),
        "mac": sys.platform == "darwin",
        "picker": sys.platform == "darwin" or bool(os.environ.get("P2S_NATIVE_PICKER")),
        "packaged": FROZEN,
        "version": VERSION,
        "repo": SOURCE_REPO,
        "os": platform.platform(terse=True),
    }


# --------------------------------------------------------------------------- #
# Nouvelle version et signalement de problème
# --------------------------------------------------------------------------- #
_UPDATE: dict = {}


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def check_update(force: bool = False) -> dict:
    """Dernière version publiée sur GitHub (une fois par lancement, sauf si on la redemande)."""
    if not load_settings().get("check_updates", True) and not force:
        return {"newer": False}
    if _UPDATE and not force:
        return _UPDATE
    try:
        r = requests.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                         headers={"Accept": "application/vnd.github+json"}, timeout=6)
        if r.status_code != 200:
            return {"newer": False, "error": f"pas de version publiée ({r.status_code})"}
        rel = r.json()
        latest = rel.get("tag_name", "").lstrip("v")
        url = rel.get("html_url", "")
        if not url.startswith(f"https://github.com/{UPDATE_REPO}/"):
            url = f"https://github.com/{UPDATE_REPO}/releases/latest"
        _UPDATE.clear()
        assets = {a.get("name"): {"name": a.get("name"), "url": a.get("browser_download_url", ""),
                                  "size": a.get("size", 0), "digest": a.get("digest") or ""}
                  for a in rel.get("assets") or []}
        mine = assets.get(updater.asset_name())
        _UPDATE.clear()
        _UPDATE.update(latest=latest, url=url, current=VERSION,
                       newer=VERSION != "dev" and _vtuple(latest) > _vtuple(VERSION),
                       notes=(rel.get("body") or "")[:1500],
                       auto=bool(mine) and not updater.why_not(), why=updater.why_not(), asset=mine)
    except (requests.RequestException, ValueError) as e:
        return {"newer": False, "error": str(e)}
    return _UPDATE


UPD = updater.Updater(UPDATE_REPO, APP_DIR / "maj")


def install_update() -> dict:
    u = _UPDATE if _UPDATE.get("newer") else check_update(force=True)
    if not u.get("newer"):
        return {"error": "Tu as déjà la dernière version."}
    if S.running:
        return {"error": "Un téléchargement est en cours : attends la fin ou arrête-le avant de mettre à jour."}
    return UPD.start(u.get("asset") or {})


def diagnostic() -> dict:
    """Texte à joindre à un signalement : versions, réglages (sans identifiants) et fin du journal."""
    s = load_settings()
    keep = {k: v for k, v in s.items() if k not in ("spotify_secret", "spotify_id", "banned_users", "library_dirs", "out")}
    bin_path = p2s.find_sockseek(None) or ""
    with S.lock:
        lines = S.lines[-60:]
        counts: dict = {}
        for st in S.status.values():
            counts[st] = counts.get(st, 0) + 1
        misses = []
        for t in S.tracks:
            if S.status.get(t.position) != "miss":
                continue
            d = S.details.get(t.position) or {}
            misses.append(f"  {t.position}. {t.artist} — {t.title} : {d.get('reason') or 'raison inconnue'}"
                          f" · {d.get('results', '?')} résultats · {d.get('locked', 0)} verrouillés")
        events = []
        for ev in S.events:
            data = {k: v for k, v in (ev.get("data") or {}).items() if "user" not in k.lower()}   # pas de pseudos
            events.append("  " + json.dumps({"type": ev.get("type"), "data": data}, ensure_ascii=False)[:400])
    home = str(Path.home())
    text = "\n".join([
        f"Playlist2Slsk {VERSION} · {platform.platform(terse=True)} · Python {platform.python_version()}"
        + (" · appli installée" if FROZEN else ""),
        f"sockseek : {'trouvé' if bin_path else 'introuvable'} · --progress-json : {bool(bin_path) and progress_json_supported(bin_path)}",
        f"Dernier lot : {counts or 'aucun'}",
        "Réglages : " + json.dumps(keep, ensure_ascii=False),
        f"Code de sortie du lot : {S.rc if S.rc is not None else '—'}",
        "", "Introuvables :", *(misses or ["  aucun"]),
        "", "Derniers évènements sockseek :", *(events or ["  aucun"]),
        "", "Fin du journal :", *lines,
    ]).replace(home, "~")
    user = read_conf().get("username")
    if user:
        text = text.replace(user, "<pseudo>")
    return {"text": text, "issues": f"https://github.com/{SOURCE_REPO}/issues/new"}


# --------------------------------------------------------------------------- #
# Serveur
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _local_only(self) -> bool:
        # Bloque les requêtes venant d'autres sites ouverts dans le navigateur.
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        if host not in ("127.0.0.1", "localhost") or (origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost")):
            self._send(403, b"forbidden", "text/plain")
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/info":
            self._json(info())
        elif u.path == "/api/current":
            with S.lock:
                self._json(current())
        elif u.path == "/spotify/callback":
            err = spotify_callback(parse_qs(u.query))
            target = "/?spotify=ok" if not err else "/?spotify=" + urlencode({"e": err})[2:]
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif u.path == "/api/spotify/playlists":
            self._json(spotify_playlists())
        elif u.path == "/api/audio":
            self._audio(int((parse_qs(u.query).get("n") or ["0"])[0] or 0))
        elif u.path == "/api/follows":
            self._json(list_follows())
        elif u.path == "/api/downloads-csv":
            self._json(newest_download_csv(float((parse_qs(u.query).get("since") or ["0"])[0] or 0)))
        elif u.path == "/api/history":
            self._json(history())
        elif u.path == "/api/update":
            self._json(check_update(force=bool(parse_qs(u.query).get("force"))))
        elif u.path == "/api/diag":
            self._json(diagnostic())
        elif u.path == "/api/discogs":
            self._json({**DG_ACCOUNT.public(), **DG.status()})
        elif u.path == "/api/bandcamp":
            self._json(BC.status())
        elif u.path == "/api/update/state":
            self._json(UPD.status())
        elif u.path == "/api/busy":
            self._json({"busy": bool(S.running or SYNC["active"])})
        elif u.path == "/api/status":
            since = int((parse_qs(u.query).get("since") or ["0"])[0])
            self._json(job_status(since))
        else:
            self._send(404, b"not found", "text/plain")

    AUDIO_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
                   ".aac": "audio/aac", ".alac": "audio/mp4", ".ogg": "audio/ogg", ".oga": "audio/ogg",
                   ".opus": "audio/ogg", ".wav": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff"}

    def _audio(self, n: int) -> None:
        """Lecture dans l'interface : seulement les fichiers du bilan en cours (par n° de morceau)."""
        with S.lock:
            path = Path((S.details.get(n) or {}).get("path") or "")
        if not path.is_file() or path.suffix.lower() not in self.AUDIO_TYPES:
            self._send(404, b"not found", "text/plain")
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:                                   # « bytes=-N » : les N derniers octets
                start = max(size - int(m.group(2)), 0)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", self.AUDIO_TYPES[path.suffix.lower()])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass   # le navigateur coupe souvent la requête en sautant dans le morceau

    def do_POST(self):
        if not self._local_only():
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        routes = {
            "/api/load": lambda: load_source(body),
            "/api/start": lambda: start_job(body),
            "/api/stop": stop_job,
            "/api/settings": lambda: update_settings(body),
            "/api/edit": lambda: save_edit(body),
            "/api/replace": lambda: replace_tracks(body),
            "/api/account": lambda: save_account(body),
            "/api/dir": lambda: set_dir(body),
            "/api/rekordbox": lambda: send_rekordbox(body),
            "/api/pick-folder": pick_folder,
            "/api/open": lambda: open_path(body.get("path", "")),
            "/api/open_url": lambda: open_url(body.get("url", "")),
            "/api/reveal": lambda: reveal(body.get("path", "")),
            "/api/follow": lambda: follow(body),
            "/api/forget": lambda: forget_history(body),
            "/api/spotify/login": spotify_login,
            "/api/spotify/logout": spotify_logout,
            "/api/sync": lambda: sync_follows(body),
            "/api/discogs/token": lambda: DG_ACCOUNT.connect(body.get("token", "")),
            "/api/discogs/logout": DG_ACCOUNT.logout,
            "/api/discogs/match": discogs_match,
            "/api/discogs/add": lambda: DG.add(body.get("ids") or []),
            "/api/bandcamp/match": bandcamp_match,
            "/api/update/install": install_update,
        }
        fn = routes.get(urlparse(self.path).path)
        if not fn:
            self._send(404, b"not found", "text/plain")
            return
        try:
            self._json(fn())
        except Exception as e:  # on renvoie l'erreur à la page plutôt que de planter
            self._json({"error": str(e)})


def _quit(*_a) -> None:
    stop_job()
    os._exit(0)


def watch_parent() -> None:
    """Lancé par l'appli Mac : on s'arrête proprement avec elle (même si elle plante)."""
    signal.signal(signal.SIGTERM, _quit)
    parent = os.environ.get("P2S_PARENT_PID")
    if not parent:
        return

    def loop() -> None:
        while os.getppid() == int(parent):
            time.sleep(1)
        _quit()
    threading.Thread(target=loop, daemon=True).start()


def make_server() -> tuple[ThreadingHTTPServer, str]:
    port = PORT
    for _ in range(20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        sys.exit("Aucun port libre trouvé.")
    global LISTEN_PORT
    LISTEN_PORT = port
    url = f"http://127.0.0.1:{port}/"
    print(f"playlist2slsk tourne sur {url}  (Ctrl+C pour quitter)", flush=True)
    return srv, url


def sync_on_launch() -> None:
    if load_settings()["sync_on_launch"] and load_follows():
        print("Mise à jour des playlists suivies…")
        sync_follows({})


def main() -> None:
    srv, url = make_server()
    if not os.environ.get("P2S_NO_BROWSER"):
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    watch_parent()
    sync_on_launch()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        stop_job()
        print("\nÀ bientôt.")


if __name__ == "__main__":
    main()
