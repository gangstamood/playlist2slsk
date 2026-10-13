#!/bin/bash
# Installe sockseek dans ./bin (sans sudo) et crée la config Soulseek si besoin.
cd "$(dirname "$0")" || exit 1
set -e

case "$(uname -m)" in
  arm64) ARCH=osx-arm64 ;;
  *)     ARCH=osx-x64 ;;
esac

echo "Recherche de la dernière version de sockseek ($ARCH)…"
URL=$(curl -fsSL https://api.github.com/repos/fiso64/sockseek/releases/latest \
      | grep -o "https://[^\"]*_${ARCH}\.tar\.gz" | head -1)
if [ -z "$URL" ]; then
  echo "Impossible de trouver le fichier à télécharger. Vérifie ta connexion."
  exit 1
fi

TMP=$(mktemp -d)
echo "Téléchargement : $(basename "$URL")"
curl -fL --progress-bar "$URL" -o "$TMP/sockseek.tar.gz"
tar -xzf "$TMP/sockseek.tar.gz" -C "$TMP"
BIN=$(find "$TMP" -type f -name sockseek | head -1)
if [ -z "$BIN" ]; then
  echo "Le binaire sockseek est introuvable dans l'archive."
  exit 1
fi
mkdir -p bin
cp -R "$(dirname "$BIN")"/. bin/
rm -f bin/sockseek.tar.gz
chmod +x bin/sockseek
xattr -dr com.apple.quarantine bin 2>/dev/null || true
rm -rf "$TMP"
echo "✓ sockseek installé dans $(pwd)/bin"

CONF="$HOME/.config/sockseek/sockseek.conf"
# Lancé depuis Lancer.command, le compte se règle dans l'interface (fenêtre de connexion).
if [ ! -f "$CONF" ] && [ -z "$P2S_GUI" ]; then
  echo
  echo "Configuration Soulseek (idéalement un compte dédié, pas celui de ton client habituel)."
  read -r -p "  Identifiant Soulseek : " SLSK_USER
  read -r -s -p "  Mot de passe : " SLSK_PASS; echo
  mkdir -p "$(dirname "$CONF")"
  printf 'username = %s\npassword = %s\n' "$SLSK_USER" "$SLSK_PASS" > "$CONF"
  chmod 600 "$CONF"
  echo "✓ Config enregistrée dans $CONF"
fi
