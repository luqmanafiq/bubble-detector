#!/usr/bin/env bash
# BubbleMeter installer for Raspberry Pi OS (Bookworm, 64-bit recommended) and Debian/Ubuntu.
# Usage:  bash install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/bubblemeter"

echo "==> Installing system packages (uses apt, needs sudo)"
sudo apt update
sudo apt install -y python3-opencv python3-numpy python3-openpyxl python3-tk python3-pil python3-pil.imagetk
# CSI ribbon-cable camera support (Raspberry Pi OS only; harmless to skip elsewhere)
sudo apt install -y python3-picamera2 || echo "   (picamera2 not available - USB/RTSP/file sources still work)"
# Optional: lets you open the .xlsx results on the Pi itself
sudo apt install -y libreoffice-calc || echo "   (LibreOffice not installed - copy the .xlsx to another computer to open it)"

echo "==> Copying program to $DEST"
mkdir -p "$DEST"
rm -rf "$DEST/bubblemeter"
cp -r "$HERE/bubblemeter" "$DEST/"
cp "$HERE/README.md" "$DEST/" 2>/dev/null || true

echo "==> Creating launchers"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/BubbleMeter/sessions"
cat > "$HOME/.local/bin/bubblemeter" <<LAUNCH
#!/usr/bin/env bash
cd "$DEST"
exec python3 -m bubblemeter "\$@"
LAUNCH
cat > "$HOME/.local/bin/bubblemeter-headless" <<LAUNCH
#!/usr/bin/env bash
cd "$DEST"
exec python3 -m bubblemeter.cli "\$@"
LAUNCH
chmod +x "$HOME/.local/bin/bubblemeter" "$HOME/.local/bin/bubblemeter-headless"

cat > "$HOME/.local/share/applications/bubblemeter.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=BubbleMeter
Comment=Live bubble counter, diameter and volume
Exec=$HOME/.local/bin/bubblemeter
Terminal=false
Categories=Science;Education;
DESK
if [ -d "$HOME/Desktop" ]; then
  cp "$HOME/.local/share/applications/bubblemeter.desktop" "$HOME/Desktop/BubbleMeter.desktop"
  chmod +x "$HOME/Desktop/BubbleMeter.desktop"
fi

echo
echo "Done.  Start it from the menu (Science > BubbleMeter), the desktop icon, or:  bubblemeter"
echo "Make sure ~/.local/bin is on your PATH (log out/in once if the command is not found)."
echo "Results are saved in  ~/BubbleMeter/sessions/session_<date_time>/  (bubbles.xlsx + bubbles.csv)"
