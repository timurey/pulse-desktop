#!/usr/bin/env bash
# DMG из собранного приложения: окно с «Pulse Scan.app» и ярлыком «Программы», плюс руководство.
#   bash packaging/make_dmg.sh "dist/Pulse Scan.app" PulseScan-0.9.0-rc1-macOS-arm64.dmg
set -euo pipefail
APP="$1"; OUT="$2"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d)/Pulse Scan"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Программы"
cp -R "$HERE/docs/manual" "$STAGE/Руководство"
rm -f "$OUT"
hdiutil create -volname "Pulse Scan" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$OUT" >/dev/null
echo "готово: $OUT ($(du -h "$OUT" | cut -f1))"
