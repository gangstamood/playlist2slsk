#!/bin/bash
# Construit l'appli autonome (utilisé par GitHub Actions, marche aussi en local).
#   packaging/build.sh <cible sockseek : osx-arm64 | osx-x64 | win-x64>
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="$1"
# Limite de temps (timeout n'existe pas sur macOS)
tlimit() { local t=$1; shift; if command -v timeout >/dev/null; then timeout "$t" "$@"; else perl -e 'alarm shift; exec @ARGV' "$t" "$@"; fi; }

# 1. sockseek pour la plateforme visée
rm -rf build/sockseek && mkdir -p build/sockseek build/dl
python - "$TARGET" <<'PY'
import json, os, sys, urllib.request
target = sys.argv[1]
# Version figée : une nouvelle version de sockseek n'entre dans l'appli que si on change ce numéro.
tag = os.environ.get("SOCKSEEK_TAG", "v3.0.5")
req = urllib.request.Request("https://api.github.com/repos/fiso64/sockseek/releases/tags/" + tag,
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
import hashlib
print("sha256", hashlib.sha256(open("build/dl/" + a["name"], "rb").read()).hexdigest())
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
tlimit 60 "build/sockseek/$EXE" --help 2>&1 < /dev/null | grep -E -- "--config|--progress-json|--name-format" || echo "(attention : options non trouvées dans --help)"

# 2. Client ID Spotify partagé (facultatif, pas secret)
if [ -n "${SPOTIFY_CLIENT_ID:-}" ]; then printf '%s' "$SPOTIFY_CLIENT_ID" > build/spotify-client-id.txt
elif [ -f packaging/spotify-client-id.txt ]; then cp packaging/spotify-client-id.txt build/spotify-client-id.txt; fi

# 3. Version (tag v1.2.3 → 1.2.3, sinon le fichier VERSION) et textes de licence livrés avec l'appli
VER="${APP_VERSION:-$(cat VERSION)}"
printf '%s' "$VER" > build/VERSION
echo "Version : $VER"

# 4. PyInstaller
ARGS=(--noconfirm --clean --windowed --name Playlist2Slsk
      --add-data "ui.html:." --add-data "playlist2slsk.py:."
      --add-data "build/VERSION:." --add-data "LICENSE:." --add-data "CREDITS.md:."
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

# 5. Contrôle : l'exécutable sait faire tourner le script de téléchargement
if [ "$TARGET" = win-x64 ]; then
  tlimit 120 ./dist/Playlist2Slsk/Playlist2Slsk.exe --p2s-script --help > build/smoke.txt 2>&1 || true
  cat build/smoke.txt
  grep -q "usage:" build/smoke.txt || { echo "L'exécutable ne fait pas tourner le script"; exit 1; }
else
  APP=dist/Playlist2Slsk.app
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName Playlist2Slsk" "$APP/Contents/Info.plist" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string Playlist2Slsk" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 11.0" "$APP/Contents/Info.plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :NSHumanReadableCopyright string © Lutin · licence GNU AGPL v3" "$APP/Contents/Info.plist" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" "$APP/Contents/Info.plist" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VER" "$APP/Contents/Info.plist"
  if [ -n "${MAC_SIGN_IDENTITY:-}" ]; then
    # Signature « Developer ID » (compte développeur Apple) : plus d'alerte « développeur non identifié »
    # une fois l'image disque notarisée. Le runtime renforcé exige ces droits pour Python et .NET (sockseek).
    find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) -print0 \
      | xargs -0 -I{} codesign --force --timestamp --options runtime \
          --entitlements packaging/entitlements.plist --sign "$MAC_SIGN_IDENTITY" {} 2>/dev/null || true
    codesign --force --timestamp --options runtime --entitlements packaging/entitlements.plist \
      --sign "$MAC_SIGN_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
  else
    codesign --force --deep --sign - "$APP"     # sans compte Apple : signature locale (« Ouvrir quand même »)
  fi
  "$APP/Contents/MacOS/Playlist2Slsk" --p2s-script --help | head -3
fi
