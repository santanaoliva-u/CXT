#!/usr/bin/env python3
"""test_stack.py — verificacion determinista del stack CXT SIN Facebook.

Arranca un cxtd aislado (CXT_DIR temporal, CXT_NO_SCHED=1) y comprueba /health,
/selftest y los endpoints de jobs. No toca la sesion real ni publica nada.
Exit 0 = PASS.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CXT = os.path.join(ROOT, "bin", "cxt")
CXT_BIN = os.path.join(ROOT, "bin", "cxtd")
PORT = os.environ.get("CXT_TEST_PORT", "8796")
BASE = "http://127.0.0.1:" + PORT

SCRIPTS = ["scheduler.py", "groups_scan.py", "groups_share.py",
           "share_post.py", "comment.py", "clone_publish.py", "news.sh", "autopost.py"]
EXTRA = ["cxtlib.py"]

fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def read_queue(tmp):
    with open(os.path.join(tmp, "autoqueue.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    if not (os.path.exists(CXT_BIN) and os.path.exists(CXT)):
        print("faltan binarios; corre ./build.sh")
        return 2
    tmp = tempfile.mkdtemp(prefix="cxt_stack_")
    for s in SCRIPTS + EXTRA:
        src = os.path.join(ROOT, s)
        if not os.path.exists(src):
            src = os.path.join(ROOT, "scripts", s)
        if os.path.exists(src):
            os.symlink(src, os.path.join(tmp, s))
    with open(os.path.join(tmp, "groups.json"), "w", encoding="utf-8") as f:
        json.dump({"count": 3, "categories": {"playa": 3}, "scanned_at": "test", "groups": []}, f)

    env = dict(os.environ, CXT_PORT=PORT, CXT_DIR=tmp, CXT_TOKEN="", CXT_NO_SCHED="1")
    log = open(os.path.join(tmp, "cxtd.log"), "w")
    proc = subprocess.Popen([CXT_BIN], env=env, stdout=log, stderr=log, preexec_fn=os.setsid)
    try:
        up = False
        for _ in range(30):
            try:
                st, _ = get("/health")
                if st == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.3)
        check("daemon arranca (/health)", up)
        if not up:
            return 1

        st, h = get("/health")
        check("health.ok", h.get("ok") is True, h)
        check("health.scripts_ok", h.get("scripts_ok") is True, h)

        st, s = get("/selftest")
        check("selftest.ok", s.get("ok") is True, s)
        check("selftest.scripts todos presentes",
              all(s.get("scripts", {}).values()) and len(s.get("scripts", {})) == len(SCRIPTS), s.get("scripts"))
        check("selftest.groups_count", s.get("groups_count") == 3, s.get("groups_count"))

        for ep in ["/schedule", "/comment", "/sharegroups", "/sharepost", "/groupscan", "/autopost"]:
            try:
                st, body = get(ep)
                check("GET " + ep + " 200+json", st == 200 and isinstance(body, dict), body)
            except Exception as e:
                check("GET " + ep, False, e)

        out = subprocess.run([CXT, "selftest"], capture_output=True, text=True,
                             env=dict(os.environ, CXT_SERVER=BASE, CXT_TOKEN=""), timeout=20)
        check("cli `cxt selftest`", out.returncode == 0 and '"ok"' in out.stdout, out.stderr or out.stdout)

        # --- /autopost: los defaults de nivel superior deben fundirse en cada item ---
        # Pausamos la cola ANTES de encolar: aunque los items sean dry, deja al
        # worker residente (aislado, sin navegador) sin poder procesar nada.
        post("/autopost", {"action": "pause", "value": True})
        post("/autopost", {"action": "clear"})
        st, r = post("/autopost", {
            "items": [{"url": "https://www.facebook.com/groups/QA_TEST/permalink/1/",
                       "expect": "qa"}],
            "filter": "playa",
            "limit": 1,
            "text": "",
            "dry": True,
            "interval_min": 15,
        })
        check("POST /autopost encola item y responde ok/added",
              st == 200 and r.get("ok") is True and r.get("added") == 1, r)
        q = read_queue(tmp)
        it = (q.get("items") or [{}])[0]
        check("autoqueue item hereda dry=true del top-level", it.get("dry") is True, it)
        check("autoqueue item hereda filter=playa", it.get("filter") == "playa", it)
        check("autoqueue item conserva url/expect",
              str(it.get("url", "")).endswith("/permalink/1/") and it.get("expect") == "qa", it)

        # Caso negativo: un dry=false explicito por-item DEBE ganar al top-level true.
        post("/autopost", {"action": "clear"})
        st, r = post("/autopost", {
            "items": [{"url": "https://www.facebook.com/groups/QA_TEST/permalink/2/",
                       "expect": "qa", "dry": False}],
            "dry": True,
            "filter": "playa",
        })
        check("POST /autopost encola item explicito", st == 200 and r.get("added") == 1, r)
        it2 = (read_queue(tmp).get("items") or [{}])[0]
        check("item dry=false explicito gana al top-level dry=true", it2.get("dry") is False, it2)
        check("item explicito igual hereda filter=playa", it2.get("filter") == "playa", it2)

        # --- /autopost: acciones nuevas (interval / next / retry / del) y /log clear ---
        post("/autopost", {"action": "clear"})
        st, r = post("/autopost", {
            "items": [{"url": "https://www.facebook.com/groups/QA_TEST/permalink/9/", "expect": "qa"}],
            "dry": True})
        check("POST /autopost encola para probar acciones", st == 200 and r.get("added") == 1, r)

        st, r = post("/autopost", {"action": "interval", "value": 20})
        check("POST /autopost interval -> ok", st == 200 and r.get("ok") is True, r)
        check("autoqueue interval_override=20", read_queue(tmp).get("interval_override") == 20,
              read_queue(tmp).get("interval_override"))
        st, g = get("/autopost")
        check("GET /autopost expone interval_override=20", g.get("interval_override") == 20,
              g.get("interval_override"))
        post("/autopost", {"action": "interval", "value": 0})
        check("interval 0 limpia el override",
              read_queue(tmp).get("interval_override") in (None, 0), read_queue(tmp).get("interval_override"))

        st, r = post("/autopost", {"action": "next"})
        check("POST /autopost next -> ok", st == 200 and r.get("ok") is True, r)

        st, r = post("/autopost", {"action": "retry",
                                   "url": "https://www.facebook.com/groups/QA_TEST/permalink/9/"})
        check("POST /autopost retry -> ok", st == 200 and r.get("ok") is True, r)
        check("retry rearma el item (state pendiente)",
              any(i.get("state") == "pendiente" for i in (read_queue(tmp).get("items") or [])),
              read_queue(tmp).get("items"))

        st, g = get("/autopost")
        check("GET /autopost trae la cola (items)", isinstance(g.get("items"), list) and len(g.get("items")) == 1, g.get("items"))

        st, r = post("/autopost", {"action": "del", "index": 0})
        check("POST /autopost del -> ok/removed", st == 200 and r.get("ok") is True and r.get("removed") is True, r)
        check("del deja la cola vacia", len(read_queue(tmp).get("items") or []) == 0, read_queue(tmp).get("items"))

        st, r = post("/log", {"clear": True, "job": "autopost"})
        check("POST /log clear -> ok/cleared", st == 200 and r.get("ok") is True and "autopost" in (r.get("cleared") or []), r)

        # Limpieza: cola vacia y despausada para no dejar estado tras el test.
        post("/autopost", {"action": "clear"})
        post("/autopost", {"action": "pause", "value": False})
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nRESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
