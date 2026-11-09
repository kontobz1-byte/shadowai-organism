"""Operator Memory Service — 4-layer local learning system.

Layers
------
preference  — UI preferences, response style, action ordering, caution thresholds
workflow    — typical action sequences (document → analysis → fix → export)
domain      — company vocabulary, proper names, document types, abbreviations
correction  — manual operator corrections that become persistent rules

All data stored locally in SQLite.  Every mutation is version-tracked.
"""

from __future__ import annotations

import json
import math
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Literal

import aiosqlite

from app.config import settings

MemoryLayer = Literal["preference", "workflow", "domain", "correction"]

LAYERS: list[MemoryLayer] = ["preference", "workflow", "domain", "correction"]

_DB_PATH = settings.db_path

_DDL = """
CREATE TABLE IF NOT EXISTS operator_memory (
    id          TEXT PRIMARY KEY,
    layer       TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL DEFAULT 'manual',
    confidence  REAL NOT NULL DEFAULT 0.5,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS operator_memory_history (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    version     INTEGER NOT NULL,
    value       TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    change_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_memory_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opmem_layer ON operator_memory(layer);
CREATE INDEX IF NOT EXISTS idx_opmem_key ON operator_memory(key);
CREATE INDEX IF NOT EXISTS idx_opmem_layer_enabled ON operator_memory(layer, enabled);
CREATE INDEX IF NOT EXISTS idx_opmem_hist_mid ON operator_memory_history(memory_id);
"""

_DEFAULT_CONFIG: dict[str, str] = {
    "learning_enabled": "true",
    "layer_preference_enabled": "true",
    "layer_workflow_enabled": "true",
    "layer_domain_enabled": "true",
    "layer_correction_enabled": "true",
    "source_dashboard_enabled": "true",
    "source_telemetry_enabled": "true",
    "source_document_enabled": "true",
    "source_correction_enabled": "true",
    "confidence_threshold": "0.3",
    "max_prompt_entries": "12",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


@asynccontextmanager
async def _conn() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(_DB_PATH, timeout=30.0) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA busy_timeout = 30000")
        except Exception:
            pass
        await db.executescript(_DDL)
        yield db


async def _ensure_defaults(db: aiosqlite.Connection) -> None:
    for k, v in _DEFAULT_CONFIG.items():
        await db.execute(
            "INSERT OR IGNORE INTO operator_memory_config (key, value) VALUES (?, ?)",
            (k, v),
        )
    await db.commit()


async def get_config() -> dict[str, str]:
    async with _conn() as db:
        await _ensure_defaults(db)
        rows = await (await db.execute("SELECT key, value FROM operator_memory_config")).fetchall()
    return {r["key"]: r["value"] for r in rows}


async def set_config(key: str, value: str) -> None:
    async with _conn() as db:
        await _ensure_defaults(db)
        await db.execute(
            "INSERT INTO operator_memory_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


def _is_layer_enabled(cfg: dict[str, str], layer: MemoryLayer) -> bool:
    if cfg.get("learning_enabled") != "true":
        return False
    return cfg.get(f"layer_{layer}_enabled", "true") == "true"


def _is_source_enabled(cfg: dict[str, str], source: str) -> bool:
    if cfg.get("learning_enabled") != "true":
        return False
    return cfg.get(f"source_{source}_enabled", "true") == "true"


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def create_entry(
    layer: MemoryLayer,
    key: str,
    value: Any,
    source: str = "manual",
    confidence: float = 0.5,
) -> dict:
    cfg = await get_config()
    if not _is_layer_enabled(cfg, layer):
        return {"status": "skipped", "reason": f"layer_{layer}_disabled"}
    if source != "manual" and not _is_source_enabled(cfg, source):
        return {"status": "skipped", "reason": f"source_{source}_disabled"}

    entry_id = _uuid()
    now = _now()
    value_json = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value

    async with _conn() as db:
        await db.execute(
            "INSERT INTO operator_memory "
            "(id, layer, key, value, source, confidence, hit_count, created_at, updated_at, version, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 1, 1)",
            (entry_id, layer, key, value_json, source, confidence, now, now),
        )
        await db.execute(
            "INSERT INTO operator_memory_history (id, memory_id, version, value, changed_at, change_type) "
            "VALUES (?, ?, 1, ?, ?, 'create')",
            (_uuid(), entry_id, value_json, now),
        )
        await db.commit()

    return {"status": "created", "id": entry_id, "layer": layer, "key": key}


async def update_entry(entry_id: str, value: Any, confidence: float | None = None) -> dict:
    value_json = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    now = _now()

    async with _conn() as db:
        row = await (await db.execute("SELECT * FROM operator_memory WHERE id = ?", (entry_id,))).fetchone()
        if not row:
            return {"status": "not_found"}

        new_version = row["version"] + 1
        set_parts = ["value = ?", "updated_at = ?", "version = ?"]
        params: list[Any] = [value_json, now, new_version]
        if confidence is not None:
            set_parts.append("confidence = ?")
            params.append(confidence)
        params.append(entry_id)

        await db.execute(
            "UPDATE operator_memory SET " + ", ".join(set_parts) + " WHERE id = ?",
            params,
        )
        await db.execute(
            "INSERT INTO operator_memory_history (id, memory_id, version, value, changed_at, change_type) "
            "VALUES (?, ?, ?, ?, ?, 'update')",
            (_uuid(), entry_id, new_version, value_json, now),
        )
        await db.commit()

    return {"status": "updated", "id": entry_id, "version": new_version}


async def delete_entry(entry_id: str) -> dict:
    now = _now()
    async with _conn() as db:
        row = await (await db.execute("SELECT * FROM operator_memory WHERE id = ?", (entry_id,))).fetchone()
        if not row:
            return {"status": "not_found"}
        await db.execute(
            "INSERT INTO operator_memory_history (id, memory_id, version, value, changed_at, change_type) "
            "VALUES (?, ?, ?, ?, ?, 'delete')",
            (_uuid(), entry_id, row["version"], row["value"], now),
        )
        await db.execute("DELETE FROM operator_memory WHERE id = ?", (entry_id,))
        await db.commit()
    return {"status": "deleted", "id": entry_id}


async def toggle_entry(entry_id: str, enabled: bool) -> dict:
    async with _conn() as db:
        await db.execute(
            "UPDATE operator_memory SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), entry_id),
        )
        await db.commit()
    return {"status": "toggled", "id": entry_id, "enabled": enabled}


async def get_entry(entry_id: str) -> dict | None:
    async with _conn() as db:
        row = await (await db.execute("SELECT * FROM operator_memory WHERE id = ?", (entry_id,))).fetchone()
    return dict(row) if row else None


async def list_entries(
    layer: MemoryLayer | None = None,
    enabled_only: bool = True,
    limit: int = 100,
) -> list[dict]:
    async with _conn() as db:
        if layer:
            where = "WHERE layer = ?" + (" AND enabled = 1" if enabled_only else "")
            rows = await (await db.execute(
                f"SELECT * FROM operator_memory {where} ORDER BY confidence DESC, hit_count DESC, updated_at DESC LIMIT ?",
                (layer, limit),
            )).fetchall()
        else:
            where = "WHERE enabled = 1" if enabled_only else ""
            rows = await (await db.execute(
                f"SELECT * FROM operator_memory {where} ORDER BY layer, confidence DESC, updated_at DESC LIMIT ?",
                (limit,),
            )).fetchall()
    return [dict(r) for r in rows]


async def get_entry_by_key(
    key: str,
    layer: MemoryLayer,
    enabled_only: bool = True,
) -> dict | None:
    async with _conn() as db:
        where = "WHERE layer = ? AND LOWER(key) = LOWER(?)"
        if enabled_only:
            where += " AND enabled = 1"
        row = await (await db.execute(
            f"SELECT * FROM operator_memory {where} ORDER BY confidence DESC, updated_at DESC LIMIT 1",
            (layer, key),
        )).fetchone()
    return dict(row) if row else None


async def put_entry(
    key: str,
    value: Any,
    *,
    section: str | None = None,
    layer: str | None = None,
    source: str = "auto_handler",
    confidence: float = 0.7,
) -> dict:
    _valid_layers = {"preference", "workflow", "domain", "correction"}
    mem_layer: MemoryLayer = layer if layer in _valid_layers else "preference"  # type: ignore[assignment]
    stored_value = value
    if section is not None:
        if isinstance(value, dict):
            stored_value = {**value, "__section": section}
        else:
            stored_value = {"value": value, "__section": section}
    existing = await get_entry_by_key(key, mem_layer, enabled_only=False)
    if existing:
        return await update_entry(existing["id"], stored_value, confidence=confidence)
    return await create_entry(
        layer=mem_layer,
        key=key,
        value=stored_value,
        source=source,
        confidence=confidence,
    )


async def get_history(entry_id: str) -> list[dict]:
    async with _conn() as db:
        rows = await (await db.execute(
            "SELECT * FROM operator_memory_history WHERE memory_id = ? ORDER BY version DESC",
            (entry_id,),
        )).fetchall()
    return [dict(r) for r in rows]


async def bump_hit(entry_id: str) -> None:
    async with _conn() as db:
        await db.execute(
            "UPDATE operator_memory SET hit_count = hit_count + 1, updated_at = ? WHERE id = ?",
            (_now(), entry_id),
        )
        await db.commit()


# ── Search & scoring ──────────────────────────────────────────────────────────

async def search_entries(query: str, layer: MemoryLayer | None = None, limit: int = 10) -> list[dict]:
    q = f"%{query.lower()}%"
    async with _conn() as db:
        if layer:
            rows = await (await db.execute(
                "SELECT * FROM operator_memory "
                "WHERE enabled = 1 AND layer = ? AND (LOWER(key) LIKE ? OR LOWER(value) LIKE ?) "
                "ORDER BY confidence DESC, hit_count DESC LIMIT ?",
                (layer, q, q, limit),
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT * FROM operator_memory "
                "WHERE enabled = 1 AND (LOWER(key) LIKE ? OR LOWER(value) LIKE ?) "
                "ORDER BY confidence DESC, hit_count DESC LIMIT ?",
                (q, q, limit),
            )).fetchall()
    return [dict(r) for r in rows]


def _memory_freshness(entry: dict, half_life_days: float = 60.0) -> float:
    """Exponential decay — recent entries score higher. Returns 0.2..1.0."""
    try:
        updated = entry.get("updated_at") or entry.get("created_at", "")
        dt = datetime.fromisoformat(updated)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return max(0.2, math.exp(-0.693 * age_days / half_life_days))
    except Exception:
        return 0.5


def score_entry(entry: dict, task_type: str | None = None) -> float:
    """
    Composite memory score (0..1):
      freshness    × 0.25  — decays exponentially (half-life 60 days)
      relevance    × 0.35  — confidence + hit_count bonus
      effectiveness× 0.25  — correction/preference layer premium
      task_align   × 0.15  — bonus if task_types_json contains current task_type
    """
    freshness = _memory_freshness(entry)

    base = entry.get("confidence", 0.5)
    hits = entry.get("hit_count", 0)
    hit_bonus = min(hits * 0.025, 0.4)
    relevance = min(base + hit_bonus, 1.0)

    layer = entry.get("layer", "")
    layer_premium = {"correction": 1.0, "preference": 0.85, "workflow": 0.7, "domain": 0.6}.get(layer, 0.5)
    effectiveness = layer_premium

    task_align = 0.5
    if task_type:
        try:
            task_types = json.loads(entry.get("task_types_json") or "[]")
            if task_type in task_types:
                task_align = 1.0
            elif task_types:
                task_align = 0.3
        except Exception:
            pass

    score = (
        freshness    * 0.25 +
        relevance    * 0.35 +
        effectiveness* 0.25 +
        task_align   * 0.15
    )
    return round(score, 4)


async def record_memory_task_use(entry_id: str, task_type: str) -> None:
    """Track which task_type benefited from this memory entry."""
    async with _conn() as db:
        row = await (await db.execute(
            "SELECT task_types_json FROM operator_memory WHERE id = ?", (entry_id,)
        )).fetchone()
        if not row:
            return
        try:
            types: list[str] = json.loads(row[0] or "[]")
        except Exception:
            types = []
        if task_type not in types:
            types.append(task_type)
            await db.execute(
                "UPDATE operator_memory SET task_types_json = ? WHERE id = ?",
                (json.dumps(types), entry_id),
            )
            await db.commit()


async def get_relevant_memories(
    cmd: str,
    limit: int | None = None,
    task_type: str | None = None,
) -> list[dict]:
    cfg = await get_config()
    if cfg.get("learning_enabled") != "true":
        return []
    threshold = float(cfg.get("confidence_threshold", "0.3"))
    max_entries = int(cfg.get("max_prompt_entries", "12")) if limit is None else limit

    words = cmd.lower().split()
    _PUNCT = '?!.,;:"\'()«»„“'
    stems: set[str] = set()
    for raw in words:
        w = raw.strip(_PUNCT)
        if len(w) < 3:
            continue
        if len(w) > 6:
            stems.add(w[:6])
        elif len(w) == 6:
            stems.add(w[:5])
        else:
            stems.add(w)
    ranked: list[tuple[float, dict]] = []
    unmatched_corrections: list[dict] = []

    async with _conn() as db:
        for layer in LAYERS:
            if not _is_layer_enabled(cfg, layer):
                continue
            rows = await (await db.execute(
                "SELECT * FROM operator_memory WHERE layer = ? AND enabled = 1",
                (layer,),
            )).fetchall()
            for r in rows:
                entry = dict(r)
                key_low = entry["key"].lower()
                val_low = entry["value"].lower()
                word_hits = sum(1 for s in stems if s in key_low or s in val_low)
                if word_hits or layer == "correction":
                    s = score_entry(entry, task_type=task_type)
                    if s >= threshold:
                        entry["_score"] = s
                        if word_hits:
                            ranked.append((s + min(word_hits * 0.05, 0.2), entry))
                        else:
                            unmatched_corrections.append(entry)

    # Korekty bez dopasowania słów: najwyżej trzy, z najwyższym score_entry.
    unmatched_corrections.sort(key=lambda e: e["_score"], reverse=True)
    ranked.extend((e["_score"], e) for e in unmatched_corrections[:3])

    ranked.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in ranked[:max_entries]]


# ── Learning helpers ──────────────────────────────────────────────────────────

async def learn_correction(original_cmd: str, correction: str, context: str = "") -> dict:
    value = json.dumps({
        "original": original_cmd,
        "correction": correction,
        "context": context,
    }, ensure_ascii=False)
    return await create_entry("correction", f"corr:{original_cmd[:60]}", value, source="correction", confidence=0.9)


async def learn_preference(key: str, value: Any, source: str = "dashboard") -> dict:
    existing = await get_entry_by_key(key, layer="preference")
    if existing:
        return await update_entry(existing["id"], value, confidence=min(existing["confidence"] + 0.05, 1.0))
    return await create_entry("preference", key, value, source=source, confidence=0.5)


async def learn_workflow(name: str, steps: list[str], source: str = "dashboard") -> dict:
    value = json.dumps({"name": name, "steps": steps}, ensure_ascii=False)
    return await create_entry("workflow", f"wf:{name}", value, source=source, confidence=0.6)


async def learn_domain_term(term: str, definition: str, source: str = "document") -> dict:
    value = json.dumps({"term": term, "definition": definition}, ensure_ascii=False)
    existing = await search_entries(term, layer="domain", limit=1)
    if existing:
        return await update_entry(existing[0]["id"], value, confidence=min(existing[0]["confidence"] + 0.05, 1.0))
    return await create_entry("domain", f"term:{term}", value, source=source, confidence=0.5)


async def record_action(command: str, action: str, section: str | None = None) -> None:
    cfg = await get_config()
    if not _is_source_enabled(cfg, "dashboard"):
        return
    if section:
        await learn_preference(f"section_freq:{section}", {"section": section, "count": 1}, source="dashboard")


# ── Prompt builder ────────────────────────────────────────────────────────────

async def build_memory_prompt(cmd: str) -> str:
    entries = await get_relevant_memories(cmd)
    if not entries:
        return ""

    lines: list[str] = ["\n\nPAMIĘĆ OPERATORA (lokalne uczenie — wpływa na sugestie, nie na automatyczne akcje):"]

    for e in entries:
        layer = e["layer"]
        key = e["key"]
        val = e["value"]
        score = e.get("_score", 0)
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                val = " | ".join(f"{k}: {v}" for k, v in parsed.items())
        except (json.JSONDecodeError, TypeError):
            pass
        lines.append(f"[{layer.upper()} score={score:.2f}] {key}: {val[:200]}")

    lines.append(
        "\nUWAGA: Powyższe wpisy pamięci to sugestie z historii operatora. "
        "Uwzględnij je w rankingu działań i stylu odpowiedzi, ale NIE wykonuj akcji automatycznie na ich podstawie."
    )
    return "\n".join(lines)
