#!/usr/bin/env python3
"""Minero de ETIQUETAS para el catalogo de grupos de Facebook (CXT).

Deriva un conjunto de etiquetas ("labels") a partir de `groups.json`, cada una
con el conteo EXACTO de grupos que cubre. El conteo usa la misma semantica que
el motor de seleccion (`share_post.select_groups`): un grupo "coincide" con una
etiqueta si `etiqueta in cxtlib.norm(grupo.name)` es cierto. Asi el numero que
muestra la UI es el numero real de grupos que esa etiqueta publicaria.

Sin navegador. Sin red. Solo logica pura sobre `groups.json`.

CLI:
    python3 groups_labels.py [--groups groups.json] [--out groups_labels.json] [--min 3]

Tambien expone `write_labels()` (entrypoint que llama `groups_scan.py` al terminar
de escanear) y `build_labels()` (funcion pura que ejercitan los tests).
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cxtlib  # noqa: E402

# NUNCA se reimplementa: la semantica del motor es exactamente esta.
norm = cxtlib.norm

GROUPS = os.path.join(HERE, "groups.json")
OUT = os.path.join(HERE, "groups_labels.json")

# Palabras que no aportan a una etiqueta. Se comparan contra las palabras ya
# normalizadas (sin acentos, en minusculas).
STOPWORDS = set(
    "de del la el los las y en para por con sin un una unos unas a al e o que "
    "se su sus mi mis tu tus es son grupo grupos oficial ventas venta compra "
    "compraventa todo solo mas nuevo nueva gratis mexico 2026 2025".split()
)

MAX_NGRAM = 3
MERGE_OVERLAP = 0.95       # dos clusters se solapan si comparten >=95 %
CANON_RATIO = 0.85         # extension canonica: count >= 0.85 * count(etiqueta)


def _is_bad_phrase(tokens):
    """True si la frase no sirve como etiqueta.

    Regla de stopwords: una frase se descarta si EMPIEZA o TERMINA con una
    palabra vacia (o es una sola palabra vacia). Las palabras vacias en el
    MEDIO se conservan a proposito: `playa del carmen` y `bolsa de trabajo`
    contienen `del`/`de` y son etiquetas validas del catalogo real.
    """
    if tokens[0] in STOPWORDS or tokens[-1] in STOPWORDS:
        return True
    return all(t in STOPWORDS for t in tokens)


def candidates(names):
    """Todas las frases candidatas (1..3 tokens) de los nombres normalizados."""
    cands = set()
    for n in names:
        toks = n.split()
        for i in range(len(toks)):
            for L in range(1, MAX_NGRAM + 1):
                piece = toks[i:i + L]
                if len(piece) < L:
                    continue
                phrase = " ".join(piece)
                if len(phrase) < 4 or phrase.isdigit():
                    continue
                if _is_bad_phrase(piece):
                    continue
                cands.add(phrase)
    return cands


def count_in(phrase, names):
    """Conteo por SUBSTRING sobre todos los nombres normalizados (motor)."""
    return sum(1 for n in names if phrase in n)


def _merge(cands, counts, names, min_count):
    """Fusiona clusters solapados; devuelve las etiquetas aceptadas.

    Orden `(count desc, len desc)`; se acepta un candidato salvo que ya haya una
    etiqueta aceptada L cuyo match set cubra al menos el 95 % del suyo.
    """
    kept = [c for c in cands if counts[c] >= min_count]
    match = {c: frozenset(i for i, n in enumerate(names) if c in n) for c in kept}
    kept.sort(key=lambda c: (-counts[c], -len(c)))
    accepted = []
    accepted_match = []
    for c in kept:
        mc = match[c]
        if any(len(mc & am) >= MERGE_OVERLAP * len(mc) for am in accepted_match):
            continue
        accepted.append(c)
        accepted_match.append(mc)
    return accepted


def _canonical(label, counts, cands):
    """Extension canonica: la candidata MAS LARGA que contiene a `label` y cuyo
    conteo es `>= CANON_RATIO * count(label)`.

    Colapsa el cluster `playa` / `carmen` / `del carmen` / `playa del` en un solo
    `playa del carmen` (con su conteo propio, 119, no 122).
    """
    cnt = counts[label]
    best = label
    for c in cands:
        if len(c) > len(best) and label in c and counts[c] >= CANON_RATIO * cnt:
            best = c
    return best


def _entry(label, groups, names):
    """Etiqueta final: count exacto, categoria mas frecuente y hasta 3 ejemplos."""
    idx = [i for i, n in enumerate(names) if label in n]
    cats = {}
    for i in idx:
        cat = groups[i].get("category") or "otros"
        cats[cat] = cats.get(cat, 0) + 1
    category = max(cats.items(), key=lambda kv: kv[1])[0] if cats else "otros"
    examples = [groups[i].get("name") for i in idx[:3]]
    return {"label": label, "count": len(idx), "category": category, "examples": examples}


def build_labels(groups_doc, min_count=3):
    """Funcion pura: devuelve la lista de etiquetas (sin tocar disco)."""
    groups = list(groups_doc.get("groups") or [])
    names = [norm(g.get("name") or "") for g in groups]
    cands = candidates(names)
    counts = {c: count_in(c, names) for c in cands}
    accepted = _merge(cands, counts, names, min_count)
    labels = {}
    for lab in accepted:
        best = _canonical(lab, counts, cands)
        entry = _entry(best, groups, names)
        # Si dos aceptadas colapsan a la misma etiqueta, se conserva la mayor.
        if best in labels and labels[best]["count"] >= entry["count"]:
            continue
        labels[best] = entry
    out = list(labels.values())
    out.sort(key=lambda L: (-L["count"], L["label"]))
    # La extension canonica puede crear solapes nuevos (p. ej. `bolsa` ->
    # `bolsa de trabajo`, subconjunto de `trabajo`). Se vuelve a fusionar sobre
    # las etiquetas FINALES para garantizar que ningun par se solape >= 95 %.
    final = []
    final_match = []
    for L in out:
        ms = frozenset(i for i, n in enumerate(names) if L["label"] in n)
        if any(len(ms & am) >= MERGE_OVERLAP * len(ms) for am in final_match):
            continue
        final.append(L)
        final_match.append(ms)
    return final


def write_labels(groups_path=None, out_path=None, min_count=3):
    """Entrypoint que llama el escaner. Escribe `groups_labels.json` atomico."""
    groups_path = groups_path or GROUPS
    out_path = out_path or OUT
    doc = cxtlib.load_json(groups_path, {})
    labels = build_labels(doc, min_count)
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": os.path.basename(groups_path),
        "scanned_at": doc.get("scanned_at"),
        "total_groups": len(doc.get("groups") or []),
        "min_count": min_count,
        "count": len(labels),
        "labels": labels,
    }
    cxtlib.write_json_atomic(out_path, out, indent=1)  # tmp + os.replace
    try:
        os.chmod(out_path, 0o644)
    except OSError:
        pass
    top = ", ".join("%s=%d" % (L["label"], L["count"]) for L in labels[:5])
    print("etiquetas: %d grupos -> %d labels (min %d) | top5: %s"
          % (out["total_groups"], out["count"], min_count, top))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Genera groups_labels.json desde groups.json")
    ap.add_argument("--groups", default=GROUPS, help="ruta a groups.json")
    ap.add_argument("--out", default=OUT, help="ruta de salida groups_labels.json")
    ap.add_argument("--min", type=int, default=3, dest="min_count",
                    help="conteo minimo para aceptar una etiqueta (default 3)")
    args = ap.parse_args(argv)
    write_labels(args.groups, args.out, args.min_count)


if __name__ == "__main__":
    main()
