#!/usr/bin/env python3
"""Test unitario (SIN navegador) de groups_share.py.

Cubre lo que no necesita Chrome ni el CLI `cxt`:
  - norm (acentos/mayusculas)
  - seleccion de grupos por filtro e ids
  - groups.json corrupto -> error claro (no traceback)
  - guarda de texto vacio y guarda de "sin grupos"
  - tope de seguridad (capped) y ausencia de sleep final
  - esquema de estado (error, started_ms/finished_ms)
  - deteccion/salto de grupos de compraventa (is_sales_name, is_sales_category,
    matches_sell_text)

Uso: python3 tests/test_groups_share_unit.py   (exit 0 = PASS)
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "..", "scripts")):
    if os.path.exists(os.path.join(_p, "groups_share.py")):
        sys.path.insert(0, _p)
        break

import groups_share as GS  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


TMP = tempfile.mkdtemp(prefix="gs_unit_")
GS.GJ = os.path.join(TMP, "groups.json")
GS.STATUS = os.path.join(TMP, "share_status.json")
GS.PID = os.path.join(TMP, "share.pid")

GROUPS = {
    "scanned_at": "test", "count": 4,
    "categories": {"playa": 2, "empleo": 1, "otros": 1},
    "groups": [
        {"id": "1", "name": "Playa del Carmen Noticias", "category": "playa", "href": "https://x/1"},
        {"id": "2", "name": "PLAYA RIVIERA MAYA", "category": "playa", "href": "https://x/2"},
        {"id": "3", "name": "Empleos Cancun", "category": "empleo", "href": "https://x/3"},
        {"id": "4", "name": "Cocina y Recetas", "category": "otros", "href": "https://x/4"},
    ],
}


def write_groups(obj):
    with open(GS.GJ, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_status():
    with open(GS.STATUS, encoding="utf-8") as f:
        return json.load(f)


# 1. norm ---------------------------------------------------------------
check("norm quita acentos y baja a minusculas",
      GS.norm("Cafe\u0301 A\u0301E\u0301I\u0301O\u0301U\u0301") == "cafe aeiou",
      GS.norm("Cafe\u0301 A\u0301E\u0301I\u0301O\u0301U\u0301"))
check("norm(None) == ''", GS.norm(None) == "")
check("norm es cxtlib.norm", GS.norm is GS.cxtlib.norm)

# 1b. deteccion de grupos de venta (compraventa) ------------------------
check("SELL_TEXT por defecto", GS.SELL_TEXT == "vender algo", GS.SELL_TEXT)
check("matches_sell_text 'Vender algo'", GS.matches_sell_text("Vender algo") is True)
check("matches_sell_text 'Vende algo'", GS.matches_sell_text("Vende algo") is True)
check("matches_sell_text acento/mayus", GS.matches_sell_text("V\u00c9NDER ALGO") is True)
check("matches_sell_text no confunde 'Escribe algo'",
      GS.matches_sell_text("Escribe algo") is False)
check("is_sales_name compraventa real",
      GS.is_sales_name('Compra y venta de motos y autos."Cancun,Merida,Playa."') is True)
check("is_sales_name 'COMPRAVENTA CANCUN'", GS.is_sales_name("COMPRAVENTA CANCUN") is True)
check("is_sales_name 'SE VENDE'", GS.is_sales_name("SE VENDE TODO") is True)
check("is_sales_name 'Vendiendo'", GS.is_sales_name("Vendiendo mis cosas") is True)
check("is_sales_name 'Marketplace'", GS.is_sales_name("Marketplace Playa") is True)
check("is_sales_name 'Segunda mano'", GS.is_sales_name("Segunda mano Cancun") is True)
check("is_sales_name 'Remato'", GS.is_sales_name("Remato muebles") is True)
check("is_sales_name 'Trueque'", GS.is_sales_name("Trueque y cambio") is True)
check("is_sales_name grupo normal (que todo playa) es False",
      GS.is_sales_name("QUE TODO PLAYA DEL CARMEN SE ENTERE") is False)
check("is_sales_name 'Playa del Carmen Noticias' es False",
      GS.is_sales_name("Playa del Carmen Noticias") is False)
check("is_sales_name 'Empleos Cancun' es False", GS.is_sales_name("Empleos Cancun") is False)
check("is_sales_name(None) es False", GS.is_sales_name(None) is False)

# 1c. deteccion de grupos de venta por CATEGORIA ------------------------
check("SKIP_CATEGORIES por defecto incluye compraventa",
      GS.norm("compraventa") in GS.SKIP_CATEGORIES, GS.SKIP_CATEGORIES)
check("is_sales_category('compraventa') es True",
      GS.is_sales_category("compraventa") is True)
check("is_sales_category('CompraVenta') es True (mayus)",
      GS.is_sales_category("CompraVenta") is True)
check("is_sales_category('venta de autos') es True (contiene venta)",
      GS.is_sales_category("venta de autos") is True)
check("is_sales_category('playa') es False", GS.is_sales_category("playa") is False)
check("is_sales_category('empleo') es False", GS.is_sales_category("empleo") is False)
check("is_sales_category('otros') es False", GS.is_sales_category("otros") is False)
check("is_sales_category('guadalupano') es False",
      GS.is_sales_category("guadalupano") is False)
check("is_sales_category(None) es False", GS.is_sales_category(None) is False)

# 2. seleccion por filtro / ids ----------------------------------------
write_groups(GROUPS)
sel = GS.select_groups([], ["playa"])
check("filtro 'playa' -> ids 1,2 (mayus/acentos-insensible)",
      [g["id"] for g in sel] == ["1", "2"], [g["id"] for g in sel])
sel = GS.select_groups([], ["cancun"])  # sin acento
check("filtro 'cancun' -> id 3", [g["id"] for g in sel] == ["3"], [g["id"] for g in sel])
sel = GS.select_groups(["4", "2"], [])
check("ids -> respeta catalogo", sorted(g["id"] for g in sel) == ["2", "4"], [g["id"] for g in sel])
check("sin ids ni filtros -> []", GS.select_groups([], []) == [])

# 3. groups.json corrupto -> error claro -------------------------------
with open(GS.GJ, "w", encoding="utf-8") as f:
    f.write("esto no es json")
try:
    GS.select_groups([], ["x"])
    check("groups.json corrupto lanza SystemExit", False)
except SystemExit as e:
    check("groups.json corrupto lanza SystemExit",
          "corrupto" in str(e) or "invalido" in str(e), str(e))

# 4. guarda texto vacio -------------------------------------------------
write_groups(GROUPS)
if os.path.exists(GS.STATUS):
    os.remove(GS.STATUS)
try:
    GS.job({"text": "   ", "ids": ["1", "2"]})
    check("job texto vacio -> exit != 0", False)
except SystemExit as e:
    check("job texto vacio -> exit != 0", e.code not in (0, None), e.code)
st = read_status()
check("status error='texto vacio'", st.get("error") == "texto vacio", st.get("error"))
check("status running=False y total=2", st.get("running") is False and st.get("total") == 2, st)
check("status conserva claves existentes",
      all(k in st for k in ("done", "published", "started_at", "finished_at",
                            "current", "results", "dry")), sorted(st))

# 5. guarda sin grupos --------------------------------------------------
try:
    GS.job({"text": "hola", "filter": ["zzzz_no_existe"]})
    check("job sin grupos -> exit != 0", False)
except SystemExit as e:
    check("job sin grupos -> exit != 0", e.code not in (0, None), e.code)
st = read_status()
check("status error='sin grupos (revisa filter/ids)'",
      st.get("error") == "sin grupos (revisa filter/ids)", st.get("error"))
check("status total=0", st.get("total") == 0, st.get("total"))

# 6. tope de seguridad (capped) + sin sleep final -----------------------
write_groups({"count": 6, "categories": {}, "groups": [
    {"id": str(i), "name": "grupo prueba %d" % i, "href": "https://x/%d" % i} for i in range(6)]})
GS._resolve_tab = lambda: "1"
posts = []
sleeps = []


def fake_post(g, text, dry=False):
    posts.append(g["name"])
    return {"name": g["name"], "ok": True}


GS.post_to_group = fake_post
GS.time.sleep = lambda s: sleeps.append(s)
GS.job({"text": "x", "filter": ["grupo"], "max": 2, "dry": False, "interval_sec": 5})
st = read_status()
check("max=2 recorta a 2 y publica 2",
      st.get("total") == 2 and st.get("published") == 2, (st.get("total"), st.get("published")))
check("status capped=True", st.get("capped") is True, st.get("capped"))
check("sin sleep tras el ultimo grupo (1 solo)", len(sleeps) == 1, sleeps)
check("status error='' en exito", st.get("error") == "", st.get("error"))
check("started_ms/finished_ms presentes y >0",
      st.get("started_ms", 0) > 0 and st.get("finished_ms", 0) > 0,
      (st.get("started_ms"), st.get("finished_ms")))

# 7. tope por defecto = 50 ----------------------------------------------
write_groups({"count": 60, "categories": {}, "groups": [
    {"id": str(i), "name": "g%d" % i, "href": "https://x/%d" % i} for i in range(60)]})
posts.clear()
sleeps.clear()
GS.job({"text": "x", "filter": ["g"], "dry": False, "interval_sec": 0})
st = read_status()
check("tope por defecto 50",
      st.get("total") == 50 and st.get("capped") is True, (st.get("total"), st.get("capped")))

# 8. grupos de ventas: se saltan por nombre sin abrirlos ----------------
write_groups({"count": 3, "categories": {}, "groups": [
    {"id": "1", "name": "Compra y venta de motos y autos", "href": "https://x/1"},
    {"id": "2", "name": "QUE TODO PLAYA DEL CARMEN SE ENTERE", "href": "https://x/2"},
    {"id": "3", "name": "Vendiendo cosas", "href": "https://x/3"}]})
opened = []
sleeps.clear()


def fake_open(g, text, dry=False):
    opened.append(g["name"])
    return {"name": g["name"], "ok": True}


GS.post_to_group = fake_open
GS.job({"text": "x", "filter": ["compra", "playa", "vendi"], "dry": False, "interval_sec": 0})
st = read_status()
res = st.get("results", [])
skipped = [r for r in res if r.get("skipped")]
check("salta 2 grupos de ventas", st.get("skipped") == 2, st.get("skipped"))
check("publica solo el grupo normal", st.get("published") == 1, st.get("published"))
check("done cuenta los 3 grupos", st.get("done") == 3, st.get("done"))
check("no abre los grupos de venta",
      opened == ["QUE TODO PLAYA DEL CARMEN SE ENTERE"], opened)
check("results de venta: skipped/stage venta",
      len(skipped) == 2
      and all(r.get("skipped") is True and r.get("stage") == "venta" and r.get("ok") is False
              for r in skipped), skipped)

# 8b. grupos de venta por CATEGORIA: se saltan sin abrirlos -----------------
write_groups({"count": 3, "categories": {}, "groups": [
    {"id": "1", "name": "QUE TODO PLAYA DEL CARMEN SE ENTERE", "category": "compraventa", "href": "https://x/1"},
    {"id": "2", "name": "venta de autos en cancun", "category": "otros", "href": "https://x/2"},
    {"id": "3", "name": "Cocina y Recetas", "category": "otros", "href": "https://x/3"}]})
opened.clear()
sleeps.clear()
GS.post_to_group = fake_open
GS.job({"text": "x", "filter": ["playa", "venta", "recetas"], "dry": False, "interval_sec": 0})
st = read_status()
res = st.get("results", [])
cat_skip = [r for r in res if r.get("reason") == "categoria"]
check("categoria: salta 2 y publica 1",
      st.get("skipped") == 2 and st.get("published") == 1,
      (st.get("skipped"), st.get("published")))
check("categoria: no abre el grupo de compraventa",
      opened == ["Cocina y Recetas"], opened)
check("categoria: reason='categoria' en el grupo de venta",
      len(cat_skip) == 1 and cat_skip[0].get("name") == "QUE TODO PLAYA DEL CARMEN SE ENTERE"
      and cat_skip[0].get("skipped") is True and cat_skip[0].get("stage") == "venta"
      and cat_skip[0].get("ok") is False, cat_skip)

# 9. ensure_groups sin groups.json ni escaner -> error claro ------------
GS.GJ = os.path.join(TMP, "no_existe.json")
GS.HERE = TMP  # no hay groups_scan.py aqui
try:
    GS.ensure_groups()
    check("ensure_groups sin groups.json -> SystemExit claro", False)
except SystemExit as e:
    check("ensure_groups sin groups.json -> SystemExit claro",
          "no pude escanear grupos" in str(e), str(e))

# 10. motor de compartir: parsers de la prueba por FEED ------------------
import shared as SH  # noqa: E402

check("shared._photo_fbid extrae fbid del permalink",
      SH._photo_fbid("https://www.facebook.com/photo/?fbid=122237246654329580&set=x")
      == "122237246654329580")
check("shared._photo_fbid sin fbid -> ''", SH._photo_fbid("https://x/y") == "")
check("shared._group_id acepta id numerico", SH._group_id("228052758915268") == "228052758915268")
check("shared._group_id sin grupo -> ''", SH._group_id("") == "")
js = SH.js_feed_count("123", "")
check("shared.js_feed_count incrusta el fbid", 'fbid=' in js and '123' in js)

# 11. selector PURO del post nuevo por permalink (sin navegador) ----------
def _cand(pid, ts, text="", fbid="", gid="9", url=None, status="PUBLISHED"):
    return {"pid": str(pid), "gid": gid, "ts": ts, "text": text, "fbid": fbid,
            "status": status,
            "url": url if url is not None else "https://www.facebook.com/groups/%s/posts/%s/" % (gid, pid)}


CANDS = [
    _cand(100, 2000, "Hola Mundo", "555"),
    _cand(101, 3000, "otro texto", "777"),
    _cand(102, 4000, "Hola Mundo", "555", url="https://www.facebook.com/groups/9/posts/102/?comment_id=3"),
]
check("pick: fbid gana al mas reciente sin identidad",
      (SH.pick_new_post_permalink(CANDS, "9", not_before_ts=1500, fbid="555") or {}).get("pid") == "100")
check("pick: needle ignora acentos y mayusculas",
      (SH.pick_new_post_permalink(CANDS, "9", needle="HOLA mundo", not_before_ts=1500) or {}).get("pid") == "100")
check("pick: gid distinto -> None",
      SH.pick_new_post_permalink(CANDS, "8", needle="hola mundo", not_before_ts=1500, fbid="555") is None)
check("pick: timestamp viejo -> None",
      SH.pick_new_post_permalink(CANDS, "9", needle="hola mundo", not_before_ts=2500, fbid="555") is None)
check("pick: URL de comentario se descarta",
      SH.pick_new_post_permalink(CANDS, "9", needle="hola mundo", not_before_ts=1500, fbid="555")["pid"] == "100")
check("pick: pid del dict en desacuerdo con la URL -> None",
      SH.pick_new_post_permalink([_cand(999, 2000, "Hola Mundo", "555",
                                        url="https://www.facebook.com/groups/9/posts/600/")],
                                 "9", needle="hola mundo", not_before_ts=1500, fbid="555") is None)
check("pick: timestamp desconocido -> None (fail-closed)",
      SH.pick_new_post_permalink([_cand(103, 0, "Hola Mundo", "555")], "9", needle="hola mundo",
                                 not_before_ts=1500, fbid="555") is None)
check("pick: sin needle ni fbid -> None",
      SH.pick_new_post_permalink(CANDS, "9", not_before_ts=1500) is None)
check("pick: sin coincidencia de texto/foto -> None",
      SH.pick_new_post_permalink(CANDS, "9", needle="no existe", not_before_ts=1500, fbid="999") is None)
check("pick: lista vacia -> None", SH.pick_new_post_permalink([], "9", needle="x", not_before_ts=0) is None)
NEWEST = [_cand(200, 2000, "mismo texto"), _cand(201, 3000, "mismo texto")]
check("pick: entre iguales gana el mas reciente",
      (SH.pick_new_post_permalink(NEWEST, "9", needle="mismo texto", not_before_ts=1500) or {}).get("pid") == "201")
PERMA = [_cand(300, 2000, "", "555",
               url="https://www.facebook.com/groups/9/permalink/300/")]
check("pick: acepta forma /permalink/<pid>/",
      (SH.pick_new_post_permalink(PERMA, "9", not_before_ts=1500, fbid="555") or {}).get("pid") == "300")
check("pick: borrador/programado se descarta",
      SH.pick_new_post_permalink([_cand(400, 2000, "Hola Mundo", "555", status="DRAFT")],
                                 "9", needle="hola mundo", not_before_ts=1500) is None)
check("pick: notif_id/notif_t se descartan",
      SH.pick_new_post_permalink(
          [_cand(500, 2000, "Hola Mundo", "555",
                 url="https://www.facebook.com/groups/9/posts/500/?notif_id=1")],
          "9", needle="hola mundo", not_before_ts=1500) is None)
# La vista member (/groups/<gid>/user/<uid>/) no pinta DOM y FB recorta el texto
# con "... Ver mas": exigir needle/fbid daba falso `sin-prueba` en posts SI
# publicados (visto en vivo con el post 1424504862646745). own=True acepta un id
# nuevo posterior al click; own=False queda igual de estricto.
OWN = [_cand(600, 2000, "texto recortado  Ver mas"), _cand(601, 900, "viejo")]
check("pick: own=True acepta id nuevo sin needle ni fbid",
      (SH.pick_new_post_permalink(OWN, "9", not_before_ts=1500, own=True) or {}).get("pid") == "600")
check("pick: own=True sigue rechazando ids anteriores al click",
      SH.pick_new_post_permalink(OWN, "9", not_before_ts=5000, own=True) is None)
check("pick: own=True no relaja la puerta por defecto (own=False estricto)",
      SH.pick_new_post_permalink([_cand(602, 2000, "texto recortado")], "9",
                                 not_before_ts=1500) is None)
check("pick: identidad fuerte puntua mas que own",
      (SH.pick_new_post_permalink([_cand(700, 2000, "texto sin needle"),
                                   _cand(701, 2000, "Hola Mundo")], "9",
                                  needle="hola mundo", not_before_ts=1500,
                                  own=True) or {}).get("pid") == "701")
import inspect as _inspect
check("confirm: la ruta member es la que habilita own",
      'own=(name == "member")' in _inspect.getsource(SH.confirm_new_post_permalink))
check("needle: emojis entre frases del post no rompen la identidad",
      SH._needle_match("d n t e o o p s 🔥 ¡oferta en playa del carmen! 🔥 🚪 puerta "
                       "corrediza de aluminio blanco con vidrio",
                       "¡oferta en playa del carmen! puerta corrediza de aluminio blanco"))
check("needle: palabras sueltas y en otro orden no cuentan",
      not SH._needle_match("blanco aluminio corrediza puerta playa carmen oferta",
                           "oferta en playa del carmen puerta corrediza aluminio blanco"))
check("needle: needle corto no usa el camino difuso",
      not SH._needle_match("mundo y hola", "hola mundo"))

check("veredicto: prueba fuerte gana sobre pendiente y pista",
      SH.classify_send_verdict(proof="permalink-nuevo", weak="composer-cerrado",
                               pending_hint="pendiente de aprobacion") ==
      (True, "permalink-nuevo", False))
check("veredicto: pendiente de aprobacion no es exito ni fallo del motor",
      SH.classify_send_verdict(weak="composer-cerrado",
                               pending_hint="Tu publicacion quedo pendiente de aprobacion") ==
      (False, "pendiente-aprobacion", True))
check("veredicto: sin click entregado no hay estado pendiente",
      SH.classify_send_verdict(pending_hint="pendiente de aprobacion", clicked=False,
                               verdict_reason="sin-prueba") == (False, "sin-prueba", False))
check("veredicto: solo pista debil -> publicado-sin-confirmar",
      SH.classify_send_verdict(weak="composer-cerrado") ==
      (False, "publicado-sin-confirmar", False))
check("veredicto: sin nada -> motivo real",
      SH.classify_send_verdict(verdict_reason="feed-ilegible") ==
      (False, "feed-ilegible", False))
check("veredicto: sin nada ni motivo -> sin-prueba",
      SH.classify_send_verdict() == (False, "sin-prueba", False))
check("veredicto: pendiente sin click y con pista -> manda la pista",
      SH.classify_send_verdict(weak="confirmado", pending_hint="pendiente de aprobacion",
                               clicked=False) == (False, "publicado-sin-confirmar", False))
check("shared.JS_PENDING_NOTICE busca el aviso de aprobacion",
      "pendiente" in SH.JS_PENDING_NOTICE.lower() and "approval" in SH.JS_PENDING_NOTICE)

# 12. aprobacion tardia por admin: parsers PUROS -------------------------
REAL_APROB = ("https://www.facebook.com/groups/264385439435725/posts/1115093868207/"
              "?notif_id=1792265467378636&notif_t=group_post_approved&ref=notif")
GRUPO_APROB = "Playa del Carmen Comunidad y Noticias Quintana Roo"
RAW_APROB = {"href": "https://www.facebook.com/notifications", "items": [
    {"url": REAL_APROB, "group": GRUPO_APROB + ".2 h",
     "text": "No le\u00eddaUn administrador aprob\u00f3 tu publicaci\u00f3n en "
             + GRUPO_APROB + ".2 h"}]}

check("parse_approvals(None) -> []", SH.parse_approvals(None) == [])
check("parse_approvals str -> []", SH.parse_approvals("basura") == [])
check("parse_approvals {'items': None} -> []", SH.parse_approvals({"items": None}) == [])
check("parse_approvals basura -> []",
      SH.parse_approvals({"items": [None, 5, {}, {"url": "no-es-permalink"}]}) == [])
ap = SH.parse_approvals(RAW_APROB)
check("parse_approvals extrae url/gid/grupo/texto",
      len(ap) == 1 and ap[0]["gid"] == "264385439435725" and ap[0]["url"] == REAL_APROB
      and bool(ap[0]["group"]) and "aprob\u00f3" in ap[0]["text"], ap)
check("parse_approvals quita el reloj relativo del grupo",
      ap and ap[0]["group"] == GRUPO_APROB, ap)
check("parse_approvals deduce el grupo del texto si falta",
      (SH.parse_approvals({"items": [
          {"url": REAL_APROB,
           "text": "Un administrador aprob\u00f3 tu publicaci\u00f3n en "
                   + GRUPO_APROB + ".5 min"}]}) or [{}])[0].get("group") == GRUPO_APROB)
check("parse_approvals deduplica por url",
      len(SH.parse_approvals({"items": [{"url": REAL_APROB, "group": "x"},
                                        {"url": REAL_APROB, "group": "y"}]})) == 1)
check("parse_approvals recorta text a 200",
      len(SH.parse_approvals({"items": [{"url": REAL_APROB, "text": "z" * 500}]})[0]["text"]) == 200)
check("match_approval acentos/mayusculas",
      SH.match_approval(ap, "PLAYA DEL CARMEN comunidad y noticias QUINTANA ROO") is ap[0])
check("match_approval grupo distinto -> None",
      SH.match_approval(ap, "Otro Grupo Cualquiera") is None)
check("match_approval([]/None) -> None",
      SH.match_approval([], "x") is None and SH.match_approval(None, "x") is None)
check("JS_APPROVALS busca notif_t=group_post_approved, ref=notif y la frase",
      "group_post_approved" in SH.JS_APPROVALS and "ref=notif" in SH.JS_APPROVALS
      and "publicaci" in SH.JS_APPROVALS)

# 12b. permalink aprobado: veredicto PURO y fail-closed -------------------
APROB_RAW = {"href": REAL_APROB, "title": "Playa del Carmen", "articles": 2,
             "has_author": True, "needle": True, "needle_hits": 3, "err": ""}
v = SH.verdict_approval_permalink(APROB_RAW, needle="recomendado")
check("verdict permalink ok -> permalink-aprobado",
      v["ok"] is True and v["reason"] == "permalink-aprobado", v)
check("verdict permalink sin needle vale con autor y sin error",
      SH.verdict_approval_permalink(APROB_RAW)["reason"] == "permalink-aprobado")
check("verdict permalink con error -> permalink-nodisponible",
      SH.verdict_approval_permalink(dict(APROB_RAW, err="Este contenido no esta disponible"),
                                    needle="recomendado")["reason"] == "permalink-nodisponible")
check("verdict permalink sin autor -> permalink-ajeno",
      SH.verdict_approval_permalink(dict(APROB_RAW, has_author=False),
                                    needle="recomendado")["reason"] == "permalink-ajeno")
check("verdict permalink autor ok sin needle -> aprobado-sin-needle",
      SH.verdict_approval_permalink(dict(APROB_RAW, needle=False, needle_hits=0),
                                    needle="recomendado")["reason"] == "aprobado-sin-needle")
check("verdict permalink None -> permalink-ilegible",
      SH.verdict_approval_permalink(None)["reason"] == "permalink-ilegible")
check("verdict permalink href ajeno -> permalink-ilegible",
      SH.verdict_approval_permalink(dict(APROB_RAW, href="https://www.facebook.com/"))["reason"]
      == "permalink-ilegible")
check("verdict permalink de OTRO post -> permalink-ilegible",
      SH.verdict_approval_permalink(
          dict(APROB_RAW, href="https://www.facebook.com/groups/264385439435725/posts/999/"),
          expect_url=REAL_APROB)["reason"] == "permalink-ilegible")
check("JS_APPROVAL_POST recibe autor/needle (sin identidad hardcodeada)",
      "__AUTHOR__" in SH.JS_APPROVAL_POST and "__NEEDLE__" in SH.JS_APPROVAL_POST
      and "Santana" not in SH.JS_APPROVAL_POST)
check("js_approval_post normaliza autor y needle",
      '"dulce crepita"' in SH.js_approval_post("Dulce Cr\u00e9pita", "Dulce Cr\u00e9pita"))

# 13. reconcile_approvals: solo asciende con permalink CONFIRMADO --------
_ORIG_READ = SH.read_approvals
_ORIG_CONFIRM = SH.confirm_new_post_permalink
_ORIG_DIRECT = SH.confirm_approval_permalink
_ORIG_GID = SH._group_id
calls = {"read": 0, "confirm": [], "direct": []}


def fake_read(cxt, tab):
    calls["read"] += 1
    return SH.parse_approvals(RAW_APROB)


def fake_direct_ok(cxt, tab, url, needle=""):
    calls["direct"].append((url, needle))
    return {"ok": True, "reason": "permalink-aprobado", "url": url,
            "signals": {"has_author": True, "needle": True, "err": ""}}


def fake_direct_no(cxt, tab, url, needle=""):
    calls["direct"].append((url, needle))
    return {"ok": False, "reason": "permalink-ilegible", "signals": {}}


def fake_confirm_ok(cxt, tab, gid, needle="", group="", not_before_ts=None):
    calls["confirm"].append((gid, needle, group))
    return {"ok": True, "count": 1, "reason": "permalink-nuevo",
            "url": "https://www.facebook.com/groups/%s/posts/1115093868207/" % gid}


def fake_confirm_no(cxt, tab, gid, needle="", group="", not_before_ts=None):
    calls["confirm"].append((gid, needle, group))
    return {"ok": False, "count": 0, "reason": "sin-prueba", "signals": {}}


SH._group_id = lambda g: ""
SH.read_approvals = fake_read
SH.confirm_approval_permalink = fake_direct_no
SH.confirm_new_post_permalink = fake_confirm_ok
res = [{"group": GRUPO_APROB, "ok": False, "reason": "sin-prueba"},
       {"group": "Otro Grupo", "ok": True, "reason": "feed", "published": True},
       {"group": "Tercero", "ok": False, "reason": "sin-prueba"}]
n = SH.reconcile_approvals(None, None, res, needle="Dulce Crepita PDC")
check("reconcile asciende solo el sin-prueba con aprobacion", n == 1, n)
check("reconcile deja ok/published/reason/stage/approval_url",
      res[0].get("ok") is True and res[0].get("published") is True
      and res[0].get("reason") == "permalink-aprobado"
      and res[0].get("stage") == "permalink-aprobado"
      and res[0].get("approval_url") == REAL_APROB, res[0])
check("reconcile no toca el exito previo (feed)",
      res[1].get("reason") == "feed" and "approval_url" not in res[1], res[1])
check("reconcile deja sin-prueba sin aviso (no inventa)",
      res[2].get("ok") is False and res[2].get("reason") == "sin-prueba", res[2])
check("reconcile lee /notifications UNA sola vez", calls["read"] == 1, calls)
check("reconcile confirma con gid del aviso y el needle",
      calls["confirm"] == [("264385439435725", "Dulce Crepita PDC", GRUPO_APROB)],
      calls["confirm"])
check("reconcile fallback recibe la url del aviso en la prueba directa",
      calls["direct"] == [(REAL_APROB, "Dulce Crepita PDC")], calls["direct"])

# La prueba DIRECTA del permalink del aviso basta y evita el fallback.
calls["read"] = 0
calls["confirm"] = []
calls["direct"] = []
SH.confirm_approval_permalink = fake_direct_ok
res4 = [{"group": GRUPO_APROB, "ok": False, "reason": "sin-prueba"}]
n4 = SH.reconcile_approvals(None, None, res4, needle="Dulce Crepita PDC")
check("reconcile asciende por el permalink DIRECTO del aviso", n4 == 1, n4)
check("reconcile directo usa la url del aviso y no el fallback",
      calls["direct"] == [(REAL_APROB, "Dulce Crepita PDC")]
      and calls["confirm"] == [], calls)
check("reconcile directo conserva approval_url del aviso",
      res4[0].get("approval_url") == REAL_APROB
      and res4[0].get("reason") == "permalink-aprobado", res4[0])

calls["read"] = 0
calls["confirm"] = []
calls["direct"] = []
SH.confirm_approval_permalink = fake_direct_no
SH.confirm_new_post_permalink = fake_confirm_no
res2 = [{"group": GRUPO_APROB, "ok": False, "reason": "sin-prueba"}]
check("reconcile sin permalink directo confirmado no asciende",
      SH.reconcile_approvals(None, None, res2, needle="x") == 0
      and res2[0].get("ok") is False and res2[0].get("reason") == "sin-prueba"
      and len(calls["direct"]) == 1 and len(calls["confirm"]) == 1, (res2, calls))

calls["read"] = 0
res3 = [{"group": GRUPO_APROB, "ok": True, "reason": "feed"},
        {"group": GRUPO_APROB, "ok": False, "reason": "pendiente-aprobacion"}]
check("reconcile sin sin-prueba no toca el navegador",
      SH.reconcile_approvals(None, None, res3) == 0 and calls["read"] == 0, calls)

calls["confirm"] = []
calls["direct"] = []
SH._group_id = lambda g: "999999"
check("reconcile gid incoherente con el grupo -> no asciende",
      SH.reconcile_approvals(None, None, [{"group": GRUPO_APROB, "ok": False,
                                           "reason": "sin-prueba"}]) == 0
      and calls["confirm"] == [] and calls["direct"] == [], calls)

SH.read_approvals = _ORIG_READ
SH.confirm_new_post_permalink = _ORIG_CONFIRM
SH.confirm_approval_permalink = _ORIG_DIRECT
SH._group_id = _ORIG_GID

# --- camino alterno: compositor del grupo ---------------------------------
# El flujo Compartir dejo 0/10 y run#4 sin rastro; el compositor del propio
# grupo si publica (permalink verificado a mano). Estos checks fijan el
# contrato: modo opt-in, guardas de exposicion y verificacion por permalink.
_src_composer = _inspect.getsource(SH.share_via_group_composer)
check("composer: exige texto (fail-closed, no toca el navegador)",
      SH.share_via_group_composer(None, None, "Bazar NENIS Playa del Carmen", "")["reason"]
      == "sin-texto")
check("composer: Publicar solo si esta expuesto",
      "and cand.get(\"exposed\")" in _src_composer
      and "publicar-no-expuesto" in _src_composer)
check("composer: verifica por permalink de la vista member",
      "confirm_new_post_permalink" in _src_composer
      and "not_before_ts=click_ts - 120" in _src_composer)
check("composer: JS del editor exige dialog de crear publicacion",
      "crear publicaci|create post" in SH.JS_GC_EDITOR
      and 'contenteditable=\"true\"' in SH.JS_GC_EDITOR)
check("composer: JS de Publicar exige texto exacto y exposicion",
      "^(publicar|post)$" in SH.JS_GC_PUBLISH and "exposed:!!top" in SH.JS_GC_PUBLISH)
check("composer: JS del disparador hace scrollIntoView",
      "scrollIntoView" in SH.JS_GC_TRIGGER and "escribe algo" in SH.JS_GC_TRIGGER)
_src_target = _inspect.getsource(SH.share_target)
check("share_target: modo composer enruta al compositor",
      '_composer_group(cxt, tab, group, dry, text, expect)' in _src_target)
check("share_target: por defecto sigue el flujo de compartir",
      '_share_group(cxt, tab, post_url, group, dry, text, expect)' in _src_target)
check("share_target: sin modo explicito usa CXT_SHARE_MODE",
      "CXT_SHARE_MODE" in _src_target)

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
