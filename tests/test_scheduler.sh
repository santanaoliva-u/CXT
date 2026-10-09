#!/usr/bin/env bash
# Prueba determinista del scheduler: 1 tick con stub de cxt (sin tocar Facebook).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"
mkdir -p "$TMP/.cxt/bin"

cat > "$TMP/.cxt/bin/cxt" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  tabs) echo '{"id":1,"url":"https://www.facebook.com/x"}' ;;
  deepfind) echo '{"items":[]}' ;;
  *) echo '{"ok":true}' ;;
esac
STUB
chmod +x "$TMP/.cxt/bin/cxt"

cp "$HERE/../scripts/clone_publish.py" "$TMP/.cxt/clone_publish.py"
[ -f "$HERE/../scripts/news.sh" ] && cp "$HERE/news.sh" "$TMP/.cxt/news.sh"

cat > "$TMP/.cxt/schedule.json" <<'J'
{"enabled":true,"interval_min":0.02,"count":1,"remaining":1,"topic":"test fijo","next_at":0,"started_at":0,"last":null,"sched_alive":0}
J

export CXT_BIN="$TMP/.cxt/bin/cxt"
HOME="$TMP" python3 "$HERE/scheduler.py" >"$TMP/sched.log" 2>&1 &
SPID=$!
for i in $(seq 1 90); do
  grep -q '"last": *{' "$TMP/.cxt/schedule.json" 2>/dev/null && break
  sleep 1
done
kill "$SPID" 2>/dev/null

echo "--- schedule.json ---"
cat "$TMP/.cxt/schedule.json"
echo
echo "--- log ---"
cat "$TMP/sched.log"

RES="$(python3 - "$TMP/.cxt/schedule.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
ok = (m.get("enabled") is False and int(m.get("remaining",1))==0 and isinstance(m.get("last"),dict) and m["last"].get("ok") is False)
print("PASS" if ok else "FAIL")
PY
)"
echo "RESULT=$RES"
rm -rf "$TMP"
[ "$RES" = "PASS" ]
