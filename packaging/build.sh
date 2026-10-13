#!/bin/bash
# Construit l'appli autonome (utilisé par GitHub Actions, marche aussi en local).
#   packaging/build.sh <cible sockseek : osx-arm64 | osx-x64 | win-x64>
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="$1"

# 1. sockseek pour la plateforme visée
rm -rf build/sockseek && mkdir -p build/sockseek build/dl
python - "$TARGET" <<'PY'
import json, os, sys, urllib.request
target = sys.argv[1]
req = urllib.request.Request("https://api.github.com/repos/fiso64/sockseek/releases/latest",
                             headers={"Accept": "application/vnd.github+json",
                                      **({"Authorization": "Bearer " + os.environ["GH_TOKEN"]} if os.environ.get("GH_TOKEN") else {})})
rel = json.load(urllib.request.urlopen(req))
print("sockseek", rel["tag_name"], "assets:", [a["name"] for a in rel["assets"]])
cands = [a for a in rel["assets"] if target in a["name"] and a["name"].endswith((".zip", ".tar.gz"))]
if not cands:
    sys.exit(f"aucun fichier sockseek pour {target}")
a = cands[0]
print("téléchargement", a["name"])
urllib.request.urlretrieve(a["browser_download_url"], "build/dl/" + a["name"])
open("build/dl/NAME", "w").write(a["name"])
PY
NAME=$(cat build/dl/NAME)
case "$NAME" in
  *.zip) python -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall('build/dl/x')" "build/dl/$NAME" ;;
  *) mkdir -p build/dl/x && tar -xzf "build/dl/$NAME" -C build/dl/x ;;
esac
EXE=sockseek; [ "$TARGET" = win-x64 ] && EXE=sockseek.exe
BIN=$(find build/dl/x -type f -name "$EXE" | head -1)
[ -n "$BIN" ] || { echo "binaire $EXE introuvable"; find build/dl/x; exit 1; }
cp -R "$(dirname "$BIN")"/. build/sockseek/
chmod +x "build/sockseek/$EXE"
echo "--- sockseek --help (options utilisées par l'appli) ---"
"build/sockseek/$EXE" --help 2>&1 | grep -E -- "--config|--progress-json|--name-format" || echo "(attention : options non trouvées dans --help)"

# 2. Client ID Spotify partagé (facultatif, pas secret)
if [ -n "${SPOTIFY_CLIENT_ID:-}" ]; then printf '%s' "$SPOTIFY_CLIENT_ID" > build/spotify-client-id.txt; fi

# 3. PyInstaller
ARGS=(--noconfirm --clean --windowed --name Playlist2Slsk
      --add-data "ui.html:." --add-data "playlist2slsk.py:."
      --hidden-import app --hidden-import playlist2slsk
      --collect-submodules yt_dlp --collect-all soundfile)
[ -f build/spotify-client-id.txt ] && ARGS+=(--add-data "build/spotify-client-id.txt:.")
for f in build/sockseek/*; do ARGS+=(--add-binary "$f:sockseek"); done
if [ "$TARGET" = win-x64 ]; then
  ARGS+=(--icon packaging/icon.ico)
else
  ARGS+=(--icon mac/AppIcon.icns --osx-bundle-identifier local.playlist2slsk.app)
fi
pyinstaller "${ARGS[@]}" desktop.py

# 4. Contrôle : l'exécutable sait faire tourner le script de téléchargement
if [ "$TARGET" = win-x64 ]; then
  ./dist/Playlist2Slsk/Playlist2Slsk.exe --p2s-script --help | head -3
else
  APP=dist/Playlist2Slsk.app
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName Playlist2Slsk" "$APP/Contents/Info.plist" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string Playlist2Slsk" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 11.0" "$APP/Contents/Info.plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :NSHumanReadableCopyright string Playlist → Soulseek → fichiers rangés" "$APP/Contents/Info.plist" 2>/dev/null || true
  codesign --force --deep --sign - "$APP"
  "$APP/Contents/MacOS/Playlist2Slsk" --p2s-script --help | head -3
fi
