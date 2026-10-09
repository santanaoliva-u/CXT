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
           "share_post.py", "comment.py", "clone_publish.py", "news.sh"]
EXTRA = ["cxtlib.py"]

fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


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

        for ep in ["/schedule", "/comment", "/sharegroups", "/sharepost", "/groupscan"]:
            try:
                st, body = get(ep)
                check("GET " + ep + " 200+json", st == 200 and isinstance(body, dict), body)
            except Exception as e:
                check("GET " + ep, False, e)

        out = subprocess.run([CXT, "selftest"], capture_output=True, text=True,
                             env=dict(os.environ, CXT_SERVER=BASE, CXT_TOKEN=""), timeout=20)
        check("cli `cxt selftest`", out.returncode == 0 and '"ok"' in out.stdout, out.stderr or out.stdout)
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
