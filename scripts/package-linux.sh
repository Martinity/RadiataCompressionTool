#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:?usage: package-linux.sh VERSION ARCH}"
ARCH="${2:?}"
SRC="dist/RadiataModdingTool"
APPDIR="RadiataModdingTool.AppDir"
rm -rf "$APPDIR"; mkdir -p "$APPDIR/usr/bin"
cp -r "$SRC"/* "$APPDIR/usr/bin/"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/RadiataModdingTool" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/RadiataModdingTool.desktop" <<'EOF'
[Desktop Entry]
Name=RadiataModdingTool
Exec=RadiataModdingTool
Icon=radiata
Type=Application
Categories=Utility;
EOF
OUT="RadiataModdingTool-${VERSION}-linux-${ARCH}.AppImage"
if command -v appimagetool >/dev/null 2>&1; then
  APPIMAGE_ARCH="${ARCH}"
  [ "${APPIMAGE_ARCH}" = "amd64" ] && APPIMAGE_ARCH="x86_64"
  ARCH=x86_64 appimagetool "$APPDIR" "$OUT"
else
  OUT="RadiataModdingTool-${VERSION}-linux-${ARCH}.tar.gz"
  tar -czf "$OUT" -C dist RadiataModdingTool
fi
echo "$OUT"
