import json, os, re, shutil, subprocess, sys, time, unicodedata

import cxtlib

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = os.path.join(HERE, "bin", "cxt")
OUT = os.path.join(HERE, "groups.json")
URL = "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added"


def active_tab_id(tabs):
    """Id de la pestana activa dentro de una lista `tabs`; None si no hay. Pura."""
    for t in tabs or []:
        if isinstance(t, dict) and t.get("active"):
            return t.get("id")
    return None


def env_tab(value):
    """CXT_TAB como entero si el harness lo fijo; None si falta o no es entero. Pura."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def tab_plan(env_value):
    """Modo de pestana, sin IO: ("honor", id) o ("own", None). Pura.

    Solo un CXT_TAB entero (harness existente) reutiliza una pestana ajena. En
    cualquier otro caso el escaneo abre su PROPIA pestana para no interferir con
    la automatizacion de publicacion."""
    tab = env_tab(env_value)
    if tab is None:
        return ("own", None)
    return ("honor", tab)

# El op `cdp` tiene un tope duro de 25s (extension/background.js OP_TIMEOUT_MS).
# Por eso el barrido entero vive DENTRO de una sola evaluacion JS que se
# auto-desplaza y se autolimita a CALL_BUDGET_MS (18s): deja ~7s de margen para
# el transporte. Si no termina, devuelve done:false y el driver puede pedir otra
# llamada (hasta MAX_CALLS) reutilizando la posicion de scroll de la pagina.
# Ademas de scroll, pulsa los toggles VISIBLES "Ver mas" (solo expanden la
# lista): sin ellos Facebook deja la pagina en ~32 grupos y no carga mas.
CALL_BUDGET_MS = 18000
MAX_CALLS = 3
MAX_CLICKS = 8          # tope duro de clics "Ver mas" en TODO el escaneo
MAX_ROUNDS = 3          # rondas de expansion por llamada
CLICK_PAUSE_MS = 140    # pausa entre clics para no disparar todo de golpe
ROUND_PAUSE_MS = 560    # deja renderizar la lista antes de releer los anchors

SCAN_JS_TMPL = r"""
(async () => {
  const BUDGET = __BUDGET__;
  const MAXCLICKS = __MAXCLICKS__;
  const MAXROUNDS = __MAXROUNDS__;
  const CLICK_PAUSE = __CLICK_PAUSE__;
  const ROUND_PAUSE = __ROUND_PAUSE__;
  const t0 = Date.now();
  const RESERVED = ['joins','feed','discover','create','search','your_groups','mine'];
  const items = {};
  const seen = new Set();
  const sc = document.scrollingElement || document.documentElement;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const outOfTime = () => (Date.now() - t0) >= BUDGET;
  const grab = () => {
    const anchors = document.querySelectorAll('a[href*="/groups/"]');
    for (const a of anchors) {
      const h = (a.href || '').split('?')[0];
      const mm = h.match(/\/groups\/([^\/]+)/);
      if (!mm) continue;
      let id;
      try { id = decodeURIComponent(mm[1]); } catch (e) { continue; }
      if (RESERVED.includes(id) || seen.has(id)) continue;
      const name = (a.innerText || '').trim();
      if (!name) continue;
      seen.add(id);
      items[id] = name;
    }
    return anchors.length;
  };
  // Solo toggles VISIBLES cuyo texto exacto es "Ver mas": expanden la lista.
  const moreBtns = () => [...document.querySelectorAll('div[role="button"],a,span')]
    .filter(b => /^Ver m[aá]s$/.test((b.innerText || '').trim()) && b.offsetParent !== null);
  grab();
  let clicks = 0, rounds = 0, failed = 0, togglesLeft = 0, sawToggles = false;
  while (rounds < MAXROUNDS && clicks < MAXCLICKS && !outOfTime()) {
    const btns = moreBtns();
    togglesLeft = btns.length;
    if (!togglesLeft) break;
    sawToggles = true;
    rounds++;
    for (const b of btns) {
      if (clicks >= MAXCLICKS || outOfTime()) break;
      try { b.click(); clicks++; } catch (e) { failed++; }
      await sleep(CLICK_PAUSE);
    }
    if (outOfTime()) break;
    await sleep(ROUND_PAUSE);
    grab();
  }
  togglesLeft = moreBtns().length;
  let ticks = 0, stable = 0, last = -1, height = 0;
  let done = false;
  if (sawToggles) {
    // Los toggles ya revelaron la lista completa. Desplazar aqui cargaria el
    // feed "Descubrir grupos" (miles de anchors de grupos NO unidos): no se
    // desplaza. Se relee hasta que el numero de grupos se estabilice, porque
    // Facebook termina de pintar la lista un instante despues del ultimo clic.
    grab();
    ticks = 1;
    let prevN = -1, steady = 0;
    while (!outOfTime()) {
      const n = Object.keys(items).length;
      if (n > prevN) { steady = 0; prevN = n; } else { steady++; }
      if (steady >= 3) break;
      await sleep(250);
      grab();
      ticks++;
    }
    last = Object.keys(items).length;
    height = sc.scrollHeight;
    done = (togglesLeft === 0);
  } else {
    while (true) {
      grab();
      ticks++;
      const n = Object.keys(items).length;
      if (n > last) { stable = 0; last = n; } else { stable++; }
      height = sc.scrollHeight;
      const atBottom = (sc.scrollTop + sc.clientHeight) >= (height - 4);
      if (stable >= 3 && atBottom) { done = true; break; }
      if (outOfTime()) break;
      sc.scrollTop = height;
      await sleep(150);
    }
  }
  return JSON.stringify({
    items,
    meta: {done, ticks, ms: Date.now() - t0, height, anchors: last,
           clicks, rounds, failed, toggles_left: togglesLeft,
           found: Object.keys(items).length}
  });
})()
"""

READY_JS = 'document.querySelectorAll(\'a[href*="/groups/"]\').length'

CATS = [
    ("playa", ["playa del carmen", "playense", "riviera maya", "riviera marya"]),
    ("guadalupan", ["guadalupe", "guadalupan", "cristo rey", "el peten", "villas riviera"]),
    ("empleo", ["empleo", "trabajo", "vacante", "bolsa de trabajo", "reclut"]),
    ("compraventa", ["compra", "venta", "mercado", "segunda mano", "comercio", "cambio"]),
    ("renta", ["renta", "rento", "departamento", "cuarto", "inmueble", "terreno"]),
    ("tech", ["codex", "program", "developer", " software", "tecnolog", "inteligencia artificial", "open source", "linux", "opencode"]),
    ("turistico", ["turismo", "turist", "hotel", "restaurante", "gastronom", "viaj"]),
]


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def categoriza(name):
    n = " " + norm(name) + " "
    for cat, keys in CATS:
        for k in keys:
            if k in n:
                return cat
    return "otros"


# Facebook concatena nombre + actividad sin separador: el anchor trae
# "...alrededoresActivo por ultima vez hace 10 minutos". Se corta en el primer
# marcador de actividad (con o sin separador previo y con \u200b).
_ACTIVITY = re.compile(
    r"\s*[\u200b·|,\-–—]*\s*(?:"
    r"activos?\b|activas?\b"
    r"|visto(?: por)?(?: última| ultima)? vez"
    r"|(?:última|ultima) vez"
    r"|hace (?:un|una|unos|unas|\d+|mucho|poco) "
    r"(?:segundos?|minutos?|horas?|d[ií]as?|semanas?|meses|momento)"
    r")",
    re.IGNORECASE,
)

_BAD_START = re.compile(
    r"^(?:activo|activa|última vez|ultima vez|ver |descubrir|unirte|"
    r"miembros(?:\s*[\d.,]+\s*(?:mil|k|m)?)?\s*$|"
    r"publicaci|buscar|crear|notificaci|sugerenc|configuraci|tus grupos|opci)",
    re.IGNORECASE,
)

_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# El panel de notificaciones tambien cuelga anchors /groups/<id>/: FB pega el
# texto del aviso al nombre ("No leidaUn administrador aprobo tu publicacion en
# <grupo>.2 h"). Se sanea el nombre y se RECHAZA lo que siga pareciendo un aviso.
_NOTIF_PREFIX = re.compile(r"^\s*no\s+le[ií]d[ao]\s*", re.IGNORECASE)
_NOTIF_GROUP = re.compile(
    r"(?:participar por primera vez en|te mencion[oó] en un comentario en|"
    r"aprob[oó] tu publicaci[oó]n en)\s+",
    re.IGNORECASE,
)
_NOTIF_MARK = re.compile(
    r"no\s+le[ií]d|te mencion(?:aron|ó)|aprob[oó] tu publicaci|"
    r"solicitaron participar|en sus comentarios",
    re.IGNORECASE,
)
# Sello de tiempo relativo pegado al nombre (".3 h", ".55 min"); exige el punto
# para no mutilar nombres legitimos tipo "GYM 24H".
_REL_TIME = re.compile(r"\.\s*\d+\s*(?:s|m|h|d|min|mins)\s*$", re.IGNORECASE)

# Etiquetas de UI que el anchor puede traer como linea propia.
_UI_LINE = re.compile(r"^(?:ver grupo|ver todo|descubrir)$", re.IGNORECASE)


def pick_name(raw):
    """Primera linea util del innerText crudo de un anchor; pura.

    El JS devuelve el innerText TAL CUAL (sin limpiar). Aqui se descartan las
    lineas vacias y las etiquetas de UI ("Ver grupo", "Ver todo", "Descubrir")."""
    for line in (raw or "").split("\n"):
        line = line.replace("\u200b", "").strip()
        if len(line) > 1 and not _UI_LINE.match(line):
            return line
    return ""


def clean_name(s):
    """Recorta avisos de notificacion, sufijo de actividad y separadores; pura."""
    s = (s or "").replace("\u200b", "").strip()
    s = _NOTIF_PREFIX.sub("", s)
    m = _NOTIF_GROUP.search(s)
    if m:
        s = s[m.end():]
    m = _ACTIVITY.search(s)
    if m:
        s = s[:m.start()]
    s = _REL_TIME.sub("", s)
    return s.strip(" \t·|,-–—.").strip()


def valid_name(s):
    """True solo si `s` es un nombre de grupo plausible; pura (sin IO)."""
    s = (s or "").strip()
    if not 3 <= len(s) <= 120:
        return False
    if not _LETTER.search(s):
        return False
    if _NOTIF_MARK.search(s):
        return False
    return not _BAD_START.match(s)


def call(op, args, timeout=70):
    p = subprocess.run([CXT, op, json.dumps(args)], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip())
    except Exception:
        return {"_raw": p.stdout.strip(), "_err": p.stderr.strip()}


def cdp_failed(r):
    """True si la respuesta de `cdp` no es un payload utilizable.

    Un fallo silencioso de CDP (debugger caido, timeout, valor no-JSON) no debe
    confundirse con "la pagina no tenia grupos"."""
    if not isinstance(r, dict):
        return True
    if "_err" in r or "_raw" in r:
        return True
    try:
        val = r["result"]["result"]["value"]
    except Exception:
        return True
    try:
        json.loads(val)
    except Exception:
        return True
    return False


def cdp_value(r):
    """Valor devuelto por `cdp`, o None si no hay payload; sin IO propio."""
    try:
        return r["result"]["result"].get("value")
    except (KeyError, TypeError, AttributeError):
        return None


def clicks_left(clicks_used, cap=MAX_CLICKS):
    """Presupuesto de clics "Ver mas" disponible; pura.

    Mantiene el tope GLOBAL de MAX_CLICKS entre las llamadas del driver: cada
    llamada recibe solo lo que aun no se gasto (nunca negativo)."""
    try:
        used = int(clicks_used)
    except (TypeError, ValueError):
        used = cap
    if used < 0:
        used = 0
    return max(0, cap - used)


def should_stop(meta, calls, max_calls, gained_this_call):
    """Decision pura (sin IO) de cortar el driver de escaneo.

    - `done` reportado por el JS (estable 3 ticks y al fondo) -> cortar.
    - se agotaron las llamadas permitidas -> cortar.
    - una llamada posterior a la primera no aporto ningun grupo nuevo -> cortar.
    """
    if not isinstance(meta, dict):
        meta = {}
    if bool(meta.get("done")):
        return True
    if calls >= max_calls:
        return True
    return gained_this_call == 0 and calls >= 2


def decide_write(previous_count, scraped_count, reset):
    """Decision pura (sin IO) sobre que hacer con el catalogo.

    - "keep"    : no escribir (escaneo vacio -> jamas destruir lo acumulado).
    - "replace" : el nuevo catalogo reemplaza al previo.
    - "merge"   : unir; conserva ids que solo existan en el archivo previo.

    Reemplazar exige >= max(50, ceil(0.6*previo)): asi un escaneo parcial no
    borra grupos ya conocidos (el umbral protege contra CDP a medias)."""
    if scraped_count == 0:
        return "keep"
    if previous_count == 0:
        return "replace"
    if scraped_count >= max(50, -(-3 * previous_count // 5)):  # ceil(0.6*previo)
        return "replace"
    return "merge"


def merge_catalogues(previous_groups, scraped):
    """Une {id:nombre} del archivo previo con lo recien escaneado.

    Nunca pierde un id que exista solo en `previous_groups`; ante nombres
    distintos conserva el mas largo."""
    out = {}
    for g in previous_groups:
        out[g["id"]] = g["name"]
    for k, v in scraped.items():
        if k not in out or len(v) > len(out[k]):
            out[k] = v
    return out


def ingest(items, scraped):
    """Aplica pick_name+clean_name+valid_name a un payload crudo; pura.

    Devuelve un dict NUEVO {id: nombre}; ante nombres distintos conserva el mas
    largo. Los ids con nombre invalido (avisos, basura) se descartan."""
    out = dict(scraped)
    for k, v in (items or {}).items():
        name = clean_name(pick_name(v))
        if not valid_name(name):
            continue
        if k not in out or len(name) > len(out[k]):
            out[k] = name
    return out


def wait_ready(tab, timeout=6.0):
    """Espera a que la lista de grupos tenga anchors; evita el sleep ciego."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = call("cdp", {"tabId": tab, "method": "Runtime.evaluate",
                         "params": {"expression": READY_JS, "returnByValue": True}})
        try:
            if int(cdp_value(r)) > 0:
                return True
        except (TypeError, ValueError):
            pass
        time.sleep(0.25)
    return False


def scan_once(tab, budget_ms=CALL_BUDGET_MS, clicks_budget=MAX_CLICKS):
    """Una evaluacion JS autocontenida: expandir "Ver mas" + scroll + DOM.

    Devuelve (items, meta, ok). `ok` es False si CDP no entrego un payload
    utilizable (fail-closed)."""
    expr = (SCAN_JS_TMPL
            .replace("__BUDGET__", str(int(budget_ms)))
            .replace("__MAXCLICKS__", str(int(clicks_budget)))
            .replace("__MAXROUNDS__", str(int(MAX_ROUNDS)))
            .replace("__CLICK_PAUSE__", str(int(CLICK_PAUSE_MS)))
            .replace("__ROUND_PAUSE__", str(int(ROUND_PAUSE_MS))))
    r = call("cdp", {"tabId": tab, "method": "Runtime.evaluate",
                     "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}})
    if cdp_failed(r):
        return {}, {}, False
    try:
        val = json.loads(r["result"]["result"]["value"])
    except (KeyError, TypeError, ValueError):
        return {}, {}, False
    if not isinstance(val, dict):
        return {}, {}, False
    items, meta = val.get("items"), val.get("meta")
    if not isinstance(items, dict) or not isinstance(meta, dict):
        return {}, {}, False
    return items, meta, True


def load_previous():
    """Catalogo previo (lista de grupos) para decidir y unir; [] si no existe."""
    if not os.path.exists(OUT):
        return []
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f).get("groups", [])
    except Exception:
        return []


def backup(path):
    """Copia de seguridad antes de escribir; conserva modo y mtime (copy2)."""
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak-%d" % int(time.time()))


def scan(tab, reset, navigate):
    """Escanea la lista de grupos SOLO en `tab` y escribe el catalogo."""
    previous = load_previous()
    previous_map = {g["id"]: g["name"] for g in previous}
    if navigate:
        # Modo CXT_TAB explicito (harness existente): reutiliza esa pestana sin
        # activarla. Solo si el usuario pide foco a proposito (CXT_ACTIVATE=1) se
        # permite activar; el modo por defecto abre su propia pestana ya cargada.
        if os.environ.get("CXT_ACTIVATE") == "1":
            cxtlib.activate_tab(CXT, tab)
        call("navigate", {"tabId": tab, "url": URL, "timeout": 45000})
    # Esperar a que la lista exista (nada de sleeps ciegos).
    if not wait_ready(tab):
        print(json.dumps({"ok": False, "error": "la lista de grupos no cargo: 0 anchors /groups/"}, ensure_ascii=False))
        sys.exit(1)
    # Verificar que la pestana SI quedo en la lista de grupos antes de confiar en nada.
    hr = call("cdp", {"tabId": tab, "method": "Runtime.evaluate",
                      "params": {"expression": "location.href", "returnByValue": True}})
    href = cdp_value(hr)
    if not href or "/groups/" not in href:
        print(json.dumps({"ok": False, "error": "la pestana no quedo en la lista de grupos: %s" % href}, ensure_ascii=False))
        sys.exit(1)
    scraped = {}
    calls = 0
    clicks_used = 0
    t0 = time.time()
    while True:
        items, meta, ok = scan_once(tab, CALL_BUDGET_MS, clicks_left(clicks_used))
        calls += 1
        if not ok:
            print(json.dumps({"ok": False, "error": "cdp fallo: sin payload utilizable en la llamada %d" % calls}, ensure_ascii=False))
            sys.exit(1)
        try:
            clicks_used += int(meta.get("clicks") or 0)
        except (TypeError, ValueError):
            pass
        before = len(scraped)
        scraped = ingest(items, scraped)
        gained = len(scraped) - before
        print("[scan] llamada %d: +%d (total %d, done=%s, ticks=%s, %.1fs)" % (
            calls, gained, len(scraped), bool(meta.get("done")), meta.get("ticks"), time.time() - t0), file=sys.stderr)
        if "toggles_left" in meta:
            print("[scan] expand: %s clics, %s rondas, %s 'Ver mas' restantes, %s anchors utiles" % (
                meta.get("clicks"), meta.get("rounds"), meta.get("toggles_left"), meta.get("found")), file=sys.stderr)
        if should_stop(meta, calls, MAX_CALLS, gained):
            break
    print("[scan] %d grupos en %.1fs (%d llamadas cdp)" % (len(scraped), time.time() - t0, calls), file=sys.stderr)
    decision = decide_write(len(previous_map), len(scraped), reset)
    if decision == "keep":
        print(json.dumps({"ok": False, "error": "escaneo sin resultados: 0 grupos (catalogo conservado)", "kept": len(previous_map)}, ensure_ascii=False))
        sys.exit(1)
    got = merge_catalogues(previous, scraped) if decision == "merge" else dict(scraped)
    groups = [{"id": k, "name": v, "category": categoriza(v), "href": "https://www.facebook.com/groups/" + k + "/"} for k, v in got.items()]
    groups.sort(key=lambda g: (g["category"], norm(g["name"])))
    cats = {}
    for g in groups:
        cats[g["category"]] = cats.get(g["category"], 0) + 1
    doc = {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(groups), "categories": cats, "groups": groups}
    backup(OUT)
    cxtlib.write_json_atomic(OUT, doc, indent=1)
    print(json.dumps({"ok": True, "count": len(groups), "categories": cats, "out": OUT}, ensure_ascii=False))
    # Un fallo al regenerar etiquetas nunca debe romper un escaneo.
    try:
        import groups_labels
        groups_labels.write_labels()
    except Exception as e:
        print("aviso: no se pudieron regenerar las etiquetas: %s" % e, file=sys.stderr)


def main():
    reset = "--reset" in sys.argv
    mode, tab = tab_plan(os.environ.get("CXT_TAB"))
    prev_active = None
    own_tab = None
    if mode == "honor":
        # CXT_TAB explicito: usa esa pestana y no crea/cierra nada.
        pass
    else:
        # Nunca reutiliza/activa/navega una pestana ajena: abre la propia EN
        # SEGUNDO PLANO (active=False) para no robar el foco; si la pestana se
        # activara, el popup de la extension se cerraria al perder el foco y
        # pareceria que "el boton no hace nada".
        prev_active = active_tab_id(cxtlib._tabs(CXT))
        r = cxtlib.call(CXT, "tab.new", {"url": URL, "active": False, "wait": True})
        tab = r.get("id") if isinstance(r, dict) else None
        if tab is None:
            sys.exit("no pude crear mi propia pestana para escanear (tab.new: %s); "
                     "no se toca ninguna pestana existente" % (r,))
        own_tab = tab
        print("[scan] tab propio %s (antes activo %s)" % (own_tab, prev_active), file=sys.stderr)
    try:
        scan(tab, reset, navigate=(own_tab is None))
    finally:
        if own_tab is not None:
            # La propia se abrio en segundo plano (active=False): solo se cierra
            # al terminar. No se toca el foco, asi que la ventana y la pestana del
            # usuario (y el popup de la extension) quedan intactos.
            cxtlib.call(CXT, "tab.close", {"tabId": own_tab})
            print("[scan] tab propio cerrado (abierto en segundo plano)", file=sys.stderr)


if __name__ == "__main__":
    main()
