#!/usr/bin/env bash
set -euo pipefail
VERSION="${1:?usage: package-macos.sh VERSION ARCH}"
ARCH="${2:?}"
APP="dist/RadiataModdingTool.app"
OUT="RadiataModdingTool-${VERSION}-macos-${ARCH}.dmg"
hdiutil create -volname "RadiataModdingTool" -srcfolder "$APP" -ov -format UDZO "$OUT"
echo "$OUT"
