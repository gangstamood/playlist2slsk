"""Mise à jour depuis l'appli : télécharge l'installateur de la dernière version,
l'installe à la place de l'appli actuelle et relance Playlist2Slsk.

Mac : on monte le .dmg, on copie Playlist2Slsk.app à côté, puis un petit script
attend la fermeture de l'appli pour échanger les deux dossiers et la rouvrir.
Windows : on lance l'installateur en mode silencieux une fois l'appli fermée ;
il rouvre l'appli à la fin (paramètre /relaunch=1).
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import requests

WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))
NOWIN = {"creationflags": 0x08000000} if WIN else {}


def asset_name() -> str:
    if WIN:
        return "Playlist2Slsk-Windows-Setup.exe"
    if MAC:
        return "Playlist2Slsk-Mac-AppleSilicon.dmg" if platform.machine() == "arm64" else "Playlist2Slsk-Mac-Intel.dmg"
    return ""


def mac_bundle() -> Path | None:
    """Playlist2Slsk.app en cours d'exécution, s'il peut être remplacé."""
    p = Path(sys.executable).resolve()
    app = p.parents[2] if len(p.parents) > 2 else None
    if not app or app.suffix != ".app":
        return None
    if str(app).startswith("/Volumes/") or "/AppTranslocation/" in str(app):
        return None   # lancée depuis l'image disque ou une copie temporaire de macOS
    return app if os.access(app.parent, os.W_OK) else None


def why_not() -> str:
    """Raison pour laquelle la mise à jour automatique n'est pas possible (vide si elle l'est)."""
    if not FROZEN:
        return "Version lancée depuis le code : mets à jour le dossier."
    if MAC and not mac_bundle():
        return "Glisse d'abord Playlist2Slsk dans le dossier Applications, puis relance-la."
    if not asset_name():
        return "Système non pris en charge."
    return ""


class Updater:
    def __init__(self, repo: str, work_dir: Path):
        self.repo = repo
        self.dir = work_dir
        self.lock = threading.Lock()
        self.state = {"phase": "idle", "got": 0, "size": 0, "error": ""}
        self.quit = None            # fonction qui ferme la fenêtre (fournie par desktop.py)

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def start(self, asset: dict) -> dict:
        reason = why_not()
        if reason:
            return {"error": reason}
        prefix = f"https://github.com/{self.repo}/releases/download/"
        if not asset or not str(asset.get("url", "")).startswith(prefix) or asset.get("name") != asset_name():
            return {"error": "Installateur introuvable pour ton ordinateur."}
        with self.lock:
            if self.state["phase"] in ("download", "install", "restart"):
                return {"ok": True}
            self.state = {"phase": "download", "got": 0, "size": int(asset.get("size") or 0), "error": ""}
        threading.Thread(target=self._run, args=(asset,), daemon=True).start()
        return {"ok": True}

    def _run(self, asset: dict) -> None:
        try:
            f = self._download(asset)
            self._set(phase="install")
            if MAC:
                self._install_mac(f)
            else:
                self._install_win(f)
            self._set(phase="restart")
            if self.quit:
                threading.Timer(0.8, self.quit).start()
        except Exception as e:   # noqa: BLE001 : on affiche l'erreur, l'appli reste ouverte
            self._set(phase="error", error=str(e) or e.__class__.__name__)

    def _download(self, asset: dict) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        dest = self.dir / asset["name"]
        part = dest.with_suffix(dest.suffix + ".part")
        h = hashlib.sha256()
        with requests.get(asset["url"], stream=True, timeout=30) as r:
            r.raise_for_status()
            got = 0
            with open(part, "wb") as out:
                for chunk in r.iter_content(1 << 16):
                    out.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self._set(got=got)
        size = int(asset.get("size") or 0)
        if size and got != size:
            raise RuntimeError("Téléchargement incomplet, réessaie.")
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:") and digest[7:].lower() != h.hexdigest():
            raise RuntimeError("Le fichier téléchargé ne correspond pas à celui publié.")
        part.replace(dest)
        return dest

    def _install_mac(self, dmg: Path) -> None:
        old = mac_bundle()
        if not old:
            raise RuntimeError(why_not())
        mnt = Path(tempfile.mkdtemp(prefix="p2s-dmg-"))
        new = self.dir / "Playlist2Slsk.app"
        shutil.rmtree(new, ignore_errors=True)
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mnt), str(dmg)],
                       check=True, capture_output=True, timeout=120)
        try:
            src = mnt / "Playlist2Slsk.app"
            if not src.is_dir():
                raise RuntimeError("Image disque inattendue : Playlist2Slsk.app absente.")
            subprocess.run(["ditto", str(src), str(new)], check=True, capture_output=True, timeout=300)
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt), "-force"], capture_output=True, timeout=60)
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(new)], capture_output=True)
        script = self.dir / "installer.sh"
        script.write_text(
            '#!/bin/sh\n'
            'while kill -0 "$1" 2>/dev/null; do sleep 0.3; done\n'
            'rm -rf "$2.ancienne"\n'
            'if mv "$2" "$2.ancienne"; then\n'
            '  if mv "$3" "$2"; then rm -rf "$2.ancienne"; else mv "$2.ancienne" "$2"; fi\n'
            'fi\n'
            'open "$2"\n', "utf-8")
        subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(old), str(new)],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _install_win(self, setup: Path) -> None:
        q = lambda s: "'" + str(s).replace("'", "''") + "'"   # chaîne PowerShell
        cmd = (f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
               f"Start-Process -FilePath {q(setup)} -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/relaunch=1'")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=0x00000008 | 0x08000000)   # DETACHED_PROCESS | CREATE_NO_WINDOW
