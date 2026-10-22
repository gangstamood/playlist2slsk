#!/bin/bash
# Double-clic dans le Finder pour lancer l'interface.
cd "$(dirname "$0")" || exit 1

# Environnement Python créé au premier lancement, HORS du dossier : si Documents est
# synchronisé avec iCloud, macOS peut décharger ses fichiers et Python reste bloqué.
VENV="$HOME/Library/Application Support/Playlist2Slsk/venv"
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
  echo "Premier lancement : préparation de l'environnement Python (une minute environ)…"
  mkdir -p "$(dirname "$VENV")"
  python3 -m venv "$VENV" || { echo "Impossible de créer l'environnement Python."; read -r; exit 1; }
fi
if ! "$PY" -c "import requests, mutagen, yt_dlp, numpy, soundfile" 2>/dev/null; then
  echo "Installation des dépendances…"
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt || { echo "Échec de l'installation."; read -r; exit 1; }
fi
# sockseek : installé dans ./bin au premier lancement s'il n'est pas déjà sur la machine.
if [ ! -x bin/sockseek ] && ! command -v sockseek >/dev/null && ! command -v sldl >/dev/null; then
  P2S_GUI=1 ./installer-sockseek.sh || { echo "Installation de sockseek échouée."; read -r; exit 1; }
fi
[ -x bin/sockseek ] && export SOCKSEEK="$PWD/bin/sockseek"

echo "Démarrage…"
exec "$PY" app.py
