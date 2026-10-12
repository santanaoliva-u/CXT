#!/usr/bin/env python3
import json, os, random, subprocess, time

import cxtlib

DIR = os.path.expanduser("~/.cxt")
CFG = os.path.join(DIR, "schedule.json")
CMD = os.path.join(DIR, "schedule.cmd.json")
POOL = os.path.join(DIR, "pool.txt")
USED = os.path.join(DIR, "used.json")
PID = os.path.join(DIR, "scheduler.pid")
CXT = os.path.join(DIR, "bin", "cxt")
PUB = os.path.join(DIR, "clone_publish.py")
NEWS = os.path.join(DIR, "news.sh")

CITY = os.environ.get("CXT_CITY", "tu ciudad")
TAGS = os.environ.get("CXT_TAGS", "#Noticias")

TEMPLATES = [
    "Buenos días, {c}. {t}",
    "Ojo, {c}: {t}",
    "{t} — ¿Qué opinan? 🌴",
    "Esto está pasando en {c}: {t}",
    "{t}\n\nBuenas noticias para {c}. 🌴",
]
HASHTAGS = TAGS

NEGATIVE = ("accidente", "lesionad", "muert", "fallec", "asesin", "crimen", "detenid", "balac", "herid",
            "ejecut", "narco", "robo", "asalt", "desaparec", "feminicid", "violencia", "balead",
            "sangre", "cadaver", "arma", "homicid", "suicid", "droga", "secuestr", "atropell",
            "incendio", "tragedia", "deceso", "cuerpo", "extorsion", "peligro", "alerta", "protesta",
            "bloqueo", "huelga", "cierre", "crisis", "deuda", "desalojo", "clausur", "multa", "denuncia")
POSITIVE = ("inaugur", "nuevo", "mejora", "abre", "logro", "apoyo", "benefi", "cultura", "deport", "turism",
            "inversio", "empleo", "obra", "festival", "evento", "programa", "beca", "arranca", "celebra",
            "impulsa", "crece", "moderniza", "amplia", "refuerza", "suma", "gana", "exito", "record",
            "gratis", "familia", "joven", "limpia", "rehabilita", "rescata", "capacita")


def _norm(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def _negative(s):
    n = _norm(s)
    return any(k in n for k in NEGATIVE)


def _positive(s):
    n = _norm(s)
    return any(k in n for k in POSITIVE)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def defaults():
    return {"enabled": False, "interval_min": 5, "count": 1, "remaining": 0,
            "topic": "auto", "next_at": 0, "started_at": 0, "last": None,
            "sched_alive": 0}


def resolve_tab():
    try:
        return str(cxtlib.resolve_tab(CXT) or "")
    except Exception as e:
        log("resolve_tab err " + str(e))
    return ""


def gen_auto():
    used = read_json(USED, [])
    if os.path.exists(POOL):
        try:
            with open(POOL, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            for l in lines:
                if l not in used:
                    used.append(l)
                    write_json(USED, used[-500:])
                    return l
        except Exception:
            pass
    out = ""
    try:
        out = subprocess.run([NEWS, CITY, "8"], capture_output=True, text=True, timeout=60).stdout
    except Exception as e:
        log("news err " + str(e))
    cand = []
    for title in [l.strip() for l in out.splitlines() if l.strip()]:
        t = title.split(" - ")[0].strip()
        if t and t not in used and not _negative(t):
            cand.append(t)
    cand.sort(key=lambda t: 0 if _positive(t) else 1)
    if cand:
        t = cand[0]
        used.append(t)
        write_json(USED, used[-500:])
        return random.choice(TEMPLATES).format(t=t, c=CITY) + "\n\n" + HASHTAGS
    return "Buenos días, " + CITY + ". 🌴 " + HASHTAGS


def publish(text):
    path = "/tmp/cxt_auto_post.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    env = dict(os.environ)
    tab = resolve_tab()
    if tab:
        env["CXT_TAB"] = tab
    env["CXT_POST_URL"] = os.environ.get("CXT_POST_URL") or "https://www.facebook.com/"
    try:
        p = subprocess.run(["python3", "-u", PUB, path], capture_output=True, text=True, timeout=420, env=env)
        for line in p.stdout.splitlines():
            line = line.strip()
            if line:
                log("pub| " + line[:200])
        for line in reversed(p.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "sin salida"}


def apply_cmd(cfg):
    if not os.path.exists(CMD):
        return cfg
    cmd = read_json(CMD, {})
    try:
        os.remove(CMD)
    except Exception:
        pass
    for k in ("enabled", "interval_min", "count", "topic"):
        if k in cmd:
            cfg[k] = cmd[k]
    if cmd.get("enabled"):
        cfg["remaining"] = int(cfg.get("count", 1))
        cfg["next_at"] = 0
        cfg["started_at"] = int(time.time() * 1000)
        log("cmd: INICIAR interval=" + str(cfg.get("interval_min")) + " count=" + str(cfg.get("remaining")))
    elif "enabled" in cmd:
        cfg["next_at"] = 0
        cfg["remaining"] = 0
        log("cmd: DETENER")
    return cfg


def main():
    if os.path.exists(PID):
        try:
            with open(PID) as f:
                old = int(f.read().strip())
            os.kill(old, 0)
            log("ya hay un scheduler vivo pid=" + str(old))
            return
        except Exception:
            pass
    with open(PID, "w") as f:
        f.write(str(os.getpid()))
    log("scheduler arranca pid=" + str(os.getpid()))
    while True:
        try:
            cfg = read_json(CFG, defaults())
            for k, v in defaults().items():
                cfg.setdefault(k, v)
            cfg = apply_cmd(cfg)
            cfg["sched_alive"] = int(time.time() * 1000)
            if cfg["enabled"] and int(cfg.get("remaining", 0)) > 0:
                now = int(time.time() * 1000)
                if int(cfg.get("next_at", 0)) == 0:
                    cfg["next_at"] = now
                if now >= int(cfg["next_at"]):
                    if cfg.get("topic") == "auto":
                        text = gen_auto()
                    else:
                        text = str(cfg.get("topic") or "").strip() or gen_auto()
                    t0 = int(time.time() * 1000)
                    res = publish(text)
                    cfg["last"] = {"ok": bool(res.get("ok")), "text": text[:300],
                                   "at": int(time.time() * 1000), "ms": int(time.time() * 1000) - t0,
                                   "error": res.get("error") or res.get("stage") or ""}
                    log("tick ok=" + str(res.get("ok")) + " " + text[:60].replace("\n", " "))
                    cfg["remaining"] = int(cfg.get("remaining", 0)) - 1
                    if cfg["remaining"] <= 0:
                        cfg["enabled"] = False
                        cfg["next_at"] = 0
                        log("parada limpia: count agotado")
                    else:
                        cfg["next_at"] = int(time.time() * 1000) + int(float(cfg.get("interval_min", 5)) * 60000)
            write_json(CFG, cfg)
        except Exception as e:
            log("loop err " + str(e))
        time.sleep(2)


if __name__ == "__main__":
    main()
