#!/bin/bash
# Double-clic dans le Finder pour lancer l'interface.
cd "$(dirname "$0")" || exit 1

# Environnement Python propre au dossier (.venv), créé au premier lancement.
if [ ! -x .venv/bin/python ]; then
  echo "Premier lancement : préparation de l'environnement Python…"
  python3 -m venv .venv || { echo "Impossible de créer l'environnement Python."; read -r; exit 1; }
fi
if ! .venv/bin/python -c "import requests, mutagen, yt_dlp, numpy, soundfile" 2>/dev/null; then
  echo "Installation des dépendances…"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt || { echo "Échec de l'installation."; read -r; exit 1; }
fi
# sockseek : installé dans ./bin au premier lancement s'il n'est pas déjà sur la machine.
if [ ! -x bin/sockseek ] && ! command -v sockseek >/dev/null && ! command -v sldl >/dev/null; then
  P2S_GUI=1 ./installer-sockseek.sh || { echo "Installation de sockseek échouée."; read -r; exit 1; }
fi
[ -x bin/sockseek ] && export SOCKSEEK="$PWD/bin/sockseek"

exec .venv/bin/python app.py
