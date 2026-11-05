# Konfiguracja kwadrantów ShadowAI
# Definicje KB, modeli, i priorytetów dla każdego kwadrantu
import os
from pathlib import Path as _Path


def _wczytaj_env_do_srodowiska() -> None:
    """Wstrzyknij backend/.env do os.environ, jeśli zmiennych tam nie ma.

    Claude 2026-08-21: komentarz niżej obiecuje „flip powrotny = env RAG_EMBED_*",
    ale to była fikcja — pydantic (`Settings.model_config.env_file`) czyta .env
    PO SWOJEMU i NIE eksportuje niczego do `os.environ`, a ten plik sięga po
    `os.getenv`. Skutkiem każda wartość brana była z defaultów w kodzie, więc
    zmiana .env nie robiła nic i nikt tego nie zauważył, bo defaulty akurat
    zgadzały się z tym, co chciano. Teraz env naprawdę rządzi.
    Zmienne już obecne w środowisku procesu mają pierwszeństwo — nie nadpisujemy.
    """
    plik = _Path(__file__).resolve().parent.parent / ".env"
    if not plik.exists():
        return
    try:
        for linia in plik.read_text(encoding="utf-8").splitlines():
            linia = linia.strip()
            if not linia or linia.startswith("#") or "=" not in linia:
                continue
            klucz, wartosc = linia.split("=", 1)
            klucz = klucz.strip()
            if klucz.startswith("RAG_EMBED") and klucz not in os.environ:
                os.environ[klucz] = wartosc.strip().strip('"').strip("'")
    except OSError:
        pass


_wczytaj_env_do_srodowiska()

QUADRANTS = {
    "tech": {
        "name": "Technical Core",
        "description": "Sistem infrastruktury, kod, architektura",
        "docs": [],
        "index": None,
        "primary_model": "qwen3:14b",
        "fallback_model": "qwen2.5:7b",
        "load_threshold": 300,
        "priority": 5,
        "max_load": 370
    },
    "creative": {
        "name": "Creative Hub",
        "description": "Muzyka, sztuka, design, storytelling",
        "docs": [],
        "index": None,
        "primary_model": "qwen2.5:7b",
        "fallback_model": "qwen3:14b",
        "load_threshold": 200,
        "priority": 2,
        "max_load": 209
    },
    "Firma": {
        "name": "Firma Integration",
        "description": "Integracja SMS, messaging, external services",
        "docs": [],
        "index": None,
        "primary_model": "qwen3:14b",
        "fallback_model": "qwen2.5:7b",
        "load_threshold": 300,
        "priority": 4,
        "max_load": 370
    },
    "mentor": {
        "name": "Learning & Mentoring",
        "description": "Edukacja, lekcje, coaching, growth",
        "docs": [],
        "index": None,
        "primary_model": "qwen3:14b",
        "fallback_model": "qwen2.5:7b",
        "load_threshold": 300,
        "priority": 3,
        "max_load": 370
    },
    "family": {
        "name": "Family & Personal",
        "description": "Osobiste, rodzinne, health, wellness",
        "docs": [],
        "index": None,
        "primary_model": "qwen2.5:7b",
        "fallback_model": "qwen3:14b",
        "load_threshold": 200,
        "priority": 1,
        "max_load": 209
    },
    # Operator 2026-07-02 (audyt trafności person): handoffy sesyjne agentów zaśmiecały
    # kwadranty person (creative 6/60, family 0/44 tematów własnych). Ops = dom dla logów
    # sesji / sync agent↔agent / metryk. Persony widzą go cross-quadrant (top-1), nie jako własny.
    "ops": {
        "name": "Operations & Session Logs",
        "description": "Handoffy sesyjne, sync agentów, metryki, przebiegi napraw",
        "docs": [],
        "index": None,
        "primary_model": "qwen3:14b",
        "fallback_model": "qwen2.5:7b",
        "load_threshold": 300,
        "priority": 2,
        "max_load": 370
    }
}

# Konfiguracja embedding modelu
# Operator 2026-06-08: text-embedding-3-small (OpenAI/CLOUD, 1536) → qwen3-embedding
# (LOKALNY .10 GPU, 1024). Zero-cloud + spójny z Qdrant rag_service.
# Operator 2026-07-02: cięższy tier embeddingu na z13 (.172, 64GB UMA) — qwen3-embedding:4b
# (2560-dim) dla MOCNIEJSZEGO recallu person. Latencja +80ms/query (180 vs 101ms 0.6b) — OK.
# OSOBNY namespace (suffix _z13): kolekcje rag_<q>_z13 + pliki {q}_z13_*, żeby NIE ruszać
# współdzielonych 1024-dim kolekcji (rag_tech pisany TEŻ przez app/services/rag_service.py,
# plus shadowai_knowledge/learning_memory itd.). Flip powrotny = env RAG_EMBED_* na
# 0.6b/1024/localhost/"" (stare indexy 1024 zostają nietknięte na dysku i w Qdrant).
EMBEDDING_CONFIG = {
    "model": os.getenv("RAG_EMBED_MODEL", "qwen3-embedding:4b"),
    "dimension": int(os.getenv("RAG_EMBED_DIM", "2560")),
    "host": os.getenv("RAG_EMBED_HOST", "http://10.0.0.172:11434"),
    "keep_alive": os.getenv("RAG_EMBED_KEEP_ALIVE", "30m"),
    "collection_suffix": os.getenv("RAG_EMBED_COLL_SUFFIX", "_z13"),
    "faiss_suffix": os.getenv("RAG_EMBED_FAISS_SUFFIX", "_z13"),
    "batch_size": 32,
}

# Operator 2026-07-30 (fail-loud): jedyną barierą między tierem 2560 a współdzielonymi
# kolekcjami 1024 (rag_tech pisany też przez app/services/rag_service.py) jest suffix.
# Pusty suffix przy dim≠1024 wysłałby 2560-wektory do przestrzeni 1024 — upsert-fail
# połykany warningiem + FAISS niezgodny wymiarowo. Lepiej nie wstać niż zatruć store.
if EMBEDDING_CONFIG["dimension"] != 1024 and not EMBEDDING_CONFIG["collection_suffix"]:
    raise RuntimeError(
        f"RAG_EMBED_DIM={EMBEDDING_CONFIG['dimension']} wymaga niepustego "
        f"RAG_EMBED_COLL_SUFFIX — pusty suffix celuje we współdzielone kolekcje 1024-dim"
    )

# Konfiguracja FAISS
FAISS_CONFIG = {
    "index_type": "IndexFlatL2",  # L2 distance (Euclidean)
    "metric_type": "L2",
    "batch_add_size": 10000
}

# Routing policy
ROUTING_POLICY = {
    "strategy": "load-aware",  # load-aware, priority-based, round-robin
    "max_retries": 3,
    "fallback_enabled": True,
    "cross_quadrant_allowed": True
}

# Cache configuration
CACHE_CONFIG = {
    "enabled": True,
    "ttl_seconds": 3600,  # 1 hour
    "max_size_mb": 512
}

# Persistence
PERSISTENCE_CONFIG = {
    "enabled": True,
    # Operator 2026-06-08: była windowsowa ścieżka (c:\...) użyta dosłownie na Linux .10
    "path": os.getenv("RAG_INDEX_DIR", "/opt/shadow/shadow/backend/data/rag/indexes"),
    "format": "faiss",
    "auto_backup_interval_hours": 24
}
