#!/usr/bin/env bash
# CXT installer — builds the bridge and tells you the last 3 steps.
set -euo pipefail
cd "$(dirname "$0")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }; }
need go

echo "[1/3] building binaries..."
./build.sh

echo "[2/3] installing helper scripts into ~/.cxt ..."
DEST="${CXT_DIR:-$HOME/.cxt}"
mkdir -p "$DEST"
for f in scripts/*.py scripts/*.sh; do
  [ -e "$f" ] || continue
  cp -f "$f" "$DEST/"
done
chmod +x "$DEST"/*.sh

cat <<'EOF'

[3/3] done. Now finish setup:

  1) Start the local bridge:
       ./bin/cxtd &
     (or: nohup setsid ./bin/cxtd >>/tmp/cxtd.log 2>&1 </dev/null &)

  2) Load the extension in Chrome:
       chrome://extensions -> enable "Developer mode" -> "Load unpacked"
       -> select the ./extension folder

  3) Tell the CLI which tab to drive (id from `./bin/cxt tabs`):
       export CXT_TAB=<id>
       ./bin/cxt status

Docs: README.md
EOF
