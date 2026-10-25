import os

# Evita que groups_scan resuelva la pestana via el daemon al importarse.
os.environ["CXT_TAB"] = "1"

import groups_scan as g

assert g.decide_write(682, 0, True) == "keep"
assert g.decide_write(682, 700, True) == "replace"
assert g.decide_write(682, 100, True) == "merge"
assert g.decide_write(682, 409, True) == "merge"  # 409 < ceil(0.6*682)=410
assert g.decide_write(0, 5, True) == "replace"
assert g.decide_write(10, 0, False) == "keep"

assert g.merge_catalogues([{"id": "a", "name": "x"}], {"a": "xx", "b": "y"}) == {"a": "xx", "b": "y"}
assert g.merge_catalogues([{"id": "a", "name": "longer"}], {"a": "s"}) == {"a": "longer"}
assert g.merge_catalogues([{"id": "only_prev", "name": "p"}], {"new": "n"}) == {"only_prev": "p", "new": "n"}

assert g.cdp_failed({"ok": False, "_err": "timeout"}) is True
assert g.cdp_failed({"_raw": "boom"}) is True
assert g.cdp_failed({}) is True
assert g.cdp_failed({"result": {"result": {}}}) is True
assert g.cdp_failed({"result": {"result": {"value": "no-json"}}}) is True
assert g.cdp_failed({"result": {"result": {"value": "{}"}}}) is False

# (1) SIN separador: corta exactamente antes de "Activo"
assert g.clean_name("Bazar Peten, Guadalupana, Villas Riviera, Mayakoba y sus alrededoresActivo por última vez hace 10 minutos") == "Bazar Peten, Guadalupana, Villas Riviera, Mayakoba y sus alrededores"
# (2) sufijo de actividad "hace unos segundos"
assert g.clean_name("VACANTES EN PLAYA DEL CARMEN, Q ROO hace unos segundos") == "VACANTES EN PLAYA DEL CARMEN, Q ROO"
# (3) nombre limpio queda intacto
assert g.clean_name("Bazar NENIS Playa del Carmen") == "Bazar NENIS Playa del Carmen"
assert g.valid_name("Bazar NENIS Playa del Carmen") is True
# (4) linea solo-basura
assert g.valid_name(g.clean_name("Activo por última vez hace 3 minutos")) is False

assert g.clean_name("Grupo · Activo hace unas horas") == "Grupo"
assert g.clean_name("Grupo\u200bActivo ahora") == "Grupo"
assert g.valid_name("") is False
assert g.valid_name("ab") is False
assert g.valid_name("12345") is False
assert g.valid_name("x" * 121) is False
assert g.valid_name("ver grupo") is False
# conteo de miembros (linea de UI) sigue rechazado; un grupo real no
assert g.valid_name("Miembros") is False
assert g.valid_name("Miembros 1,234") is False
assert g.valid_name("Miembros 1.2 mil") is False
assert g.valid_name("Miembros de la Elite") is True

# --- driver de escaneo: should_stop (pura, sin IO) ---
assert g.should_stop({"done": True}, 1, 3, 0) is True          # done manda
assert g.should_stop({"done": True}, 1, 3, 99) is True
assert g.should_stop({"done": False}, 3, 3, 50) is True        # tope de llamadas
assert g.should_stop({"done": False}, 2, 3, 0) is True         # 0 ganancia tras 2
assert g.should_stop({"done": False}, 2, 3, 7) is False        # gano y aun hay margen
assert g.should_stop({"done": False}, 1, 3, 0) is False        # primera llamada
assert g.should_stop({}, 1, 3, 0) is False
assert g.should_stop(None, 1, 3, 0) is False                   # meta no dict

# --- clicks_left: tope GLOBAL de clics "Ver mas" entre llamadas (pura) ---
assert g.clicks_left(0) == g.MAX_CLICKS
assert g.clicks_left(3) == g.MAX_CLICKS - 3
assert g.clicks_left(g.MAX_CLICKS) == 0
assert g.clicks_left(99) == 0                                  # nunca negativo
assert g.clicks_left(-2) == g.MAX_CLICKS                       # basura -> cap
assert g.clicks_left("x") == 0                                # ilegible -> fail-closed
assert g.clicks_left(None) == 0
assert g.clicks_left(2, cap=5) == 3

# --- pick_name: parte el innerText crudo y descarta etiquetas de UI ---
assert g.pick_name("Bazar NENIS Playa del Carmen\nVer grupo") == "Bazar NENIS Playa del Carmen"
assert g.pick_name("\n  Ver todo \n\nOtro Grupo") == "Otro Grupo"
assert g.pick_name(None) == ""
assert g.pick_name("  ") == ""

# --- payload crudo del JS -> clean_name + valid_name (pura) ---
fake = {
    "123": "Bazar NENIS Playa del Carmen\nVer grupo",
    "456": "No leida\nTe mencionaron en un comentario",   # aviso -> descartado
    "789": "Activo por última vez hace 3 minutos",        # solo actividad -> descartado
    "999": "   ",
    "joins": "Tus grupos",
}
assert g.ingest(fake, {}) == {"123": "Bazar NENIS Playa del Carmen"}
# id ya conocido con nombre corto: gana el mas largo
assert g.ingest({"123": "Bazar NENIS Playa del Carmen"}, {"123": "Bazar NENIS"}) == {
    "123": "Bazar NENIS Playa del Carmen"}
# no muta el dict de entrada
base = {"x": "Grupo X"}
assert g.ingest({"y": "Grupo Y"}, base) == {"x": "Grupo X", "y": "Grupo Y"} and base == {"x": "Grupo X"}
# payload vacio / None no rompe
assert g.ingest({}, {"a": "A"}) == {"a": "A"}
assert g.ingest(None, {}) == {}

# --- pestana propia: helpers puros (sin IO) ---
assert g.active_tab_id([{"id": 7, "active": False}, {"id": 9, "active": True}]) == 9
assert g.active_tab_id([{"id": 1}, "basura", {"id": 2, "active": True}]) == 2
assert g.active_tab_id([]) is None
assert g.active_tab_id(None) is None
assert g.active_tab_id([{"id": 3, "active": False}]) is None

assert g.env_tab("42") == 42
assert g.env_tab(" 42 ") == 42
assert g.env_tab("abc") is None
assert g.env_tab("") is None
assert g.env_tab(None) is None

assert g.tab_plan("42") == ("honor", 42)      # harness: usa esa pestana
assert g.tab_plan("abc") == ("own", None)     # no numerico -> propia
assert g.tab_plan(None) == ("own", None)
assert g.tab_plan("") == ("own", None)

print("OK test_groups_scan_unit")
