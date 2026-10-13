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

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
sys.exit(0 if not fails else 1)
