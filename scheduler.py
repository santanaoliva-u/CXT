#!/usr/bin/env python3
import json, os, random, re, subprocess, time
import urllib.request

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

CITY = os.environ.get("CXT_CITY", "Playa del Carmen")
TAGS = os.environ.get("CXT_TAGS", "#PlayaDelCarmen #RivieraMaya #QuintanaRoo")
AI_LAST_ERROR = ""
EDITORIAL_LAST_ERROR = ""
EDITORIAL_META = {}

DEFAULT_EDITORIAL_INSTRUCTION = (
    "Actualidad local de Playa del Carmen: una sola noticia positiva o útil sobre "
    "tecnología, negocios, emprendimiento, cultura o comunidad. Explica qué cambió y "
    "por qué importa, en tono cercano. No incluyas cifras, fechas ni datos numéricos. "
    "Evita política, religión, finanzas, personas privadas, conflictos, clausuras, "
    "accidentes, tragedias y violencia."
)
DEFAULT_EDITORIAL_MUST_NOT = [
    "política", "politica", "político", "politico", "presidente", "presidenta",
    "gobierno", "partido", "elección", "eleccion", "senador", "diputado",
    "religión", "religion", "iglesia", "dios", "jesucristo",
    "finanzas", "dinero", "inversión", "inversion", "criptomoneda", "bitcoin",
    "violencia", "crimen", "asesinato", "homicidio", "accidente", "muerte",
    "herido", "balacera", "narco", "terceros", "datos personales",
]

HOOKS_CITY = [
    "Esto me llamó la atención de " + CITY + ":",
    "Andaba leyendo las noticias de " + CITY + " y me encontré esto:",
    "Buena noticia para " + CITY + ":",
    "Ojo con esto, " + CITY + ":",
]
HOOKS_PLAIN = [
    "Esto me llamó la atención:",
    "Andaba leyendo las noticias y me encontré esto:",
    "Buena noticia:",
    "Me topé con esto:",
]
CLOSERS = [
    "Ojalá se haga bien.",
    "Me gusta ver que la ciudad avanza.",
    "Ojalá de verdad le sirva a la gente de aquí.",
    "Ojalá se sostenga en el tiempo.",
    "Está bien, siempre que beneficie a los de acá.",
    "Un pasito más para Playa.",
]
EMOJIS = ["🌴", "🌊", "🙌", "👏", "☀️", ""]
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


# palabra clave en el titular -> hashtag relevante (0-3, nunca el bloque fijo).
_TAGMAP = [
    ("turism", "#Turismo"), ("viaj", "#Turismo"), ("hotel", "#Turismo"), ("crucer", "#Turismo"),
    ("emple", "#Empleo"), ("vacante", "#Empleo"), ("trabaj", "#Empleo"),
    ("cultur", "#Cultura"), ("hanal", "#Cultura"), ("festival", "#Cultura"), ("musica", "#Cultura"),
    ("deport", "#Deporte"), ("alberca", "#Deporte"), ("futbol", "#Deporte"),
    ("conserva", "#MedioAmbiente"), ("tiburon", "#MedioAmbiente"), ("sargazo", "#MedioAmbiente"),
    ("ambient", "#MedioAmbiente"),
    ("seguridad", "#Seguridad"), ("policia", "#Seguridad"),
    ("tren", "#TrenMaya"), ("maya", "#TrenMaya"),
    ("emprend", "#Emprendedores"), ("negocio", "#Negocios"), ("pyme", "#Negocios"),
    ("obra", "#Obras"), ("parque", "#Espacios"), ("calle", "#Obras"), ("paviment", "#Obras"),
    ("salud", "#Salud"), ("hospital", "#Salud"),
    ("educa", "#Educación"), ("beca", "#Educación"), ("escuela", "#Educación"),
    ("econom", "#Economía"), ("invers", "#Inversión"),
]


def _clean(t):
    t = (t or "").replace("''", '"').replace("´´", '"').replace("“", '"').replace("”", '"')
    t = re.sub(r"\s+", " ", t).strip()
    return t.rstrip(" .;,")


def _has_city(t):
    return CITY.lower() in _norm(t)


def _tags(t):
    tags = ["#PlayaDelCarmen"]
    n = _norm(t)
    for key, tag in _TAGMAP:
        if key in n and tag not in tags:
            tags.append(tag)
        if len(tags) >= 3:
            break
    if len(tags) == 1:
        tags.append("#RivieraMaya")
    return " ".join(tags[:3])


def _humanize(title, city_already):
    head = _clean(title)
    if not head:
        return ""
    if head[0].islower():
        head = head[0].upper() + head[1:]
    intro = random.choice(HOOKS_PLAIN if city_already else HOOKS_CITY)
    body = intro + " " + head
    if not body.endswith("."):
        body += "."
    body += " " + random.choice(CLOSERS)
    emoji = random.choice(EMOJIS)
    if emoji:
        body += " " + emoji
    return body + "\n\n" + _tags(title)


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
            "repeat": False, "topic": "auto", "text": "", "preview": None, "next_at": 0,
            "started_at": 0, "last": None, "sched_alive": 0, "editorial": {}}


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
        return _humanize(t, _has_city(t))
    return "Buenos días, " + CITY + " 🌴 Ojalá sea un gran día para todos por acá.\n\n" + HASHTAGS


def deepseek_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    path = os.path.expanduser("~/.config/opencode/opencode.jsonc")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = f.read()
    except Exception:
        return ""
    m = re.search(r'"deepseek"\s*:\s*\{.*?"apiKey"\s*:\s*"([^"]+)"', cfg, re.S)
    return m.group(1) if m else ""


DANGER_RE = re.compile(
    r"(destru(?:ir|ye|cci[oó]n)|da[nñ]ar|lastimar|matar|asesinar|acosar|acoso|"
    r"humillar|arruinar|veng(?:ar|anza)|hacer\s+da[nñ]o)",
    re.I,
)


def blocked_instruction(text):
    return bool(DANGER_RE.search(str(text or "")))


def gen_ai(instruction):
    global AI_LAST_ERROR
    AI_LAST_ERROR = ""
    instruction = str(instruction or "").strip()
    if not instruction:
        AI_LAST_ERROR = "instrucción vacía"
        log("ai err: instruccion vacia")
        return ""
    if blocked_instruction(instruction):
        AI_LAST_ERROR = "instrucción bloqueada por seguridad"
        log("ai err: instruccion bloqueada por seguridad")
        return ""
    key = deepseek_key()
    if not key:
        AI_LAST_ERROR = "falta la API key de DeepSeek"
        log("ai err: falta DEEPSEEK_API_KEY")
        return ""
    prompt = (
        "Eres redactor de Facebook para %s. Escribe una publicación breve y natural "
        "en español (2 a 5 líneas), con gancho, sin inventar datos, sin promesas "
        "médicas ni legales. No generes contenido que promueva daño, acoso, "
        "violencia o humillación. Incluye 2 a 4 hashtags relevantes. Devuelve SOLO "
        "el texto final, sin comillas ni explicación. Instrucción del usuario: %s"
        % (CITY, instruction)
    )
    body = {
        "model": os.environ.get("CXT_AI_MODEL", "deepseek-flash"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.85,
        "max_tokens": 1200,
    }
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        text = str(data["choices"][0]["message"]["content"]).strip()
        return text[:1000]
    except Exception as e:
        AI_LAST_ERROR = "IA no disponible"
        log("ai err " + str(e)[:160])
        return ""


def gen_editorial(cfg):
    global EDITORIAL_LAST_ERROR, EDITORIAL_META
    EDITORIAL_LAST_ERROR = ""
    EDITORIAL_META = {}
    instruction = str(cfg.get("text") or "").strip() or DEFAULT_EDITORIAL_INSTRUCTION
    if blocked_instruction(instruction):
        EDITORIAL_LAST_ERROR = "instrucción bloqueada por seguridad"
        log("editorial err: instruccion bloqueada por seguridad")
        return ""
    opts = cfg.get("editorial") if isinstance(cfg.get("editorial"), dict) else {}
    must_not = opts.get("must_not")
    if not isinstance(must_not, list) or not must_not:
        must_not = DEFAULT_EDITORIAL_MUST_NOT
    try:
        import editorial
    except Exception as exc:
        EDITORIAL_LAST_ERROR = "mesa editorial no disponible"
        log("editorial err import " + str(exc)[:160])
        return ""
    try:
        result = editorial.generate(
            instruction,
            mode="editorial",
            freshness_hours=int(opts.get("freshness_hours", 96) or 96),
            min_sources=int(opts.get("min_sources", 2) or 2),
            tone=str(opts.get("tone") or "cercano, humano y verificable"),
            audience=str(opts.get("audience") or "seguidores de Playa del Carmen"),
            must_not=[str(x) for x in must_not],
            max_length=int(opts.get("max_length", 700) or 700),
        )
    except Exception as exc:
        EDITORIAL_LAST_ERROR = "error generando el borrador editorial"
        log("editorial err " + str(exc)[:200])
        return ""

    if not isinstance(result, dict):
        EDITORIAL_LAST_ERROR = "respuesta editorial inválida"
        log("editorial err respuesta inválida")
        return ""

    run_id = str(result.get("run_id") or "")
    used_angle = str((result.get("draft") or {}).get("angle_id") or "")
    angle_ids = []
    for angle in result.get("angles") or []:
        if isinstance(angle, dict):
            aid = str(angle.get("id") or "")
            if aid and aid != used_angle and aid not in angle_ids:
                angle_ids.append(aid)

    attempts = [None] + angle_ids[:2]
    last_stamp = {}
    for attempt_no, angle_id in enumerate(attempts):
        if attempt_no:
            if not run_id:
                break
            try:
                result = editorial.draft_for_run(run_id, angle_id=angle_id)
            except Exception as exc:
                log("editorial retry err " + str(exc)[:160])
                continue
            if not isinstance(result, dict):
                continue
        stamp = result.get("stamp") if isinstance(result.get("stamp"), dict) else {}
        draft = result.get("draft") if isinstance(result.get("draft"), dict) else {}
        last_stamp = stamp
        EDITORIAL_META = {
            "run_id": run_id,
            "stamp_color": stamp.get("color", ""),
            "publishable": bool(stamp.get("publishable")),
            "angle_id": str(draft.get("angle_id") or angle_id or ""),
            "attempt": attempt_no + 1,
        }
        text = str(draft.get("text") or "").strip()
        if not result.get("ok") or not text:
            EDITORIAL_LAST_ERROR = str(result.get("error") or "sin borrador editorial")
            log("editorial err " + EDITORIAL_LAST_ERROR[:200])
            continue
        if EDITORIAL_META["publishable"] and EDITORIAL_META["stamp_color"] == "green":
            return text
        reasons = stamp.get("reasons") or []
        detail = "; ".join(str(x) for x in reasons[:3]) if isinstance(reasons, list) else str(reasons)
        EDITORIAL_LAST_ERROR = "sello " + (EDITORIAL_META["stamp_color"] or "desconocido")
        if detail:
            EDITORIAL_LAST_ERROR += ": " + detail
        log("editorial skip intento %d %s" % (attempt_no + 1, EDITORIAL_LAST_ERROR[:240]))

    if not EDITORIAL_LAST_ERROR:
        reasons = last_stamp.get("reasons") or []
        detail = "; ".join(str(x) for x in reasons[:3]) if isinstance(reasons, list) else str(reasons)
        EDITORIAL_LAST_ERROR = "sello no publicable" + (": " + detail if detail else "")
    return ""


def compose_text(cfg):
    topic = str(cfg.get("topic") or "auto").strip()
    if topic == "auto":
        return gen_auto()
    if topic == "fixed":
        return str(cfg.get("text") or "").strip()
    if topic == "ai":
        return gen_ai(cfg.get("text"))
    if topic == "editorial":
        return gen_editorial(cfg)
    return topic


def publish(text):
    path = "/tmp/cxt_auto_post.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    env = dict(os.environ)
    tab = resolve_tab()
    if not tab:
        try:
            tab = str(cxtlib.ensure_background_tab(CXT, "https://www.facebook.com/") or "")
            log("publish_tab ensured " + tab)
        except Exception as e:
            log("ensure_background_tab err " + str(e))
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


RETRYABLE_PUBLISH_STAGES = {"composer-not-found", "modal", "publicar"}


def publish_retryable(res):
    """Fallo de publicacion ocurrido antes de pulsar Publicar.

    Si Chrome esta cerrado o la pestana no esta lista, se puede reintentar sin
    riesgo de duplicar. Un fallo despues del clic no es reintentable.
    """
    if not isinstance(res, dict) or res.get("ok"):
        return False
    if str(res.get("stage") or "") in RETRYABLE_PUBLISH_STAGES:
        return True
    return str(res.get("error") or "").strip().lower() == "sin salida"


def apply_cmd(cfg):
    if not os.path.exists(CMD):
        return cfg
    cmd = read_json(CMD, {})
    try:
        os.remove(CMD)
    except Exception:
        pass
    # La vista previa es una consulta: no debe tocar la programación activa.
    # Por eso se resuelve antes de copiar enabled/topic/text al estado.
    if str(cmd.get("action") or "") == "preview":
        topic = str(cmd.get("topic") if "topic" in cmd else cfg.get("topic") or "auto").strip()
        value = cmd.get("text") if "text" in cmd else cfg.get("text")
        preview_cfg = {"topic": topic, "text": value}
        if "editorial" in cmd:
            preview_cfg["editorial"] = cmd["editorial"]
        text = compose_text(preview_cfg)
        if topic == "editorial":
            meta = dict(EDITORIAL_META)
            error = "" if text else (EDITORIAL_LAST_ERROR or "sin texto editorial publicable")
        else:
            meta = {}
            error = "" if text else (AI_LAST_ERROR or "sin texto o IA no disponible")
        cfg["preview"] = {"ok": bool(text), "text": text, "topic": topic,
                          "source": {"topic": topic, "text": str(value or "")},
                          "at": int(time.time() * 1000),
                          "error": error, "meta": meta}
        log("preview ok=%s %s" % (bool(text), text[:60].replace("\n", " ")))
        return cfg
    # Publicación inmediata de una vista previa ya aprobada: no toca enabled,
    # repeat, count, remaining, topic, text ni preview. Se ejecuta como un
    # pendiente de un solo uso para no detener el autopiloto.
    if str(cmd.get("action") or "") == "publish_now":
        text = str(cmd.get("text") or "").strip()
        if not text:
            log("publish_now sin texto")
            return cfg
        meta = cmd.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        cfg["pending"] = {"text": text, "topic": str(cmd.get("topic") or "fixed"),
                          "meta": meta, "at": int(time.time() * 1000)}
        log("cmd: PUBLICAR AHORA len=" + str(len(text)))
        return cfg
    for k in ("enabled", "interval_min", "count", "repeat", "topic", "text", "editorial"):
        if k in cmd:
            cfg[k] = cmd[k]
    # Un comando nuevo sin "repeat" es one-shot. Evita heredar el repeat
    # editorial y quedar publicando en bucle.
    if "enabled" in cmd and "repeat" not in cmd:
        cfg["repeat"] = False
    if cmd.get("enabled"):
        cfg["preview"] = None
        cfg["remaining"] = int(cfg.get("count", 1))
        cfg["next_at"] = 0
        cfg["started_at"] = int(time.time() * 1000)
        log("cmd: INICIAR interval=" + str(cfg.get("interval_min")) + " count=" + str(cfg.get("remaining")))
    elif "enabled" in cmd:
        cfg["next_at"] = 0
        cfg["remaining"] = 0
        log("cmd: DETENER")
    return cfg


def publish_pending(cfg):
    pending = cfg.get("pending")
    if not isinstance(pending, dict):
        return cfg, None
    text = str(pending.get("text") or "").strip()
    topic = str(pending.get("topic") or "fixed")
    meta = pending.get("meta") if isinstance(pending.get("meta"), dict) else {}
    cfg.pop("pending", None)
    if not text:
        log("publish_now sin texto: omitido")
        return cfg, None
    t0 = int(time.time() * 1000)
    res = publish(text)
    last = {"ok": bool(res.get("ok")), "text": text[:300],
            "at": int(time.time() * 1000), "ms": int(time.time() * 1000) - t0,
            "error": res.get("error") or res.get("stage") or ""}
    if meta:
        last["run_id"] = str(meta.get("run_id") or "")
        last["stamp_color"] = str(meta.get("stamp_color") or "")
    elif topic == "editorial":
        last["run_id"] = EDITORIAL_META.get("run_id", "")
        last["stamp_color"] = EDITORIAL_META.get("stamp_color", "")
    cfg["last"] = last
    log("publish_now ok=" + str(res.get("ok")) + " " + text[:60].replace("\n", " "))
    # Si el próximo post regular ya venció, moverlo para no publicar dos veces
    # seguidas. La programación (enabled/repeat/remaining) queda intacta.
    if cfg.get("enabled"):
        try:
            now = int(time.time() * 1000)
            if int(cfg.get("next_at") or 0) <= now:
                cfg["next_at"] = now + int(float(cfg.get("interval_min", 5)) * 60000)
                log("publish_now: proximo regular movido para evitar doble post")
        except Exception:
            pass
    return cfg, res


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
            cfg, _ = publish_pending(cfg)
            cfg["sched_alive"] = int(time.time() * 1000)
            repeat = bool(cfg.get("repeat"))
            if cfg["enabled"] and (repeat or int(cfg.get("remaining", 0)) > 0):
                now = int(time.time() * 1000)
                if int(cfg.get("next_at", 0)) == 0:
                    cfg["next_at"] = now
                if now >= int(cfg["next_at"]):
                    text = compose_text(cfg)
                    if not text:
                        topic = str(cfg.get("topic") or "")
                        if topic == "ai":
                            err = AI_LAST_ERROR
                        elif topic == "editorial":
                            err = EDITORIAL_LAST_ERROR
                        else:
                            err = ""
                        cfg["last"] = {"ok": False, "text": "", "at": now, "ms": 0,
                                       "error": err or "sin texto: revisa el tema o la instrucción"}
                        if repeat:
                            cfg["next_at"] = int(time.time() * 1000) + int(float(cfg.get("interval_min", 5)) * 60000)
                            log("tick sin texto: reintento programado")
                        else:
                            cfg["enabled"] = False
                            cfg["remaining"] = 0
                            cfg["next_at"] = 0
                            log("tick sin texto: no se publica")
                    else:
                        t0 = int(time.time() * 1000)
                        res = publish(text)
                        last = {"ok": bool(res.get("ok")), "text": text[:300],
                                "at": int(time.time() * 1000), "ms": int(time.time() * 1000) - t0,
                                "error": res.get("error") or res.get("stage") or ""}
                        if str(cfg.get("topic") or "") == "editorial":
                            last["run_id"] = EDITORIAL_META.get("run_id", "")
                            last["stamp_color"] = EDITORIAL_META.get("stamp_color", "")
                        cfg["last"] = last
                        log("tick ok=" + str(res.get("ok")) + " " + text[:60].replace("\n", " "))
                        if not res.get("ok"):
                            if repeat and publish_retryable(res):
                                cfg["next_at"] = int(time.time() * 1000) + 15 * 60000
                                log("fallo de publicación reintentable; reintento en 15 min")
                            else:
                                cfg["enabled"] = False
                                cfg["remaining"] = 0
                                cfg["next_at"] = 0
                                log("parada por fallo de publicación")
                        elif repeat:
                            cfg["next_at"] = int(time.time() * 1000) + int(float(cfg.get("interval_min", 5)) * 60000)
                            log("tick repetirá en " + str(cfg.get("interval_min")) + " min")
                        else:
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
