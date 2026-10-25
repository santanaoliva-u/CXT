#!/usr/bin/env python3
"""Test unitario (SIN navegador, SIN daemon, SIN tocar el ledger real) de ledger.py.

Cubre el contrato congelado del §2 de `work/f1_interfaces.md`:
  - `db_path()` perezosa y esquema idempotente (4 tablas + indice de batch)
  - identidad de publicacion (`post_key`: fbid vs permalink normalizado)
  - dia UTC, intencion `inflight` que no acredita y resultado que si
  - mapas de lectura (grupos / publicacion-grupo) y conteos
  - siembra de grupos ya unidos (calientes, sin pisar estado aprendido)
  - lock exclusivo del navegador y senales operativas

Toda la BD vive en un `tempfile`; jamas se escribe `~/.cxt/ledger.db`.

Uso: python3 test_ledger_unit.py   (exit 0 = PASS)
"""
import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# Antes de importar: cualquier `db_path()` implicita cae en el tempdir.
_TMP = tempfile.mkdtemp(prefix="ledger_unit_")
os.environ["CXT_DIR"] = _TMP
_REPO_DB = os.path.join(_HERE, "ledger.db")
_REPO_DB_BEFORE = os.path.exists(_REPO_DB)

import ledger as L  # noqa: E402

fails = []
HOUR = 3600 * 1000
DAY = 24 * HOUR


def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" :: " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        fails.append(name)


try:
    DB = os.path.join(_TMP, "main.db")
    GROUPS = os.path.join(_HERE, "groups.json")

    # 1. ruta perezosa y esquema -------------------------------------------
    check("db_path usa CXT_DIR (sin I/O al importar)",
          L.db_path() == os.path.join(_TMP, "ledger.db"), L.db_path())
    check("db_path no crea el archivo por si sola",
          not os.path.exists(os.path.join(_TMP, "ledger.db")))

    L.init(DB)
    L.init(DB)  # idempotente
    con = L.connect(DB)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    pg_cols = {r[1] for r in con.execute("PRAGMA table_info(post_group)")}
    con.close()
    check("esquema: 4 tablas", {"post_group", "grp", "post", "signal"} <= tables, tables)
    check("esquema: indice ix_pg_batch", "ix_pg_batch" in indexes, indexes)
    check("esquema: columna batch", "batch" in pg_cols, pg_cols)

    # 2. identidad de publicacion -----------------------------------------
    check("post_key: fbid explicito",
          L.post_key("https://www.facebook.com/groups/1/posts/9/", "123") == "fbid:123",
          L.post_key("https://www.facebook.com/groups/1/posts/9/", "123"))
    check("post_key: fbid en la query",
          L.post_key("https://www.facebook.com/photo.php?fbid=456&set=a") == "fbid:456",
          L.post_key("https://www.facebook.com/photo.php?fbid=456&set=a"))
    u1 = "https://www.facebook.com/groups/1/posts/10/?ref=share&fbclid=abc#frag"
    u2 = "https://www.facebook.com/groups/1/posts/10?fbclid=zzz&ref_src=xy&mibextid=x"
    check("post_key: tracking normalizado (misma clave)", L.post_key(u1) == L.post_key(u2),
          (L.post_key(u1), L.post_key(u2)))
    check("post_key: permalink limpio",
          L.post_key(u1) == "post:1:10", L.post_key(u1))

    # 2b. identidad endurecida (F1.1 §2.1) --------------------------------
    k_photo_a = L.post_key("https://www.facebook.com/photo/?fbid=123&set=a.9")
    k_photo_b = L.post_key("https://www.facebook.com/photo/?SET=A&FBID=123")
    check("post_key: FBID insensible a mayusculas == fbid:123",
          k_photo_a == k_photo_b == "fbid:123", (k_photo_a, k_photo_b))
    k_group = L.post_key("https://www.facebook.com/groups/1/posts/123/")
    k_perm = L.post_key("https://www.facebook.com/permalink.php?story_fbid=123&id=1")
    check("post_key: grupo y permalink clasico unificados",
          k_group == k_perm == "post:1:123", (k_group, k_perm))
    check("post_key: story_fbid/id en cualquier orden",
          L.post_key("https://www.facebook.com/permalink.php?id=1&story_fbid=123") == "post:1:123",
          L.post_key("https://www.facebook.com/permalink.php?id=1&story_fbid=123"))
    k_lo = L.post_key("https://WWW.Facebook.com/SomePage/?B=2&a=1&fbclid=zz#frag")
    k_hi = L.post_key("https://www.facebook.com/somepage/?a=1&b=2")
    check("post_key: pl: minusculas, params ordenados y sin tracking",
          k_lo == k_hi == "pl:https://www.facebook.com/somepage?a=1&b=2", (k_lo, k_hi))
    check("post_key: fbid distinto NO colisiona",
          L.post_key("https://www.facebook.com/photo/?fbid=123")
          != L.post_key("https://www.facebook.com/photo/?fbid=124"))
    check("post_key: post de grupo distinto NO colisiona",
          L.post_key("https://www.facebook.com/groups/1/posts/123/")
          != L.post_key("https://www.facebook.com/groups/1/posts/124/"))
    check("post_key: permalink distinto NO colisiona",
          L.post_key("https://www.facebook.com/permalink.php?story_fbid=123&id=1")
          != L.post_key("https://www.facebook.com/permalink.php?story_fbid=123&id=2"))
    check("post_key: conserva parametros utiles",
          L.post_key("https://a/b?mibextid=1&locale=es&x=1") == "pl:https://a/b?x=1",
          L.post_key("https://a/b?mibextid=1&locale=es&x=1"))

    # 3. dia UTC -----------------------------------------------------------
    check("utc_day(0) = epoch", L.utc_day(0) == "1970-01-01", L.utc_day(0))
    check("utc_day(1700000000000) = 2023-11-14",
          L.utc_day(1700000000000) == "2023-11-14", L.utc_day(1700000000000))
    check("utc_day(None) = hoy UTC",
          L.utc_day() == time.strftime("%Y-%m-%d", time.gmtime()), L.utc_day())

    # 4. intent: inflight sin acreditar ------------------------------------
    t0 = 1_700_000_000_000
    L.intent([{"gid": "g1", "post_key": "k1", "ts": t0},
              {"gid": "g2", "post_key": "k2", "ts": t0 + 1}], batch="b1", db=DB)
    pm = L.post_group_map(DB)
    check("intent: 2 filas", len(pm) == 2, pm)
    check("intent: status inflight", all(v["status"] == "inflight" for v in pm.values()), pm)
    check("intent: no acredita n/ok/proof",
          all(v["n"] == 0 and v["ok"] == 0 and v["proof"] == "" for v in pm.values()), pm)
    check("intent: guarda last_ts", pm[("g2", "k2")]["last_ts"] == t0 + 1, pm[("g2", "k2")])
    check("intent: guarda batch", pm[("g1", "k1")]["batch"] == "b1", pm[("g1", "k1")])
    check("intent: counts no lo cuenta como ok", L.counts(DB)["ok"] == 0, L.counts(DB))
    check("intent: inflight(batch) filtra",
          [r["gid"] for r in L.inflight("b1", DB)] == ["g1", "g2"], L.inflight("b1", DB))

    # 5. result: acredita y limpia batch -----------------------------------
    L.result("g1", "k1", "ok", 1, proof="https://www.facebook.com/groups/1/posts/10/",
             batch="b1", ts=t0 + 100, db=DB)
    pm = L.post_group_map(DB)
    row = pm[("g1", "k1")]
    check("result: n=1", row["n"] == 1, row)
    check("result: ok=1", row["ok"] == 1, row)
    check("result: guarda proof", row["proof"] == "https://www.facebook.com/groups/1/posts/10/", row)
    check("result: fija status", row["status"] == "ok", row)
    check("result: limpia batch", row["batch"] == "", row)
    check("result: sale de inflight", [r["gid"] for r in L.inflight("b1", DB)] == ["g2"],
          L.inflight("b1", DB))

    L.result("g1", "k1", "ok", 1, proof="p2", batch="b1", ts=t0 + 101, db=DB)
    check("result: incrementa n (n+1)", L.post_group_map(DB)[("g1", "k1")]["n"] == 2,
          L.post_group_map(DB)[("g1", "k1")])

    L.result("g2", "k2", "unknown", 0, ts=t0 + 2, db=DB)
    row2 = L.post_group_map(DB)[("g2", "k2")]
    check("result: unknown/ok=0 sin proof",
          row2["status"] == "unknown" and row2["ok"] == 0 and row2["proof"] == "" and row2["n"] == 1,
          row2)
    check("result: batch vacio al terminar", L.inflight("b1", DB) == [], L.inflight("b1", DB))

    # 6. inflight por batch ------------------------------------------------
    L.intent([{"gid": "g3", "post_key": "k3", "ts": t0 + 3}], batch="b2", db=DB)
    check("inflight: b2 solo trae su fila",
          [r["gid"] for r in L.inflight("b2", DB)] == ["g3"], L.inflight("b2", DB))
    check("inflight: sin batch devuelve todas",
          sorted(r["gid"] for r in L.inflight("", DB)) == ["g3"], L.inflight("", DB))

    # 7. estado de grupos --------------------------------------------------
    L.group_state("g1", name="Grupo Uno", status="blocked", status_until=t0 + 5000,
                  last_any=t0 + 9, day_n=2, day_utc="2026-10-19", db=DB)
    gm = L.groups_map(DB)
    check("groups_map: devuelve lo sembrado",
          gm["g1"]["name"] == "Grupo Uno" and gm["g1"]["status"] == "blocked"
          and gm["g1"]["status_until"] == t0 + 5000 and gm["g1"]["last_any"] == t0 + 9
          and gm["g1"]["day_n"] == 2 and gm["g1"]["day_utc"] == "2026-10-19", gm.get("g1"))
    L.group_state("g1", status="ok", db=DB)
    gm = L.groups_map(DB)
    check("group_state: actualiza solo lo pedido",
          gm["g1"]["status"] == "ok" and gm["g1"]["name"] == "Grupo Uno"
          and gm["g1"]["status_until"] == t0 + 5000, gm.get("g1"))
    L.group_state("g_new", db=DB)
    check("group_state: crea fila con defaults",
          L.groups_map(DB)["g_new"]["status"] == "ok", L.groups_map(DB).get("g_new"))

    # 7b. caducidad de cuarentena (F1.1 §2.3) ------------------------------
    EQ = os.path.join(_TMP, "quarantine.db")
    L.init(EQ)
    NOW = 1_700_000_000_000
    L.group_state("q_future", status="blocked", status_until=NOW + DAY, db=EQ)
    L.group_state("q_removed", status="removed", status_until=NOW - 1, db=EQ)
    L.group_state("q_blocked", status="blocked", status_until=NOW - 2, db=EQ)
    L.group_state("q_perm", status="blocked", status_until=0, db=EQ)
    L.group_state("q_ok", status="ok", status_until=NOW - 3, db=EQ)
    flipped = L.expire_quarantine(NOW, EQ)
    qm = L.groups_map(EQ)
    check("expire_quarantine: devuelve filas caducadas", flipped == 2, flipped)
    check("expire_quarantine: cuarentena futura NO caduca",
          qm["q_future"]["status"] == "blocked"
          and qm["q_future"]["status_until"] == NOW + DAY, qm.get("q_future"))
    check("expire_quarantine: blocked/removed caducados vuelven a ok",
          qm["q_removed"]["status"] == "ok" and qm["q_removed"]["status_until"] == 0
          and qm["q_blocked"]["status"] == "ok" and qm["q_blocked"]["status_until"] == 0,
          (qm.get("q_removed"), qm.get("q_blocked")))
    check("expire_quarantine: status_until=0 es permanente",
          qm["q_perm"]["status"] == "blocked" and qm["q_perm"]["status_until"] == 0,
          qm.get("q_perm"))
    check("expire_quarantine: no toca un grupo ya ok",
          qm["q_ok"]["status"] == "ok" and qm["q_ok"]["status_until"] == NOW - 3,
          qm.get("q_ok"))
    L.result("q_removed", "otra-publicacion", "ok", 1, ts=NOW, db=EQ)
    check("expire_quarantine: el grupo caducado sirve para otra publicacion",
          ("q_removed", "otra-publicacion") in L.post_group_map(EQ),
          L.post_group_map(EQ).get(("q_removed", "otra-publicacion")))
    check("expire_quarantine: sin caducados devuelve 0",
          L.expire_quarantine(NOW, EQ) == 0)

    # 8. publicacion fuente ------------------------------------------------
    L.note_post("fbid:999", url="https://fb/x", fbid="999", created=1234, db=DB)
    pr = L.post_row("fbid:999", DB)
    check("note_post/post_row: guarda url/fbid/created",
          pr.get("url") == "https://fb/x" and pr.get("fbid") == "999" and pr.get("created") == 1234, pr)
    L.note_post("fbid:999", db=DB)
    check("note_post: no pisa con vacios", L.post_row("fbid:999", DB).get("url") == "https://fb/x",
          L.post_row("fbid:999", DB))
    check("post_row: clave inexistente -> {}", L.post_row("nope", DB) == {})

    # 9. senales -----------------------------------------------------------
    SIG = os.path.join(_TMP, "signals.db")
    L.init(SIG)
    L.note_signal("clock_jump", "reloj -2h", ts=1, db=SIG)
    L.note_signal("diferido", "sin elegibles", ts=2, db=SIG)
    L.note_signal("blocked", "temporarily blocked", ts=3, db=SIG)
    sig = L.signals(2, SIG)
    check("signals: respeta limit", len(sig) == 2, sig)
    check("signals: mas nuevas primero", [s["kind"] for s in sig] == ["blocked", "diferido"], sig)
    check("signals: conserva detail", sig[0]["detail"] == "temporarily blocked", sig[0])

    # 10. counts -----------------------------------------------------------
    CNT = os.path.join(_TMP, "counts.db")
    L.init(CNT)
    L.intent([{"gid": "cg1", "post_key": "ck1"},
              {"gid": "cg2", "post_key": "ck2"},
              {"gid": "cg3", "post_key": "ck3"},
              {"gid": "cg4", "post_key": "ck4"},
              {"gid": "cg5", "post_key": "ck5"}], batch="cb", db=CNT)
    L.result("cg1", "ck1", "ok", 1, db=CNT)
    L.result("cg2", "ck2", "unknown", 0, db=CNT)
    L.result("cg4", "ck4", "blocked", 0, db=CNT)
    L.result("cg5", "ck5", "removed", 0, db=CNT)
    L.group_state("cg1", status="blocked", db=CNT)
    L.group_state("cg2", status="removed", db=CNT)
    c = L.counts(CNT)
    check("counts: claves completas",
          set(c) == {"ok", "unknown", "inflight", "blocked", "removed", "groups"}, c)
    check("counts: ok=1", c["ok"] == 1, c)
    check("counts: unknown=1", c["unknown"] == 1, c)
    check("counts: inflight=1", c["inflight"] == 1, c)
    check("counts: blocked=1", c["blocked"] == 1, c)
    check("counts: removed=1", c["removed"] == 1, c)
    check("counts: groups=5 (intent tambien crea estado de grupo)",
          c["groups"] == 5, c)

    # 10b. intentos del dia (F1.1 §2.2) ------------------------------------
    ATT_DB = os.path.join(_TMP, "attempted.db")
    L.init(ATT_DB)
    D0 = 1_700_000_000_000
    D0_DAY = L.utc_day(D0)
    PREV = D0 - DAY
    L.result("ag1", "ap1", "ok", 1, ts=D0 + 1000, db=ATT_DB)
    L.result("ag2", "ap2", "unknown", 0, ts=D0 + 2000, db=ATT_DB)
    L.result("ag3", "ap3", "blocked", 0, ts=D0 + 3000, db=ATT_DB)
    L.result("ag1", "ap9", "removed", 0, ts=D0 + 4000, db=ATT_DB)
    L.result("ag4", "ap4", "pending", 0, ts=D0 + 5000, db=ATT_DB)
    L.intent([{"gid": "ag6", "post_key": "ap6", "ts": D0 + 6000}], batch="ab", db=ATT_DB)
    L.result("ag7", "ap7", "ok", 1, ts=PREV + 6000, db=ATT_DB)
    at = L.attempted_today(D0_DAY, ATT_DB)
    check("attempted_today: claves completas",
          set(at) == {"ok", "unknown", "blocked", "removed", "pending", "inflight",
                      "attempted"}, at)
    check("attempted_today: cuenta por estado",
          (at["ok"], at["unknown"], at["blocked"], at["removed"], at["pending"],
           at["inflight"]) == (1, 1, 1, 1, 1, 1), at)
    check("attempted_today: attempted = gids distintos (inflight incluido)",
          at["attempted"] == 5, at)
    check("attempted_today: fila de otro dia no cuenta", at["ok"] == 1, at)
    empty = L.attempted_today("1999-01-01", ATT_DB)
    check("attempted_today: dia vacio devuelve ceros",
          len(empty) == 7 and all(v == 0 for v in empty.values()), empty)
    L.result("ag8", "ap8", "ok", 1, ts=L.now_ms(), db=ATT_DB)
    check("attempted_today: dia por defecto = hoy UTC",
          L.attempted_today(db=ATT_DB)["ok"] == 1, L.attempted_today(db=ATT_DB))

    # 10c. identidad P1 (F1.2 §P1) ----------------------------------------
    kp1 = L.post_key("https://www.facebook.com/groups/123/posts/456/")
    kp2 = L.post_key("https://www.facebook.com/groups/123/permalink/456")
    kp3 = L.post_key("https://www.facebook.com/permalink.php?story_fbid=456&id=123")
    check("post_key P1: posts == permalink == permalink.php",
          kp1 == kp2 == kp3 == "post:123:456", (kp1, kp2, kp3))
    check("post_key P1: group permalink con query/fragmento",
          L.post_key("https://www.facebook.com/groups/123/permalink/456/?ref=share#x")
          == "post:123:456",
          L.post_key("https://www.facebook.com/groups/123/permalink/456/?ref=share#x"))
    f1 = L.post_key("https://www.facebook.com/photo.php?fbid=789")
    f2 = L.post_key("https://www.facebook.com/photo.php?fbid=789&set=a.1")
    f3 = L.post_key("https://www.facebook.com/photo.php?__tn__=x&FBID=789&set=")
    check("post_key P1: fbid numerico ignora set/tracking/otros",
          f1 == f2 == f3 == "fbid:789", (f1, f2, f3))
    pf1 = L.post_key("https://www.facebook.com/photo.php?fbid=pfbid0AbC")
    pf2 = L.post_key("https://www.facebook.com/photo.php?fbid=pfbid0AbC&set=a.1")
    pf3 = L.post_key("https://www.facebook.com/photo.php?fbid=pfbid0abc")
    check("post_key P1: pfbid conserva mayusculas e ignora set",
          pf1 == pf2 == "fbid:pfbid0AbC", (pf1, pf2))
    check("post_key P1: pfbid distinto si cambia el caso", pf1 != pf3, (pf1, pf3))
    check("post_key P1: fbid explicito manda y conserva el caso",
          L.post_key("https://www.facebook.com/groups/1/posts/2/", "pfbid0AbC")
          == "fbid:pfbid0AbC",
          L.post_key("https://www.facebook.com/groups/1/posts/2/", "pfbid0AbC"))
    check("post_key P1: story_fbid sin id sigue siendo fbid",
          L.post_key("https://www.facebook.com/photo.php?story_fbid=321&set=a")
          == "fbid:321",
          L.post_key("https://www.facebook.com/photo.php?story_fbid=321&set=a"))

    # 10d. contadores de intento P2 ---------------------------------------
    GDB = os.path.join(_TMP, "groups_counter.db")
    L.init(GDB)
    T0 = 1_700_000_000_000
    T1 = T0 + 60_000
    L.intent([{"gid": "gg1", "post_key": "p1", "ts": T0},
              {"gid": "gg1", "post_key": "p2", "ts": T1},
              {"gid": "gg2", "post_key": "p3", "ts": T1}], batch="B1", db=GDB)
    gm = L.groups_map(GDB)
    check("intent P2: un intento por gid distinto en el lote",
          gm["gg1"]["day_n"] == 1 and gm["gg2"]["day_n"] == 1, gm)
    check("intent P2: last_any = max del lote y dia UTC",
          gm["gg1"]["last_any"] == T1 and gm["gg1"]["day_utc"] == L.utc_day(T1),
          gm.get("gg1"))
    L.intent([{"gid": "gg1", "post_key": "p1", "ts": T0 + 2}], batch="B1", db=GDB)
    check("intent P2: reenviar el mismo lote NO doble cuenta",
          L.groups_map(GDB)["gg1"]["day_n"] == 1, L.groups_map(GDB)["gg1"])
    L.intent([{"gid": "gg1", "post_key": "p1", "ts": T0 + 3}], batch="B2", db=GDB)
    check("intent P2: lote nuevo el mismo dia suma uno",
          L.groups_map(GDB)["gg1"]["day_n"] == 2, L.groups_map(GDB)["gg1"])
    check("intent P2: last_any no retrocede",
          L.groups_map(GDB)["gg1"]["last_any"] == T1, L.groups_map(GDB)["gg1"])
    NDAY = T0 + DAY
    L.intent([{"gid": "gg2", "post_key": "p3", "ts": NDAY}], batch="B3", db=GDB)
    check("intent P2: dia distinto reinicia day_n",
          L.groups_map(GDB)["gg2"]["day_n"] == 1
          and L.groups_map(GDB)["gg2"]["day_utc"] == L.utc_day(NDAY),
          L.groups_map(GDB)["gg2"])
    check("ensure P2: fila nueva -> 1", L.ensure_group_attempt("ge1", T0, GDB) == 1)
    check("ensure P2: mismo dia nunca incrementa",
          L.ensure_group_attempt("ge1", T1, GDB) == 1
          and L.ensure_group_attempt("ge1", T1 + 1, GDB) == 1,
          L.groups_map(GDB)["ge1"])
    L.group_state("ge2", day_utc=L.utc_day(T0), day_n=4, last_any=T0, db=GDB)
    check("ensure P2: conserva day_n existente del dia",
          L.ensure_group_attempt("ge2", T1, GDB) == 4, L.groups_map(GDB)["ge2"])
    L.group_state("ge3", day_utc="1999-01-01", day_n=9, last_any=T0, db=GDB)
    check("ensure P2: dia nuevo reinicia a 1",
          L.ensure_group_attempt("ge3", T1, GDB) == 1, L.groups_map(GDB)["ge3"])
    L.group_state("ge4", day_utc=L.utc_day(T0), day_n=0, last_any=0, db=GDB)
    check("ensure P2: mismo dia con day_n=0 garantiza 1",
          L.ensure_group_attempt("ge4", T1, GDB) == 1, L.groups_map(GDB)["ge4"])

    # 10e. sellado de ronda parcial P3 ------------------------------------
    SDB = os.path.join(_TMP, "seal.db")
    L.init(SDB)
    L.intent([{"gid": "s1", "post_key": "sp1", "ts": T0},
              {"gid": "s2", "post_key": "sp2", "ts": T0},
              {"gid": "s3", "post_key": "sp3", "ts": T0}], batch="SB1", db=SDB)
    L.intent([{"gid": "s4", "post_key": "sp4", "ts": T0}], batch="SB2", db=SDB)
    sealed = L.seal_partial("SB1", gids=["s1", "s3"], now=T1, db=SDB)
    sm = L.post_group_map(SDB)
    check("seal_partial P3: cuenta solo batch+gids indicados", sealed == 2, sealed)
    check("seal_partial P3: sella unknown terminal con proof y sin batch",
          sm[("s1", "sp1")]["status"] == "unknown"
          and sm[("s1", "sp1")]["proof"] == '{"reason":"timeout_parcial"}'
          and sm[("s1", "sp1")]["batch"] == ""
          and sm[("s3", "sp3")]["status"] == "unknown", sm)
    check("seal_partial P3: no toca gid fuera de la lista ni otro lote",
          sm[("s2", "sp2")]["status"] == "inflight"
          and sm[("s4", "sp4")]["status"] == "inflight"
          and sm[("s4", "sp4")]["batch"] == "SB2", sm)
    sigs = [s for s in L.signals(10, SDB) if s["kind"] == "timeout_parcial"]
    check("seal_partial P3: una sola senal con el conteo",
          len(sigs) == 1 and "2" in sigs[0]["detail"], sigs)
    check("seal_partial P3: sella el resto del lote (gids=None)",
          L.seal_partial("SB1", now=T1, db=SDB) == 1)
    check("seal_partial P3: ya terminal no se vuelve a sellar",
          L.seal_partial("SB1", gids=["s2"], now=T1, db=SDB) == 0)
    check("seal_partial P3: unknown no queda reseleccionable",
          L.inflight("SB1", SDB) == []
          and L.post_group_map(SDB)[("s2", "sp2")]["status"] == "unknown",
          L.post_group_map(SDB))

    # 10f. attempted_today incluye inflight (P2) --------------------------
    ADB = os.path.join(_TMP, "attempted_inflight.db")
    L.init(ADB)
    L.intent([{"gid": "ai1", "post_key": "aip1", "ts": D0}], batch="AIB", db=ADB)
    ati = L.attempted_today(D0_DAY, ADB)
    check("attempted_today P2: gid solo inflight cuenta",
          ati["attempted"] == 1 and ati["inflight"] == 1, ati)
    L.intent([{"gid": "ai2", "post_key": "aip2", "ts": D0},
              {"gid": "ai2", "post_key": "aip3", "ts": D0}], batch="AIB2", db=ADB)
    ati2 = L.attempted_today(D0_DAY, ADB)
    check("attempted_today P2: COUNT(DISTINCT gid) con parejas repetidas",
          ati2["attempted"] == 2 and ati2["inflight"] == 3, ati2)

    # 11. siembra de grupos ------------------------------------------------
    SEED = os.path.join(_TMP, "seed.db")
    L.init(SEED)
    seeded = L.seed_groups(GROUPS, SEED)
    with open(GROUPS, encoding="utf-8") as fh:
        expected = json.load(fh).get("groups", [])
    sg = L.groups_map(SEED)
    check("seed_groups: siembra 54", seeded == 54 == len(expected), (seeded, len(expected)))
    check("seed_groups: 54 filas en grp", len(sg) == 54, len(sg))
    check("seed_groups: status ok", all(v["status"] == "ok" for v in sg.values()),
          {v["status"] for v in sg.values()})
    check("seed_groups: warm_at = first_seen - 72h",
          all(v["warm_at"] == v["first_seen"] - 72 * HOUR for v in sg.values()),
          next(iter(sg.values())))
    check("seed_groups: idempotente (no resiembra)", L.seed_groups(GROUPS, SEED) == 0,
          L.seed_groups(GROUPS, SEED))

    SEED2 = os.path.join(_TMP, "seed2.db")
    L.init(SEED2)
    first = expected[0]
    L.group_state(str(first["id"]), name="NO PISAR", status="blocked", db=SEED2)
    seeded2 = L.seed_groups(GROUPS, SEED2)
    sg2 = L.groups_map(SEED2)
    check("seed_groups: no pisa fila existente", seeded2 == 53, seeded2)
    check("seed_groups: conserva nombre/estado aprendidos",
          sg2[str(first["id"])]["name"] == "NO PISAR"
          and sg2[str(first["id"])]["status"] == "blocked"
          and sg2[str(first["id"])]["warm_at"] == 0, sg2.get(str(first["id"])))

    # 12. lock del navegador ----------------------------------------------
    LOCK = os.path.join(_TMP, "browser.lock")
    fh1 = L.lock_browser(LOCK)
    check("lock: primer intento lo toma", fh1 is not None)
    fh2 = L.lock_browser(LOCK)
    check("lock: segundo intento NO bloqueante -> None", fh2 is None)
    L.unlock_browser(fh1)
    fh3 = L.lock_browser(LOCK)
    check("lock: tras liberar se puede volver a tomar", fh3 is not None)
    L.unlock_browser(fh3)
    L.unlock_browser(None)
    check("lock: unlock(None) no falla", True)

    # 13. migracion de intentos viejos ------------------------------------
    ATT = os.path.join(_TMP, "autopost_attempts.json")
    with open(ATT, "w", encoding="utf-8") as fh:
        json.dump({"https://a/p/1": {"state": "hecho"},
                   "https://a/p/2": {"state": "error", "error": "x"}}, fh)
    MIG = os.path.join(_TMP, "migrate.db")
    L.init(MIG)
    migrated = L.migrate_attempts(ATT, MIG)
    check("migrate_attempts: cuenta solo 'hecho'", migrated == 1, migrated)
    check("migrate_attempts: deja senal 'migrado'",
          [s["kind"] for s in L.signals(10, MIG)] == ["migrado"], L.signals(10, MIG))
    check("migrate_attempts: archivo inexistente -> 0",
          L.migrate_attempts(os.path.join(_TMP, "nope.json"), MIG) == 0)

    # 14. aislamiento del ledger real -------------------------------------
    check("no se creo ~/.cxt/ledger.db",
          os.path.exists(_REPO_DB) == _REPO_DB_BEFORE, _REPO_DB)
finally:
    shutil.rmtree(_TMP, ignore_errors=True)

print()
print("RESULT: " + ("PASS" if not fails else "FAIL " + ",".join(fails)))
if not fails:
    print("PASS")
sys.exit(0 if not fails else 1)
