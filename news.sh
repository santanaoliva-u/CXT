#!/usr/bin/env bash
# news.sh "<consulta>" [n] -> titulares compactos (1 por linea), sin ruido.
set -euo pipefail
Q="${1:-Mexico}"; N="${2:-5}"
OBSCURA="${OBSCURA:-$HOME/.local/bin/obscura}"
ENC="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$Q")"
URL="https://news.google.com/rss/search?q=${ENC}&hl=es-419&gl=MX&ceid=MX:es-419"
EVAL="(()=>[...document.querySelectorAll('item title')].slice(0,${N}).map(e=>e.textContent).join('\\n'))()"
"$OBSCURA" scrape "$URL" --eval "$EVAL" --format text -q \
  | python3 -c '
import sys
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if parts:
        s = parts[-1].strip().strip("\"")
        s = s.replace("\\n", "\n").replace("\\t", "\t").replace("\\\"", "\"").replace("\\\\", "\\")
        print(s)
' | head -n "$N"
