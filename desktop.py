#!/usr/bin/env python3
"""Point d'entrée de l'appli installée (Mac et Windows).

Ouvre l'interface dans une vraie fenêtre (pywebview) avec le serveur local en
arrière-plan. Le même exécutable sert aussi à faire tourner le script de
téléchargement : `Playlist2Slsk --p2s-script <arguments de playlist2slsk.py>`.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
LOG_DIR = Path.home() / ".playlist2slsk"


def run_script() -> None:
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    sys.path.insert(0, str(BASE))
    import playlist2slsk
    playlist2slsk.main()


def main() -> None:
    # Appli sans console : on garde les messages dans un journal.
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / "appli.log", "w", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    exe = "sockseek.exe" if sys.platform == "win32" else "sockseek"
    bundled = BASE / "sockseek" / exe
    if bundled.exists():
        os.environ.setdefault("SOCKSEEK", str(bundled))
    os.environ["P2S_NO_BROWSER"] = "1"
    os.environ["P2S_NATIVE_PICKER"] = "1"

    sys.path.insert(0, str(BASE))
    import app
    import webview

    srv, url = app.make_server()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=app.sync_on_launch, daemon=True).start()

    class Api:
        def pick_folder(self):
            r = window.create_file_dialog(webview.FOLDER_DIALOG)
            if not r:
                return None
            path = str(r[0] if isinstance(r, (list, tuple)) else r).rstrip("/\\")
            home = str(Path.home())
            return "~" + path[len(home):] if path.startswith(home) else path

    window = webview.create_window("Playlist2Slsk", url, js_api=Api(), width=1240, height=880,
                                   min_size=(820, 600), background_color="#ECEBE6", text_select=True)
    webview.start(private_mode=False)   # garde la connexion Spotify d'une fois sur l'autre
    app.stop_job()
    os._exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--p2s-script":
        run_script()
    else:
        main()
