#!/usr/bin/env python3
"""Contratos y verificación de la mesa editorial CXT.

Este módulo no investiga ni publica: define los objetos que circulan entre
investigación, redacción y la mesa, y decide el sello con reglas deterministas.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional


FILLER_PHRASES = (
    "más que una pregunta",
    "mas que una pregunta",
    "en el mundo actual",
    "en la sociedad actual",
    "sin duda",
    "es importante destacar",
    "es fundamental recordar",
    "hoy más que nunca",
    "hoy mas que nunca",
    "debemos reflexionar",
    "una invitación a",
    "una invitacion a",
    "mantente al tanto",
    "no cabe duda",
    "en estos tiempos",
    "cabe destacar",
    "sin lugar a dudas",
    "como todos sabemos",
)

INJECTION_PATTERNS = (
    r"ignora\s+(?:todas\s+)?(?:las\s+)?instrucciones",
    r"ignore\s+(?:all\s+)?(?:previous\s+)?instructions",
    r"system\s*prompt",
    r"revela\s+(?:tu|el)\s+(?:prompt|configuraci[oó]n)",
    r"act[uú]a\s+como\s+(?:si\s+fueras|un)\s+",
)

HARM_PATTERNS = (
    r"c[oó]mo\s+(?:destruir|da[nñ]ar|matar|arruinar|acosar|humillar|manipular)\s+(?:a\s+)?(?:una?|mi|su|la|el)\s+(?:persona|perna|pareja|ex|exnovi[oa]|compa[nñ]er[oa]|familiar|vecin[oa]|jefe|jefa)",
    r"(?:destruye|da[nñ]a|mata|arruina|acosa|humilla)\s+(?:a\s+)?(?:una?|mi|su|la|el)\s+(?:persona|perna|pareja|ex|exnovi[oa]|compa[nñ]er[oa]|familiar|vecin[oa]|jefe|jefa)",
    r"plan\s+para\s+(?:destruir|da[nñ]ar|matar|arruinar|acosar|humillar)",
    r"venganza\s+contra\s+(?:una?|mi|su|la|el)\s+",
    r"hacer(?:le)?\s+da[nñ]o\s+a\s+(?:una?|mi|su|la|el)\s+",
)

NUMBER_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(%|por\s*ciento|millones?|mil|pesos?|d[oó]lares?|a[nñ]os?|meses?|d[ií]as?|horas?)?", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-záéíóúüñ]{4,}", re.IGNORECASE)

STOPWORDS = {
    "para", "como", "sobre", "desde", "hasta", "entre", "cuando", "donde",
    "porque", "aunque", "tambien", "también", "quiero", "quiere", "hacer",
    "hazme", "genera", "generar", "publica", "publicar", "escribe", "redacta",
    "texto", "post", "muro", "facebook", "noticia", "noticias", "tema", "temas",
    "esto", "esta", "este", "estos", "estas", "algo", "cosa", "cosas", "mundo",
    "actual", "actualidad", "pasando", "pasa", "segun", "según", "fuente",
    "fuentes", "investiga", "investigar", "informacion", "información",
}

HASTAG_RE = re.compile(r"#[^\s#]+")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return datetime.fromtimestamp(time.time(), tz=timezone.utc).isoformat()


def parse_time(value: Any) -> Optional[float]:
    """Devuelve epoch en segundos para ISO-8601, RFC-822 o segundos/ms numéricos."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        return n / 1000.0 if n > 1e12 else n
    text = str(value).strip()
    if not text:
        return None
    try:
        n = float(text)
        if n > 0:
            return n / 1000.0 if n > 1e12 else n
    except ValueError:
        pass
    try:
        clean = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(text: Any) -> str:
    text = normalize_text(text).lower()
    text = re.sub(r"[^a-z0-9áéíóúüñ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def plain(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def keywords(text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for token in TOKEN_RE.findall(normalize_text(text).lower()):
        if plain(token) in STOPWORDS or token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def sentence_split(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", normalize_text(text))
    return [p.strip() for p in parts if p.strip()]


def has_filler(text: str) -> list[str]:
    low = normalize_key(text)
    return [p for p in FILLER_PHRASES if p in low]


def has_injection(text: str) -> list[str]:
    low = normalize_text(text).lower()
    return [p for p in INJECTION_PATTERNS if re.search(p, low)]


def has_harm_instruction(text: str) -> bool:
    low = normalize_text(text).lower()
    return any(re.search(p, low) for p in HARM_PATTERNS)


def sanitize_for_prompt(text: str) -> str:
    """Neutraliza instrucciones incrustadas en material de terceros."""
    low = normalize_text(text)
    if not low:
        return ""
    if has_injection(low):
        return "[texto de fuente bloqueado: contenía instrucciones para el modelo]"
    return low


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_key(text).encode("utf-8")).hexdigest()[:16]


@dataclass
class Assignment:
    instruction: str
    intent: str = "research"
    source_policy: str = "reference"
    audience: str = "público general"
    locale: str = "es-MX"
    tone: str = "claro, humano y verificable"
    must_include: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    freshness_hours: int = 48
    min_sources: int = 2
    source_mix: list[str] = field(default_factory=lambda: ["nacional", "internacional"])
    max_length: int = 900
    cta: str = ""
    hashtags: list[str] = field(default_factory=list)
    mode: str = "editorial"
    local: bool = False
    focus_terms: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=iso_now)

    @property
    def requires_sources(self) -> bool:
        return self.source_policy in ("news", "reference")

    def validate(self) -> None:
        self.instruction = normalize_text(self.instruction)
        if not self.instruction:
            raise ValueError("la instrucción está vacía")
        if len(self.instruction) > 2000:
            raise ValueError("la instrucción supera 2000 caracteres")
        self.must_include = [normalize_text(x) for x in self.must_include if normalize_text(x)]
        self.must_not = [normalize_text(x) for x in self.must_not if normalize_text(x)]
        self.focus_terms = self.focus_terms or keywords(self.instruction)
        if self.freshness_hours < 1:
            self.freshness_hours = 1
        if self.min_sources < 1:
            self.min_sources = 1
        if self.max_length < 180:
            self.max_length = 180
        if self.mode not in ("editorial", "autonomous", "research", "ghost"):
            self.mode = "editorial"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["requires_sources"] = self.requires_sources
        return data


def build_assignment(
    instruction: str,
    *,
    mode: str = "editorial",
    freshness_hours: int = 48,
    min_sources: int = 2,
    tone: str = "",
    audience: str = "",
    must_include: Optional[Iterable[str]] = None,
    must_not: Optional[Iterable[str]] = None,
    max_length: int = 900,
    cta: str = "",
    hashtags: Optional[Iterable[str]] = None,
) -> Assignment:
    text = normalize_text(instruction)
    low = normalize_key(text)
    local = any(w in low for w in ("playa del carmen", "quintana roo", "solidaridad", "riviera maya"))
    promo = any(w in low for w in ("mi negocio", "mi barberia", "mi barbería", "mi restaurante", "mi tienda", "mi servicio", "mis precios", "mi marca", "mi producto"))
    explainer = any(w in low for w in ("explica", "explicame", "explícame", "psicologia", "psicología", "como funciona", "cómo funciona", "guia", "guía"))
    news = bool(re.search(r"\b(noticias?|actualidad|ultima hora|última hora|que esta pasando|qué está pasando|hoy|mundo|presidente|gobierno|elecciones|guerra|economia|economía)\b", low))
    if promo:
        policy, intent = "brief", "promotion"
    elif explainer and not news:
        policy, intent = "reference", "explainer"
    elif news:
        policy, intent = "news", "news"
    else:
        policy, intent = "reference", "research"
    a = Assignment(
        instruction=text,
        intent=intent,
        source_policy=policy,
        audience=normalize_text(audience) or "público general",
        tone=normalize_text(tone) or "claro, humano y verificable",
        must_include=list(must_include or []),
        must_not=list(must_not or []),
        freshness_hours=freshness_hours,
        min_sources=min_sources,
        source_mix=["local", "nacional", "internacional"] if local else ["nacional", "internacional"],
        max_length=max_length,
        cta=normalize_text(cta),
        hashtags=[h if h.startswith("#") else "#" + h for h in (hashtags or [])],
        mode=mode,
        local=local,
    )
    a.validate()
    return a


@dataclass
class Source:
    title: str
    outlet: str
    url: str
    published_at: str = ""
    excerpt: str = ""
    region: str = "internacional"
    language: str = "es"
    confidence: str = "media"
    claim: str = ""
    warnings: list[str] = field(default_factory=list)

    def timestamp(self) -> Optional[float]:
        return parse_time(self.published_at)

    def is_fresh(self, hours: int, now: Optional[float] = None) -> bool:
        ts = self.timestamp()
        if not ts:
            return False
        current = now if now is not None else time.time()
        return 0 <= current - ts <= hours * 3600

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dossier:
    query: str
    sources: list[Source] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cached: bool = False
    fetched_at: str = field(default_factory=iso_now)

    def dedupe(self) -> None:
        kept: list[Source] = []
        for source in self.sources:
            duplicate = False
            for prev in kept:
                if source.url and normalize_key(source.url) == normalize_key(prev.url):
                    duplicate = True
                    break
                if normalize_key(source.title) == normalize_key(prev.title):
                    duplicate = True
                    break
                if SequenceMatcher(None, normalize_key(source.title), normalize_key(prev.title)).ratio() >= 0.92:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(source)
        self.sources = kept

    def fresh_sources(self, hours: int, now: Optional[float] = None) -> list[Source]:
        return [s for s in self.sources if s.is_fresh(hours, now)]

    def independent_outlets(self) -> list[str]:
        seen: list[str] = []
        for s in self.sources:
            key = normalize_key(s.outlet)
            if key and key not in seen:
                seen.append(key)
        return seen

    def regions(self) -> list[str]:
        return sorted({normalize_key(s.region) for s in self.sources if s.region})

    def to_prompt(self, limit: int = 8) -> str:
        lines: list[str] = []
        for idx, s in enumerate(self.sources[:limit], 1):
            excerpt = sanitize_for_prompt(s.excerpt)[:500]
            lines.append(
                f"[S{idx}] {sanitize_for_prompt(s.title)} | {sanitize_for_prompt(s.outlet)} | "
                f"{s.published_at} | {s.url} | {excerpt}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "sources": [s.to_dict() for s in self.sources],
            "errors": list(self.errors),
            "cached": self.cached,
            "fetched_at": self.fetched_at,
            "fresh": len(self.fresh_sources(48)),
            "outlets": self.independent_outlets(),
            "regions": self.regions(),
        }


@dataclass
class Angle:
    id: str
    label: str
    thesis: str
    source_ids: list[str] = field(default_factory=list)
    why_now: str = ""
    warning: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Draft:
    text: str
    claims: list[dict] = field(default_factory=list)
    angle_id: str = ""
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    created_at: str = field(default_factory=iso_now)
    edited: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Stamp:
    color: str
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, dict] = field(default_factory=dict)
    blocked: bool = True
    publishable: bool = False
    requires_review: bool = True
    created_at: str = field(default_factory=iso_now)

    def to_dict(self) -> dict:
        return asdict(self)


def _check(ok: bool, detail: str = "") -> dict:
    return {"ok": bool(ok), "detail": detail}


def _source_text(dossier: Dossier) -> str:
    return normalize_key(" ".join((s.title + " " + s.excerpt + " " + s.claim) for s in dossier.sources))


def _unsupported_numbers(draft: str, dossier: Dossier, extra: str = "") -> list[str]:
    allowed_text = _source_text(dossier)
    if extra:
        allowed_text += " " + normalize_key(extra)
    allowed = {m.group(1).replace(",", ".") for m in NUMBER_RE.finditer(allowed_text)}
    bad: list[str] = []
    for match in NUMBER_RE.finditer(draft):
        value = match.group(1).replace(",", ".")
        if value and value not in allowed:
            bad.append(match.group(0).strip())
    return bad[:6]


def _duplicate_of(draft_text: str, previous: Optional[Iterable[str]]) -> Optional[str]:
    current = normalize_key(draft_text)
    for old in previous or []:
        old_key = normalize_key(old)
        if old_key and SequenceMatcher(None, current, old_key).ratio() >= 0.88:
            return old[:120]
    return None


def evaluate_stamp(
    assignment: Assignment,
    dossier: Dossier,
    draft: Optional[Draft],
    *,
    now: Optional[float] = None,
    previous_posts: Optional[Iterable[str]] = None,
) -> Stamp:
    """Sello editorial determinista. Sin fuentes no hay verde en actualidad."""
    now = now if now is not None else time.time()
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    harm = has_harm_instruction(assignment.instruction)
    checks["safety_instruction"] = _check(not harm, "instrucción para dañar a una persona" if harm else "")
    if not checks["safety_instruction"]["ok"]:
        reasons.append("La instrucción pide dañar a una persona; reformúlala como análisis o tema educativo.")

    if not draft or not normalize_text(draft.text):
        checks["draft"] = _check(False, "sin borrador")
        reasons.append("Sin borrador no hay sello.")
        return Stamp("red", reasons, checks, True, False, True)

    text = normalize_text(draft.text)
    checks["draft"] = _check(True, "")

    fresh = dossier.fresh_sources(assignment.freshness_hours, now)
    outlets = dossier.independent_outlets()
    checks["sources_present"] = _check(bool(dossier.sources), f"{len(dossier.sources)} fuentes")
    checks["freshness"] = _check(bool(fresh), f"{len(fresh)} fuentes dentro de {assignment.freshness_hours} h")
    checks["independent_outlets"] = _check(len(outlets) >= 2, f"{len(outlets)} medios: {', '.join(outlets[:4])}")

    if assignment.requires_sources:
        if not dossier.sources:
            reasons.append("No hay fuentes: la mesa no redacta actualidad sin evidencia.")
        if assignment.source_policy == "news" and not fresh:
            reasons.append("No hay fuentes frescas para un tema de actualidad.")
        if len(dossier.sources) < assignment.min_sources and dossier.sources:
            reasons.append(f"Solo hay {len(dossier.sources)} fuentes; el mínimo es {assignment.min_sources}.")
        if len(outlets) < 2 and dossier.sources:
            reasons.append("Las fuentes no son independientes: se necesitan al menos dos medios.")

    low = normalize_key(text)
    fillers = has_filler(text)
    checks["no_filler"] = _check(not fillers, ", ".join(fillers))
    if fillers:
        reasons.append("El borrador usa frases de relleno: " + ", ".join(fillers[:3]) + ".")

    missing = [x for x in assignment.must_include if normalize_key(x) not in low]
    checks["must_include"] = _check(not missing, ", ".join(missing))
    if missing:
        reasons.append("Falta cumplir la instrucción: " + ", ".join(missing) + ".")

    forbidden = [x for x in assignment.must_not if normalize_key(x) in low]
    checks["must_not"] = _check(not forbidden, ", ".join(forbidden))
    if forbidden:
        reasons.append("El borrador incluye algo prohibido: " + ", ".join(forbidden) + ".")

    focus = assignment.focus_terms[:5]
    focus_hit = [x for x in focus if normalize_key(x) in low]
    checks["instruction_focus"] = _check(not focus or bool(focus_hit), ", ".join(focus_hit))
    if focus and not focus_hit:
        reasons.append("El borrador no aborda el tema pedido.")

    claims = draft.claims if isinstance(draft.claims, list) else []
    if assignment.requires_sources:
        bad_claims = []
        valid_urls = {normalize_key(s.url) for s in dossier.sources if s.url}
        valid_ids = {f"s{idx}" for idx in range(1, len(dossier.sources) + 1)}
        for claim in claims:
            if not isinstance(claim, dict):
                bad_claims.append("claim no estructurado")
                continue
            srcs = claim.get("sources") or claim.get("source_ids") or []
            if isinstance(srcs, str):
                srcs = [srcs]
            if not isinstance(srcs, list) or not srcs:
                bad_claims.append(str(claim.get("text", ""))[:80] or "claim sin fuente")
                continue
            unknown = [
                str(src)
                for src in srcs
                if normalize_key(str(src)) not in valid_urls and normalize_key(str(src)) not in valid_ids
            ]
            if unknown:
                bad_claims.append("fuente fuera del dossier: " + ", ".join(unknown[:2]))
        checks["claims_sourced"] = _check(not bad_claims, "; ".join(bad_claims[:3]))
        if bad_claims:
            reasons.append("Hay afirmaciones sin fuente verificable en el borrador.")
        if not claims:
            checks["claims_sourced"] = _check(False, "sin claims")
            reasons.append("El borrador no declara de qué fuente sale cada afirmación.")
    else:
        checks["claims_sourced"] = _check(True, "brief sin fuentes externas")

    brief_facts = "" if assignment.requires_sources else (assignment.instruction + " " + " ".join(assignment.must_include))
    numbers = _unsupported_numbers(text, dossier, brief_facts)
    checks["numbers_grounded"] = _check(not numbers, ", ".join(numbers))
    if numbers:
        reasons.append("Cifras o datos que no aparecen en las fuentes: " + ", ".join(numbers) + ".")

    injection = has_injection(text)
    checks["prompt_injection"] = _check(not injection, "; ".join(injection[:2]))
    if injection:
        reasons.append("El borrador contiene instrucciones incrustadas; se bloquea.")

    duplicate = _duplicate_of(text, previous_posts)
    checks["duplicate"] = _check(not duplicate, duplicate or "")
    if duplicate:
        reasons.append("El borrador se parece demasiado a una publicación anterior.")

    hashtags = HASTAG_RE.findall(text)
    checks["format"] = _check(
        len(text) <= max(assignment.max_length * 1.25, 240) and len(hashtags) <= 6,
        f"{len(text)} caracteres, {len(hashtags)} hashtags",
    )
    if not checks["format"]["ok"]:
        reasons.append("Formato fuera de rango: demasiado largo o demasiados hashtags.")

    edited = bool(getattr(draft, "edited", False))
    checks["not_edited"] = _check(not edited, "borrador editado" if edited else "sin editar")
    if edited:
        reasons.append("El borrador fue editado: vuelve a generar o re-verifica los claims antes de publicar.")

    hard_fail = (
        not checks["safety_instruction"]["ok"]
        or not checks["no_filler"]["ok"]
        or not checks["must_include"]["ok"]
        or not checks["must_not"]["ok"]
        or not checks["instruction_focus"]["ok"]
        or not checks["claims_sourced"]["ok"]
        or not checks["numbers_grounded"]["ok"]
        or not checks["prompt_injection"]["ok"]
        or not checks["duplicate"]["ok"]
        or (assignment.requires_sources and not dossier.sources)
        or (assignment.source_policy == "news" and not fresh)
    )
    soft_fail = (
        (assignment.requires_sources and not checks["independent_outlets"]["ok"])
        or (assignment.requires_sources and len(dossier.sources) < assignment.min_sources)
        or not checks["format"]["ok"]
        or not checks["not_edited"]["ok"]
    )
    if hard_fail:
        color = "red"
    elif soft_fail:
        color = "amber"
    else:
        color = "green"
    if not reasons:
        reasons.append("Fuentes, instrucción, claims y formato pasan la verificación.")
    return Stamp(
        color=color,
        reasons=reasons,
        checks=checks,
        blocked=color == "red",
        publishable=color == "green",
        requires_review=color != "green",
    )


def draft_from_model(payload: dict, *, angle_id: str, model: str = "deepseek-flash") -> Draft:
    text = normalize_text(payload.get("text") if isinstance(payload, dict) else "")
    claims = payload.get("claims") if isinstance(payload, dict) else []
    if not isinstance(claims, list):
        claims = []
    clean: list[dict] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        ctext = normalize_text(claim.get("text"))
        srcs = claim.get("sources") or claim.get("source_ids") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        if ctext:
            clean.append({"text": ctext, "sources": [str(s) for s in srcs]})
    return Draft(text=text, claims=clean, angle_id=angle_id, model=model)


def audit_row(run_id: str, assignment: Assignment, dossier: Dossier, draft: Optional[Draft], stamp: Stamp, angles: list[Angle]) -> dict:
    return {
        "run_id": run_id,
        "created_at": iso_now(),
        "instruction": assignment.instruction,
        "mode": assignment.mode,
        "assignment": assignment.to_dict(),
        "dossier": dossier.to_dict(),
        "angles": [a.to_dict() for a in angles],
        "draft": draft.to_dict() if draft else None,
        "stamp": stamp.to_dict(),
    }


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]
