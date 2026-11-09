"""
ShadowAI Tension & Contradiction Engine v2
============================================
Detects, classifies and stores tensions between agent outputs, memory and goals.

Six conflict types (replaces the single "sprzeczność" bucket):
  factual_contradiction  — real negation between facts/replies
  priority_disagreement  — conflict between agent priorities / goals
  risk_disagreement      — two agents assess the same risk differently
  uncertainty            — model flagged missing / conflicting data (not a contradiction)
  insufficient_evidence  — evidence too thin to decide
  strategic_tension      — speed vs quality, shortcut vs long-term goal

Improvements over v1:
  - fine-grained conflict_type instead of 4 overloaded types
  - continuous confidence_score (0.0-1.0) with human-readable reason
  - claim_a / claim_b fields for UI diffing
  - deduplication via signature hash (no repeated identical tensions)
  - clean Polish descriptions — no raw JSON slugs
  - resolution_status mirrors status but is more granular for UI
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.config import settings

logger = logging.getLogger("shadowai.tension")

_DB = settings.db_path

# ── Stopwords dla analizy tematycznej ──────────────────────────────────────────
_STOP_WORDS = frozenset({
    "dlaczego", "kiedy", "gdzie", "jaki", "jakie", "który", "która", "które",
    "bardzo", "tylko", "może", "proszę", "tutaj", "teraz", "zawsze", "wtedy",
    "nigdy", "często", "wiele", "trochę", "dobrze", "żeby", "jeśli", "jeżeli",
    "jest", "tego", "sobie", "ciebie", "tobie", "mnie", "chcę", "chce", "masz"
})

# ── Conflict types ─────────────────────────────────────────────────────────────

CONFLICT_TYPES = (
    "factual_contradiction",   # real logical negation of a fact
    "priority_disagreement",   # agents/memory disagree on what matters most
    "risk_disagreement",       # two sources give conflicting risk assessments
    "uncertainty",             # model unsure — not a contradiction, just a signal
    "insufficient_evidence",   # not enough info to decide
    "strategic_tension",       # speed vs quality, shortcut vs long-term goal
)

# Legacy broad types kept for backward compat (UI filters)
_TYPE_MAP: dict[str, str] = {
    "factual_contradiction":  "memory_vs_evidence",
    "priority_disagreement":  "preference_vs_goal",
    "risk_disagreement":      "agent_disagreement",
    "uncertainty":            "memory_vs_evidence",
    "insufficient_evidence":  "memory_vs_evidence",
    "strategic_tension":      "speed_vs_accuracy",
}

# ── Pattern banks ──────────────────────────────────────────────────────────────

_NEGATION_PAIRS: list[tuple[re.Pattern, re.Pattern]] = [
    (re.compile(r"\btak\b|\byes\b|\bprawda\b|\btrue\b",      re.I),
     re.compile(r"\bnie\b|\bno\b|\bfałsz\b|\bfalse\b",       re.I)),
    (re.compile(r"\bdostępny\b|\baktywn|\bactiv",             re.I),
     re.compile(r"\bniedostępny\b|\boffline\b|\bniedostępny", re.I)),
    (re.compile(r"\bdziała\b|\bworks\b|\bOK\b|\bsprawny",     re.I),
     re.compile(r"\bnie działa\b|\bfail\b|\berror\b|\bcrash", re.I)),
    (re.compile(r"\bwysok\b|\bhigh\b|\bdużo\b|\bwięcej",      re.I),
     re.compile(r"\bniski\b|\blow\b|\bmało\b|\bmniej",         re.I)),
    (re.compile(r"\bbezpieczn\b|\bsafe\b|\bok\b",             re.I),
     re.compile(r"\bryzyko\b|\bunsafe\b|\bdanger",            re.I)),
    (re.compile(r"\bzalecan\b|\brecommend\b|\bnajlepiej",     re.I),
     re.compile(r"\bodradzam\b|\bunikaj\b|\bnot recommended", re.I)),
]

_RISK_WORDS = re.compile(
    r"\bryzyko\b|\bzagrożen|\bnebezpiecz|\brisk\b|\bhazard\b|\bniebezpiecz", re.I
)

_UNCERTAINTY_SOURCES_SIGNAL = frozenset({
    "conflicting_info", "missing_data", "weak_reasoning",
    "insufficient_context", "low_confidence",
})

_SPEED_MARKERS = frozenset({"critical", "high"})
_CAREFUL_STRATS = frozenset({"research_first", "hypothesis_testing", "step_by_step"})

#: Ile CAŁYCH słów (≥4 znaki, bez stop-words) musi się pokryć między korektą operatora
#: a komendą, żeby uznać, że korekta dotyczy TEJ komendy.
#:
#: Operator 2026-08-01 („naprawiaj”): stała była UŻYWANA w `_detect_priority_disagreement`
#: (linia ~367), ale NIGDY nigdzie nie zdefiniowana — `git log -S` nie zna jej z żadnego
#: commita, więc weszła niezacommitowaną zmianą 26.07. Skutek był większy niż jeden zepsuty
#: detektor: `NameError` leci w DRUGIM z sześciu detektorów w `detect_and_store`, więc
#: przerywał całą funkcję — cztery kolejne (risk/uncertainty/evidence/strategic) nie
#: wykonywały się wcale i ŻADNE napięcie nie trafiało do bazy. I to dokładnie w przypadku,
#: który miał być najcenniejszy: gdy operator ma zapisaną korektę pasującą do komendy.
#:
#: Wartość 2, nie 1: przy jednym wspólnym słowie („publikacja”, „montaż”) każda korekta
#: pasowałaby do każdej komendy z tej dziedziny i heartbeat mieliłby napięcia bez treści —
#: ten sam błąd klasy co stopword-fix w tool_registry ([[project_tool_registry_stopword_fix]]).
_MIN_CORRECTION_OVERLAP = 2


# ── Data structure ─────────────────────────────────────────────────────────────

@dataclass
class Tension:
    id:                str  = field(default_factory=lambda: str(uuid.uuid4()))
    conflict_type:     str  = "uncertainty"
    type:              str  = "memory_vs_evidence"   # legacy broad type
    severity:          str  = "low"
    confidence_score:  float = 0.5
    confidence_reason: str  = ""
    source_a:          str  = ""
    source_b:          str  = ""
    claim_a:           str  = ""
    claim_b:           str  = ""
    description:       str  = ""
    evidence:          dict = field(default_factory=dict)
    status:            str  = "open"
    resolution_status: str  = "open"
    session_id:        str  = "global"
    signature:         str  = ""
    created_at:        str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        self.type = _TYPE_MAP.get(self.conflict_type, "memory_vs_evidence")
        if not self.signature:
            self.signature = _sig(self.source_a, self.source_b, self.conflict_type, self.claim_a[:40])

    def to_dict(self) -> dict:
        return asdict(self)


# ── Context passed by caller ───────────────────────────────────────────────────

@dataclass
class TensionContext:
    cmd:                  str
    reply:                str
    action:               str | None
    confidence:           float
    uncertainty_sources:  list[str]
    reasoning_strategy:   str
    priority:             str
    session_id:           str       = "global"
    recent_workspace:     list[dict] = field(default_factory=list)
    operator_memory:      list[dict] = field(default_factory=list)


# ── Core engine ────────────────────────────────────────────────────────────────

class TensionEngine:

    @staticmethod
    async def detect_and_store(ctx: TensionContext) -> list[Tension]:
        """Detect all tensions from ctx, persist new ones (with dedup), return list."""
        tensions: list[Tension] = []
        tensions += _detect_factual_contradiction(ctx)
        tensions += _detect_priority_disagreement(ctx)
        tensions += _detect_risk_disagreement(ctx)
        tensions += _detect_uncertainty(ctx)
        tensions += _detect_insufficient_evidence(ctx)
        tensions += _detect_strategic_tension(ctx)

        new = await _store_deduped(tensions)
        if new:
            logger.info("[tension] stored %d new tension(s): %s", len(new), [t.conflict_type for t in new])
        return new

    @staticmethod
    async def list_open(session_id: str = "global", limit: int = 50) -> list[dict]:
        await _ensure_table()
        async with aiosqlite.connect(_DB) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM tensions WHERE session_id=? AND status='open' "
                "ORDER BY confidence_score DESC, created_at DESC LIMIT ?",
                (session_id, limit),
            )).fetchall()
        return _rows_to_dicts(rows)

    @staticmethod
    async def list_all(limit: int = 100) -> list[dict]:
        await _ensure_table()
        async with aiosqlite.connect(_DB) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM tensions ORDER BY confidence_score DESC, created_at DESC LIMIT ?",
                (limit,)
            )).fetchall()
        return _rows_to_dicts(rows)

    @staticmethod
    async def list_by_type(conflict_type: str, limit: int = 50) -> list[dict]:
        await _ensure_table()
        async with aiosqlite.connect(_DB) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM tensions WHERE conflict_type=? "
                "ORDER BY confidence_score DESC, created_at DESC LIMIT ?",
                (conflict_type, limit),
            )).fetchall()
        return _rows_to_dicts(rows)

    @staticmethod
    async def resolve(tension_id: str, resolution: str) -> bool:
        await _ensure_table()
        ts = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(_DB) as db:
            res = await db.execute(
                "UPDATE tensions SET status='resolved', resolution_status='resolved', "
                "resolved_at=?, resolution=? WHERE id=?",
                (ts, resolution, tension_id),
            )
            await db.commit()
        return res.rowcount > 0

    @staticmethod
    async def ignore(tension_id: str) -> bool:
        await _ensure_table()
        async with aiosqlite.connect(_DB) as db:
            res = await db.execute(
                "UPDATE tensions SET status='ignored', resolution_status='false_positive' "
                "WHERE id=?", (tension_id,)
            )
            await db.commit()
        return res.rowcount > 0

    @staticmethod
    async def check_blocking(
        action: str | None,
        cmd: str,
        session_id: str = "global",
        threshold: float = 0.70,
    ) -> "Tension | None":
        """
        Pre-execution check: return the first open HIGH-severity tension
        whose description overlaps with cmd and whose confidence_score >= threshold.

        Threshold = 0.70 matches _score_to_severity "high" boundary.
        Returns None when execution is clear to proceed.

        Usage in agent.py (before action dispatch, after constitution):
            blocker = await TensionEngine.check_blocking(action, cmd)
            if blocker:
                return soft_block_response(blocker)
        """
        await _ensure_table()
        async with aiosqlite.connect(_DB) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM tensions "
                "WHERE session_id=? AND status='open' "
                "  AND confidence_score >= ? "
                "  AND severity='high' "
                "  AND conflict_type IN ('factual_contradiction','priority_disagreement','strategic_tension') "
                "ORDER BY confidence_score DESC LIMIT 10",
                (session_id, threshold),
            )).fetchall()

        if not rows:
            return None

        action_str = (action or "").lower()

        for row in rows:
            d = dict(row)
            # Napięcia o spójności odpowiedzi (factual_contradiction z porównania
            # dwóch odpowiedzi) nie dotyczą akcji — nigdy nie blokują.
            if (d.get("description") or "").startswith("Nowa odpowiedź przeczy poprzedniemu wpisowi"):
                continue
            # Samo pokrycie słów z opisem NIE blokuje — wyłącznie dopasowanie akcji.
            action_match = bool(action_str) and (
                action_str in (d.get("source_a") or "") or action_str in (d.get("source_b") or "")
            )
            if action_match:
                payload = {
                    key: value
                    for key, value in d.items()
                    if key in Tension.__dataclass_fields__
                }
                evidence = payload.get("evidence")
                if isinstance(evidence, str):
                    try:
                        payload["evidence"] = json.loads(evidence)
                    except Exception:
                        payload["evidence"] = {"raw": evidence}
                return Tension(**payload)

        return None

    @staticmethod
    async def summary() -> dict:
        """Return count per conflict_type for dashboard widgets."""
        await _ensure_table()
        async with aiosqlite.connect(_DB) as db:
            rows = await (await db.execute(
                "SELECT conflict_type, COUNT(*) as cnt, AVG(confidence_score) as avg_conf "
                "FROM tensions WHERE status='open' GROUP BY conflict_type"
            )).fetchall()
        return {r[0]: {"count": r[1], "avg_confidence": round(r[2] or 0, 3)} for r in rows}


# ── Detectors ──────────────────────────────────────────────────────────────────

def _detect_factual_contradiction(ctx: TensionContext) -> list[Tension]:
    """Real logical negation between new reply and a recent workspace entry."""
    out: list[Tension] = []

    for entry in ctx.recent_workspace[-8:]:
        prev: str = entry.get("content", "")
        if not prev or entry.get("type") == "introspection":
            continue
        if not _topic_overlap(ctx.cmd, prev):
            continue

        for pat_pos, pat_neg in _NEGATION_PAIRS:
            in_new  = pat_pos.search(ctx.reply) and pat_neg.search(prev)
            in_prev = pat_pos.search(prev) and pat_neg.search(ctx.reply)
            if not (in_new or in_prev):
                continue

            score, reason = _contradiction_score(ctx.confidence, ctx.priority, ctx.recent_workspace)
            claim_a = _clip(prev, 120)
            claim_b = _clip(ctx.reply, 120)

            out.append(Tension(
                conflict_type="factual_contradiction",
                severity=_score_to_severity(score),
                confidence_score=score,
                confidence_reason=reason,
                source_a=f"workspace:{entry.get('id','?')[:8]}",
                source_b=f"agent:{ctx.reply[:20]}",
                claim_a=claim_a,
                claim_b=claim_b,
                description=(
                    "Nowa odpowiedź przeczy poprzedniemu wpisowi. "
                    f"Poprzednio: <<{_clip(prev, 60)}>>. "
                    f"Teraz: <<{_clip(ctx.reply, 60)}>>."
                ),
                evidence={
                    "previous": prev[:200],
                    "current":  ctx.reply[:200],
                    "pattern":  pat_pos.pattern,
                },
                session_id=ctx.session_id,
            ))
            break

    return out


def _detect_priority_disagreement(ctx: TensionContext) -> list[Tension]:
    """Memory correction says do X carefully — agent did Y without care."""
    out: list[Tension] = []

    for mem in ctx.operator_memory:
        if mem.get("layer") != "correction":
            continue
        raw_val: Any = mem.get("value", "")
        val = _parse_mem_value(raw_val)   # clean string, no JSON slugs
        if not val:
            continue

        # Napiecie przeciw BRAKOWI akcji nie ma tresci — a truje petle heartbeatu
        # (zamknieta klasa: korekta vs "brak akcji" -> mozg mieli te sama konkluzje w kolko).
        if not ctx.action:
            continue

        kws = {w.lower() for w in re.findall(r"\w{4,}", val) if w.lower() not in _STOP_WORDS}
        cmd_tokens = {w.lower() for w in re.findall(r"\w{4,}", ctx.cmd) if w.lower() not in _STOP_WORDS}
        overlap = sorted(kws & cmd_tokens)   # CALE slowa, nie podciagi ("stan" in "instancja")
        if len(overlap) < _MIN_CORRECTION_OVERLAP:
            continue
        if "verify" in ctx.action or "analiz" in ctx.action:
            continue

        action_label = ctx.action
        score = 0.35 if ctx.confidence > 0.6 else 0.55
        out.append(Tension(
            conflict_type="priority_disagreement",
            severity=_score_to_severity(score),
            confidence_score=score,
            confidence_reason=(
                f"Korekta operatora pokrywa się z tematem ({', '.join(overlap[:3])}), "
                f"ale akcja '{action_label}' jej nie uwzględnia."
            ),
            source_a=f"korekta:{mem.get('id','?')[:8]}",
            source_b=f"akcja:{action_label}",
            claim_a=_clip(val, 120),
            claim_b=f"Wykonana akcja: {action_label}",
            description=(
                f"Zapamiętana korekta sugeruje ostroznosc przy <<{_clip(val, 60)}>>. "
                f"Akcja <<{action_label}>> moze to pomijac."
            ),
            evidence={"correction": val[:200], "action": action_label, "overlap": overlap},
            session_id=ctx.session_id,
        ))
        break  # one tension per correction entry

    return out


def _detect_risk_disagreement(ctx: TensionContext) -> list[Tension]:
    """Two recent workspace entries by different agents assess same risk differently."""
    out: list[Tension] = []

    if ctx.reasoning_strategy != "multi_agent_debate":
        return out

    agent_entries = [
        e for e in ctx.recent_workspace[-10:]
        if e.get("type") == "agent_response" and _RISK_WORDS.search(e.get("content", ""))
    ]
    if len(agent_entries) < 2:
        return out

    a, b = agent_entries[0], agent_entries[1]
    if a.get("agent_id") == b.get("agent_id"):
        return out

    for pat_pos, pat_neg in _NEGATION_PAIRS:
        ca, cb = a.get("content", ""), b.get("content", "")
        if pat_pos.search(ca) and pat_neg.search(cb):
            diff = abs((a.get("score") or 0.5) - (b.get("score") or 0.5))
            score = min(0.4 + diff, 0.85)
            out.append(Tension(
                conflict_type="risk_disagreement",
                severity=_score_to_severity(score),
                confidence_score=round(score, 3),
                confidence_reason=(
                    f"Agenci {a.get('agent_id')} i {b.get('agent_id')} "
                    f"różnią się oceną ryzyka (diff score={diff:.2f})."
                ),
                source_a=a.get("agent_id", "agent_a"),
                source_b=b.get("agent_id", "agent_b"),
                claim_a=_clip(ca, 120),
                claim_b=_clip(cb, 120),
                description=(
                    f"Agenci {a.get('agent_id')} i {b.get('agent_id')} "
                    f"podali sprzeczne oceny ryzyka na ten sam temat."
                ),
                evidence={"snippet_a": ca[:150], "snippet_b": cb[:150]},
                session_id=ctx.session_id,
            ))
            break

    return out


def _detect_uncertainty(ctx: TensionContext) -> list[Tension]:
    """
    Model flagged uncertainty sources — this is NOT a contradiction,
    it's a signal that more data or review is needed.
    """
    out: list[Tension] = []

    flagged = _UNCERTAINTY_SOURCES_SIGNAL & set(ctx.uncertainty_sources)
    if not flagged:
        return out

    labels: dict[str, str] = {
        "conflicting_info":      "sprzeczne informacje w kontekście",
        "missing_data":          "brakujące dane",
        "weak_reasoning":        "słabe uzasadnienie",
        "insufficient_context":  "niewystarczający kontekst",
        "low_confidence":        "niska pewność modelu",
    }
    reasons = [labels.get(f, f) for f in flagged]
    score = 0.2 + 0.1 * len(flagged) + (0.15 if ctx.confidence < 0.5 else 0.0)
    score = min(round(score, 3), 0.55)

    out.append(Tension(
        conflict_type="uncertainty",
        severity="low",
        confidence_score=score,
        confidence_reason=f"Model sygnalizuje: {', '.join(reasons)}.",
        source_a="model_signal",
        source_b="current_reply",
        claim_a=", ".join(reasons),
        claim_b=_clip(ctx.reply, 120),
        description=(
            f"Model sygnalizuje niepewność: {', '.join(reasons)}. "
            f"Pewność odpowiedzi: {ctx.confidence:.0%}."
        ),
        evidence={
            "uncertainty_sources": list(flagged),
            "confidence":          ctx.confidence,
        },
        session_id=ctx.session_id,
    ))

    return out


def _detect_insufficient_evidence(ctx: TensionContext) -> list[Tension]:
    """Evidence is too thin — confidence low, no supporting workspace entries."""
    out: list[Tension] = []

    if ctx.confidence > 0.45:
        return out

    supporting = [
        e for e in ctx.recent_workspace[-6:]
        if _topic_overlap(ctx.cmd, e.get("content", "")) and e.get("type") != "introspection"
    ]
    if supporting:
        return out  # there IS evidence, just low confidence — not this type

    score = round(0.25 + (0.45 - ctx.confidence) * 0.5, 3)
    score = min(score, 0.60)

    out.append(Tension(
        conflict_type="insufficient_evidence",
        severity="low",
        confidence_score=score,
        confidence_reason=(
            f"Pewność odpowiedzi {ctx.confidence:.0%} i brak powiązanych wpisów "
            f"w przestrzeni roboczej."
        ),
        source_a="low_confidence_signal",
        source_b="empty_workspace",
        claim_a=f"Pewność: {ctx.confidence:.0%}",
        claim_b="Brak potwierdzających wpisów w workspace",
        description=(
            f"Odpowiedź ma niską pewność ({ctx.confidence:.0%}) i nie ma "
            f"potwierdzających wpisów — podstawa decyzji jest słaba."
        ),
        evidence={"confidence": ctx.confidence, "workspace_hits": len(supporting)},
        session_id=ctx.session_id,
    ))

    return out


def _detect_strategic_tension(ctx: TensionContext) -> list[Tension]:
    """Speed vs quality, urgent priority vs careful strategy."""
    out: list[Tension] = []

    # Case 1: slow strategy + urgent priority
    if ctx.priority in _SPEED_MARKERS and ctx.reasoning_strategy in _CAREFUL_STRATS:
        urgency_penalty = 0.15 if ctx.priority == "critical" else 0.0
        score = round(0.35 + urgency_penalty + max(0, 0.75 - ctx.confidence) * 0.4, 3)
        score = min(score, 0.75)
        out.append(Tension(
            conflict_type="strategic_tension",
            severity=_score_to_severity(score),
            confidence_score=score,
            confidence_reason=(
                f"Strategia [{ctx.reasoning_strategy}] wymaga czasu, "
                f"ale priorytet [{ctx.priority}] wymusza szybkosc."
            ),
            source_a=f"strategia:{ctx.reasoning_strategy}",
            source_b=f"priorytet:{ctx.priority}",
            claim_a=f"Uzyta strategia: {ctx.reasoning_strategy}",
            claim_b=f"Priorytet zadania: {ctx.priority}",
            description=(
                f"Strategia [{ctx.reasoning_strategy}] koliduje z priorytetem [{ctx.priority}]. "
                f"Wynik moze byc niekompletny (pewnosc: {ctx.confidence:.0%})."
            ),
            evidence={
                "strategy":   ctx.reasoning_strategy,
                "priority":   ctx.priority,
                "confidence": ctx.confidence,
            },
            session_id=ctx.session_id,
        ))

    # Case 2: direct_answer + critical priority + missing data
    if (ctx.priority in _SPEED_MARKERS
            and ctx.reasoning_strategy == "direct_answer"
            and "missing_data" in ctx.uncertainty_sources):
        score = round(0.50 + (0.15 if ctx.priority == "critical" else 0.0), 3)
        out.append(Tension(
            conflict_type="strategic_tension",
            severity="medium",
            confidence_score=score,
            confidence_reason=(
                f"Szybka odpowiedz (direct_answer) przy priorytecie [{ctx.priority}] "
                f"i brakujacych danych — ryzyko blednej decyzji."
            ),
            source_a="direct_answer",
            source_b="missing_data",
            claim_a="Akcja: direct_answer (szybka decyzja)",
            claim_b="Brakujace dane w kontekscie",
            description=(
                f"Wydano szybka odpowiedz przy priorytecie [{ctx.priority}] "
                f"mimo brakujacych danych — ryzyko bledu."
            ),
            evidence={
                "strategy":             ctx.reasoning_strategy,
                "priority":             ctx.priority,
                "uncertainty_sources":  ctx.uncertainty_sources,
            },
            session_id=ctx.session_id,
        ))

    return out


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sig(*parts: str) -> str:
    """Deterministic signature for deduplication."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _clip(text: str, n: int) -> str:
    """Clip text to n chars, strip trailing whitespace."""
    return text[:n].rstrip()


def _parse_mem_value(raw: Any) -> str:
    """
    Extract human-readable string from memory value.
    Handles plain strings and dicts/JSON with 'original'/'correction'/'content' keys.
    Never returns raw JSON blobs.
    """
    if isinstance(raw, dict):
        return raw.get("correction") or raw.get("original") or raw.get("content") or ""
    s = str(raw).strip()
    # Try to parse if it looks like JSON
    if s.startswith("{") or s.startswith("["):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj.get("correction") or obj.get("original") or obj.get("content") or s[:100]
        except Exception:
            pass
    return s


def _topic_overlap(cmd: str, text: str, min_words: int = 2) -> bool:
    cmd_w  = {w.lower() for w in re.findall(r"\w{4,}", cmd) if w.lower() not in _STOP_WORDS}
    text_w = {w.lower() for w in re.findall(r"\w{4,}", text) if w.lower() not in _STOP_WORDS}
    return len(cmd_w & text_w) >= min_words


def _contradiction_score(confidence: float, priority: str, workspace: list[dict]) -> tuple[float, str]:
    """
    Score a factual_contradiction between 0.45 and 0.90.
    Higher if: low confidence, critical priority, many recent entries on same topic.
    """
    base = 0.45
    reasons: list[str] = []

    if confidence < 0.4:
        base += 0.25
        reasons.append(f"niska pewność ({confidence:.0%})")
    elif confidence < 0.6:
        base += 0.15
        reasons.append(f"umiarkowana pewność ({confidence:.0%})")

    if priority == "critical":
        base += 0.12
        reasons.append("priorytet krytyczny")
    elif priority == "high":
        base += 0.06
        reasons.append("priorytet wysoki")

    scored_count = sum(1 for e in workspace if e.get("score") is not None)
    if scored_count >= 3:
        base += 0.05
        reasons.append(f"{scored_count} ocenionych wpisów w workspace")

    score = round(min(base, 0.90), 3)
    reason = "Sprzeczność faktyczna — " + ("; ".join(reasons) if reasons else "wykryta negacja")
    return score, reason


def _score_to_severity(score: float) -> str:
    if score >= 0.70:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _rows_to_dicts(rows) -> list[dict]:
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["evidence"] = json.loads(d.get("evidence", "{}"))
        except Exception:
            d["evidence"] = {}
        result.append(d)
    return result


# ── Table bootstrap ────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS tensions (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    conflict_type     TEXT NOT NULL DEFAULT 'uncertainty',
    severity          TEXT NOT NULL DEFAULT 'low',
    confidence_score  REAL NOT NULL DEFAULT 0.5,
    confidence_reason TEXT NOT NULL DEFAULT '',
    source_a          TEXT NOT NULL,
    source_b          TEXT NOT NULL,
    claim_a           TEXT NOT NULL DEFAULT '',
    claim_b           TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL,
    evidence          TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'open',
    resolution_status TEXT NOT NULL DEFAULT 'open',
    session_id        TEXT NOT NULL DEFAULT 'global',
    signature         TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    resolution        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tensions_type     ON tensions(type);
CREATE INDEX IF NOT EXISTS idx_tensions_ctype    ON tensions(conflict_type);
CREATE INDEX IF NOT EXISTS idx_tensions_status   ON tensions(status);
CREATE INDEX IF NOT EXISTS idx_tensions_sess     ON tensions(session_id);
CREATE INDEX IF NOT EXISTS idx_tensions_sig      ON tensions(signature);
"""

_table_ensured = False


async def _ensure_table() -> None:
    global _table_ensured
    if _table_ensured:
        return
    async with aiosqlite.connect(_DB) as db:
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                await db.execute(stmt)
        await db.commit()
    _table_ensured = True


async def _store_deduped(tensions: list[Tension]) -> list[Tension]:
    """Store only tensions whose signature is not already in DB (open or recent)."""
    if not tensions:
        return []

    await _ensure_table()
    stored: list[Tension] = []

    async with aiosqlite.connect(_DB) as db:
        for t in tensions:
            existing = await (await db.execute(
                "SELECT id FROM tensions WHERE signature=? AND status='open'", (t.signature,)
            )).fetchone()
            if existing:
                continue  # already tracked

            await db.execute(
                """INSERT OR IGNORE INTO tensions
                   (id, type, conflict_type, severity, confidence_score, confidence_reason,
                    source_a, source_b, claim_a, claim_b,
                    description, evidence, status, resolution_status,
                    session_id, signature, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t.id, t.type, t.conflict_type, t.severity,
                    t.confidence_score, t.confidence_reason,
                    t.source_a, t.source_b, t.claim_a, t.claim_b,
                    t.description,
                    json.dumps(t.evidence, ensure_ascii=False),
                    t.status, t.resolution_status,
                    t.session_id, t.signature, t.created_at,
                ),
            )
            stored.append(t)
        await db.commit()

    return stored
