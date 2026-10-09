import os, sys, json, tempfile, threading, time

HOME = tempfile.mkdtemp()
os.environ["HOME"] = HOME
os.makedirs(os.path.join(HOME, ".cxt"), exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts"))
import scheduler as S  # noqa: E402

S.publish = lambda text: {"ok": True}
S.resolve_tab = lambda: "1"

with open(S.CFG, "w") as f:
    json.dump({"enabled": True, "interval_min": 0.02, "count": 1, "remaining": 1,
               "topic": "test fijo", "next_at": 0, "started_at": 0, "last": None,
               "sched_alive": 0}, f)

threading.Thread(target=S.main, daemon=True).start()
time.sleep(3)

m = json.load(open(S.CFG))
ok = (m.get("enabled") is False and int(m.get("remaining", 1)) == 0
      and isinstance(m.get("last"), dict) and m["last"].get("ok") is True)
print(json.dumps(m))
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
