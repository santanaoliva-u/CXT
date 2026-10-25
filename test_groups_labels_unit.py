#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon, SIN red) de groups_labels.py.

Cubre la logica pura del minero de etiquetas:
  (a) invariante: el count de cada label == conteo por substring recomputado
  (b) 'playa del carmen' existe con el conteo correcto (119 en el catalogo actual)
  (c) 'guadalupana' existe con el conteo correcto (6 en el catalogo actual)
  (d) labels ordenadas por count desc, luego label asc
  (e) ningun par de labels se solapa >= 95 %
  (f) ninguna label es una palabra vacia (stopword)
  (g) un documento sintetico ejercita merge + canonicalizacion de forma determinista

Uso: python3 test_groups_labels_unit.py   (exit 0 = PASS)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import groups_labels as GL  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


GROUPS = os.path.join(HERE, "groups.json")
if os.path.exists(GROUPS):
    with open(GROUPS, encoding="utf-8") as f:
        doc = json.load(f)
else:
    doc = {"groups": []}

names = [GL.norm(g.get("name") or "") for g in doc.get("groups") or []]
labels = GL.build_labels(doc, 3)
stop = GL.STOPWORDS
known = (doc.get("count") == 682 and doc.get("scanned_at") == "2026-10-08T22:39:51")

print("catalogo: %d grupos, %d labels (min 3)"
      % (len(names), len(labels)))
print("top5:", [(L["label"], L["count"]) for L in labels[:5]])
print()

# (a) invariante fundamental: count == conteo por substring ------------------
bad = []
for L in labels:
    exp = sum(1 for n in names if L["label"] in n)
    if L["count"] != exp:
        bad.append((L["label"], L["count"], exp))
check("(a) count == conteo por substring para cada label", not bad, bad[:5])

# (b) 'playa del carmen' -----------------------------------------------------
pdc = next((L for L in labels if L["label"] == "playa del carmen"), None)
sub_pdc = sum(1 for n in names if "playa del carmen" in n)
print("   observado 'playa del carmen':", None if pdc is None else pdc["count"],
      "(substring directo=%d)" % sub_pdc)
if known:
    check("(b) 'playa del carmen' existe con count 119",
          pdc is not None and pdc["count"] == 119, pdc)
else:
    check("(b) 'playa del carmen' cumple el invariante de substring",
          pdc is not None and pdc["count"] == sub_pdc, (pdc, sub_pdc))

# (c) 'guadalupana' ----------------------------------------------------------
guad = next((L for L in labels if L["label"] == "guadalupana"), None)
sub_guad = sum(1 for n in names if "guadalupana" in n)
print("   observado 'guadalupana':", None if guad is None else guad["count"],
      "(substring directo=%d)" % sub_guad)
if known:
    check("(c) 'guadalupana' existe con count 6",
          guad is not None and guad["count"] == 6, guad)
else:
    check("(c) 'guadalupana' cumple el invariante de substring",
          guad is not None and guad["count"] == sub_guad, (guad, sub_guad))

# 'free fire' (referencia): se reporta el conteo observado del substring.
sub_ff = sum(1 for n in names if "free fire" in n)
ff = next((L for L in labels if L["label"] == "free fire"), None)
print("   observado 'free fire': label=%s substring=%d"
      % (None if ff is None else ff["count"], sub_ff))

# (d) orden ----------------------------------------------------------------
ok = True
for i in range(len(labels) - 1):
    a, b = labels[i], labels[i + 1]
    if a["count"] < b["count"] or (a["count"] == b["count"] and a["label"] > b["label"]):
        ok = False
        break
check("(d) labels ordenadas por count desc, label asc", ok)

# (e) sin solape >= 95 % ----------------------------------------------------
msets = [frozenset(i for i, n in enumerate(names) if L["label"] in n) for L in labels]
overlap = False
for i in range(len(msets)):
    for j in range(i + 1, len(msets)):
        m = min(len(msets[i]), len(msets[j]))
        if m and len(msets[i] & msets[j]) >= 0.95 * m:
            overlap = True
            break
    if overlap:
        break
check("(e) ningun par de labels se solapa >= 95 %", not overlap)

# (f) sin labels que sean palabras vacias -----------------------------------
bad_sw = [L["label"] for L in labels
          if L["label"] in stop or all(w in stop for w in L["label"].split())]
check("(f) ninguna label es una palabra vacia", not bad_sw, bad_sw[:5])

# (g) documento sintetico: merge + canonicalizacion deterministas ------------
syn = {
    "scanned_at": "synthetic",
    "count": 5,
    "categories": {},
    "groups": [
        {"id": "1", "name": "Playa del Carmen", "category": "playa"},
        {"id": "2", "name": "Playa del Carmen", "category": "playa"},
        {"id": "3", "name": "PLAYA DEL CARMEN", "category": "playa"},
        {"id": "4", "name": "free fire", "category": "otros"},
        {"id": "5", "name": "Free Fire", "category": "otros"},
    ],
}
expected = [
    {"label": "playa del carmen", "count": 3, "category": "playa",
     "examples": ["Playa del Carmen", "Playa del Carmen", "PLAYA DEL CARMEN"]},
    {"label": "free fire", "count": 2, "category": "otros",
     "examples": ["free fire", "Free Fire"]},
]
syn_labels = GL.build_labels(syn, 2)
check("(g) sintetico: merge colapsa el cluster playa -> 'playa del carmen'",
      syn_labels == expected, syn_labels)

# rango esperado del numero de labels con --min 3 ---------------------------
check("count de labels en [60, 200] con min 3", 60 <= len(labels) <= 200, len(labels))

print()
if fails:
    print("RESULT: FAIL " + ",".join(fails))
    sys.exit(1)
print("OK test_groups_labels_unit")
