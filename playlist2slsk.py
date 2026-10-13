#!/usr/bin/env python3
"""
playlist2slsk — d'une playlist Spotify à des fichiers propres, via Soulseek.

1. Lit la playlist : export CSV (Exportify, TuneMyMusic, Soundiiz…) ou URL Spotify
   (nécessite une app développeur Spotify).
2. Lance sockseek (ex-sldl) sur la liste, chaque fichier étant nommé d'après son
   numéro de ligne, ce qui garantit la correspondance fichier ↔ morceau.
3. Renomme chaque fichier selon les métadonnées Spotify, réécrit les tags et
   intègre la jaquette.
4. Écrit une playlist .m3u dans l'ordre d'origine et un missing.csv des morceaux
   introuvables, relançable tel quel.

Les identifiants Soulseek ne passent jamais par ce script : ils restent dans
sockseek.conf.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Module manquant : pip install requests mutagen")

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".alac", ".ogg", ".oga",
              ".opus", ".wav", ".aif", ".aiff"}
UA = {"User-Agent": "playlist2slsk/1.0"}


# --------------------------------------------------------------------------- #
# Modèle
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    title: str
    artists: list[str]
    album: str = ""
    album_artist: str = ""
    duration_s: int = 0
    uri: str = ""            # spotify:track:xxx
    cover_url: str = ""
    thumb: str = ""          # vignette pour l'affichage seulement (YouTube : image 16:9)
    date: str = ""           # AAAA ou AAAA-MM-JJ
    track_no: str = ""
    position: int = 0        # position dans la playlist (1-indexée)
    final_path: Path | None = field(default=None, repr=False)

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)

    @property
    def search_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def search_title(self) -> str:
        # « (Original Mix) » / « - Original Mix » : rarement dans les noms de fichiers partagés,
        # et Soulseek exige que chaque mot cherché soit présent. On l'enlève pour la recherche seulement.
        t = re.sub(r"\s*[\(\[][^\)\]]*\boriginal\s+(?:mix|version)\b[^\)\]]*[\)\]]", "", self.title, flags=re.I)
        t = re.sub(r"\s+[-–]\s+original\s+(?:mix|version)\s*$", "", t, flags=re.I)
        return t.strip() or self.title

    @property
    def key(self) -> str:
        """Identité stable du morceau dans sa source (sert aux corrections manuelles)."""
        return self.uri or f"{self.artist}|{self.title}"

    @property
    def spotify_id(self) -> str:
        m = re.search(r"(?:track[:/])([A-Za-z0-9]{22})", self.uri or "")
        return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Lecture des sources
# --------------------------------------------------------------------------- #
def _norm(h: str) -> str:
    # "Durée du titre (ms)" → "dureedutitrems" : accents retirés avant de nettoyer
    h = unicodedata.normalize("NFKD", h).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", h.lower())


COL_ALIASES = {
    "title": ["trackname", "nomdutitre", "titre", "title", "name", "song", "songname", "tracktitle", "track"],
    "artist": ["artistnames", "nomsdelartiste", "nomdelartiste", "artiste", "artistes", "artistname", "artist", "artists"],
    "album": ["albumname", "nomdelalbum", "album", "albumtitle"],
    "album_artist": ["albumartistnames", "nomsdelartistedelalbum", "nomdelartistedelalbum", "albumartistname", "albumartist"],
    "duration_ms": ["durationms", "trackdurationms", "dureedutitrems", "dureems"],
    "duration": ["duration", "duree", "length", "time"],
    "uri": ["trackuri", "uridutitre", "spotifyuri", "spotifyid", "uri", "spotifytrackid", "id"],
    "cover": ["albumimageurl", "urldelimagedelalbum", "imageurl", "coverurl", "cover", "artwork", "artworkurl"],
    "date": ["releasedate", "datedesortiedelalbum", "datedesortie", "annee", "albumreleasedate", "year", "date"],
    "track_no": ["tracknumber", "numerodutitre", "trackno"],
}


def _split_artists(s: str) -> list[str]:
    s = s.strip()
    if not s:
        return []
    parts = s.split(";") if ";" in s else re.split(r",\s+", s)
    return [p.strip() for p in parts if p.strip()]


def _parse_duration(v: str) -> int:
    v = v.strip()
    if not v:
        return 0
    if ":" in v:
        secs = 0
        for p in v.split(":"):
            secs = secs * 60 + float(p or 0)
        return int(round(secs))
    try:
        return int(round(float(v)))
    except ValueError:
        return 0


def read_csv_tracks(path: Path) -> list[Track]:
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t") if raw else csv.excel
    rows = list(csv.reader(raw.splitlines(), dialect))
    if not rows:
        return []
    header = [_norm(h) for h in rows[0]]
    idx = {}
    for key, aliases in COL_ALIASES.items():
        for a in aliases:
            if a in header:
                idx[key] = header.index(a)
                break
    if "title" not in idx:
        sys.exit(f"Colonne titre introuvable dans {path.name} (en-têtes : {rows[0]})")

    def get(row, key):
        i = idx.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    tracks = []
    for row in rows[1:]:
        title = get(row, "title")
        if not title:
            continue
        dur = 0
        if get(row, "duration_ms"):
            try:
                dur = int(round(float(get(row, "duration_ms")) / 1000))
            except ValueError:
                pass
        elif get(row, "duration"):
            dur = _parse_duration(get(row, "duration"))
        uri = get(row, "uri")
        if uri and re.fullmatch(r"[A-Za-z0-9]{22}", uri):
            uri = f"spotify:track:{uri}"
        tracks.append(Track(
            title=title,
            artists=_split_artists(get(row, "artist")),
            album=get(row, "album"),
            album_artist=", ".join(_split_artists(get(row, "album_artist"))),
            duration_s=dur,
            uri=uri,
            cover_url=get(row, "cover"),
            date=get(row, "date"),
            track_no=get(row, "track_no"),
        ))
    return tracks


def _spotify_token(cid: str, secret: str) -> str:
    r = requests.post("https://accounts.spotify.com/api/token",
                      data={"grant_type": "client_credentials"},
                      auth=(cid, secret), timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def _track_from_api(t: dict, album: dict | None = None) -> Track | None:
    if not t or t.get("type", "track") != "track" or not t.get("name"):
        return None
    album = album or t.get("album") or {}
    images = album.get("images") or []
    return Track(
        title=t["name"],
        artists=[a["name"] for a in t.get("artists", []) if a.get("name")],
        album=album.get("name", ""),
        album_artist=", ".join(a["name"] for a in album.get("artists", []) if a.get("name")),
        duration_s=int(round((t.get("duration_ms") or 0) / 1000)),
        uri=t.get("uri", ""),
        cover_url=images[0]["url"] if images else "",
        date=album.get("release_date", ""),
        track_no=str(t.get("track_number") or ""),
    )


LIKED = "spotify:liked"     # source spéciale : les titres likés du compte connecté


def _spotify_get(url: str, hdr: dict, **params) -> dict:
    r = requests.get(url, headers=hdr, params=params or None, timeout=20)
    if r.status_code == 429:   # limite de débit : on attend ce que Spotify demande
        time.sleep(min(int(r.headers.get("Retry-After", "2")), 30))
        r = requests.get(url, headers=hdr, params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def read_spotify_url(url: str, cid: str, secret: str, user_token: str = "") -> tuple[list[Track], str]:
    """Playlist, album ou titres likés. Avec user_token (connexion Spotify de l'interface),
    on lit comme Exportify ; sinon on tente le mode client (souvent refusé depuis 2026)."""
    user_token = user_token or os.environ.get("SPOTIFY_USER_TOKEN", "")
    liked = url.strip() == LIKED
    m = re.search(r"(playlist|album)[/:]([A-Za-z0-9]{22})", url)
    if not (m or liked):
        sys.exit("URL Spotify non reconnue (playlist ou album attendu).")
    kind, sid = m.groups() if m else ("liked", "")
    if user_token:
        hdr = {"Authorization": f"Bearer {user_token}"}
    elif liked:
        sys.exit("Connecte-toi à Spotify dans l'interface pour lire tes titres likés.")
    elif cid and secret:
        hdr = {"Authorization": f"Bearer {_spotify_token(cid, secret)}"}
    else:
        sys.exit("Spotify : connecte-toi à Spotify dans l'interface (bouton Spotify), "
                 "ou exporte la playlist en CSV sur https://exportify.app et passe le CSV.")
    api = os.environ.get("P2S_SPOTIFY_API", "https://api.spotify.com/v1")
    tracks: list[Track] = []
    try:
        if kind == "liked":
            name, nxt = "Titres likés", f"{api}/me/tracks?limit=50"
            while nxt:
                data = _spotify_get(nxt, hdr)
                tracks += [t for t in (_track_from_api(i.get("track") or i.get("item") or {}) for i in data.get("items", [])) if t]
                nxt = data.get("next")
        elif kind == "album":
            album = _spotify_get(f"{api}/albums/{sid}", hdr)
            name = album.get("name", "album")
            nxt = album["tracks"].get("next")
            items = album["tracks"]["items"]
            while True:
                tracks += [t for t in (_track_from_api(i, album) for i in items) if t]
                if not nxt:
                    break
                data = _spotify_get(nxt, hdr)
                items, nxt = data["items"], data.get("next")
        else:
            name = _spotify_get(f"{api}/playlists/{sid}", hdr, fields="name").get("name", "playlist")
            nxt = f"{api}/playlists/{sid}/items?limit=50"
            while nxt:
                try:
                    data = _spotify_get(nxt, hdr)
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 404 and "/items" in nxt:
                        nxt = nxt.replace("/items", "/tracks")      # ancien endpoint
                        continue
                    raise
                for it in data.get("items", []):
                    t = _track_from_api(it.get("item") or it.get("track") or {})
                    if t:
                        tracks.append(t)
                nxt = data.get("next")
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if user_token and code in (403, 404):
            sys.exit("Spotify ne donne le contenu que des playlists que tu as créées ou où tu es "
                     "collaborateur. Copie-la dans une de tes playlists, puis charge celle-ci.")
        if user_token and code == 401:
            sys.exit("Connexion Spotify expirée : reconnecte-toi depuis l'interface.")
        sys.exit(f"L'API Spotify a refusé la lecture (HTTP {code}). Connecte-toi à Spotify dans "
                 "l'interface, ou exporte la playlist en CSV sur https://exportify.app.")
    if not tracks and kind == "playlist" and user_token:
        sys.exit("Spotify ne donne le contenu que des playlists que tu as créées ou où tu es "
                 "collaborateur. Copie-la dans une de tes playlists, puis charge celle-ci.")
    return tracks, name


# Deezer : l'API publique lit les playlists publiques et les albums sans compte ni clé.
DEEZER_RE = re.compile(r"https?://([a-z0-9-]+\.)*(deezer\.com|deezer\.page\.link|dzr\.page\.link)/", re.I)


def read_deezer_url(url: str) -> tuple[list[Track], str]:
    api = os.environ.get("P2S_DEEZER_API", "https://api.deezer.com")
    try:
        if not re.search(r"/(playlist|album)/\d+", url):     # lien court de partage → vraie adresse
            url = requests.get(url, headers=UA, timeout=20, allow_redirects=True).url
        m = re.search(r"/(playlist|album)/(\d+)", url)
        if not m:
            sys.exit("Lien Deezer non reconnu : il faut une playlist ou un album.")
        kind, did = m.groups()
        meta = requests.get(f"{api}/{kind}/{did}", headers=UA, timeout=20).json()
        if meta.get("error"):
            sys.exit("Deezer refuse de lire cette playlist : vérifie qu'elle est publique "
                     "(sur Deezer : ⋯ › Rendre publique), puis réessaie.")
        name = meta.get("title") or kind
        album_cover = meta.get("cover_xl") or meta.get("cover_big") or ""
        tracks: list[Track] = []
        nxt = f"{api}/{kind}/{did}/tracks?limit=100&index=0"
        while nxt:
            data = requests.get(nxt, headers=UA, timeout=20).json()
            if data.get("error"):
                break
            for i in data.get("data") or []:
                if not i.get("title"):
                    continue
                alb = i.get("album") or {}
                artist = (i.get("artist") or {}).get("name") or (meta.get("artist") or {}).get("name", "")
                tracks.append(Track(
                    title=i["title"], artists=_split_artists(artist),
                    album=alb.get("title") or (name if kind == "album" else ""),
                    album_artist=(meta.get("artist") or {}).get("name", "") if kind == "album" else "",
                    duration_s=int(i.get("duration") or 0), uri=f"deezer:track:{i.get('id', '')}",
                    cover_url=alb.get("cover_xl") or alb.get("cover_big") or album_cover,
                    date=meta.get("release_date", "") if kind == "album" else "",
                    track_no=str(i.get("track_position") or ""),
                ))
            nxt = data.get("next")
    except requests.RequestException as e:
        sys.exit(f"Impossible de joindre Deezer : {e}")
    return tracks, name


# YouTube / SoundCloud (via yt-dlp)
YTDLP_RE = re.compile(r"https?://([a-z0-9-]+\.)*(youtube\.com|youtu\.be|soundcloud\.com|on\.soundcloud\.com)/", re.I)

_NOISE = re.compile(
    r"\s*[\(\[](?:[^\)\]]*\b(?:official|officiel|video|vid[ée]o|clip|lyrics?|paroles|audio|visuali[sz]er|"
    r"hd|hq|4k|quality|qualit[ée]|free\s*(?:dl|download)|out now|premiere)\b[^\)\]]*)[\)\]]", re.I)
_TAIL = re.compile(r"\s*[-–|]?\s*\b(?:hq|hd|4k|full hd|high quality)\s*$", re.I)
_YEAR = re.compile(r"\s+\(?(?:19[5-9]\d|20[0-4]\d)\)?\s*$")


def _strip_year(title: str) -> str:
    """« Game Over (Extended Mix) 2001 » → « Game Over (Extended Mix) », mais « 1999 » reste « 1999 »."""
    t = _YEAR.sub("", title).strip()
    return t if re.search(r"[^\W\d_]", t) else title
_PREFIX = re.compile(r"^\s*(?:premiere|exclusive|free\s*(?:dl|download))\s*[:|-]\s*", re.I)
_SPLIT = re.compile(r"\s+[-–—]\s+")


def clean_video_title(title: str, uploader: str = "") -> tuple[str, str]:
    """« Artiste - Titre (Official Video) » → (artiste, titre). Sans tiret, l'artiste est la chaîne."""
    t = _PREFIX.sub("", _NOISE.sub("", title or "")).strip()
    t = re.sub(r"\s*\|.*$", "", t).strip()           # « Titre | Label » → « Titre »
    up = re.sub(r"\s*(?:-\s*Topic|VEVO|Official|Officiel)\s*$", "", uploader or "", flags=re.I).strip()
    t = _TAIL.sub("", t).strip()                       # « … - HQ » en fin de titre
    parts = _SPLIT.split(t, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        artist = parts[0].strip()
        # « CHAINE- Artiste - Titre » : préfixe de chaîne collé au tiret
        m = re.match(r"^\S.*?\S-\s+(.+)$", artist)
        return (m.group(1) if m else artist), _strip_year(parts[1].strip(" \"'"))
    return up, _strip_year(t.strip(" \"'"))


def read_ytdlp_url(url: str) -> tuple[list[Track], str]:
    try:
        import yt_dlp
    except ImportError:
        sys.exit("Module manquant pour YouTube/SoundCloud : pip install yt-dlp")
    soundcloud = "soundcloud" in url.lower()
    # YouTube : liste « à plat », rapide (une seule requête). SoundCloud : il faut résoudre chaque titre.
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": False if soundcloud else "in_playlist", "ignoreerrors": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        sys.exit(f"Impossible de lire ce lien : {e}")
    if not info:
        sys.exit("Impossible de lire ce lien (playlist privée ou supprimée ?)")
    entries = info.get("entries")
    entries = [e for e in entries if e] if entries is not None else [info]
    tracks: list[Track] = []
    for e in entries:
        title = e.get("track") or e.get("title") or ""
        if not title or title in ("[Deleted video]", "[Private video]"):
            continue
        uploader = e.get("channel") or e.get("uploader") or ""
        if e.get("track") and (e.get("artist") or e.get("artists")):
            artist = e.get("artist") or ", ".join(e.get("artists"))
            artist, title = artist, _NOISE.sub("", title).strip()
        else:
            artist, title = clean_video_title(title, uploader)
        thumbs = e.get("thumbnails") or []
        thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else "")
        cover = ""
        if soundcloud and thumb:   # pochettes SoundCloud carrées : on prend la grande taille
            cover = re.sub(r"-(large|t\d+x\d+|original|crop)\.", "-t500x500.", thumb)
        tracks.append(Track(
            title=title, artists=_split_artists(artist) or ([uploader] if uploader else []),
            album=e.get("album") or "", duration_s=int(e.get("duration") or 0),
            uri=e.get("webpage_url") or e.get("url") or "", cover_url=cover, thumb=cover or thumb,
            date=(e.get("upload_date") or "")[:4],
        ))
    return tracks, info.get("title") or ("soundcloud" if soundcloud else "youtube")


# --------------------------------------------------------------------------- #
# Jaquettes
# --------------------------------------------------------------------------- #
_cover_cache: dict[str, bytes | None] = {}


def _upgrade_scdn(url: str) -> str:
    # i.scdn.co : ab67616d00001e02 (300px) / ab67616d00004851 (64px) → ab67616d0000b273 (640px)
    return re.sub(r"ab67616d0000(1e02|4851)", "ab67616d0000b273", url)


def _get_bytes(url: str) -> bytes | None:
    if url in _cover_cache:
        return _cover_cache[url]
    data = None
    try:
        r = requests.get(url, headers=UA, timeout=20)
        if r.ok and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
            data = r.content
    except requests.RequestException:
        pass
    _cover_cache[url] = data
    return data


def find_cover(t: Track) -> bytes | None:
    # 1. URL fournie (API Spotify ou colonne du CSV)
    if t.cover_url:
        img = _get_bytes(_upgrade_scdn(t.cover_url)) or _get_bytes(t.cover_url)
        if img:
            return img
    # 2. oEmbed Spotify : public, sans clé
    if t.spotify_id:
        try:
            r = requests.get("https://open.spotify.com/oembed",
                             params={"url": f"https://open.spotify.com/track/{t.spotify_id}"},
                             headers=UA, timeout=20)
            thumb = r.json().get("thumbnail_url") if r.ok else None
            if thumb:
                img = _get_bytes(_upgrade_scdn(thumb)) or _get_bytes(thumb)
                if img:
                    return img
        except (requests.RequestException, ValueError):
            pass
    # 3. iTunes Search : repli pour les CSV sans URI
    term = f"{t.search_artist} {t.album or t.title}".strip()
    try:
        r = requests.get("https://itunes.apple.com/search",
                         params={"term": term, "entity": "album" if t.album else "song",
                                 "limit": 1}, headers=UA, timeout=20)
        res = r.json().get("results", []) if r.ok else []
        if res and res[0].get("artworkUrl100"):
            return _get_bytes(res[0]["artworkUrl100"].replace("100x100bb", "1000x1000bb"))
    except (requests.RequestException, ValueError):
        pass
    return None


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def _mime(img: bytes) -> str:
    return "image/png" if img[:4] == b"\x89PNG" else "image/jpeg"


def write_tags(path: Path, t: Track, cover: bytes | None) -> str:
    """Réécrit les tags principaux et intègre la jaquette. Renvoie un statut court."""
    import mutagen
    ext = path.suffix.lower()
    year = t.date[:4] if t.date else ""
    try:
        if ext in (".mp3", ".wav", ".aif", ".aiff"):
            from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
            if ext == ".mp3":
                try:
                    tags = ID3(path)
                except ID3NoHeaderError:
                    tags = ID3()
                holder = None
            else:
                holder = mutagen.File(path)
                if holder.tags is None:
                    holder.add_tags()
                tags = holder.tags
            tags.setall("TIT2", [TIT2(encoding=3, text=t.title)])
            tags.setall("TPE1", [TPE1(encoding=3, text=t.artist)])
            if t.album:
                tags.setall("TALB", [TALB(encoding=3, text=t.album)])
            if t.album_artist:
                tags.setall("TPE2", [TPE2(encoding=3, text=t.album_artist)])
            if year:
                tags.setall("TDRC", [TDRC(encoding=3, text=year)])
            if t.track_no:
                tags.setall("TRCK", [TRCK(encoding=3, text=t.track_no)])
            if cover:
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=_mime(cover), type=3, desc="Cover", data=cover))
            if holder is None:
                tags.save(path, v2_version=3)
            else:
                holder.save()

        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            f = FLAC(path)
            f["title"], f["artist"] = t.title, t.artist
            if t.album:
                f["album"] = t.album
            if t.album_artist:
                f["albumartist"] = t.album_artist
            if year:
                f["date"] = year
            if t.track_no:
                f["tracknumber"] = t.track_no
            if cover:
                f.clear_pictures()
                p = Picture()
                p.type, p.mime, p.desc, p.data = 3, _mime(cover), "Cover", cover
                f.add_picture(p)
            f.save()

        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(path)
            f["\xa9nam"], f["\xa9ART"] = [t.title], [t.artist]
            if t.album:
                f["\xa9alb"] = [t.album]
            if t.album_artist:
                f["aART"] = [t.album_artist]
            if year:
                f["\xa9day"] = [year]
            if t.track_no.isdigit():
                f["trkn"] = [(int(t.track_no), 0)]
            if cover:
                fmt = MP4Cover.FORMAT_PNG if _mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
                f["covr"] = [MP4Cover(cover, imageformat=fmt)]
            f.save()

        elif ext in (".ogg", ".oga", ".opus"):
            from mutagen.flac import Picture
            f = mutagen.File(path)
            f["title"], f["artist"] = [t.title], [t.artist]
            if t.album:
                f["album"] = [t.album]
            if t.album_artist:
                f["albumartist"] = [t.album_artist]
            if year:
                f["date"] = [year]
            if t.track_no:
                f["tracknumber"] = [t.track_no]
            if cover:
                p = Picture()
                p.type, p.mime, p.desc, p.data = 3, _mime(cover), "Cover", cover
                f["metadata_block_picture"] = [base64.b64encode(p.write()).decode("ascii")]
            f.save()
        else:
            return "tags ignorés (format non géré)"
    except Exception as e:  # un fichier mal formé ne doit pas bloquer le lot
        return f"tags en échec ({type(e).__name__}: {e})"
    return "tags + jaquette" if cover else "tags (pas de jaquette trouvée)"


# --------------------------------------------------------------------------- #
# Noms de fichiers
# --------------------------------------------------------------------------- #
def safe_name(s: str, max_len: int = 180) -> str:
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:max_len].rstrip(" .") or "sans titre"


def render_name(pattern: str, t: Track, width: int) -> str:
    values = {
        "artist": t.artist, "artist1": t.search_artist, "title": t.title,
        "album": t.album, "year": t.date[:4], "n": str(t.position).zfill(width),
    }
    # chaque segment de chemin est nettoyé séparément : "{album}/{artist} - {title}" fonctionne
    parts = [safe_name(re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), ""), seg))
             for seg in pattern.split("/")]
    return "/".join(p for p in parts if p)


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        c = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not c.exists():
            return c
        i += 1


def existing_file(dest: Path, rel: str) -> Path | None:
    for ext in AUDIO_EXTS:
        p = Path(str(dest / rel) + ext)   # pas with_suffix : "Mr. Oizo - Flat Beat" contient un point
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------- #
# sockseek
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Faux FLAC (MP3 réencodé)
# --------------------------------------------------------------------------- #
LOSSLESS_EXTS = {".flac", ".wav", ".aif", ".aiff"}


def analyze_lossless(path):
    """Cherche la « falaise » spectrale typique d'un MP3/AAC réencodé en FLAC.

    Un vrai fichier CD garde du signal jusqu'à ~22 kHz, avec une pente douce. Un MP3 coupe net
    (16 kHz en 128k, ~19-20 kHz en 320k) : on cherche une chute brutale suivie de quasi-silence.
    """
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:
        return None
    try:
        info = sf.info(str(path))
        sr = info.samplerate
        start = max(0, info.frames // 2 - 30 * sr)
        data, _ = sf.read(str(path), start=start, frames=60 * sr, dtype="float32", always_2d=True)
    except Exception:
        return None
    x = data.mean(axis=1)
    n = 8192
    if len(x) < n * 4 or float(np.abs(x).max()) < 1e-4:
        return None
    frames = x[: len(x) // n * n].reshape(-1, n) * np.hanning(n)
    power = (np.abs(np.fft.rfft(frames, axis=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(power + 1e-20)
    hz = sr / n                                    # largeur d'un « bin »
    k = max(1, int(150 / hz))
    db = np.convolve(db, np.ones(k) / k, mode="same")
    nyq = sr / 2

    def band(lo, hi):
        a, b = int(lo / hz), int(hi / hz)
        return float(db[a:b].mean()) if b > a else -200.0

    best_f, best_drop = None, 0.0
    f = 11000.0
    while f < min(nyq, 24000) - 800:
        drop = band(f - 1000, f - 150) - band(f + 150, f + 800)
        if drop > best_drop:
            best_f, best_drop = f, drop
        f += hz
    cutoff = None
    if best_f and best_drop > 18 and band(best_f + 150, min(nyq, 24000)) < band(best_f - 1000, best_f - 150) - 25:
        cutoff = best_f
    if cutoff is None:
        verdict = "ok"
    elif cutoff < 19000:
        verdict = "fake"
    elif cutoff < 20800 or sr > 48000:
        verdict = "check"            # MP3 320 réencodé, ou « hi-res » gonflé depuis un CD
    else:
        verdict = "ok"
    return {"cutoff": round(cutoff) if cutoff else None, "verdict": verdict, "rate": sr}


QUALITY_TEXT = {
    "fake": "faux FLAC probable",
    "check": "à vérifier",
}


# --------------------------------------------------------------------------- #
# Export DJ : Traktor (.nml) et Rekordbox (rekordbox.xml)
# --------------------------------------------------------------------------- #
_VOLUME_CACHE: dict[str, str] = {}


def _mac_volume(path: Path) -> tuple[str, Path]:
    """(nom du volume, chemin relatif au volume) : Traktor référence les fichiers ainsi."""
    if path.drive:   # Windows : Traktor note le lecteur (« C: ») comme volume
        return path.drive, path
    parts = path.parts
    if len(parts) > 2 and parts[1] == "Volumes":
        return parts[2], Path("/", *parts[3:])
    if "/" not in _VOLUME_CACHE:
        name = "Macintosh HD"
        if sys.platform == "darwin":
            try:
                out = subprocess.run(["diskutil", "info", "/"], capture_output=True, text=True, timeout=10).stdout
                m = re.search(r"Volume Name:\s*(.+)", out)
                if m:
                    name = m.group(1).strip()
            except (OSError, subprocess.SubprocessError):
                pass
        _VOLUME_CACHE["/"] = name
    return _VOLUME_CACHE["/"], path


def write_traktor_nml(tracks: list[Track], name: str, out: Path) -> None:
    from xml.sax.saxutils import quoteattr as q
    rows, keys = [], []
    for t in tracks:
        if not (t.final_path and t.final_path.exists()):
            continue
        vol, rel = _mac_volume(t.final_path.resolve())
        d = "".join(f"/:{p}" for p in rel.parent.parts[1:]) + "/:"
        key = f"{vol}{d}{rel.name}"
        keys.append(key)
        info = f' PLAYTIME={q(str(t.duration_s))}' if t.duration_s else ""
        rows.append(f'<ENTRY TITLE={q(t.title)} ARTIST={q(t.artist)}><LOCATION DIR={q(d)} FILE={q(rel.name)} '
                    f'VOLUME={q(vol)}></LOCATION><ALBUM TITLE={q(t.album)}></ALBUM>'
                    f'<INFO{info}></INFO></ENTRY>')
    entries = "".join(f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY={q(k)}></PRIMARYKEY></ENTRY>' for k in keys)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n<NML VERSION="19">'
           '<HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"></HEAD>'
           f'<COLLECTION ENTRIES="{len(rows)}">{"".join(rows)}</COLLECTION>'
           '<SETS ENTRIES="0"></SETS><PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">'
           f'<NODE TYPE="PLAYLIST" NAME={q(name)}><PLAYLIST ENTRIES="{len(keys)}" TYPE="LIST" UUID="{uuid.uuid4().hex}">'
           f'{entries}</PLAYLIST></NODE></SUBNODES></NODE></PLAYLISTS></NML>\n')
    out.write_text(xml, "utf-8")


_URL_SAFE = "/()!'&,-_.~"     # Rekordbox garde ces caractères tels quels dans Location


def _rb_location(path: str) -> str:
    """Chemin → partie URL de Location (Windows : /C:/Users/…)."""
    from urllib.parse import quote as urlquote
    p = Path(path)
    if p.drive:
        return "/" + p.drive + urlquote(p.as_posix()[len(p.drive):], safe=_URL_SAFE)
    return urlquote(path, safe=_URL_SAFE)


def update_rekordbox_xml(xml_path: Path, name: str, tracks: list[Track]) -> None:
    """Ajoute (ou remplace) la playlist dans un rekordbox.xml partagé par toutes les playlists."""
    from xml.sax.saxutils import quoteattr as q
    store = xml_path.with_suffix(".json")
    try:
        lists = json.loads(store.read_text("utf-8"))
    except (OSError, ValueError):
        lists = {}
    lists[name] = [{"path": str(t.final_path.resolve()), "artist": t.artist, "title": t.title,
                    "album": t.album, "time": t.duration_s, "year": t.date[:4]}
                   for t in tracks if t.final_path and t.final_path.exists()]
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(lists, ensure_ascii=False, indent=1), "utf-8")
    ids: dict[str, int] = {}
    coll = []
    for items in lists.values():
        for it in items:
            if it["path"] in ids or not Path(it["path"]).exists():
                continue
            ids[it["path"]] = len(ids) + 1
            coll.append(f'<TRACK TrackID="{ids[it["path"]]}" Name={q(it["title"])} Artist={q(it["artist"])} '
                        f'Album={q(it["album"])} TotalTime="{it["time"] or 0}" Year={q(it["year"] or "")} '
                        f'Location={q("file://localhost" + _rb_location(it["path"]))}/>')
    nodes = []
    for pl, items in lists.items():
        keys = [ids[it["path"]] for it in items if it["path"] in ids]
        nodes.append(f'<NODE Name={q(pl)} Type="1" KeyType="0" Entries="{len(keys)}">'
                     + "".join(f'<TRACK Key="{k}"/>' for k in keys) + "</NODE>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">'
           '<PRODUCT Name="rekordbox" Version="6.0.0" Company="AlphaTheta"/>'
           f'<COLLECTION Entries="{len(coll)}">{"".join(coll)}</COLLECTION>'
           f'<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1"><NODE Type="0" Name="playlist2slsk" Count="{len(nodes)}">'
           f'{"".join(nodes)}</NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>\n')
    xml_path.write_text(xml, "utf-8")


# --------------------------------------------------------------------------- #
# Bibliothèque existante (doublons)
# --------------------------------------------------------------------------- #
def _words(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return {w for w in re.findall(r"[a-z0-9]+", s) if w not in {"the", "feat", "ft", "and", "x"}}


def index_library(dirs: list[str], exclude: Path | None = None) -> list[tuple[set[str], Path]]:
    out = []
    for d in dirs:
        root = Path(d).expanduser()
        if not root.is_dir():
            print(f"▸ Bibliothèque introuvable, ignorée : {root}")
            continue
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            here = Path(dirpath)
            if exclude and (here == exclude or exclude in here.parents):
                continue
            for f in files:
                if Path(f).suffix.lower() in AUDIO_EXTS and not f.startswith("."):
                    out.append((_words(Path(dirpath).name + " " + Path(f).stem), here / f))
    return out


def find_in_library(t: Track, index: list[tuple[set[str], Path]]) -> Path | None:
    """Un fichier dont le nom (ou le dossier) contient tous les mots de l'artiste et du titre."""
    need = _words(t.search_artist) | _words(t.search_title)
    if not need:
        return None
    for words, path in index:
        if need <= words:
            return path
    return None


def emit(**data) -> None:
    """Ligne lue par l'interface graphique (ignorée en ligne de commande)."""
    print("@@P2S " + json.dumps(data, ensure_ascii=False), flush=True)


def find_sockseek(explicit: str | None) -> str | None:
    for cand in [explicit, os.environ.get("SOCKSEEK"), "sockseek", "sldl"]:
        if not cand:
            continue
        p = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if p:
            return p
    return None


def write_job_csv(tracks: list[Track], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Artist", "Title", "Album", "Length", "URI"])
        for t in tracks:
            w.writerow([t.search_artist, t.search_title, t.album,
                        t.duration_s or "", t.uri])


def write_missing_csv(tracks: list[Track], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Track Name", "Artist Name(s)", "Album Name", "Duration (ms)",
                    "Track URI", "Album Image URL", "Release Date", "Track Number"])
        for t in tracks:
            w.writerow([t.title, "; ".join(t.artists), t.album, t.duration_s * 1000 or "",
                        t.uri, t.cover_url, t.date, t.track_no])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Playlist Spotify → Soulseek (via sockseek), avec renommage, tags et jaquettes.",
        epilog="Tout ce qui suit « -- » est transmis tel quel à sockseek, "
               "ex. : -- --strict-title --length-tol 10")
    ap.add_argument("source", help="CSV exporté (Exportify, TuneMyMusic…) ou URL Spotify playlist/album")
    ap.add_argument("-o", "--out", default="~/Music/playlist2slsk",
                    help="dossier racine (défaut : ~/Music/playlist2slsk)")
    ap.add_argument("--name", help="nom du sous-dossier (défaut : nom de la playlist / du CSV)")
    ap.add_argument("--pattern", default="{artist} - {title}",
                    help='modèle de nom : {artist} {artist1} {title} {album} {year} {n}. '
                         'Ex. "{n} - {artist} - {title}" ou "{album}/{artist} - {title}"')
    ap.add_argument("--lossless", action="store_true", help="préférer FLAC/WAV (sockseek --pref-format flac,wav)")
    ap.add_argument("--only-lossless", action="store_true", help="exiger FLAC (sockseek --format flac)")
    ap.add_argument("-n", "--limit", type=int, help="ne traiter que les N premiers morceaux")
    ap.add_argument("--no-cover", action="store_true", help="ne pas chercher de jaquette")
    ap.add_argument("--no-tags", action="store_true", help="renommer seulement, sans toucher aux tags")
    ap.add_argument("--keep-staging", action="store_true", help="garder le dossier de travail temporaire")
    ap.add_argument("--dry-run", action="store_true", help="afficher la liste et la commande sans rien télécharger")
    ap.add_argument("--sockseek", help="chemin du binaire sockseek/sldl (défaut : PATH ou $SOCKSEEK)")
    ap.add_argument("--spotify-id", default=os.environ.get("SPOTIFY_CLIENT_ID", ""))
    ap.add_argument("--spotify-secret", default=os.environ.get("SPOTIFY_CLIENT_SECRET", ""))
    ap.add_argument("--check-lossless", action="store_true",
                    help="analyser le spectre des FLAC/WAV pour repérer les MP3 réencodés (numpy + soundfile)")
    ap.add_argument("--traktor", action="store_true",
                    help="écrire aussi une playlist Traktor <nom>.nml (Traktor : Import Playlist)")
    ap.add_argument("--rekordbox-xml", metavar="FICHIER.xml",
                    help="ajouter la playlist à ce rekordbox.xml (Rekordbox : Préférences > Avancé > rekordbox xml)")
    ap.add_argument("--library", action="append", default=[], metavar="DOSSIER",
                    help="ne pas télécharger ce qui est déjà dans ce dossier (répétable)")
    ap.add_argument("--edits", metavar="FICHIER.json",
                    help="corrections manuelles {clé: {artist, title}} appliquées à la source")
    ap.add_argument("--only", metavar="N,N", help="ne chercher que ces positions (1-indexées)")
    ap.add_argument("--postprocess-only", metavar="DOSSIER",
                    help="(avancé) sauter le téléchargement et traiter un dossier déjà rempli par sockseek")

    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    args = ap.parse_args(argv)

    # 1. Source
    from_video = bool(YTDLP_RE.match(args.source))
    if re.match(r"https?://open\.spotify\.com/|spotify:", args.source):
        tracks, src_name = read_spotify_url(args.source, args.spotify_id, args.spotify_secret)
    elif from_video:
        tracks, src_name = read_ytdlp_url(args.source)
    elif DEEZER_RE.match(args.source):
        tracks, src_name = read_deezer_url(args.source)
    else:
        p = Path(args.source).expanduser()
        if not p.is_file():
            sys.exit(f"Fichier introuvable : {p}")
        tracks, src_name = read_csv_tracks(p), p.stem
    if args.edits:
        try:
            edits = json.loads(Path(args.edits).expanduser().read_text("utf-8"))
        except (OSError, ValueError):
            edits = {}
        for t in tracks:
            e = edits.get(t.key)
            if e:
                if e.get("artist"):
                    t.artists = _split_artists(e["artist"])
                if e.get("title"):
                    t.title = e["title"]
    if args.limit:
        tracks = tracks[: args.limit]
    if not tracks:
        sys.exit("Aucun morceau trouvé dans la source.")
    for i, t in enumerate(tracks, 1):
        t.position = i
    width = max(2, len(str(len(tracks))))
    only = {int(x) for x in re.findall(r"\d+", args.only or "")}

    dest = Path(args.out).expanduser() / safe_name(args.name or src_name)
    print(f"▸ {len(tracks)} morceaux · destination : {dest}")

    # 2. Ce qui est déjà là : dans le dossier (relance après interruption) ou dans la bibliothèque
    library = index_library(args.library, exclude=dest) if args.library else []
    if args.library:
        print(f"▸ Bibliothèque : {len(library)} fichiers indexés")
    todo: list[Track] = []
    n_lib = n_skip = 0
    for t in tracks:
        found = existing_file(dest, render_name(args.pattern, t, width))
        if found:
            t.final_path = found
            n_skip += 1
            if not only or t.position in only:
                emit(pos=t.position, state="skip", path=str(found))
            continue
        lib = find_in_library(t, library) if library else None
        if lib:
            t.final_path = lib
            n_lib += 1
            if not only or t.position in only:
                emit(pos=t.position, state="lib", path=str(lib))
        elif not only or t.position in only:
            todo.append(t)
    if n_skip:
        print(f"▸ {n_skip} déjà présents, ignorés")
    if n_lib:
        print(f"▸ {n_lib} déjà dans ta bibliothèque, ignorés")
    emit(todo=[t.position for t in todo])

    bin_path = find_sockseek(args.sockseek)
    if args.postprocess_only:
        staging = Path(args.postprocess_only).expanduser()
    else:  # même disque que la destination → déplacements instantanés
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".p2s_tmp_", dir=dest.parent))
    job_csv = staging / "_jobs.csv"

    cmd = [bin_path or "sockseek", str(job_csv), "-o", str(staging),
           "--name-format", "{snum}", "--no-write-index", "--input-type", "csv"]
    if os.environ.get("P2S_SOCKSEEK_CONF"):   # appli : on indique où est le compte Soulseek
        cmd += ["--config", os.environ["P2S_SOCKSEEK_CONF"]]
    if args.only_lossless:
        cmd += ["--format", "flac"]
    elif args.lossless:
        cmd += ["--pref-format", "flac,wav"]
    if from_video:
        # Titres et durées de vidéos approximatifs : recherche aussi sans l'artiste (souvent la
        # chaîne) et tolérance de durée plus large. Les options passées après « -- » priment.
        cmd += ["--artist-maybe-wrong", "--length-tol", "15"]
    cmd += extra

    if args.dry_run:
        for t in todo:
            print(f"  {str(t.position).zfill(width)}. {t.artist} — {t.title}"
                  f"{'  [' + t.album + ']' if t.album else ''}")
        print("\nCommande :", " ".join(f'"{c}"' if " " in c else c for c in cmd))
        shutil.rmtree(staging, ignore_errors=True)
        return

    # 3. Téléchargement
    if todo and not args.postprocess_only:
        if not bin_path:
            sys.exit("sockseek introuvable. Installe-le (voir README) ou passe --sockseek /chemin/vers/sockseek")
        staging.mkdir(parents=True, exist_ok=True)
        write_job_csv(todo, job_csv)
        print(f"▸ Recherche Soulseek de {len(todo)} morceaux…\n")
        rc = subprocess.call(cmd)
        if rc not in (0,):
            print(f"\n(sockseek a terminé avec le code {rc} : on traite ce qui a été téléchargé)")

    # 4. Post-traitement : {snum}.ext → todo[snum-1]
    got: dict[int, Path] = {}
    if staging.exists():
        for f in staging.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS and f.stem.isdigit():
                got.setdefault(int(f.stem), f)

    dest.mkdir(parents=True, exist_ok=True)
    print()
    for i, t in enumerate(todo, 1):
        src = got.get(i)
        if not src:
            print(f"  ✗ {t.artist} — {t.title}")
            emit(pos=t.position, state="miss")
            continue
        target = unique_path(dest / (render_name(args.pattern, t, width) + src.suffix.lower()))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), target)
        t.final_path = target
        status = "renommé"
        if not args.no_tags:
            cover = None if args.no_cover else find_cover(t)
            status = write_tags(target, t, cover)
        quality = analyze_lossless(target) if args.check_lossless and target.suffix.lower() in LOSSLESS_EXTS else None
        if quality and quality["verdict"] != "ok":
            status += f" · ⚠ {QUALITY_TEXT[quality['verdict']]}, coupure à {quality['cutoff'] / 1000:.1f} kHz"
        print(f"  ✓ {target.relative_to(dest)}  ({status})")
        emit(pos=t.position, state="ok", path=str(target), quality=quality)

    # 5. Playlist + rapport
    m3u = dest / f"{safe_name(args.name or src_name)}.m3u8"
    with m3u.open("w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for t in tracks:
            if t.final_path and t.final_path.exists():
                f.write(f"#EXTINF:{t.duration_s or -1},{t.artist} - {t.title}\n")
                inside = dest in t.final_path.parents
                f.write(f"{(t.final_path.relative_to(dest) if inside else t.final_path).as_posix()}\n")

    pl_name = safe_name(args.name or src_name)
    if args.traktor:
        nml = dest / f"{pl_name}.nml"
        write_traktor_nml(tracks, pl_name, nml)
        emit(export="traktor", path=str(nml))
    if args.rekordbox_xml:
        xml = Path(args.rekordbox_xml).expanduser()
        update_rekordbox_xml(xml, pl_name, tracks)
        emit(export="rekordbox", path=str(xml))

    missing = [t for t in tracks if not (t.final_path and t.final_path.exists())]
    miss_csv = dest / "missing.csv"
    if missing:
        write_missing_csv(missing, miss_csv)
    elif miss_csv.exists():
        miss_csv.unlink()

    if not args.keep_staging and not args.postprocess_only:
        shutil.rmtree(staging, ignore_errors=True)

    ok = sum(1 for t in tracks if t.final_path)
    print(f"\n▸ {ok}/{len(tracks)} morceaux dans {dest}")
    if missing:
        print(f"▸ {len(missing)} introuvables → {miss_csv}\n"
              f"  Pour réessayer avec des filtres plus larges, relance la même commande\n"
              f"  (les morceaux déjà présents sont ignorés) en ajoutant :  -- --length-tol 10 --desperate")


if __name__ == "__main__":
    main()
