#!/usr/bin/env bash
# Install the ImageGenerate launcher (Linux/GNOME).
# Run once after cloning:  bash install.sh
# Generates ~/.local/share/applications/image-generate.desktop with the
# correct absolute paths for THIS machine (a .desktop file cannot use
# relative paths) and registers the icon in the hicolor theme.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"

mkdir -p "$APP_DIR" "$ICON_DIR"
cp "$REPO/logo.png" "$ICON_DIR/image-generate.png"

cat > "$APP_DIR/image-generate.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=ImageGenerate
GenericName=AI Image Generator
Comment=Generate images via AI (prompt + context + references)
Exec=python3 $REPO/image_generate.py --gui
Icon=image-generate
Terminal=false
Categories=Graphics;2DGraphics;
StartupNotify=true
# Must match the Tk window class EXACTLY (case-sensitive).
# Verified with: xwininfo/xprop -> WM_CLASS = "imageGenerate", "Imagegenerate".
StartupWMClass=Imagegenerate
Keywords=ai;image;generate;openrouter;
EOF

chmod 644 "$APP_DIR/image-generate.desktop"
command -v desktop-file-validate >/dev/null && desktop-file-validate "$APP_DIR/image-generate.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$APP_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" || true

echo "installed: $APP_DIR/image-generate.desktop"
echo "Open it via Super key -> ImageGenerate (launch from there, not the terminal)."
