#!/bin/bash
# Double-clic dans le Finder : construit l'appli « Playlist2Slsk » et la range
# dans Applications. À relancer après une mise à jour de l'outil.
cd "$(dirname "$0")" || exit 1
DIR="$PWD"
APP_NAME="Playlist2Slsk"
mkdir -p mac
exec > >(tee mac/build.log) 2>&1
fail() { echo; echo "✗ $1"; echo "(détails dans mac/build.log)"; echo "Appuie sur Entrée pour fermer."; read -r; exit 1; }

echo "== Playlist2Slsk : création de l'appli Mac =="

# 1. Compilateur Swift (outils en ligne de commande d'Apple, déjà là si Homebrew est installé)
if ! xcrun --find swiftc >/dev/null 2>&1; then
  xcode-select --install 2>/dev/null
  fail "Il manque les outils de développement d'Apple. Une fenêtre vient de s'ouvrir pour les installer : accepte, puis relance ce fichier."
fi

# 2. Environnement Python et dépendances (comme Lancer.command)
if [ ! -x .venv/bin/python ]; then
  echo "Préparation de l'environnement Python…"
  python3 -m venv .venv || fail "Impossible de créer l'environnement Python."
fi
if ! .venv/bin/python -c "import requests, mutagen, yt_dlp, numpy, soundfile" 2>/dev/null; then
  echo "Installation des dépendances…"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt || fail "Échec de l'installation des dépendances."
fi
if [ ! -x bin/sockseek ] && ! command -v sockseek >/dev/null && ! command -v sldl >/dev/null; then
  P2S_GUI=1 ./installer-sockseek.sh || fail "Installation de sockseek échouée."
fi

# 3. Compilation
echo "Compilation de l'appli…"
BUILD="$(mktemp -d)"
xcrun swiftc -O -parse-as-library -target "$(uname -m)-apple-macos11.0" \
  -o "$BUILD/$APP_NAME" mac/Playlist2Slsk.swift || fail "La compilation a échoué."

# 4. Paquet .app
APP="$BUILD/$APP_NAME.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv "$BUILD/$APP_NAME" "$APP/Contents/MacOS/$APP_NAME"
cp mac/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
XML_DIR=$(printf '%s' "$DIR" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>local.playlist2slsk.app</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>$(date +%Y%m%d%H%M)</string>
  <key>CFBundleDevelopmentRegion</key><string>fr</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.music</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Playlist → Soulseek → fichiers rangés</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSDocumentsFolderUsageDescription</key><string>Playlist2Slsk range tes morceaux et lit ses propres fichiers dans Documents.</string>
  <key>NSDownloadsFolderUsageDescription</key><string>Playlist2Slsk récupère l'export Exportify que tu viens de télécharger.</string>
  <key>NSDesktopFolderUsageDescription</key><string>Playlist2Slsk peut ranger tes morceaux sur le Bureau si tu le choisis.</string>
  <key>NSRemovableVolumesUsageDescription</key><string>Playlist2Slsk peut ranger tes morceaux sur un disque externe ou une clé USB.</string>
  <key>NSNetworkVolumesUsageDescription</key><string>Playlist2Slsk peut ranger tes morceaux sur un disque réseau.</string>
  <key>P2SProjectDir</key><string>$XML_DIR</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null || fail "Info.plist invalide."
codesign --force --deep --sign - "$APP" || fail "Signature de l'appli échouée."

# 5. Installation dans Applications (ou ~/Applications si besoin)
osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1
DEST="/Applications"
if ! { rm -rf "$DEST/$APP_NAME.app" && ditto "$APP" "$DEST/$APP_NAME.app"; } 2>/dev/null; then
  DEST="$HOME/Applications"; mkdir -p "$DEST"
  rm -rf "$DEST/$APP_NAME.app"
  ditto "$APP" "$DEST/$APP_NAME.app" || fail "Impossible de copier l'appli dans Applications."
fi
xattr -cr "$DEST/$APP_NAME.app" 2>/dev/null
rm -rf "$BUILD"
touch "$DEST/$APP_NAME.app"

echo
echo "✓ Appli installée : $DEST/$APP_NAME.app"
echo "  Astuce : clic droit sur son icône dans le Dock › Options › Garder dans le Dock."
open "$DEST/$APP_NAME.app"
echo "Tu peux fermer cette fenêtre."
exit 0
