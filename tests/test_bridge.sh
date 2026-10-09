#!/usr/bin/env bash
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${CXT_TEST_PORT:-8798}"
TOK=tt
export CXT_PORT="$PORT" CXT_TOKEN="$TOK"

"$HERE/bin/cxtd" >/tmp/cxt_test_daemon.log 2>&1 &
DPID=$!
MPID=""
trap 'kill ${DPID:-} ${MPID:-} 2>/dev/null' EXIT
sleep 0.6

CXT_SERVER="http://127.0.0.1:$PORT" CXT_TOKEN="$TOK" node "$HERE/mock_ext.mjs" &
MPID=$!
sleep 0.6

run() { CXT_SERVER="http://127.0.0.1:$PORT" CXT_TOKEN="$TOK" "$HERE/bin/cxt" "$@"; }
FAIL=0
chk() { if [ "$1" = 0 ]; then echo "PASS: $2"; else echo "FAIL: $2"; FAIL=1; fi; }

OUT="$(run ping)"; echo "$OUT" | grep -q '"pong": true'; chk $? "ping -> pong"
OUT="$(run sum '{"a":2,"b":3}')"; echo "$OUT" | grep -q '"sum": 5'; chk $? "sum 2+3 = 5"
OUT="$(run echo hola)"; echo "$OUT" | grep -q '"text": "hola"'; chk $? "texto libre -> {text}"
OUT="$(run read '{"max":10}')"; echo "$OUT" | grep -q '"title": "Mock"'; chk $? "read -> title Mock"

if [ "$FAIL" = 0 ]; then echo "BRIDGE PASS"; else echo "BRIDGE FAIL"; fi
exit "$FAIL"
