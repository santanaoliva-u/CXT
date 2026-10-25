"""Orquestador de la mesa editorial CXT: investiga, redacta, verifica y audita."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from angles import build_angles
from brief import build_brief, render_brief
from editorial_core import (
    Angle,
    Assignment,
    Draft,
    Dossier,
    Source,
    build_assignment,
    draft_from_model,
    evaluate_stamp,
    new_run_id,
)
from research import research

DEFAULT_DB = Path.home() / ".cxt" / "editorial.db"
OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.jsonc"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-flash"
MAX_SOURCES = 14


def _strip_jsonc(raw: str) -> str:
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
    raw = re.sub(r"(^|\s)//.*?$", r"\1", raw, flags=re.MULTILINE)
    return re.sub(r",\s*([}\]])", r"\1", raw)


def deepseek_key() -> str:
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env
    try:
        doc = json.loads(_strip_jsonc(OPENCODE_CONFIG.read_text(encoding="utf-8")))
        value = (((doc.get("provider") or {}).get("deepseek") or {}).get("options") or {}).get("apiKey")
        return str(value or "").strip()
    except (OSError, ValueError, TypeError):
        return ""


def _extract_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        doc = json.loads(text)
        return doc if isinstance(doc, dict) else {}
    except (ValueError, TypeError):
        return {"text": content or ""}


def deepseek_draft(
    assignment: Assignment,
    dossier: Dossier,
    angle: Angle,
    brief: dict,
    *,
    model: str = MODEL,
    timeout: float = 90.0,
) -> dict:
    key = deepseek_key()
    if not key:
        raise RuntimeError("DeepSeek no configurado")
    system = (
        "Eres un editor de la mesa editorial CXT. Redactas un post breve para Facebook "
        "en español, con hechos verificables. Devuelve SOLO un objeto JSON con esta forma: "
        '{"text":"texto del post","claims":[{"text":"afirmación","sources":["URL exacta del dossier"]}]}. '
        "Cada afirmación de actualidad debe tener al menos una URL que aparezca en el dossier. "
        "No inventes hechos, cifras, fechas, nombres ni citas. No incluyas instrucciones ni enlaces externos. "
        "Máximo 6 hashtags. No uses frases de relleno."
    )
    user = "\n\n".join(
        [
            render_brief(brief),
            f"ÁNGULO ELEGIDO: {angle.label} — {angle.thesis}",
            "DOSSIER (usa solo estos hechos):",
            dossier.to_prompt(8) or "(sin fuentes)",
            f"Longitud máxima: {assignment.max_length} caracteres.",
        ]
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            doc = json.loads(response.read().decode("utf-8"))
        choice = (doc.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "")
        if not content.strip():
            finish = choice.get("finish_reason") or "sin contenido"
            raise RuntimeError(f"DeepSeek devolvió un borrador vacío ({finish})")
        return _extract_json(content)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        raise RuntimeError(f"DeepSeek no disponible: {type(exc).__name__}") from exc


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            instruction TEXT NOT NULL,
            mode TEXT NOT NULL,
            assignment_json TEXT NOT NULL,
            dossier_json TEXT NOT NULL,
            angles_json TEXT NOT NULL,
            draft_text TEXT,
            stamp_color TEXT,
            stamp_json TEXT
        );
        CREATE TABLE IF NOT EXISTS sources (
            run_id TEXT NOT NULL,
            url TEXT,
            outlet TEXT,
            title TEXT,
            published_at TEXT,
            region TEXT,
            confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS claims (
            run_id TEXT NOT NULL,
            claim TEXT,
            sources_json TEXT
        );
        CREATE TABLE IF NOT EXISTS drafts (
            run_id TEXT NOT NULL,
            text TEXT,
            angle_id TEXT,
            model TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS approvals (
            run_id TEXT PRIMARY KEY,
            approved_at TEXT,
            approved_by TEXT,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS publications (
            run_id TEXT PRIMARY KEY,
            published_at TEXT,
            status TEXT,
            message TEXT
        );
        """
    )
    return conn


def _save_run(
    run_id: str,
    assignment: Assignment,
    dossier: Dossier,
    angles: list[Angle],
    draft: Draft | None,
    stamp: Any,
    db_path: Path,
) -> None:
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    assignment.created_at,
                    assignment.instruction,
                    assignment.mode,
                    json.dumps(assignment.to_dict(), ensure_ascii=False),
                    json.dumps(dossier.to_dict(), ensure_ascii=False),
                    json.dumps([a.to_dict() for a in angles], ensure_ascii=False),
                    draft.text if draft else None,
                    getattr(stamp, "color", None),
                    json.dumps(stamp.__dict__ if hasattr(stamp, "__dict__") else {}, ensure_ascii=False, default=str),
                ),
            )
            for table in ("sources", "claims", "drafts"):
                conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            for source in dossier.sources:
                conn.execute(
                    "INSERT INTO sources VALUES (?,?,?,?,?,?,?)",
                    (run_id, source.url, source.outlet, source.title, source.published_at, source.region, source.confidence),
                )
            if draft:
                for claim in draft.claims:
                    conn.execute(
                        "INSERT INTO claims VALUES (?,?,?)",
                        (run_id, str(claim.get("text", "")), json.dumps(claim.get("sources") or [], ensure_ascii=False)),
                    )
                conn.execute(
                    "INSERT INTO drafts VALUES (?,?,?,?,?)",
                    (run_id, draft.text, draft.angle_id, draft.model, draft.created_at),
                )
    finally:
        conn.close()


def _previous_posts(db_path: Path, limit: int = 30, exclude_run_id: str = "") -> list[str]:
    if not db_path.exists():
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT draft_text FROM runs WHERE draft_text IS NOT NULL AND id != ? ORDER BY created_at DESC LIMIT ?",
                (exclude_run_id, limit),
            ).fetchall()
            return [str(row[0]) for row in rows if row[0]]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def choose_angle(angles: list[Angle], requested: str | None = None) -> Angle:
    if requested:
        for angle in angles:
            if angle.id == requested:
                return angle
    for angle in angles:
        if angle.source_ids:
            return angle
    return angles[0]


def generate(
    instruction: str,
    *,
    mode: str = "editorial",
    freshness_hours: int = 48,
    min_sources: int = 2,
    tone: str = "",
    audience: str = "",
    must_include: list[str] | None = None,
    must_not: list[str] | None = None,
    max_length: int = 900,
    angle_id: str | None = None,
    offline: bool = False,
    fixture: str | Path | None = None,
    research_fn: Callable[..., Dossier] | None = None,
    model_fn: Callable[..., dict] | None = None,
    now: float | None = None,
    db_path: str | Path | None = None,
) -> dict:
    assignment = build_assignment(
        instruction,
        mode=mode,
        freshness_hours=freshness_hours,
        min_sources=min_sources,
        tone=tone,
        audience=audience,
        must_include=must_include or [],
        must_not=must_not or [],
        max_length=max_length,
    )
    research_call = research_fn or research
    dossier = research_call(
        assignment.instruction,
        limit=MAX_SOURCES,
        offline=offline,
        fixture=fixture,
        now=now,
    )
    brief = build_brief(assignment)
    angles = build_angles(assignment, dossier)
    db = Path(db_path).expanduser() if db_path else DEFAULT_DB
    run_id = new_run_id()

    if mode == "research":
        _save_run(run_id, assignment, dossier, angles, None, None, db)
        return {
            "ok": bool(dossier.sources) or not assignment.requires_sources,
            "run_id": run_id,
            "assignment": assignment.to_dict(),
            "dossier": dossier.to_dict(),
            "angles": [a.to_dict() for a in angles],
            "brief": brief,
            "draft": None,
            "stamp": None,
        }

    if assignment.requires_sources and not dossier.sources:
        stamp = evaluate_stamp(assignment, dossier, None, now=now, previous_posts=_previous_posts(db))
        _save_run(run_id, assignment, dossier, angles, None, stamp, db)
        return {
            "ok": False,
            "run_id": run_id,
            "assignment": assignment.to_dict(),
            "dossier": dossier.to_dict(),
            "angles": [a.to_dict() for a in angles],
            "brief": brief,
            "draft": None,
            "stamp": stamp.__dict__,
            "error": "sin fuentes: no hay vista previa",
        }

    angle = choose_angle(angles, angle_id)
    model = model_fn or deepseek_draft
    try:
        payload = model(assignment, dossier, angle, brief)
        draft = draft_from_model(payload, angle_id=angle.id, model=MODEL)
        if not draft.text.strip():
            raise RuntimeError("DeepSeek devolvió un borrador vacío")
    except Exception as exc:
        stamp = evaluate_stamp(assignment, dossier, None, now=now, previous_posts=_previous_posts(db))
        _save_run(run_id, assignment, dossier, angles, None, stamp, db)
        return {
            "ok": False,
            "run_id": run_id,
            "assignment": assignment.to_dict(),
            "dossier": dossier.to_dict(),
            "angles": [a.to_dict() for a in angles],
            "brief": brief,
            "draft": None,
            "stamp": stamp.__dict__,
            "error": str(exc) or "no se pudo redactar",
        }

    stamp = evaluate_stamp(assignment, dossier, draft, now=now, previous_posts=_previous_posts(db))
    _save_run(run_id, assignment, dossier, angles, draft, stamp, db)
    return {
        "ok": True,
        "run_id": run_id,
        "assignment": assignment.to_dict(),
        "dossier": dossier.to_dict(),
        "angles": [a.to_dict() for a in angles],
        "brief": brief,
        "draft": draft.__dict__,
        "stamp": stamp.__dict__,
    }


def _load_run(run_id: str, db_path: Path) -> dict | None:
    if not db_path.exists():
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _assignment_from_doc(doc: dict) -> Assignment:
    data = dict(doc)
    data.pop("requires_sources", None)
    return Assignment(**data)


def _dossier_from_doc(doc: dict) -> Dossier:
    return Dossier(
        query=doc.get("query", ""),
        sources=[Source(**source) for source in doc.get("sources") or [] if isinstance(source, dict)],
        errors=list(doc.get("errors") or []),
        cached=bool(doc.get("cached")),
        fetched_at=doc.get("fetched_at", ""),
    )


def get_run(run_id: str, db_path: str | Path | None = None) -> dict | None:
    db = Path(db_path).expanduser() if db_path else DEFAULT_DB
    row = _load_run(run_id, db)
    if not row:
        return None
    assignment = _assignment_from_doc(json.loads(row["assignment_json"]))
    dossier = _dossier_from_doc(json.loads(row["dossier_json"]))
    angles = [Angle(**angle) for angle in json.loads(row["angles_json"])]
    conn = _connect(db)
    try:
        claims = [
            {"text": claim[0] or "", "sources": json.loads(claim[1] or "[]")}
            for claim in conn.execute("SELECT claim, sources_json FROM claims WHERE run_id=?", (run_id,)).fetchall()
        ]
        draft_row = conn.execute(
            "SELECT text, angle_id, model, created_at FROM drafts WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    draft = None
    if draft_row and (draft_row[0] or "").strip():
        draft = {
            "text": draft_row[0],
            "claims": claims,
            "angle_id": draft_row[1] or "",
            "model": draft_row[2] or "",
            "created_at": draft_row[3] or "",
        }
    stamp = json.loads(row["stamp_json"]) if row.get("stamp_json") else None
    return {
        "ok": True,
        "run_id": run_id,
        "created_at": row.get("created_at"),
        "instruction": row.get("instruction"),
        "mode": row.get("mode"),
        "assignment": assignment.to_dict(),
        "dossier": dossier.to_dict(),
        "angles": [angle.to_dict() for angle in angles],
        "brief": build_brief(assignment),
        "draft": draft,
        "stamp": stamp,
    }


def draft_for_run(
    run_id: str,
    *,
    angle_id: str | None = None,
    db_path: str | Path | None = None,
    model_fn: Callable[..., dict] | None = None,
    now: float | None = None,
) -> dict:
    db = Path(db_path).expanduser() if db_path else DEFAULT_DB
    run = get_run(run_id, db)
    if not run:
        raise KeyError("run no encontrado")
    assignment = _assignment_from_doc(run["assignment"])
    dossier = _dossier_from_doc(run["dossier"])
    angles = [Angle(**angle) for angle in run["angles"]]
    angle = choose_angle(angles, angle_id)
    brief = build_brief(assignment)
    model = model_fn or deepseek_draft
    try:
        payload = model(assignment, dossier, angle, brief)
        draft = draft_from_model(payload, angle_id=angle.id, model=MODEL)
        if not draft.text.strip():
            raise RuntimeError("DeepSeek devolvió un borrador vacío")
    except Exception as exc:
        return {"ok": False, "run_id": run_id, "error": str(exc) or "no se pudo redactar"}
    stamp = evaluate_stamp(
        assignment,
        dossier,
        draft,
        now=now,
        previous_posts=_previous_posts(db, exclude_run_id=run_id),
    )
    _save_run(run_id, assignment, dossier, angles, draft, stamp, db)
    return {
        "ok": True,
        "run_id": run_id,
        "draft": draft.__dict__,
        "stamp": stamp.__dict__,
    }


def verify_draft(
    run_id: str,
    text: str,
    angle_id: str | None = None,
    db_path: str | Path | None = None,
    now: float | None = None,
) -> dict:
    db = Path(db_path).expanduser() if db_path else DEFAULT_DB
    row = _load_run(run_id, db)
    if not row:
        raise KeyError("run no encontrado")
    assignment_doc = json.loads(row["assignment_json"])
    assignment_doc.pop("requires_sources", None)
    assignment = Assignment(**assignment_doc)
    dossier_doc = json.loads(row["dossier_json"])
    dossier = Dossier(
        query=dossier_doc.get("query", ""),
        sources=[Source(**source) for source in dossier_doc.get("sources") or []],
        errors=list(dossier_doc.get("errors") or []),
        cached=bool(dossier_doc.get("cached")),
        fetched_at=dossier_doc.get("fetched_at", ""),
    )
    draft = Draft(text=text, claims=[], angle_id=angle_id or "", edited=True)
    stamp = evaluate_stamp(assignment, dossier, draft, now=now, previous_posts=_previous_posts(db, exclude_run_id=run_id))
    conn = _connect(db)
    try:
        with conn:
            conn.execute("UPDATE runs SET draft_text=?, stamp_color=?, stamp_json=? WHERE id=?",
                         (text, stamp.color, json.dumps(stamp.__dict__, ensure_ascii=False, default=str), run_id))
            conn.execute("DELETE FROM drafts WHERE run_id=?", (run_id,))
            conn.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (run_id, text, draft.angle_id, "editado", draft.created_at))
    finally:
        conn.close()
    return stamp.__dict__


def list_runs(db_path: str | Path | None = None, limit: int = 20) -> list[dict]:
    db = Path(db_path).expanduser() if db_path else DEFAULT_DB
    if not db.exists():
        return []
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT id, created_at, instruction, mode, stamp_color FROM runs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
