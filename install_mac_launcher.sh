#!/usr/bin/env bash
# Create a Finder-clickable application in ~/Applications, linked to this folder.
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Applications/Sky Observation Planner.app"
MACOS="$DEST/Contents/MacOS"
RESOURCES="$DEST/Contents/Resources"
mkdir -p "$MACOS" "$RESOURCES"
# Quote APP_DIR for safe use with spaces or apostrophes in the project path.
printf '#!/usr/bin/env bash\nexec /bin/bash %q\n' "$APP_DIR/launch_sky_planner_mac.sh" > "$MACOS/SkyPlanner"
chmod +x "$MACOS/SkyPlanner"
cat > "$DEST/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Sky Observation Planner</string>
  <key>CFBundleDisplayName</key><string>Sky Observation Planner</string>
  <key>CFBundleIdentifier</key><string>org.skyplanner.observation</string>
  <key>CFBundleExecutable</key><string>SkyPlanner</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>0.6.1</string>
  <key>CFBundleShortVersionString</key><string>0.6.1</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
# macOS .icns generation is optional: launcher works even when conversion fails.
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/SkyPlanner.iconset"
  for size in 16 32 128 256 512; do
    sips -s format png -z "$size" "$size" "$APP_DIR/assets/sky_planner_icon.png" --out "$tmp/SkyPlanner.iconset/icon_${size}x${size}.png" >/dev/null 2>&1 || true
  done
  for size in 16 32 128 256 512; do
    double=$((size*2))
    sips -s format png -z "$double" "$double" "$APP_DIR/assets/sky_planner_icon.png" --out "$tmp/SkyPlanner.iconset/icon_${size}x${size}@2x.png" >/dev/null 2>&1 || true
  done
  if iconutil -c icns "$tmp/SkyPlanner.iconset" -o "$RESOURCES/SkyPlanner.icns" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c 'Add :CFBundleIconFile string SkyPlanner' "$DEST/Contents/Info.plist" || true
  fi
  rm -rf "$tmp"
fi
printf 'Installed: %s\nDouble-click it from Finder or Launchpad (when indexed).\nLog: ~/Library/Logs/SkyPlanner/server.log\n' "$DEST"
printf 'Keep the original Sky Planner folder in place. If you move it, rerun this installer.\n'
