#!/bin/zsh
# Installe Patchbay Webflow sur macOS : bundle .app dans ~/Applications (icône incluse) + CLI wfmcp.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
NAME="Patchbay Webflow"
APP="$HOME/Applications/$NAME.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>com.efougerouse.patchbay-webflow</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

# shell de login : récupère le PATH de ~/.zprofile (Homebrew, nvm…) que le Finder ne transmet pas
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/zsh -l
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH"
exec python3 "$DIR/app.py"
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"

# icône : rendu du SVG par QuickLook, déclinaisons par sips, assemblage iconutil
TMP=$(mktemp -d)
if qlmanage -t -s 1024 -o "$TMP" "$DIR/icon.svg" >/dev/null 2>&1 && [ -f "$TMP/icon.svg.png" ]; then
  mkdir "$TMP/AppIcon.iconset"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/icon.svg.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) "$TMP/icon.svg.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
else
  echo "Icône non générée (QuickLook n'a pas rendu icon.svg) — l'app fonctionne sans."
fi
rm -rf "$TMP"

mkdir -p "$HOME/.local/bin"
cp "$DIR/wfmcp" "$HOME/.local/bin/wfmcp" && chmod +x "$HOME/.local/bin/wfmcp"

echo "Installé : $APP"
echo "Au premier lancement, macOS demandera : l'accès au trousseau (lecture des tokens MCP de Claude Code)"
echo "et le contrôle de Terminal (bouton « Ouvrir le terminal ici »). Répondre « Toujours autoriser »."
