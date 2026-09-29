#!/usr/bin/env bash
# Install the ImageGenerate launcher (Linux/GNOME) and, optionally, the
# StoryGenerate one.
# Run after cloning (safe to re-run):  bash install.sh
#   bash install.sh --with-story   # also install StoryGenerate, no question
#   bash install.sh --no-story     # ImageGenerate only, no question
# Without a flag it asks (non-interactive runs default to "no").
# Generates ~/.local/share/applications/<app>.desktop with the correct
# absolute paths for THIS machine (a .desktop file cannot use relative paths)
# and registers the icon in the hicolor theme. Only launchers and icons are
# written: settings, saved keys and logs (~/.config/image_generate/) are
# never touched.
set -euo pipefail

# Per-user install: under sudo HOME becomes /root, so the launchers would land
# in root's menu (not yours) and gio has no session to mark them trusted.
if [ "$(id -u)" -eq 0 ]; then
  echo "error: não rode com sudo — o instalador é por usuário (~/.local)." >&2
  echo "       rode assim:  bash install.sh" >&2
  exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"

WITH_STORY=""
for arg in "$@"; do
  case "$arg" in
    --with-story) WITH_STORY="yes" ;;
    --no-story) WITH_STORY="no" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (use --with-story or --no-story)" >&2; exit 2 ;;
  esac
done

mkdir -p "$APP_DIR" "$ICON_DIR"

# install_launcher <id> <Name> <GenericName> <Comment> <script> <WM_CLASS instance> <WM_CLASS class> <Categories> <Keywords>
install_launcher() {
  local id="$1" name="$2" generic="$3" comment="$4" script="$5" wminstance="$6"
  local wmclass="$7" categories="$8" keywords="$9"
  local desktop="$APP_DIR/$id.desktop"
  cp "$REPO/img/logo.png" "$ICON_DIR/$id.png"
  cat > "$desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$name
GenericName=$generic
Comment=$comment
Exec=python3 $REPO/$script --gui
Icon=$id
Terminal=false
Categories=$categories
StartupNotify=true
# Must match the Tk window class EXACTLY (case-sensitive).
# Verified with: xwininfo/xprop -> WM_CLASS = "$wminstance", "$wmclass".
StartupWMClass=$wmclass
Keywords=$keywords
EOF
  chmod 644 "$desktop"
  # Nautilus only launches .desktop files with the exec bit ("trusted").
  chmod +x "$desktop"
  # Best effort: needs a GNOME/GVfs session (unsupported elsewhere, harmless).
  command -v gio >/dev/null && gio set "$desktop" metadata::trusted true 2>/dev/null || true
  command -v desktop-file-validate >/dev/null && desktop-file-validate "$desktop"
  echo "installed: $desktop"
}

install_launcher image-generate ImageGenerate "AI Image Generator" \
  "Generate images via AI (prompt + context + references)" \
  image_generate.py imageGenerate Imagegenerate "Graphics;2DGraphics;" "ai;image;generate;openrouter;"

if [ -z "$WITH_STORY" ]; then
  if [ -t 0 ]; then
    read -r -p "Instalar também o StoryGenerate (storyboard -> história narrada)? [s/N] " answer
    case "${answer,,}" in
      s|sim|y|yes) WITH_STORY="yes" ;;
      *) WITH_STORY="no" ;;
    esac
  else
    WITH_STORY="no"
  fi
fi

if [ "$WITH_STORY" = "yes" ]; then
  install_launcher story-generate StoryGenerate "AI Storyboard Narrator" \
    "Turn storyboards into written, narrated stories (writer + Fish Audio voices)" \
    story_generate.py storyGenerate Storygenerate "AudioVideo;Audio;" \
    "ai;story;storyboard;narration;tts;fish;audio;"
else
  echo "skipped: StoryGenerate (run again with --with-story to add it)"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$APP_DIR" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" || true

apps="ImageGenerate"
[ "$WITH_STORY" = "yes" ] && apps="ImageGenerate / StoryGenerate"
echo "Open via Super key -> $apps (launch from there, not the terminal)."
