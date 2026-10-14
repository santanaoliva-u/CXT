#!/usr/bin/env python3
"""autopost.py - worker residente que consume la cola del daemon y publica
cada item en grupos invocando share_post.py como subproceso (CLI intacto).

Contrato externo estable:

  cola     : autoqueue.json        (la escribe SOLO el daemon; este worker es
                                    el unico lector: NUNCA escribe la cola)
  intentos : autopost_attempts.json (archivo PROPIEDAD DEL WORKER, clave =
                                    permalink normalizado; a-lo-mas-una-vez)
  control  : autopost.cmd.json     (se consume y se borra)
  estado   : autopost_status.json   pid: autopost.pid
  CLI      : python3 autopost.py --cmd <cmd-file>

Respeta el kill-switch (/health -> enabled), los intervalos con jitter, los
topes de grupos y diario, la a-lo-mas-una-vez (persistida en el archivo de
intentos, no en la cola) y no roba el foco al usuario.
"""
import argparse
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.request

import cxtlib

DIR = cxtlib.resolve_dir()
QUEUE = os.path.join(DIR, "autoqueue.json")
CMD = os.path.join(DIR, "autopost.cmd.json")
STATUS = os.path.join(DIR, "autopost_status.json")
PID = os.path.join(DIR, "autopost.pid")
ATTEMPTS = os.path.join(DIR, "autopost_attempts.json")
SHARE = cxtlib.resolve_script("share_post.py")
SHARE_CMD = os.path.join(DIR, "share_post.cmd.json")
SHARE_STATUS = os.path.join(DIR, "share_post_status.json")
CXT = cxtlib.resolve_cxt()
PORT = os.environ.get("CXT_PORT") or "8799"

INTERVAL_MIN = float(os.environ.get("CXT_AUTO_MIN", 10))
INTERVAL_REC = float(os.environ.get("CXT_AUTO_REC", 15))
INTERVAL_MAX = float(os.environ.get("CXT_AUTO_MAX", 60))
MAXGROUPS = int(os.environ.get("CXT_AUTO_MAXGROUPS", 10))
MAXDAY = int(os.environ.get("CXT_AUTO_MAXDAY", 20))
DEPTH = 200
EXPIRE_MS = 7 * 24 * 3600 * 1000
JITTER = 0.30
LOOP_SLEEP = float(os.environ.get("CXT_AUTO_LOOP", 2))
# Cota dura del share: el plan fija 420 s (7 min); un share colgado no puede
# bloquear el bucle de 2 s ni el sondeo del kill-switch mas de eso.
SHARE_TIMEOUT = int(os.environ.get("CXT_AUTO_SHARE_TIMEOUT", "420"))

CHECKPOINT_PAT = ("/checkpoint", "/recover")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------ logica pura

def clamp_interval(mins, lo=INTERVAL_MIN, hi=INTERVAL_MAX):
    try:
        m = float(mins)
    except (TypeError, ValueError):
        m = INTERVAL_REC
    return max(lo, min(hi, m))


def jitter_ms(base_ms, rng=None):
    r = (rng or random).uniform(-JITTER, JITTER)
    return int(base_ms * (1.0 + r))


def interval_ms(mins, rng=None):
    return jitter_ms(clamp_interval(mins) * 60000, rng)


def first_next_at(now_ms):
    return int(now_ms) + int(INTERVAL_REC * 60000)


def next_at_for(now_ms, mins, rng=None):
    return int(now_ms) + interval_ms(mins, rng)


def startup_next_at(now_ms, persisted_ms=0):
    """RE4: al arrancar, si el next_at persistido ya vencio se reprograma a
    ahora+REC (la primera publicacion del worker siempre espera >= REC, tambien
    tras un reinicio). Un next_at futuro se respeta (espera conservadora)."""
    now_ms = int(now_ms)
    persisted = int(persisted_ms or 0)
    if persisted > now_ms:
        return persisted
    return first_next_at(now_ms)


def roll_day(st, today):
    if st.get("day") != today:
        st["day"] = today
        st["done_today"] = 0
    return st


def under_daily_cap(done_today, cap=MAXDAY):
    return int(done_today or 0) < int(cap)


def group_limit(item_limit, cap=MAXGROUPS):
    n = int(item_limit or 0)
    if n <= 0:
        n = cap
    return min(n, cap)


def permalink_key(url):
    u = (url or "").strip().split("#")[0].split("?")[0].rstrip("/")
    return cxtlib.norm(u)


def item_key(item):
    """Clave estable del item para el registro de intentos.

    Permalink si lo hay; si no, el `expect`; y como ultimo recurso el texto."""
    key = permalink_key(item.get("url"))
    if key:
        return key
    exp = cxtlib.norm(item.get("expect") or "")
    if exp:
        return "expect:" + exp
    return "text:" + cxtlib.norm(item.get("text") or "")


def dedupe_items(items):
    seen = set()
    out = []
    for it in items:
        key = permalink_key(it.get("url"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(it)
    return out


def trim_depth(items, cap=DEPTH):
    return list(items)[:cap]


def queue_accepts(items, cap=DEPTH):
    return len(items) < cap


def is_expired(item, now_ms, ttl_ms=EXPIRE_MS):
    try:
        at = float(item.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return at > 0 and (int(now_ms) - at) > ttl_ms


def load_attempts():
    """Registro de intentos (propiedad del worker); nunca falla."""
    a = cxtlib.load_json(ATTEMPTS, {})
    return a if isinstance(a, dict) else {}


def save_attempts(a):
    try:
        cxtlib.write_json_atomic(ATTEMPTS, a)
    except Exception as e:
        log("save_attempts err " + str(e))


def attempt_mark(key, state, error=""):
    """Marca a-lo-mas-una-vez en el archivo de intentos del worker.

    Se llama con "publicando" ANTES de invocar share_post y con "hecho"/"error"
    DESPUES. Nunca toca autoqueue.json (el daemon sigue siendo su dueño)."""
    if not key:
        return
    a = load_attempts()
    a[key] = {"state": state, "error": (error or "")[:200],
              "at": int(time.time() * 1000)}
    save_attempts(a)


def recover_states(items, attempts=None):
    """Reconciliacion al leer la cola (sin escribirla).

    - Un estado persistido "publicando" ya se intento -> "hecho".
    - Si el registro de intentos tiene la clave, ese item ya se intento:
      "publicando"/"hecho" -> hecho, "error" -> error. Nunca se re-comparte."""
    if attempts is None:
        attempts = load_attempts()
    for it in items:
        if (it.get("state") or "pendiente") == "publicando":
            it["state"] = "hecho"
            it["error"] = it.get("error") or "recuperado tras reinicio"
        a = attempts.get(item_key(it)) if attempts else None
        if not a:
            continue
        stt = a.get("state")
        if stt == "publicando":
            it["state"] = "hecho"
            it["error"] = it.get("error") or "intento interrumpido (at-most-once)"
        elif stt == "hecho":
            it["state"] = "hecho"
        elif stt == "error":
            it["state"] = "error"
            it["error"] = it.get("error") or a.get("error") or "error"
    return items


def is_checkpoint_url(url):
    u = (url or "").lower()
    return any(p in u for p in CHECKPOINT_PAT)


def pause_for_checkpoint(st):
    st["pausado"] = True
    st["error"] = "cuenta restringida"
    return st


def apply_checkpoint(st, url):
    if is_checkpoint_url(url):
        pause_for_checkpoint(st)
        return True
    return False


def set_waiting(st, job):
    """Otro job usa la pestaña: el estado visible marca esperando:<job>."""
    st["current"] = "esperando:" + str(job)
    st["error"] = ""
    return st


def clear_waiting(st):
    if str(st.get("current") or "").startswith("esperando:"):
        st["current"] = None
    return st


def bad_item(item):
    # Un item sin url NI expect no puede identificar una publicacion: compartir
    # con url="" haria que share_post compartiera el post visible (el equivocado).
    if not (item.get("url") or item.get("expect")):
        return "item sin url ni expect"
    return ""


def item_outcome(share_st):
    if not isinstance(share_st, dict) or not share_st:
        return "error", "sin estado de share_post"
    err = share_st.get("error") or ""
    if err:
        return "error", str(err)[:200]
    results = share_st.get("results") or []
    if not results:
        return "error", "share_post sin resultados"
    failed = [r for r in results if not r.get("ok")]
    if failed:
        return "error", "fallo en %d grupo(s)" % len(failed)
    return "hecho", ""


def count_pending(items):
    return len([it for it in items if (it.get("state") or "pendiente") == "pendiente"])


def pick_pending(items, now_ms):
    for it in items:
        if (it.get("state") or "pendiente") != "pendiente":
            continue
        if is_expired(it, now_ms):
            it["state"] = "error"
            it["error"] = "expirado"
            continue
        return it
    return None


# ------------------------------------------------------------ IO atomica

def read_queue():
    """Lee la cola (SOLO lectura; el daemon es su unico escritor) y aplica la
    reconciliacion a-lo-mas-una-vez en memoria."""
    q = cxtlib.load_json(QUEUE, {})
    if not isinstance(q, dict):
        q = {}
    q.setdefault("paused", False)
    raw = q.get("items")
    if not isinstance(raw, list):
        raw = []
    q["items"] = trim_depth(dedupe_items([it for it in raw if isinstance(it, dict)]))
    recover_states(q["items"])
    return q


def default_status():
    return {"running": False, "pausado": False, "pending": 0, "current": None,
            "next_at": 0, "done": 0, "results": [], "error": "",
            "started_ms": 0, "finished_ms": 0,
            "dom": {"tagged": 0, "injected": 0, "errors": 0},
            "day": "", "done_today": 0}


def load_status():
    st = cxtlib.load_json(STATUS, {})
    if not isinstance(st, dict):
        st = {}
    for k, v in default_status().items():
        st.setdefault(k, v)
    if not isinstance(st.get("dom"), dict):
        st["dom"] = {"tagged": 0, "injected": 0, "errors": 0}
    else:
        for k, v in default_status()["dom"].items():
            st["dom"].setdefault(k, v)
    if not isinstance(st.get("results"), list):
        st["results"] = []
    return st


def write_status(st):
    cxtlib.write_status(STATUS, st)


def consume_cmd(path):
    if not os.path.exists(path):
        return {}
    cmd = cxtlib.load_json(path, {})
    try:
        os.remove(path)
    except OSError:
        pass
    return cmd if isinstance(cmd, dict) else {}


def _token():
    tok = os.environ.get("CXT_TOKEN")
    if tok:
        return tok.strip()
    try:
        with open(os.path.join(DIR, "token"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def health():
    url = "http://127.0.0.1:%s/health" % PORT
    tok = _token()
    if tok:
        url += "?token=" + tok
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _dedicated_tab():
    try:
        return cxtlib.resolve_tab(CXT)
    except Exception as e:
        log("resolve_tab err " + str(e))
        return None


def _tab_list():
    try:
        r = cxtlib.call(CXT, "tabs", {})
    except Exception:
        r = None
    tabs = r if isinstance(r, list) else (r or {}).get("tabs") or []
    return tabs if isinstance(tabs, list) else []


def _tab_urls():
    return [t.get("url") for t in _tab_list()
            if isinstance(t, dict) and t.get("url")]


def active_tabs():
    """Ids de las pestañas que el USUARIO tiene activas ahora mismo (una por
    ventana). Se captura antes del share para restaurarla luego."""
    return [t.get("id") for t in _tab_list()
            if isinstance(t, dict) and t.get("active") and t.get("id") is not None]


def restore_tabs(ids):
    """Reactiva las pestañas que el usuario tenia activas. NO roba el foco:
    deshace el tab.activate que share_post hace sobre su pestaña dedicada."""
    for tid in ids or []:
        try:
            cxtlib.call(CXT, "tab.activate", {"tabId": tid})
        except Exception:
            pass


JS_WARN = ("(()=>{for(const x of document.querySelectorAll('[role=dialog]')){"
           "const t=(x.innerText||'').toLowerCase();"
           "if(/checkpoint|restring|suspend|bloquead|inhabilit/.test(t))return true;}"
           "return false;})()")


def warning_dialog(tab):
    if tab is None:
        return False
    try:
        return bool(cxtlib.cdp(CXT, tab, JS_WARN))
    except Exception:
        return False


def checkpoint_now(tab):
    # Devuelve la url del tab que dispara el checkpoint ('' si no hay).
    for u in _tab_urls():
        if is_checkpoint_url(u):
            return u
    if warning_dialog(tab):
        return "dialog"
    return ""


def run_share(item):
    cfg = {"url": item.get("url") or "", "expect": item.get("expect") or "",
           "text": item.get("text") or "", "dry": bool(item.get("dry", False)),
           "delay": 8, "limit": group_limit(item.get("limit"))}
    if item.get("filter"):
        cfg["filter"] = item["filter"]
    cxtlib.write_json_atomic(SHARE_CMD, cfg)
    env = dict(os.environ)
    tab = _dedicated_tab()
    if tab is not None:
        env["CXT_TAB"] = str(tab)

    # Estatus rancio: borrar el status anterior ANTES de correr share_post, si
    # no un share que muere dejaria el ok:true de la corrida previa y lo
    # contariamos como "hecho".
    try:
        os.remove(SHARE_STATUS)
    except OSError:
        pass
    t0 = time.time()

    # Foco: share_post.py -> cxtlib.ensure_tab activa su pestaña de Facebook,
    # moviendo el foco del usuario. Guardamos las pestañas activas y las
    # restauramos pase lo que pase (timeout, excepcion o exito).
    prev_active = active_tabs()
    try:
        subprocess.run(["python3", "-u", SHARE, "--cmd", SHARE_CMD],
                       capture_output=True, text=True,
                       timeout=SHARE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout tras %ss" % SHARE_TIMEOUT}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        restore_tabs(prev_active)

    try:
        fresh = os.path.getmtime(SHARE_STATUS) >= t0 - 2
    except OSError:
        return {"ok": False, "error": "share_post no dejo estado"}
    if not fresh:
        return {"ok": False, "error": "share_post no actualizo su estado"}
    share_st = cxtlib.load_json(SHARE_STATUS, {})
    if not share_st:
        return {"ok": False, "error": "share_post sin estado"}
    return share_st


# ------------------------------------------------------------ bucle

def _pid_alive():
    try:
        with open(PID, encoding="utf-8") as f:
            old = int(f.read().strip())
    except Exception:
        return False
    try:
        os.kill(old, 0)
        return True
    except OSError:
        return False


def _write_pid():
    with open(PID, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _remove_pid():
    try:
        os.remove(PID)
    except OSError:
        pass


def _on_term(*_):
    _remove_pid()
    raise SystemExit(0)


def _label(item):
    return (item.get("url") or item.get("expect") or item.get("text") or "")[:80]


def _refresh_pending(st, items=None):
    if items is None:
        items = read_queue().get("items") or []
    st["pending"] = count_pending(items)


def _publish(st, q, items, item, now):
    # A-lo-mas-una-vez: se marca "publicando" en el registro de intentos del
    # worker ANTES de invocar share. NO se escribe autoqueue.json.
    # La a-lo-mas-una-vez solo aplica a shares reales: un dry que marcara
    # "hecho" bloquearia el share real posterior del mismo permalink.
    dry = bool(item.get("dry", False))
    key = item_key(item)
    if not dry:
        attempt_mark(key, "publicando")
    item["state"] = "publicando"
    item["error"] = ""
    item["at"] = item.get("at") or now
    st["dom"]["tagged"] = int(st["dom"].get("tagged", 0)) + 1
    st["current"] = _label(item)
    st["error"] = ""
    write_status(st)

    bad = bad_item(item)
    if bad:
        res = {"ok": False, "error": bad}
    else:
        try:
            raw = run_share(item)
            st["dom"]["injected"] = int(st["dom"].get("injected", 0)) + 1
            state, err = item_outcome(raw)
            res = {"ok": state == "hecho", "error": err}
        except Exception as e:
            res = {"ok": False, "error": str(e)[:200]}

    item["state"] = "hecho" if res.get("ok") else "error"
    item["error"] = res.get("error") or ""
    item["done_ms"] = int(time.time() * 1000)
    if not dry:
        attempt_mark(key, item["state"], item["error"])

    st["results"] = (st.get("results") or [])[-99:] + [{
        "url": item.get("url") or "", "state": item["state"],
        "ok": bool(res.get("ok")), "error": item["error"],
        "at": int(time.time() * 1000)}]
    st["done"] = int(st.get("done", 0)) + 1
    if res.get("ok"):
        # Un dry-run no es una publicacion real: aparece en results/done pero NO
        # consume el tope diario anti-ban (done_today cuenta solo exitos reales).
        if not dry:
            st["done_today"] = int(st.get("done_today", 0)) + 1
    else:
        st["dom"]["errors"] = int(st["dom"].get("errors", 0)) + 1
    st["current"] = None
    interval = clamp_interval(q.get("interval_min") or st.get("interval_min") or INTERVAL_REC)
    st["interval_min"] = interval
    st["next_at"] = next_at_for(int(time.time() * 1000), interval)
    st["pending"] = count_pending(items)
    log("item %s ok=%s %s" % (item["state"], res.get("ok"), _label(item)))
    write_status(st)


def _tick(st, cmd_path):
    cmd = consume_cmd(cmd_path)
    if cmd.get("stop"):
        log("stop (cmd)")
        return "stop"
    if cmd.get("action") == "pause":
        st["pausado"] = bool(cmd.get("value"))
        log("cmd pause=%s" % st["pausado"])

    h = health()
    if h is None:
        st["pausado"] = True
        st["current"] = None
        st["error"] = "daemon no responde"
        _refresh_pending(st)
        write_status(st)
        return
    if not h.get("enabled"):
        st["pausado"] = True
        st["current"] = None
        log("pausado (kill switch)")
        _refresh_pending(st)
        write_status(st)
        return
    if st.get("error") == "daemon no responde":
        st["error"] = ""

    job = "share" if h.get("share_alive") else ("share_post" if h.get("share_post_alive") else "")
    tab = _dedicated_tab()
    cp = checkpoint_now(tab)
    if cp:
        if cp == "dialog":
            pause_for_checkpoint(st)
        else:
            apply_checkpoint(st, cp)
        st["current"] = None
        log("pausado: cuenta restringida")
        _refresh_pending(st)
        write_status(st)
        return
    if job:
        set_waiting(st, job)
        _refresh_pending(st)
        write_status(st)
        return
    clear_waiting(st)
    if st.get("pausado"):
        st["current"] = None
        _refresh_pending(st)
        write_status(st)
        return

    q = read_queue()
    items = q.get("items") or []
    now = int(time.time() * 1000)
    roll_day(st, time.strftime("%Y-%m-%d"))
    _refresh_pending(st, items)
    if not under_daily_cap(st.get("done_today", 0)):
        st["error"] = "tope diario alcanzado"
        write_status(st)
        return
    if st.get("error") == "tope diario alcanzado":
        st["error"] = ""
    if now < int(st.get("next_at", 0) or 0):
        write_status(st)
        return
    item = pick_pending(items, now)
    if item is None:
        write_status(st)
        return
    _publish(st, q, items, item, now)


def _loop(st, cmd_path):
    while True:
        try:
            if _tick(st, cmd_path) == "stop":
                return
        except Exception as e:
            st["error"] = str(e)[:200]
            log("loop err " + str(e))
            write_status(st)
        time.sleep(LOOP_SLEEP)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", default="")
    a = ap.parse_args(argv)
    cmd_path = os.path.abspath(a.cmd) if a.cmd else CMD

    if _pid_alive():
        log("ya hay un autopost vivo pid=%s" % PID)
        return
    _write_pid()
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    st = load_status()
    st["running"] = True
    st["pausado"] = bool(read_queue().get("paused"))
    st["started_ms"] = int(time.time() * 1000)
    st["finished_ms"] = 0
    st["current"] = None
    st["error"] = ""
    now = int(time.time() * 1000)
    prev_next = int(st.get("next_at", 0) or 0)
    st["next_at"] = startup_next_at(now, prev_next)
    if prev_next and st["next_at"] != prev_next:
        log("RE4: next_at vencido -> reprogramo a ahora+REC")
    q = read_queue()
    _refresh_pending(st, q.get("items") or [])
    write_status(st)
    log("autopost arranca pid=%d" % os.getpid())
    try:
        _loop(st, cmd_path)
    finally:
        st["running"] = False
        st["current"] = None
        st["finished_ms"] = int(time.time() * 1000)
        write_status(st)
        _remove_pid()


if __name__ == "__main__":
    main()
