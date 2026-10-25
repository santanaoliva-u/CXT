#!/usr/bin/env python3
"""ledger.py - libro mayor de publicaciones por grupo (SQLite, stdlib pura).

El ledger existe para que un corte a mitad de ronda no se convierta en
re-publicacion masiva: la fila `inflight` se escribe ANTES del clic y solo se
acredita con evidencia. Un JSON grande reescrito por cada grupo cuesta O(n) por
escritura y no da atomicidad; SQLite en WAL da escritura O(1), transacciones
cortas y consultas baratas para el panel.

Un solo escritor (`autopost.py`); `share_post.py` solo lee. `db_path()` es
perezosa a proposito: importar este modulo no debe tocar el disco (los tests y
el daemon lo importan en frio).

Nada de dependencias externas: `sqlite3`, `os`, `time`, `json`, `fcntl`, `re`.
"""
import fcntl
import json
import os
import re
import sqlite3
import time

# Parametros de tracking/consentimiento de Facebook: no identifican la
# publicacion. Deben coincidir EXACTAMENTE con `autopost.py:_QS_TRACK`; si
# divergen, el mismo permalink produce dos claves y se vuelve a publicar.
_QS_TRACK = frozenset({
    "ref", "ref_src", "ref_url", "mibextid", "fbclid", "gclid",
    "__cft__", "__aaid", "__hs", "hc_location", "locale",
})

# Post dentro de un grupo: `/groups/<N>/posts/<M>` o `/groups/<N>/permalink/<M>`
# (con o sin barra final y con cualquier query/fragmento detras). Se unifica
# con el permalink clasico `permalink.php?story_fbid=<M>&id=<N>` bajo la MISMA
# clave.
_GROUP_POST_RE = re.compile(
    r"/groups/(\d+)/(?:posts|permalink)/(\d+)(?:[/?#]|$)", re.IGNORECASE)
# `fbid`/`story_fbid` de la query conservando el valor TAL CUAL: un `pfbid`
# distingue mayusculas y bajarlo a minusculas abriria dos claves para la misma
# publicacion. El nombre si se compara sin mayusculas.
_FBID_QUERY_RE = re.compile(r"[?&](?:story_)?fbid=([^&#]*)", re.IGNORECASE)
_LOCK_RE = re.compile(r"\blocked\b", re.IGNORECASE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS post_group(
  gid TEXT, post_key TEXT, last_ts INTEGER, n INTEGER DEFAULT 0, ok INTEGER DEFAULT 0,
  proof TEXT DEFAULT '', status TEXT DEFAULT 'inflight', batch TEXT DEFAULT '',
  PRIMARY KEY(gid, post_key));
CREATE TABLE IF NOT EXISTS grp(
  gid TEXT PRIMARY KEY, name TEXT DEFAULT '', label TEXT DEFAULT '', first_seen INTEGER DEFAULT 0,
  warm_at INTEGER DEFAULT 0, status TEXT DEFAULT 'ok', status_until INTEGER DEFAULT 0,
  last_any INTEGER DEFAULT 0, day_utc TEXT DEFAULT '', day_n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS post(
  post_key TEXT PRIMARY KEY, url TEXT DEFAULT '', fbid TEXT DEFAULT '', created INTEGER DEFAULT 0,
  spread_hour_key TEXT DEFAULT '', spread_hour_n INTEGER DEFAULT 0,
  spread_day_key TEXT DEFAULT '', spread_day_n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS signal(ts INTEGER, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_pg_batch ON post_group(batch);
"""

# Backoff del reintento ante SQLite "locked" (50/150/400 ms). Es corto a
# proposito: una ronda no puede quedarse esperando minutos por otro proceso.
_RETRY_DELAYS = (0.05, 0.15, 0.40)


def db_path() -> str:
    """Ruta por defecto del ledger; se resuelve en cada llamada (CXT_DIR puede
    cambiar entre el arranque del daemon y los tests)."""
    import cxtlib  # import perezoso: el modulo debe poder importarse sin efectos

    return os.path.join(cxtlib.resolve_dir(), "ledger.db")


def connect(db=None) -> sqlite3.Connection:
    """Abre la BD con WAL y espera acotada; `db` admite ruta o conexion.

    `busy_timeout=2000` evita que un checkpoint de WAL haga fallar una ronda
    entera; el reintento de las escrituras cubre el resto. FK apagado porque el
    esquema es denormalizado (no hay claves foraneas declaradas).
    """
    if isinstance(db, sqlite3.Connection):
        return db
    path = db or db_path()
    conn = sqlite3.connect(path, timeout=2.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _retry(fn):
    """Reintenta 3 veces ante un 'database is locked' transitorio.

    Solo ese error se reintenta: cualquier otro `OperationalError` (esquema,
    SQL mal formado) debe salir de inmediato para no ocultar bugs reales.
    """
    for attempt in range(4):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not _LOCK_RE.search(str(exc)) or attempt == 3:
                raise
            time.sleep(_RETRY_DELAYS[attempt])
    raise AssertionError("inalcanzable")


def _conn(db):
    """Devuelve (conexion, propia). Si el llamador paso una conexion, no se
    cierra: su dueno decide."""
    if isinstance(db, sqlite3.Connection):
        return db, False
    return connect(db), True


def _write(db, fn):
    """Ejecuta `fn(conn)` dentro de una transaccion corta y reintentable."""
    conn, own = _conn(db)

    def attempt():
        try:
            conn.execute("BEGIN IMMEDIATE")
            value = fn(conn)
            conn.execute("COMMIT")
            return value
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    try:
        return _retry(attempt)
    finally:
        if own:
            conn.close()


def _read(db, fn):
    conn, own = _conn(db)
    try:
        return _retry(lambda: fn(conn))
    finally:
        if own:
            conn.close()


def init(db=None) -> None:
    """Crea el esquema de forma idempotente (seguro en cada arranque)."""
    conn, own = _conn(db)
    try:
        _retry(lambda: conn.executescript(_SCHEMA))
    finally:
        if own:
            conn.close()


def _norm_url(url: str) -> str:
    """Normaliza sin `urllib` (la lista de imports esta congelada): minusculas,
    sin fragmento, sin parametros de tracking, parametros ordenados y sin
    barras finales del path.

    Las barras finales, los parametros de tracking, el orden de la query y las
    mayusculas no cambian la publicacion; conservarlos convertiria el mismo
    permalink en dos claves distintas y reabriria el vector de re-publicacion.
    """
    u = (url or "").strip().lower().split("#", 1)[0]
    if "?" in u:
        head, query = u.split("?", 1)
        parts = [
            p for p in query.split("&")
            if p and p.split("=", 1)[0] not in _QS_TRACK
        ]
        parts.sort()
    else:
        head, parts = u, []
    head = head.rstrip("/")
    return head + ("?" + "&".join(parts) if parts else "")


def _query_pairs(url: str) -> dict:
    """Parametros de la query con el nombre en minusculas y el VALOR crudo.

    Facebook sirve el mismo permalink con `STORY_FBID`/`ID` en mayusculas; el
    nombre debe compararse normalizado, pero el valor no puede bajarse a
    minusculas porque un `pfbid` distingue mayusculas.
    """
    u = (url or "").split("#", 1)[0]
    if "?" not in u:
        return {}
    pairs = {}
    for part in u.split("?", 1)[1].split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.setdefault(name.lower(), value)
    return pairs


def post_key(url: str, fbid: str = "") -> str:
    """Identidad estable de una publicacion (precedencia P1, la primera gana):
    1. `fbid` explicito no vacio, tal cual (sin minusculas).
    2. `/groups/<N>/posts/<M>` o `/groups/<N>/permalink/<M>`.
    3. `story_fbid` + `id` numericos -> `post:<id>:<story_fbid>`.
    4. `(story_)?fbid` numerico o `pfbid...` -> `fbid:<valor>` (crudo).
    5. Permalink normalizado con prefijo `pl:`.
    """
    if fbid:
        return "fbid:" + str(fbid)
    url = url or ""
    m = _GROUP_POST_RE.search(url)
    if m:
        return "post:%s:%s" % (m.group(1), m.group(2))
    params = _query_pairs(url)
    story = params.get("story_fbid", "")
    pid = params.get("id", "")
    if story.isdigit() and pid.isdigit():
        return "post:%s:%s" % (pid, story)
    for m in _FBID_QUERY_RE.finditer(url):
        value = m.group(1)
        if value.isdigit() or value.lower().startswith("pfbid"):
            return "fbid:" + value
    return "pl:" + _norm_url(url)


def now_ms() -> int:
    """Reloj de pared en ms (el mismo que usan cola y trazas)."""
    return int(time.time() * 1000)


def utc_day(ts_ms=None) -> str:
    """Dia en UTC: el dia local se desfasa con DST y estanca los contadores."""
    secs = time.time() if ts_ms is None else int(ts_ms) / 1000.0
    return time.strftime("%Y-%m-%d", time.gmtime(secs))


# ------------------------------------------------------------ escritura

# Contadores atomicos de intento por grupo (P2). El CASE compara `day_utc`
# viejo contra el nuevo: SQLite evalua todos los SET con la fila previa.
_NOTE_ATTEMPT_SQL = (
    "INSERT INTO grp(gid, last_any, day_utc, day_n) VALUES(?, ?, ?, 1)"
    " ON CONFLICT(gid) DO UPDATE SET"
    "   day_n=CASE WHEN grp.day_utc != excluded.day_utc THEN 1"
    "              ELSE COALESCE(grp.day_n, 0) + 1 END,"
    "   day_utc=excluded.day_utc,"
    "   last_any=MAX(COALESCE(grp.last_any, 0), excluded.last_any)"
)
_ENSURE_ATTEMPT_SQL = (
    "INSERT INTO grp(gid, last_any, day_utc, day_n) VALUES(?, ?, ?, 1)"
    " ON CONFLICT(gid) DO UPDATE SET"
    "   day_n=CASE WHEN grp.day_utc != excluded.day_utc THEN 1"
    "              ELSE MAX(COALESCE(grp.day_n, 0), 1) END,"
    "   day_utc=excluded.day_utc,"
    "   last_any=MAX(COALESCE(grp.last_any, 0), excluded.last_any)"
)


def _note_attempt_conn(conn, gid, ts, idempotent=False) -> int:
    """Aplica el upsert de intento dentro de una transaccion ya abierta."""
    sql = _ENSURE_ATTEMPT_SQL if idempotent else _NOTE_ATTEMPT_SQL
    conn.execute(sql, (gid, ts, utc_day(ts)))
    row = conn.execute("SELECT day_n FROM grp WHERE gid=?", (gid,)).fetchone()
    return int(row["day_n"] or 0) if row is not None else 0


def note_group_attempt(gid, ts=None, db=None) -> int:
    """Cuenta un intento del grupo en el dia UTC (atomico) y devuelve `day_n`.

    Se llama desde `intent()`: un corte a mitad de ronda no puede dejar el
    contador del grupo gratis. Mismo dia suma 1; dia distinto reinicia a 1.
    """
    ts = now_ms() if ts is None else int(ts)

    def op(conn):
        return _note_attempt_conn(conn, gid, ts)

    return _write(db, op)


def ensure_group_attempt(gid, ts=None, db=None) -> int:
    """Garantiza `day_n >= 1` sin doble contar lo que ya conto `intent()`.

    Para reconciliacion de filas creadas por versiones anteriores: mismo dia
    nunca incrementa; dia distinto reinicia a 1.
    """
    ts = now_ms() if ts is None else int(ts)

    def op(conn):
        return _note_attempt_conn(conn, gid, ts, idempotent=True)

    return _write(db, op)


def intent(rows, batch="", db=None) -> None:
    """Marca intenciones `inflight` ANTES del clic.

    No toca `n`/`ok`/`proof`: una intencion no acredita nada. Si el proceso
    muere despues, la fila queda visible para reconciliar por evidencia.

    Ademas cuenta el intento del grupo en `grp` (P2) dentro de la MISMA
    transaccion, una sola vez por gid del lote. Reenviar el mismo lote no
    vuelve a contar: solo cuenta si la pareja es nueva o cambia su `batch`.
    """
    rows = list(rows or [])
    batch = batch or ""

    def op(conn):
        noted = {}
        for row in rows:
            gid = row.get("gid")
            post_key_value = row.get("post_key")
            ts = row.get("ts")
            ts = now_ms() if ts is None else int(ts)
            prev = conn.execute(
                "SELECT batch FROM post_group WHERE gid=? AND post_key=?",
                (gid, post_key_value)).fetchone()
            conn.execute(
                "INSERT INTO post_group(gid, post_key, last_ts, n, ok, proof, status, batch)"
                " VALUES(?, ?, ?, 0, 0, '', 'inflight', ?)"
                " ON CONFLICT(gid, post_key) DO UPDATE SET"
                "   last_ts=excluded.last_ts, status='inflight', batch=excluded.batch",
                (gid, post_key_value, ts, batch),
            )
            if gid and (prev is None or (prev["batch"] or "") != batch):
                noted[gid] = max(noted.get(gid, ts), ts)
        for gid, gts in noted.items():
            _note_attempt_conn(conn, gid, gts)

    _write(db, op)


def result(gid, post_key, status, ok, proof="", batch="", ts=None, db=None) -> None:
    """Acredita el resultado real: incrementa `n`, fija evidencia y limpia el
    batch. `status` es uno de ok|unknown|blocked|removed|pending."""
    ts = now_ms() if ts is None else int(ts)

    def op(conn):
        conn.execute(
            "INSERT INTO post_group(gid, post_key, last_ts, n, ok, proof, status, batch)"
            " VALUES(?, ?, ?, 1, ?, ?, ?, '')"
            " ON CONFLICT(gid, post_key) DO UPDATE SET"
            "   last_ts=excluded.last_ts, n=COALESCE(post_group.n, 0) + 1,"
            "   ok=excluded.ok, proof=excluded.proof, status=excluded.status, batch=''",
            (gid, post_key, ts, 1 if ok else 0, proof or "", status),
        )

    _write(db, op)


def group_state(gid, status=None, status_until=None, last_any=None, day_n=None,
                day_utc=None, name=None, db=None) -> None:
    """Upsert parcial del estado de un grupo (solo actualiza lo que se pasa)."""
    cols = []
    vals = []
    for col, value in (
        ("status", status), ("status_until", status_until), ("last_any", last_any),
        ("day_n", day_n), ("day_utc", day_utc), ("name", name),
    ):
        if value is not None:
            cols.append(col)
            vals.append(value)

    def op(conn):
        if not cols:
            conn.execute("INSERT INTO grp(gid) VALUES(?) ON CONFLICT(gid) DO NOTHING", (gid,))
            return
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join("%s=excluded.%s" % (c, c) for c in cols)
        conn.execute(
            "INSERT INTO grp(gid, %s) VALUES(?, %s)"
            " ON CONFLICT(gid) DO UPDATE SET %s" % (names, marks, updates),
            [gid] + vals,
        )

    _write(db, op)


def note_post(key, url="", fbid="", created=None, db=None) -> None:
    """Registra la publicacion fuente. No pisa url/fbid con vacios."""
    created = now_ms() if created is None else int(created)

    def op(conn):
        conn.execute(
            "INSERT INTO post(post_key, url, fbid, created) VALUES(?, ?, ?, ?)"
            " ON CONFLICT(post_key) DO UPDATE SET"
            "   url=CASE WHEN excluded.url != '' THEN excluded.url ELSE post.url END,"
            "   fbid=CASE WHEN excluded.fbid != '' THEN excluded.fbid ELSE post.fbid END,"
            "   created=CASE WHEN excluded.created > 0 THEN excluded.created ELSE post.created END",
            (key, url or "", fbid or "", created),
        )

    _write(db, op)


def expire_quarantine(now=None, db=None) -> int:
    """Devuelve a `ok` los grupos cuya cuarentena ya caduco.

    `blocked`/`removed` con `status_until` futuro siguen en cuarentena; uno con
    `status_until = 0` es permanente y no se toca. Devuelve las filas afectadas
    para que la ronda pueda dejar constancia en el log.
    """
    now = now_ms() if now is None else int(now)

    def op(conn):
        cur = conn.execute(
            "UPDATE grp SET status='ok', status_until=0"
            " WHERE status IN ('blocked','removed')"
            " AND status_until > 0 AND status_until <= ?", (now,))
        return int(cur.rowcount or 0)

    return _write(db, op)


def note_signal(kind, detail="", ts=None, db=None) -> None:
    """Anota una senal operativa (bloqueo, diferido, clock_jump...) para el
    panel y para explicar por que se paro."""
    ts = now_ms() if ts is None else int(ts)

    def op(conn):
        conn.execute("INSERT INTO signal(ts, kind, detail) VALUES(?, ?, ?)",
                     (ts, kind, detail or ""))

    _write(db, op)


def seal_partial(batch, gids=None, now=None, db=None) -> int:
    """Sella como `unknown` las filas `inflight` de una ronda parcial (P3).

    Terminal: `unknown` no vuelve a seleccionarse automaticamente, asi que los
    gids que no dieron resultado no se re-publican. Acepta `gids` para sellar
    solo los no vistos; `None` sella todo el lote. Escribe UNA senal con el
    conteo y devuelve cuantas filas sello.
    """
    now = now_ms() if now is None else int(now)
    proof = '{"reason":"timeout_parcial"}'
    gid_list = None if gids is None else [str(g) for g in gids]

    def op(conn):
        if gid_list is None:
            cur = conn.execute(
                "UPDATE post_group SET status='unknown', proof=?, batch=''"
                " WHERE batch=? AND status='inflight'", (proof, batch or ""))
        elif gid_list:
            marks = ",".join("?" for _ in gid_list)
            cur = conn.execute(
                "UPDATE post_group SET status='unknown', proof=?, batch=''"
                " WHERE batch=? AND status='inflight' AND gid IN (%s)" % marks,
                [proof, batch or ""] + gid_list)
        else:
            cur = None
        sealed = 0 if cur is None else int(cur.rowcount or 0)
        conn.execute(
            "INSERT INTO signal(ts, kind, detail) VALUES(?, 'timeout_parcial', ?)",
            (now, "sellados=%d" % sealed))
        return sealed

    return _write(db, op)


# ------------------------------------------------------------ lectura

def inflight(batch="", db=None) -> list:
    """Intenciones sin resultado. `batch` vacio = todas (reconciliacion global)."""
    def op(conn):
        if batch:
            cur = conn.execute(
                "SELECT gid, post_key, last_ts, batch FROM post_group"
                " WHERE status='inflight' AND batch=? ORDER BY last_ts ASC", (batch,))
        else:
            cur = conn.execute(
                "SELECT gid, post_key, last_ts, batch FROM post_group"
                " WHERE status='inflight' ORDER BY last_ts ASC")
        return [dict(row) for row in cur]

    return _read(db, op)


def post_group_map(db=None) -> dict:
    """Mapa (gid, post_key) -> ultimo resultado; lo usa el selector para el
    enfriamiento por publicacion."""
    def op(conn):
        cur = conn.execute(
            "SELECT gid, post_key, last_ts, n, ok, proof, status, batch FROM post_group")
        return {
            (row["gid"], row["post_key"]): {
                "last_ts": row["last_ts"], "n": row["n"], "ok": row["ok"],
                "proof": row["proof"], "status": row["status"], "batch": row["batch"],
            }
            for row in cur
        }

    return _read(db, op)


def groups_map(db=None) -> dict:
    """Mapa gid -> estado del grupo (elegibilidad, cuarentena y contador UTC)."""
    def op(conn):
        cur = conn.execute(
            "SELECT gid, name, label, first_seen, warm_at, status, status_until,"
            " last_any, day_utc, day_n FROM grp")
        return {
            row["gid"]: {
                "name": row["name"], "label": row["label"],
                "first_seen": row["first_seen"], "warm_at": row["warm_at"],
                "status": row["status"], "status_until": row["status_until"],
                "last_any": row["last_any"], "day_utc": row["day_utc"],
                "day_n": row["day_n"],
            }
            for row in cur
        }

    return _read(db, op)


def post_row(key, db=None) -> dict:
    """Fila de la publicacion fuente, o {} si aun no se registro."""
    def op(conn):
        row = conn.execute("SELECT * FROM post WHERE post_key=?", (key,)).fetchone()
        return dict(row) if row is not None else {}

    return _read(db, op)


def signals(limit=50, db=None) -> list:
    """Ultimas senales, mas nuevas primero."""
    def op(conn):
        cur = conn.execute(
            "SELECT ts, kind, detail FROM signal ORDER BY ts DESC, rowid DESC LIMIT ?",
            (int(limit),))
        return [dict(row) for row in cur]

    return _read(db, op)


def counts(db=None) -> dict:
    """Resumen para el panel; siempre devuelve todas las claves."""
    def op(conn):
        out = {"ok": 0, "unknown": 0, "inflight": 0, "blocked": 0, "removed": 0, "groups": 0}
        for row in conn.execute(
                "SELECT status, COUNT(*) AS c FROM post_group GROUP BY status"):
            if row["status"] in out:
                out[row["status"]] = int(row["c"])
        out["groups"] = int(conn.execute("SELECT COUNT(*) FROM grp").fetchone()[0])
        return out

    return _read(db, op)


def attempted_today(day=None, db=None) -> dict:
    """Intentos del dia UTC por estado; `attempted` es el gasto del tope.

    Cuenta `post_group` por `utc_day(last_ts)` (no por el status file) e
    incluye TODOS los estados, `inflight` incluido: una ronda parcial que se
    corto ya consumio el intento real. `attempted` es `COUNT(DISTINCT gid)`
    (las parejas repetidas del mismo grupo no inflan el tope). Siempre
    devuelve todas las claves; `inflight` solo se anade, no rompe al llamador.
    """
    day = utc_day() if day is None else str(day)

    def op(conn):
        out = {"ok": 0, "unknown": 0, "blocked": 0, "removed": 0, "pending": 0,
               "inflight": 0, "attempted": 0}
        for row in conn.execute(
                "SELECT status, COUNT(*) AS c FROM post_group"
                " WHERE date(last_ts / 1000, 'unixepoch') = ? GROUP BY status",
                (day,)):
            if row["status"] in out:
                out[row["status"]] = int(row["c"])
        out["attempted"] = int(conn.execute(
            "SELECT COUNT(DISTINCT gid) FROM post_group"
            " WHERE date(last_ts / 1000, 'unixepoch') = ?",
            (day,)).fetchone()[0])
        return out

    return _read(db, op)


# ------------------------------------------------------------ sesion / lock

def lock_browser(path, blocking=False, wait_s=0.0):
    """Toma el lock exclusivo del navegador. Devuelve el fh o None.

    `flock` (no un PID) evita el TOCTOU de `_pid_alive` y cubre a cualquier
    driver, no solo a los que cooperan con autopost. En modo bloqueante se
    reintenta hasta `wait_s`; nunca se espera para siempre.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.time() + max(0.0, float(wait_s))
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if not blocking or time.time() >= deadline:
                fh.close()
                return None
            time.sleep(0.05)


def unlock_browser(fh) -> None:
    """Libera y cierra el fh del lock (idempotente ante None)."""
    if fh is None:
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ------------------------------------------------------------ migracion

def seed_groups(groups_json_path, db=None) -> int:
    """Siembra los grupos ya unidos como calientes (`warm_at = first_seen - 72h`).

    Sin esto todos los grupos recien descubiertos quedarian inelegibles 72 h y
    la ronda se detendria en silencio. Solo inserta faltantes: jamas pisa un
    estado que autopost ya haya aprendido (blocked, cuarentena, contadores).
    """
    with open(groups_json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    groups = data.get("groups", []) if isinstance(data, dict) else data
    now = now_ms()
    warm = now - 72 * 3600 * 1000

    def op(conn):
        seeded = 0
        for group in groups:
            gid = str(group.get("id") or "")
            if not gid:
                continue
            cur = conn.execute(
                "INSERT INTO grp(gid, name, label, first_seen, warm_at, status)"
                " VALUES(?, ?, ?, ?, ?, 'ok') ON CONFLICT(gid) DO NOTHING",
                (gid, group.get("name") or "", group.get("category") or "", now, warm),
            )
            seeded += cur.rowcount if cur.rowcount > 0 else 0
        return seeded

    return _write(db, op)


def migrate_attempts(attempts_json_path, db=None) -> int:
    """Convierte intentos 'hecho' del JSON viejo en senales `migrado`.

    `post_group` exige gid y la clave del JSON no lo trae; inventarlo apuntaria
    el exito al grupo equivocado. Se deja constancia y se devuelve el conteo.
    """
    try:
        with open(attempts_json_path, encoding="utf-8") as fh:
            attempts = json.load(fh)
    except FileNotFoundError:
        return 0
    done = [key for key, value in attempts.items()
            if isinstance(value, dict) and value.get("state") == "hecho"]

    def op(conn):
        for key in done:
            conn.execute("INSERT INTO signal(ts, kind, detail) VALUES(?, 'migrado', ?)",
                         (now_ms(), key))
        return len(done)

    return _write(db, op)
