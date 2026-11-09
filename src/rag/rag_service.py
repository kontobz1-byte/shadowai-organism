"""RAG service — embeddings via qwen3-embedding on shadow + Qdrant vector search.

Primary store is **Qdrant** (settings.qdrant_url). SQLite (rag_vectors) is kept as a
durable mirror AND automatic fallback when Qdrant is unreachable. On first use each
logical store backfills any pre-existing SQLite vectors into its Qdrant collection —
zero data loss on the SQLite→Qdrant migration (2026-05-28).

Embeddings via Ollama /api/embeddings (qwen3-embedding:0.6b, dim = settings.vector_dim).
Falls back to in-process cosine over the SQLite mirror if Qdrant or the embedding
node is unavailable, so callers (heartbeat, scheduler, Firma, whatsapp, voice) never
hard-fail.

Public API is UNCHANGED — `knowledge_store`, `training_store`, `semantic_search`,
`index_document`, `get_embedding`, and `VectorStore.upsert/search/remove`.

SQLite table (auto-created; mirror + fallback + backfill source):
  rag_vectors(id TEXT, collection TEXT, embedding BLOB, meta TEXT, updated_at TEXT, PK(id, collection))
"""
from __future__ import annotations

import json
import logging
import math
import struct
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import httpx

from app.config import settings

logger = logging.getLogger("shadowai.rag")

_EMBED_URL = f"{settings.ollama_gpu_url}/api/embeddings"
_EMBED_MODEL = "qwen3-embedding:0.6b"
_TIMEOUT = 30.0
_DB = str(settings.db_path)

# Stable namespace so doc_id → Qdrant point UUID is deterministic across restarts
# (Qdrant point IDs must be uint or UUID; legacy doc_ids are arbitrary strings).
_POINT_NS = uuid.UUID("a1b2c3d4-5e6f-4a8b-9c0d-1e2f3a4b5c6d")


# ── Serialization helpers (SQLite mirror + fallback) ───────────────────────────

def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Qdrant client (lazy module singleton) ──────────────────────────────────────

_qdrant_client = None


_qclients_per_loop: dict[int, Any] = {}


def _get_qclient():
    """Klient Qdranta NA PĘTLĘ asyncio.

    2026-09-25: klient był jeden globalny, przywiązany do pierwszej pętli. Skrypt, który robi
    drugie `asyncio.run` (albo narzędzie wołające RAG z innej pętli), dostawał „Event loop is
    closed" — upsert padał, SQLite trzymał kopię, a `index_document` i tak mówił True. Wpis był
    niewidoczny dla wyszukiwania na zawsze (backfill działa tylko przy tworzeniu kolekcji)."""
    global _qdrant_client
    import asyncio as _aio
    try:
        key = id(_aio.get_running_loop())
    except RuntimeError:
        key = 0
    cl = _qclients_per_loop.get(key)
    if cl is None:
        from qdrant_client import AsyncQdrantClient
        cl = AsyncQdrantClient(url=settings.qdrant_url, api_key=(settings.qdrant_api_key or None))
        _qclients_per_loop[key] = cl
        _qdrant_client = cl
    return cl


# ── Qdrant-backed Vector store (SQLite mirror + fallback) ──────────────────────

def _payload_has_text(payload: dict[str, Any]) -> bool:
    """Czy trafienie da się w ogóle wyrenderować u konsumenta recallu.

    Konsumenci (heartbeat_service, voice_agent, web_research) czytają
    text|content|summary i POMIJAJĄ puste — takie punkty zjadałyby sloty top_k,
    nic nie wnosząc. Claude-10 2026-08-01.
    """
    return bool(
        str(payload.get("text") or "").strip()
        or str(payload.get("content") or "").strip()
        or str(payload.get("summary") or "").strip()
    )


class VectorStore:
    """Qdrant-backed vector store with a durable SQLite mirror + fallback.

    One Qdrant collection per logical store: ``rag_<collection>``. The original
    string ``doc_id`` is preserved in the point payload under ``_doc_id`` so the
    public API keeps returning string ids regardless of Qdrant's UUID point ids.
    """

    def __init__(self, collection: str) -> None:
        self.collection = collection
        self._qcoll = f"rag_{collection}"
        self._ready: bool | None = None  # None = unchecked, True = Qdrant, False = SQLite fallback
        self._cache: dict[str, tuple[list[float], dict[str, Any]]] | None = None

    def _point_id(self, doc_id: str) -> str:
        return str(uuid.uuid5(_POINT_NS, f"{self.collection}:{doc_id}"))

    # ---- Qdrant lifecycle ----------------------------------------------------

    async def _ensure(self) -> bool:
        """Ensure the Qdrant collection exists (+ one-time SQLite backfill).
        Returns True if Qdrant is usable, False to signal SQLite fallback."""
        if self._ready is not None:
            return self._ready
        try:
            from qdrant_client.models import Distance, VectorParams
            client = _get_qclient()
            info = await client.get_collections()
            existing = {c.name for c in info.collections}
            if self._qcoll not in existing:
                await client.create_collection(
                    collection_name=self._qcoll,
                    vectors_config=VectorParams(size=settings.vector_dim, distance=Distance.COSINE),
                )
                logger.info("[RAG] Qdrant collection '%s' created (dim=%d)", self._qcoll, settings.vector_dim)
            self._ready = True
            await self._backfill_from_sqlite()
        except Exception as exc:
            logger.warning("[RAG] Qdrant unavailable, SQLite fallback for '%s': %s", self.collection, exc)
            self._ready = False
        return self._ready

    async def _backfill_from_sqlite(self) -> None:
        """If the Qdrant collection is empty but SQLite holds vectors, copy them
        over once (preserves all pre-migration knowledge)."""
        try:
            client = _get_qclient()
            cnt = await client.count(collection_name=self._qcoll, exact=True)
            if cnt.count > 0:
                return
            rows = await self._sqlite_all()
            if not rows:
                return
            from qdrant_client.models import PointStruct
            points = []
            pominiete = 0
            for doc_id, vec, meta in rows:
                # 2026-11-06: wpisy BEZ tekstu skasowano z Qdranta 01.08 za zgodą operatora —
                # pusta kolekcja nie może ich wskrzesić z kopii SQLite.
                if not isinstance(meta, dict) or not _payload_has_text(meta):
                    pominiete += 1
                    continue
                payload = dict(meta)
                payload["_doc_id"] = doc_id
                points.append(PointStruct(id=self._point_id(doc_id), vector=vec, payload=payload))
            if points:
                await client.upsert(collection_name=self._qcoll, points=points)
                logger.info("[RAG] backfilled %d vectors SQLite→Qdrant for '%s' (pominięto bez tekstu: %d)",
                            len(points), self.collection, pominiete)
        except Exception as exc:
            logger.warning("[RAG] backfill failed for '%s': %s", self.collection, exc)

    async def uzgodnij_z_qdrantem(self, limit: int = 200, partia: int = 50) -> dict[str, Any]:
        """Uzgodnienie kopii SQLite z kolekcją Qdranta (2026-11-06).

        Dogrywa do Qdranta tylko BRAKUJĄCE wpisy Z tekstem (wektor z kopii, bez liczenia
        embeddingów). Brakujących bez tekstu nie wysyła (skasowane 01.08 za zgodą operatora),
        obecnych nie nadpisuje (część ma w Qdrancie tekst, którego kopia nie ma), nic nie kasuje."""
        if self._ready is False:
            self._ready = None
        if not await self._ensure():
            logger.warning("[RAG] uzgodnienie '%s': Qdrant niedostępny", self.collection)
            return {"status": "qdrant_niedostepny", "kolekcja": self.collection}
        client = _get_qclient()

        metas: dict[str, dict[str, Any]] = {}
        try:
            async with aiosqlite.connect(_DB) as db:
                db.row_factory = aiosqlite.Row
                await self._ensure_table(db)
                rows = await (await db.execute(
                    "SELECT id, meta FROM rag_vectors WHERE collection=?",
                    (self.collection,),
                )).fetchall()
            for row in rows:
                try:
                    meta = json.loads(row["meta"] or "{}")
                except Exception:
                    meta = {}
                metas[row["id"]] = meta if isinstance(meta, dict) else {}
        except Exception as exc:
            logger.warning("[RAG] uzgodnienie '%s': odczyt SQLite padł: %s", self.collection, exc)
            return {"status": "blad_sqlite", "kolekcja": self.collection, "blad": str(exc)[:200]}

        try:
            cnt = await client.count(collection_name=self._qcoll, exact=True)
            qdrant_n = int(getattr(cnt, "count", 0) or 0)
            obecne_pid: set[str] = set()
            ids = list(metas)
            for i in range(0, len(ids), 256):
                porcja_pid = [self._point_id(d) for d in ids[i:i + 256]]
                znalezione = await client.retrieve(
                    collection_name=self._qcoll, ids=porcja_pid,
                    with_payload=False, with_vectors=False,
                )
                obecne_pid.update(str(p.id) for p in (znalezione or []))
        except Exception as exc:
            logger.warning("[RAG] uzgodnienie '%s': odczyt Qdranta padł: %s", self.collection, exc)
            return {"status": "blad_qdrant", "kolekcja": self.collection, "blad": str(exc)[:200]}

        obecne = 0
        brak_bez = 0
        brak_z: list[str] = []
        for doc_id, meta in metas.items():
            if self._point_id(doc_id) in obecne_pid:
                obecne += 1
            elif _payload_has_text(meta):
                brak_z.append(doc_id)
            else:
                brak_bez += 1
        brak_z.sort()

        do_wyslania = brak_z[:max(0, int(limit))]
        krok = max(1, int(partia))
        dograne = 0
        bledy = 0
        for i in range(0, len(do_wyslania), krok):
            porcja = do_wyslania[i:i + krok]
            try:
                from qdrant_client.models import PointStruct
                async with aiosqlite.connect(_DB) as db:
                    db.row_factory = aiosqlite.Row
                    ph = ",".join("?" * len(porcja))
                    wiersze = await (await db.execute(
                        f"SELECT id, embedding FROM rag_vectors WHERE collection=? AND id IN ({ph})",
                        (self.collection, *porcja),
                    )).fetchall()
                points = []
                for row in wiersze:
                    doc_id = row["id"]
                    payload = dict(metas.get(doc_id) or {})
                    payload["_doc_id"] = doc_id
                    points.append(PointStruct(id=self._point_id(doc_id),
                                              vector=_unpack(row["embedding"]), payload=payload))
                if points:
                    await client.upsert(collection_name=self._qcoll, points=points)
                    dograne += len(points)
            except Exception as exc:
                bledy += 1
                logger.warning("[RAG] uzgodnienie '%s': porcja %d (%d wpisów) padła: %s",
                               self.collection, i // krok, len(porcja), exc)

        wynik = {
            "status": "ok",
            "kolekcja": self.collection,
            "sqlite": len(metas),
            "qdrant": qdrant_n,
            "obecne": obecne,
            "brak_z_tekstem": len(brak_z),
            "brak_bez_tekstu": brak_bez,
            "tylko_w_qdrancie": max(0, qdrant_n - obecne),
            "dograne": dograne,
            "bledy": bledy,
        }
        logger.log(logging.WARNING if brak_z else logging.INFO,
                   "[RAG] uzgodnienie SQLite↔Qdrant '%s': %s", self.collection, wynik)
        return wynik

    # ---- public API (signatures unchanged) -----------------------------------

    async def upsert(self, doc_id: str, embedding: list[float], meta: dict[str, Any]) -> bool:
        """True = wpis jest tam, gdzie szuka `search` (Qdrant albo — gdy Qdrant niedostępny —
        SQLite, z którego wtedy czyta fallback). False = Qdrant żyje, a zapis do niego padł:
        wyszukiwanie tego wpisu NIE zobaczy, mimo kopii w SQLite."""
        # Durable SQLite mirror first (also the fallback + backfill source).
        await self._sqlite_upsert(doc_id, embedding, meta)
        if await self._ensure():
            try:
                from qdrant_client.models import PointStruct
                payload = dict(meta)
                payload["_doc_id"] = doc_id
                await _get_qclient().upsert(
                    collection_name=self._qcoll,
                    points=[PointStruct(id=self._point_id(doc_id), vector=embedding, payload=payload)],
                )
            except Exception as exc:
                logger.warning("[RAG] Qdrant upsert failed (%s), SQLite mirror kept: %s", doc_id, exc)
                return False
        return True

    async def remove(self, doc_id: str) -> None:
        await self._sqlite_remove(doc_id)
        if await self._ensure():
            try:
                from qdrant_client.models import PointIdsList
                await _get_qclient().delete(
                    collection_name=self._qcoll,
                    points_selector=PointIdsList(points=[self._point_id(doc_id)]),
                )
            except Exception:
                pass

    async def search(self, query_vec: list[float], top_k: int = 8) -> list[dict[str, Any]]:
        if await self._ensure():
            try:
                # Operator/Claude-10 2026-08-01: over-fetch + odsiew trafień BEZ tekstu.
                # Historyczne punkty (browser_read/curiosity_engine/agentic_executor,
                # zapisane zanim index_document zaczęło dokładać `text` do payloadu) mają
                # centroidalne wektory z wysokim cosine dla DOWOLNEGO zapytania: wchodziły
                # w top-k, a konsumenci renderowali z nich pustkę (heartbeat dostawał dwie
                # puste linie zamiast pamięci). Filtrujemy przy odczycie zamiast kasować
                # dane — odwracalne i nie niszczy niczego, czego nie da się odtworzyć.
                # qdrant-client >= 1.12: query_points replaces the removed .search()
                resp = await _get_qclient().query_points(
                    collection_name=self._qcoll,
                    query=query_vec,
                    # Zapas ×20: przy ~85% korpusu bez tekstu (dług historyczny, patrz
                    # TESTAMENT §VII) mniejszy over-fetch zwracał 1 wynik zamiast 4.
                    limit=min(max(top_k * 20, 64), 256),
                    with_payload=True,
                    with_vectors=False,
                )
                out: list[dict[str, Any]] = []
                empty_skipped = 0
                for h in resp.points:
                    payload = dict(h.payload or {})
                    if not _payload_has_text(payload):
                        empty_skipped += 1
                        continue
                    doc_id = payload.pop("_doc_id", str(h.id))
                    out.append({"doc_id": doc_id, "score": h.score, **payload})
                    if len(out) >= top_k:
                        break
                if empty_skipped:
                    logger.debug("[RAG] %s: pominięto %d trafień bez tekstu w payloadzie",
                                 self._qcoll, empty_skipped)
                if out:
                    return out
                # Empty Qdrant result → fall through to SQLite (covers brief race
                # before a backfill completes on a fresh collection).
            except Exception as exc:
                logger.warning("[RAG] Qdrant search failed, SQLite fallback: %s", exc)
        return await self._sqlite_search(query_vec, top_k)

    # ---- SQLite mirror / fallback --------------------------------------------

    async def _ensure_table(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS rag_vectors ("
            "  id TEXT NOT NULL,"
            "  collection TEXT NOT NULL,"
            "  embedding BLOB NOT NULL,"
            "  meta TEXT NOT NULL DEFAULT '{}',"
            "  updated_at TEXT NOT NULL,"
            "  PRIMARY KEY (id, collection)"
            ")"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rag_coll ON rag_vectors(collection)")
        await db.commit()

    async def _sqlite_all(self) -> list[tuple[str, list[float], dict[str, Any]]]:
        """All (doc_id, vector, meta) rows for this collection — backfill source."""
        out: list[tuple[str, list[float], dict[str, Any]]] = []
        try:
            async with aiosqlite.connect(_DB) as db:
                db.row_factory = aiosqlite.Row
                await self._ensure_table(db)
                rows = await (await db.execute(
                    "SELECT id, embedding, meta FROM rag_vectors WHERE collection=?",
                    (self.collection,),
                )).fetchall()
            for row in rows:
                try:
                    out.append((row["id"], _unpack(row["embedding"]), json.loads(row["meta"])))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[RAG] _sqlite_all failed: %s", exc)
        return out

    async def _load_cache(self) -> dict[str, tuple[list[float], dict[str, Any]]]:
        if self._cache is not None:
            return self._cache
        self._cache = {doc_id: (vec, meta) for doc_id, vec, meta in await self._sqlite_all()}
        logger.info("[RAG] SQLite cache: %d vectors for collection=%s", len(self._cache), self.collection)
        return self._cache

    async def _sqlite_upsert(self, doc_id: str, embedding: list[float], meta: dict[str, Any]) -> None:
        blob = _pack(embedding)
        meta_json = json.dumps(meta, ensure_ascii=False)
        try:
            async with aiosqlite.connect(_DB) as db:
                await self._ensure_table(db)
                await db.execute(
                    "INSERT OR REPLACE INTO rag_vectors(id, collection, embedding, meta, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, self.collection, blob, meta_json, _now()),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("[RAG] SQLite mirror upsert failed: %s", exc)
        if self._cache is not None:
            self._cache[doc_id] = (embedding, meta)

    async def _sqlite_remove(self, doc_id: str) -> None:
        try:
            async with aiosqlite.connect(_DB) as db:
                await db.execute(
                    "DELETE FROM rag_vectors WHERE id=? AND collection=?",
                    (doc_id, self.collection),
                )
                await db.commit()
        except Exception:
            pass
        if self._cache is not None:
            self._cache.pop(doc_id, None)

    async def _sqlite_search(self, query_vec: list[float], top_k: int = 8) -> list[dict[str, Any]]:
        cache = await self._load_cache()
        if not cache:
            return []
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for doc_id, (vec, meta) in cache.items():
            # Ten sam odsiew co w gałęzi Qdrant — inaczej fallback SQLite wpuszczał
            # z powrotem trafienia bez tekstu (Claude-10 2026-08-01).
            if not _payload_has_text(meta):
                continue
            scored.append((_cosine(query_vec, vec), doc_id, meta))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"doc_id": doc_id, "score": score, **meta} for score, doc_id, meta in scored[:top_k]]

    def __len__(self) -> int:
        return len(self._cache) if self._cache is not None else 0


# ── Embedding helper ──────────────────────────────────────────────────────────

async def _resolve_embed_base() -> str:
    """Pick the best LOCAL Ollama tier for embedding via vram_manager
    (deflects GPU→RAM when VRAM full per resource_metabolism.md §3; never
    cloud/WIN — operator 'nie chmury'). Falls back to the GPU node URL."""
    try:
        from app.services.vram_manager import find_best_node_for, _NODES
        # 2026-09-25: embedder mieszka na CPU (.10:11435). Na GPU .10 Ollama trzyma JEDEN model —
        # embedder i odruch JEV wypychały się nawzajem, stąd timeouty 60 s i zgubione wpisy RAG.
        # Zmierzone: CPU ciepły 0,11 s vs GPU 0,08 s, cosine wektorów GPU↔CPU 0,9995 (zgodne).
        if _NODES.get("cpu"):
            return _NODES["cpu"].rstrip("/")
        node_id, _reason = await find_best_node_for(_EMBED_MODEL)
        url = _NODES.get(node_id)
        if url:
            return url.rstrip("/")
    except Exception as exc:
        logger.debug("[RAG] embed routing fallback: %s", exc)
    return settings.ollama_gpu_url.rstrip("/")


async def get_embedding(text: str) -> list[float] | None:
    """Embed via qwen3-embedding, routed across LOCAL tiers (GPU→RAM) by
    vram_manager so it survives VRAM pressure.  Returns None if unavailable."""
    base = await _resolve_embed_base()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base}/api/embeddings",
                json={"model": _EMBED_MODEL, "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding") or data.get("embeddings", [None])[0]
    except Exception:
        return None


# ── Global stores (one per collection) ───────────────────────────────────────

knowledge_store = VectorStore("knowledge")
training_store = VectorStore("training")


# ── Index helpers ─────────────────────────────────────────────────────────────

async def index_document(
    store: VectorStore,
    doc_id: str,
    text: str,
    meta: dict[str, Any],
) -> bool:
    """Embed text and upsert into store.  Returns True on success."""
    if not text.strip():
        return False
    vec = await get_embedding(text[:3000])
    if vec is None:
        return False
    # Operator/Claude-10 2026-08-01: payload MUSI nieść tekst źródłowy. Konsumenci
    # recallu (heartbeat_service, voice_agent, web_research) czytają
    # meta['text'|'content'|'summary'] i POMIJAJĄ trafienie, gdy jest puste — a
    # index_document zapisywał wyłącznie `meta`, więc tekst przepadał, jeśli wołający
    # sam go tam nie zdublował. Audyt: 4382/4437 punktów w rag_knowledge bez tekstu =
    # pamięć epizodyczna trafiała poprawnie i renderowała się jako pustka.
    payload = dict(meta)
    if not (payload.get("text") or payload.get("content") or payload.get("summary")):
        payload["text"] = text[:2000]
    return await store.upsert(doc_id, vec, payload)


async def semantic_search(
    store: VectorStore,
    query: str,
    top_k: int = 8,
) -> list[dict[str, Any]] | None:
    """Search store by semantic similarity.  Returns None if embedding fails."""
    vec = await get_embedding(query)
    if vec is None:
        return None
    return await store.search(vec, top_k=top_k)
