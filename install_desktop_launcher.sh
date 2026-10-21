#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$HOME/Desktop"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"
DESKTOP_FILE_NAME="Sky Observation Planner.desktop"

mkdir -p "$APPS_DIR" "$ICON_DIR"
[[ -d "$DESKTOP_DIR" ]] || mkdir -p "$DESKTOP_DIR"

install_one() {
  local dest="$1"
  sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/sky_planner.desktop.template" > "$dest"
  chmod +x "$dest"
}

cp "$APP_DIR/assets/sky_planner_icon.png" "$ICON_DIR/sky-observation-planner.png"
install_one "$APPS_DIR/sky-observation-planner.desktop"
install_one "$DESKTOP_DIR/$DESKTOP_FILE_NAME"

# Remove legacy Stop shortcut from earlier installations; v0.6 does not install one.
rm -f "$APPS_DIR/sky-observation-planner-stop.desktop"

update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed launcher files:"
echo "  $APPS_DIR/sky-observation-planner.desktop"
echo "  $DESKTOP_DIR/$DESKTOP_FILE_NAME"
echo
echo "If your GNOME desktop does not show desktop icons, the launcher will still appear in the app grid/search."
