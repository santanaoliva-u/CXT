#!/usr/bin/env python3
"""share_post.py - Comparte un post YA EXISTENTE de Facebook a varios grupos.

El MOTOR vive en shared.py (esperas por condicion, traza share_trace.jsonl).
Este archivo es la capa CLI/estado con el MISMO contrato externo que antes:
mismos flags, mismo stdout JSON y mismos ficheros share_post_status.json /
share_post.cmd.json. Todo el trabajo de navegador se delega a shared.py.

Uso:
  python3 share_post.py --ids "Nombre grupo 1,Nombre grupo 2" [--text "..."] [--delay 10] [--dry]
  python3 share_post.py --ids 123456789,987654321 --delay 0 --dry
  # --url <permalink> navega primero al post; si no, usa el post visible.
  # --dry (por defecto True): hace todo MENOS pulsar Publicar.
"""
import argparse
import json
import os
import re
import sys
import time

import cxtlib
import fastmode
import ledger
import shared

CXT = cxtlib.resolve_cxt()
TAB = cxtlib.resolve_tab(CXT)
STATUS = os.path.join(cxtlib.resolve_dir(), "share_post_status.json")


def sleep(ms):
    time.sleep(ms / 1000.0)


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --- wrappers finos: el motor real esta en shared.py -----------------------
def share_url_group(url, g, dry, text, expect=""):
    """Identifica la publicacion y la comparte al grupo `g` (motor compartido)."""
    return shared.share_target(CXT, TAB, url, g, dry, text, expect)


def close_dialog():
    return shared.close_share_dialog(CXT, TAB)


def load_groups():
    return cxtlib.load_json(os.path.join(cxtlib.resolve_dir(), "groups.json")).get("groups", [])


def select_groups(ids, filt, limit):
    if ids:
        return [g.strip() for g in ids.split(",") if g.strip()]
    kws = [cxtlib.norm(x) for x in (filt or "").split(",") if x.strip()]
    if not kws:
        return []
    out = []
    for g in load_groups():
        n = cxtlib.norm(g.get("name", ""))
        if any(k in n for k in kws):
            out.append(g.get("name") or g.get("id"))
    if limit and limit > 0:
        out = out[:limit]
    return out


# --- logica pura: ids/nombres, evidencia y contadores ----------------------
_PERMALINK_GID_RE = re.compile(r"/groups/(\d+)/")


def resolve_gids(gids, groups):
    """Resuelve `gids` a [{gid, name}] conservando EXACTAMENTE el orden pedido.

    `groups` es la lista de groups.json. Un id que no esta en el catalogo se
    devuelve con `name=None`: el llamador lo salta y lo reporta como
    `unknown`. Nunca se inventa un nombre (teclearlo seria publicar a ciegas).
    """
    by_id = {}
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip()
        if gid and gid not in by_id:
            by_id[gid] = g.get("name") or None
    out = []
    for raw in gids or []:
        gid = str(raw or "").strip()
        if not gid:
            continue
        out.append({"gid": gid, "name": by_id.get(gid)})
    return out


def _gid_for_name(name, groups):
    """gid de un nombre del camino viejo (ids/filtro); "" si no se puede probar."""
    n = (name or "").strip()
    if n.isdigit():
        return n
    tgt = cxtlib.norm(n)
    if not tgt:
        return ""
    for g in groups or []:
        if cxtlib.norm(g.get("name") or "") == tgt:
            return str(g.get("id") or "")
    return ""


def _permalink_gid(*urls):
    """gid del primer permalink con forma /groups/<gid>/...; "" si no hay."""
    for u in urls:
        m = _PERMALINK_GID_RE.search(str(u or ""))
        if m:
            return m.group(1)
    return ""


def status_entry(step, gid, name, post_key):
    """Normaliza el paso del motor a una entrada de status con evidencia.

    Puro: no toca navegador ni disco. Conserva TODAS las claves que ya traia el
    paso (el popup y autopost leen las historicas: no se renombra ni se quita
    ninguna) y agrega `gid`, `proof`, `status` y `reason`.

    Reglas:
      - `name is None` (gid fuera de groups.json) -> unknown / "gid desconocido".
      - aviso de aprobacion pendiente -> pending.
      - permalink confirmado de OTRO gid -> unknown / "gid no coincide" (jamas ok).
      - sin prueba fuerte -> unknown (nunca se asciende una duda a exito).
    """
    entry = dict(step) if isinstance(step, dict) else {}
    gid = str(gid or "").strip()
    name = name.strip() if isinstance(name, str) else name
    ok = bool(entry.get("ok"))
    proof = str(entry.get("proof") or entry.get("permalink")
                or entry.get("approval_url") or "")
    reason = str(entry.get("reason") or "")
    pending = bool(entry.get("pending")) or reason == "pendiente-aprobacion"
    low = reason.lower()
    if name is None:
        ok, status, reason = False, "unknown", "gid desconocido"
    elif pending:
        ok, status = False, "pending"
    elif ok:
        status = "ok"
    elif "bloqueado" in low or "blocked" in low:
        status = "blocked"
    elif "eliminado" in low or "remov" in low or "no-miembro" in low:
        status = "removed"
    else:
        status = "unknown"
    if status == "ok":
        pgid = _permalink_gid(entry.get("approval_url"), entry.get("permalink"),
                              proof if _PERMALINK_GID_RE.search(proof) else "")
        if pgid and gid and pgid != gid:
            ok, status, reason = False, "unknown", "gid no coincide"
    entry.update({"group": name if name is not None else entry.get("group") or "",
                  "gid": gid, "ok": ok, "proof": proof, "status": status,
                  "reason": reason, "mode": fastmode.tag()})
    return entry


def _refresh_counts(st):
    """Recuenta `unknown`/`pending` desde results (top-level del contrato)."""
    results = st.get("results") or []
    st["unknown"] = sum(1 for r in results if r.get("status") == "unknown")
    st["pending"] = sum(1 for r in results if r.get("status") == "pending")
    return st


def run_from_cmd(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    url = (cfg.get("url") or "").strip()
    text = cfg.get("text") or ""
    dry = bool(cfg.get("dry", True))
    # FAST por cmd o por entorno. Se fija ANTES de llamar al motor: shared.py
    # lee CXT_FAST en tiempo de llamada.
    fast = bool(cfg.get("fast", False)) or fastmode.enabled()
    if fast:
        os.environ["CXT_FAST"] = "1"
    if "delay" in cfg:
        delay = max(0.0, float(cfg.get("delay") or 0))
    elif fast:
        delay = float(fastmode.knobs()["inter_group_delay_s"])
    else:
        delay = 8.0
    limit = int(cfg.get("limit", 0) or 0)
    post_key = str(cfg.get("post_key") or "")
    catalogue = load_groups()
    gids = cfg.get("gids") or []
    if gids:
        # Orden EXACTO del driver; el nombre sale de groups.json (id -> name).
        plan = [(r["gid"], r["name"]) for r in resolve_gids(gids, catalogue)]
    else:
        # Camino viejo intacto: ids/filtro -> nombres -> gid (si se puede probar).
        groups = select_groups(cfg.get("ids") or "", cfg.get("filter") or "", limit)
        plan = [(_gid_for_name(g, catalogue), g) for g in groups]
    st = {"running": True, "dry": dry, "total": len(plan), "done": 0, "published": 0,
          "current": "", "results": [], "started_at": int(time.time() * 1000),
          "finished_at": 0, "unknown": 0, "pending": 0, "post_key": post_key,
          "mode": fastmode.tag()}

    lock = None
    if os.environ.get("CXT_LOCK_HELD") != "1":
        lock = ledger.lock_browser(os.path.join(cxtlib.resolve_dir(), "browser.lock"))
        if lock is None:
            st.update(running=False, finished_at=int(time.time() * 1000),
                      error="browser ocupado por otro driver")
            cxtlib.write_status(STATUS, st)
            log("=== ABORTO: browser ocupado por otro driver ===")
            return
    try:
        cxtlib.write_status(STATUS, st)
        log("=== COMPARTIR POST: %d grupos | simulacro=%s | identificar=%r ==="
            % (len(plan), dry, cfg.get("expect") or ""))
        if not plan:
            log("sin grupos (revisa el filtro o los ids)")
            st.update(running=False, error="sin grupos (revisa filter/ids)",
                      finished_at=int(time.time() * 1000))
            cxtlib.write_status(STATUS, st)
            return

        import signal
        def on_term(*_):
            st.update(running=False, current="", error="detenido",
                      finished_at=int(time.time() * 1000))
            cxtlib.write_status(STATUS, st)
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGINT, on_term)

        global TAB
        try:
            # Pestana propia en segundo plano: la extension crea/reutiliza una
            # ventana sin foco. Nunca se activa la pestana del usuario.
            TAB = cxtlib.ensure_background_tab(CXT)
            for i, (gid, name) in enumerate(plan):
                st["current"] = name or gid
                st["done"] = i
                cxtlib.write_status(STATUS, st)
                log("[%d/%d] %s (%s)" % (i + 1, len(plan), name or "-", gid or "-"))
                if name is None:
                    # Gid fuera de groups.json: no se inventa nombre ni se toca el navegador.
                    step = {}
                else:
                    step = {"group": name}
                    step.update(share_url_group(url, name, dry, text, cfg.get("expect") or ""))
                entry = status_entry(step, gid, name, post_key)
                st["results"].append(entry)
                if entry.get("status") == "ok" and not dry:
                    st["published"] = st.get("published", 0) + 1
                _refresh_counts(st)
                st["done"] = i + 1
                cxtlib.write_status(STATUS, st)
                if name is None:
                    continue
                if i < len(plan) - 1:
                    close_dialog()
                    sleep(int(delay * 1000))          # delay del usuario, intacto
            if not dry:
                # Un grupo pudo dejar el post "pendiente de aprobacion": si un admin
                # ya lo aprobo, el aviso esta en /notifications y el permalink se
                # confirma. Solo asciende `sin-prueba` (nunca baja nada).
                expect = cfg.get("expect") or ""
                needle = "" if shared.is_boilerplate_expect(expect) else expect
                needle = needle or text
                n = shared.reconcile_approvals(CXT, TAB, st["results"], needle=needle)
                if n > 0:
                    # Re-normaliza: el permalink ascendido manda (incluido su gid).
                    for r in st["results"]:
                        r.update(status_entry(r, r.get("gid", ""), r.get("group") or None,
                                              post_key))
                    st["published"] = sum(1 for r in st["results"]
                                          if r.get("status") == "ok")
                    _refresh_counts(st)
                    log("=== APROBADAS por admin: +%d publicacion(es) ===" % n)
        finally:
            _refresh_counts(st)
            st.update(running=False, current="", finished_at=int(time.time() * 1000))
            cxtlib.write_status(STATUS, st)
        log("=== FIN: %d/%d publicados ===" % (st.get("published", 0), len(plan)))
    finally:
        if lock is not None:
            ledger.unlock_browser(lock)


def main():
    global TAB
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--cmd", default="")
    ap.add_argument("--text", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--expect", default="")
    ap.add_argument("--delay", type=float, default=None)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--dry", action="store_true", default=True)
    ap.add_argument("--no-dry", dest="dry", action="store_false")
    a = ap.parse_args()
    if a.cmd:
        run_from_cmd(a.cmd)
        return
    if not a.ids:
        sys.exit("usa --ids o --cmd")
    if a.fast:
        os.environ["CXT_FAST"] = "1"
    delay = (float(fastmode.knobs()["inter_group_delay_s"]) if a.fast else 10.0) \
        if a.delay is None else a.delay
    # CLI directo: tambien trabaja en la pestana propia (sin foco).
    TAB = cxtlib.ensure_background_tab(CXT)
    groups = [g.strip() for g in a.ids.split(",") if g.strip()]
    results = []
    log("=== COMPARTIR POST: %d grupos | simulacro=%s ===" % (len(groups), a.dry))
    for i, g in enumerate(groups):
        log("[%d/%d] %s" % (i + 1, len(groups), g))
        step = {"group": g}
        step.update(share_url_group(a.url, g, a.dry, a.text, a.expect))
        results.append(step)
        if i < len(groups) - 1:
            close_dialog()
            sleep(int(max(0.0, delay) * 1000))  # delay explicito o perfil FAST
    log("=== FIN ===")
    print(json.dumps({"dry": a.dry, "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    main()
