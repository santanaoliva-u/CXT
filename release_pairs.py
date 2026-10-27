#!/usr/bin/env python3
"""Libera parejas (grupo x publicacion) marcadas `unknown` sin publicar.

Por defecto SOLO LISTA. Para escribir hace falta `--apply --reason "..."`.

Motivo: una ronda que falla antes de pulsar Publicar (menu/picker/fila/cleanup)
deja la pareja `unknown`, que el selector trata como TERMINAL: esos grupos no
reciben nunca la publicacion (caso real: ronda 0/10 = perdida total). El
veredicto `absent` no es terminal y la pareja vuelve a ser elegible.

Regla dura: si la traza de la ronda tiene un `submit`/`confirm_post` con ok, la
pareja NO se libera aunque figure como `unknown` (pudo publicarse: liberar
seria la via del duplicado). Sin trazas tampoco se libera.

Uso:
  python3 release_pairs.py --post-key fbid:122237246654329580
  python3 release_pairs.py --post-key ... --apply --reason "ronda 17:16 fallo en menu"
"""
import argparse
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import ledger as L  # noqa: E402

SEND_STEPS = ("submit", "confirm_post")
FAIL_STEPS = ("cleanup", "open_share", "group_option", "search_select", "cleanup_end")


def load_trace(path, since_ms):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if int(row.get("ts") or 0) >= since_ms:
                    rows.append(row)
    except OSError:
        return None
    return rows


def verdict(rows, last_ts, window_ms, group=""):
    if rows is None:
        return "no-liberar", "sin fichero de traza"
    want = (group or "").strip()
    near = [r for r in rows
            if abs(int(r.get("ts") or 0) - int(last_ts or 0)) <= window_ms
            and (not want or (r.get("group") or "") == want)]
    sent = [r for r in near if r.get("step") in SEND_STEPS and r.get("ok")]
    if sent:
        return "no-liberar", "hubo %s ok" % "/".join(sorted({r["step"] for r in sent}))
    if not near:
        return "no-liberar", "sin tramos de traza en la ventana"
    fails = [r for r in near if r.get("step") in FAIL_STEPS and not r.get("ok")]
    if not fails:
        return "no-liberar", "traza sin fallo previo al envio"
    return "liberable", "fallo en %s" % ",".join(sorted({r["step"] for r in fails}))


def main(argv=None):
    ap = argparse.ArgumentParser(description="libera parejas unknown sin publicar")
    ap.add_argument("--db", default=None, help="ledger.db (por defecto el de CXT_DIR)")
    ap.add_argument("--trace", default=os.path.join(_HERE, "share_trace.jsonl"))
    ap.add_argument("--post-key", default="", help="acota a una publicacion")
    ap.add_argument("--gid", action="append", default=[], help="acota a un grupo (repetible)")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--window-min", type=int, default=20, help="ventana de traza por pareja")
    ap.add_argument("--apply", action="store_true", help="escribe (sin esto solo lista)")
    ap.add_argument("--reason", default="", help="motivo que queda en proof (obligatorio con --apply)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    db = args.db or L.db_path()
    if args.apply and not args.reason.strip():
        print("falta --reason con --apply", file=sys.stderr)
        return 2

    pairs = L.unresolved(args.limit, post_key=args.post_key or None,
                         gids=args.gid or None, db=db)
    names = {gid: (v.get("name") or "") for gid, v in (L.groups_map(db) or {}).items()}
    window_ms = int(args.window_min) * 60 * 1000
    oldest = min([int(p.get("last_ts") or 0) for p in pairs] or [0]) - window_ms
    rows = load_trace(args.trace, oldest)

    out = []
    for pair in pairs:
        gid = str(pair.get("gid"))
        last_ts = int(pair.get("last_ts") or 0)
        state, why = verdict(rows, last_ts, window_ms, names.get(gid, "") or gid)
        item = {"gid": gid, "group": names.get(gid, ""), "post_key": pair.get("post_key"),
                "last_ts": last_ts, "veredicto": state, "motivo": why, "released": 0}
        if args.apply and state == "liberable":
            item["released"] = L.release(gid, pair.get("post_key"), args.reason, db=db)
        out.append(item)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for it in out:
            print("%-14s %-46s %s  %s%s" % (
                it["veredicto"], (it["group"] or it["gid"])[:46], it["post_key"],
                it["motivo"], "  (liberadas %d)" % it["released"] if it["released"] else ""))
        libre = len([i for i in out if i["veredicto"] == "liberable"])
        print("\n%d pareja(s) unknown, %d liberables, %d liberadas%s" % (
            len(out), libre, len([i for i in out if i["released"]]),
            "" if args.apply else " (simulacro: usa --apply --reason ...)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
