#!/usr/bin/env bash
# CXT installer — builds the bridge and tells you the last 3 steps.
set -euo pipefail
cd "$(dirname "$0")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }; }
need go

echo "[1/2] building binaries..."
./build.sh

cat <<'EOF'

[2/2] done. Now finish setup:

  1) Start the local bridge:
       ./bin/cxtd &
     (or: nohup setsid ./bin/cxtd >>/tmp/cxtd.log 2>&1 </dev/null &)

  2) Load the extension in Chrome:
       chrome://extensions -> enable "Developer mode" -> "Load unpacked"
       -> select the ./extension folder

  3) Check it works:
       ./bin/cxt status

Docs: README.md
EOF
