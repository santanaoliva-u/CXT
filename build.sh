#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p bin
go build -trimpath -ldflags '-s -w' -o bin/cxtd ./server
go build -trimpath -ldflags '-s -w' -o bin/cxt ./cli
echo "OK: $(ls -la bin/cxt bin/cxtd | awk '{print $NF, $5}')"
