#!/usr/bin/env bash
# test_scraping_e2e.sh - E2E del puente CXT para las 4 ops de scraping (T8).
# Arranca cxtd aislado + el doble de la extension (mock_ext.mjs), sin Chrome ni red.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${CXT_TEST_PORT:-8797}"
TOK=tt
TMP="$(mktemp -d)"
export CXT_PORT="$PORT" CXT_TOKEN="$TOK" CXT_DIR="$TMP" CXT_NO_SCHED=1

for f in scheduler.py groups_scan.py groups_share.py share_post.py comment.py clone_publish.py news.sh autopost.py cxtlib.py; do
  ln -s "$HERE/$f" "$TMP/$f"
done
printf '%s' '{"count":0,"groups":[]}' > "$TMP/groups.json"

# Sin esto un daemon ajeno en $PORT haria pasar el test en falso.
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  exec 3>&- 3<&-
  echo "FAIL puerto $PORT ocupado"
  rm -rf "$TMP"
  exit 1
fi

DPID=""
MPID=""
trap 'kill ${DPID:-} ${MPID:-} 2>/dev/null; wait 2>/dev/null; rm -rf "$TMP"' EXIT INT TERM

"$HERE/bin/cxtd" >"$TMP/cxtd.log" 2>&1 &
DPID=$!
sleep 0.6
CXT_SERVER="http://127.0.0.1:$PORT" CXT_TOKEN="$TOK" node "$HERE/mock_ext.mjs" &
MPID=$!
sleep 0.6

run() { CXT_SERVER="http://127.0.0.1:$PORT" CXT_TOKEN="$TOK" "$HERE/bin/cxt" "$@"; }

FAIL=0
chk() { if [ "$1" = 0 ]; then echo "PASS $2"; else echo "FAIL $2"; FAIL=1; fi; }

# Cada op: exit 0 + esquema completo del payload del doble.
OUT="$(run scrape.page '{}')"; RC=$?
echo "$OUT"
if [ "$RC" = 0 ] \
   && printf '%s' "$OUT" | grep -Fq '"url"' \
   && printf '%s' "$OUT" | grep -Fq '"title"' \
   && printf '%s' "$OUT" | grep -Fq '"text"' \
   && printf '%s' "$OUT" | grep -Fq '"chars"' \
   && printf '%s' "$OUT" | grep -Fq '"mode"' \
   && printf '%s' "$OUT" | grep -Fq '"frames"'; then
  chk 0 scrape.page
else
  chk 1 scrape.page
fi

OUT="$(run scrape.markdown '{}')"; RC=$?
echo "$OUT"
if [ "$RC" = 0 ] \
   && printf '%s' "$OUT" | grep -Fq '"url"' \
   && printf '%s' "$OUT" | grep -Fq '"title"' \
   && printf '%s' "$OUT" | grep -Fq '"markdown"' \
   && printf '%s' "$OUT" | grep -Fq '"chars"'; then
  chk 0 scrape.markdown
else
  chk 1 scrape.markdown
fi

OUT="$(run scrape.tables '{}')"; RC=$?
echo "$OUT"
if [ "$RC" = 0 ] \
   && printf '%s' "$OUT" | grep -Fq '"url"' \
   && printf '%s' "$OUT" | grep -Fq '"tables"' \
   && printf '%s' "$OUT" | grep -Fq '"truncated"'; then
  chk 0 scrape.tables
else
  chk 1 scrape.tables
fi

OUT="$(run scrape.links '{}')"; RC=$?
echo "$OUT"
if [ "$RC" = 0 ] \
   && printf '%s' "$OUT" | grep -Fq '"url"' \
   && printf '%s' "$OUT" | grep -Fq '"links"' \
   && printf '%s' "$OUT" | grep -Fq '"truncated"'; then
  chk 0 scrape.links
else
  chk 1 scrape.links
fi

# Caso de error: op desconocida -> el CLI sale 1 y reporta "error: op desconocida".
OUT="$(run scrape.nope '{}' 2>&1)"; RC=$?
echo "$OUT"
if [ "$RC" = 1 ] \
   && printf '%s' "$OUT" | grep -Fq 'error:' \
   && printf '%s' "$OUT" | grep -Fq 'op desconocida'; then
  chk 0 'scrape.nope exit=1'
else
  chk 1 'scrape.nope exit=1'
fi

# Parada explicita antes de salir (el trap sigue haciendo la limpieza final).
kill "$DPID" "$MPID" 2>/dev/null
sleep 0.3
if kill -0 "$DPID" 2>/dev/null || kill -0 "$MPID" 2>/dev/null; then
  echo "FAIL procesos vivos"
  exit 1
fi
echo "CLEAN"

exit "$FAIL"
