"""JEV — System One dla organizmu ShadowAI.

Claude-10 2026-09-20. TypeSafe AI wypuściło 15.09.2026 model `Jev`: zamiast prozy
zwraca TYPOWANE sądy z prawdopodobieństwem (bool / choice / score), kalibrowane tak,
żeby „80% znaczyło 80%". Ich model jest zamknięty (API, $0.042/M tok, RLCD nieujawnione).
Ten moduł to NIE klient ich API. To ten sam KONTRAKT zrobiony na naszym żelazie:
tani, szybki, mierzalny odruch, który spina organizm jednym słownikiem decyzji.

Dlaczego to „zespaja organizm" (i dlaczego psychologia, nie metafora):

  System 1 / System 2 (Kahneman; Evans & Stanovich). Dziś KAŻDA bramka Shadowa woła
  System 2 — duży model piszący prozę — nawet po odpowiedź „tak/nie". Mentor płaci
  60–120 s za ocenę, router pisze zdanie żeby wybrać węzeł, dedup pisze esej żeby
  powiedzieć „to ten sam klip". Organizm nie ma odruchu. JEV jest odruchem: jedno
  przejście, jeden token, rozkład prawdopodobieństwa zamiast zdania.

  Metapoznanie i kalibracja (Fleming, Lau; „meta-d'"). Odruch bez wiedzy o własnej
  pewności jest groźniejszy niż brak odruchu. Dlatego każdy sąd niesie pewność, a
  pewność jest KALIBROWANA na historii — temperature scaling na logitach, mierzone
  Brier score i ECE. Bez kalibracji sąd wychodzi z `calibrated=False` i mówi to wprost.

  Teoria detekcji sygnału (SDT). Pewność = odległość zmiennej decyzyjnej od kryterium.
  Kryterium NIE jest 0.5 — przesuwa je asymetria kosztów: przepuścić destrukcyjną
  akcję kosztuje więcej niż zapytać operatora. Stąd `cost_fp`/`cost_fn` w pytaniu
  i próg = cost_fp/(cost_fp+cost_fn), a nie połowa.

  Selective prediction / reject option. Pas niepewności wokół kryterium to ABSTYNENCJA,
  nie zgadywanie. Abstynencja eskaluje do Systemu 2 (duży model) albo do operatora.
  Nigdy nie zwracamy „0.5 bo nie wiem" jako werdyktu — [[feedback_szlif_ukrywal_martwy_model]]
  kosztowało nas dokładnie to: domyślne 0.5 udające ocenę modelu.

Jak liczymy prawdopodobieństwo (to jest cały algorytm):

  1. Prompt = STAN (wspólny prefiks, cache KV) + JEDNO pytanie na końcu.
  2. Jedno wywołanie /v1/chat/completions, `max_tokens` ~8, temperature 0,
     `logprobs: true`, `top_logprobs: 20`. Ollama 0.34 to oddaje (zmierzone 2026-09-20).
  3. Skanujemy kolejne pozycje tokenów. Model bywa gadatliwy albo wypluwa token
     kanału (gemma4 zaczyna od `<|channel|>` — zmierzone), więc bierzemy PIERWSZĄ
     pozycję, na której kandydaci odpowiedzi mają sensowną masę. Nie pierwszy token.
  4. Masa kandydatów → rozkład: p_i = exp(lp_i) / Σ exp(lp_j) po kandydatach.
     Warianty pisowni ("yes", "Yes", " yes", "tak", "TRUE") sumują się do jednej klasy.
  5. Kalibracja: p_i ∝ p_i^(1/T), T dopasowane per klucz pytania na odpowiedziach
     zwrotnych (`record_outcome`). T=1 i `calibrated=False` dopóki nie ma danych.
  6. Pewność: bool → |2p−1|; choice/score → 1 − H/H_max (entropia znormalizowana).
  7. Decyzja: próg z kosztów, pas abstynencji z `margin`.

  `score` to NIE jedna liczba z modelu (modele są słabe w arytmetyce — to ograniczenie
  wprost z opisu Jeva). To wartość OCZEKIWANA po rozkładzie na uporządkowanych poziomach:
  E[level] = Σ p_i · i. Rozkład robi arytmetykę za model.

Uczciwość ponad wygodę:
  - `model_used` to model, który ZWRÓCIŁ odpowiedź, nie zamówiony ([[feedback_router_cicha_podmiana_modelu]]).
  - brak logprobów → `method="vote"` (k próbek, częstość) albo `method="format"`
    (constrained JSON, bez rozkładu) — i to widać w wyniku, nie chowa się.
  - brak sygnału → `abstain=True` + powód. Nigdy cichy fallback.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
import contextvars
import copy
import importlib
import statistics
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

import aiosqlite
import httpx

from app.config import settings

logger = logging.getLogger("shadowai.jev")

_DB = str(settings.db_path)

# ── konfiguracja odruchu ─────────────────────────────────────────────────────
# Model ma być MAŁY i rezydentny. Sens JEV-a znika, gdy każde pytanie ładuje 17 GB.
# 2026-09-26 (operator: „zobacz, czy JEV sobie radzi”): na GPU .10 odruch był co chwilę wypychany
# (MAX_LOADED_MODELS=1, gemma4:12b od WA) — `/health` 55 s, pielęgnacja co 6 h zawsze `cold`, bramka
# rag_resolve z budżetem 8 s nie miała szans. Na instancji CPU .10 (:11435, keep_alive −1, ta sama,
# na której od 25.09 mieszka embedder) sąd trwa stale 0,76–0,94 s, bez eksmisji. Ten sam model →
# kalibracje (klucz: pytanie + model) zostają ważne. GPU zostaje do jawnego wyboru przez env.
JEV_URL = os.getenv("SHADOW_JEV_URL", getattr(settings, "ollama_cpu_url", "") or "http://127.0.0.1:11435")
# Węzeł można wybrać per wywołanie (2026-09-24, Minecraft): na .10 Ollama ma MAX_LOADED_MODELS=1,
# więc każdy inny model (np. gemma4:12b od odpowiedzi WA) wypycha odruch i następny sąd czeka ~54 s.
# Pętla gry pytająca co sekundę zagłodziłaby resztę organizmu — dostaje własny węzeł (.130).
# Tylko węzły z tej listy: `node` przychodzi z API, więc nie wolno z niego zrobić dowolnego URL-a.
# 29.09 (operator: „lapek stoi … naucz shadow rozumieć w ten sposób, jev?”): .237 (GTX 1650 Ti 4 GB)
# trzyma gemma3:4b-rozdzielacz cały na karcie (~1 s na sąd rozdzielacza, zmierzone).
JEV_NODES = {"10": JEV_URL, "130": settings.ollama_guest_url, "237": "http://10.0.0.237:11434"}
_jev_url: contextvars.ContextVar[str] = contextvars.ContextVar("jev_url", default=JEV_URL)


# ── pamięć podręczna sądów (2026-11-01) ──────────────────────────────────────
# Minecraft: 94 % z 356 tys. sądów mc_akcja to powtórki tego samego state_hash z tą samą etykietą
# (jeden stan 47 878 razy) — każdy to wywołanie modelu i wiersz w jev_judgments. Pamięć w procesie:
# klucz (sha1(state)[:16], qkey, model), wartość = ostatni sąd BEZ błędu i bez abstynencji.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


JEV_CACHE_TTL_S = _env_float("JEV_CACHE_TTL_S", 600.0)
JEV_CACHE_MAX = 5000
_jev_cache: "OrderedDict[tuple[str, str, str], tuple[float, Judgment]]" = OrderedDict()
_jev_cache_hits = 0


def _cache_clock() -> float:
    return time.monotonic()


def _state_hash(state: str) -> str:
    """Ten sam odcisk stanu, który ląduje w jev_judgments.state_hash."""
    return hashlib.sha1(state.encode("utf-8", "replace")).hexdigest()[:16]


def _cache_get(key: tuple[str, str, str]) -> "Judgment | None":
    global _jev_cache_hits
    hit = _jev_cache.get(key)
    if hit is None:
        return None
    at, j = hit
    if _cache_clock() - at > JEV_CACHE_TTL_S:
        _jev_cache.pop(key, None)
        return None
    out = copy.deepcopy(j)
    out.method = "cache"
    out.latency_ms = 0
    _jev_cache_hits += 1
    if _jev_cache_hits % 1000 == 0:
        logger.info("[jev] pamięć sądów: %d trafień (wpisów %d)", _jev_cache_hits, len(_jev_cache))
    return out


def _cache_put(key: tuple[str, str, str], j: "Judgment") -> None:
    if j.method == "error" or j.abstain:
        return
    _jev_cache[key] = (_cache_clock(), copy.deepcopy(j))
    _jev_cache.move_to_end(key)
    while len(_jev_cache) > JEV_CACHE_MAX:
        _jev_cache.popitem(last=False)


def _url() -> str:
    return _jev_url.get()
# Wybór modelu jest POMIAREM, nie gustem. Dwa losowania po 80–120 realnych decyzji
# z `action_audit` (etykieta: czy bramka faktycznie zatrzymała akcję do potwierdzenia):
#   losowanie 1: gemma4:e2b AUC 0.705 | qwen2.5:7b 0.853 | gemma4:e4b 0.828 | gemma3:4b 0.825
#   losowanie 2: gemma4:e2b AUC 0.790 | qwen2.5:7b 0.608  (to samo pytanie, inne próbki)
# Różnice mieszczą się w szumie próbki (n=120 → ±0.08), więc rozstrzyga koszt:
# e2b liczy sąd w ~160 ms ciepły (p50 490 ms przy 6 równoległych), 7b dwa razy wolniej.
# Zmiana modelu UNIEWAŻNIA kalibrację — dlatego kalibracja jest kluczowana (pytanie, model).
# Przemierzyć: `scripts/jev.py ab <model>`.
JEV_MODEL = os.getenv("SHADOW_JEV_MODEL", "gemma4:e2b-it-qat")
# Na CPU (RAM, nikt nie wypycha) odruch trzymamy na stałe; na GPU 30 min jak dotąd.
# 2026-10-08: liczone per węzeł zapytania, nie raz od JEV_URL — "-1" z .10 leciał też na .130,
# a Ollama 0.35.1 odrzuca go bez jednostki („missing unit in duration”), bot MC dostawał 400 co 1,5 s.
_JEV_KEEP_ALIVE_ENV = os.getenv("SHADOW_JEV_KEEP_ALIVE")
# 2026-10-29: po aktualizacji Ollamy .10 do 0.40.13 także :11435 odrzuca gołe "-1" (HTTP 400, 1077 osądów bez etykiety
# od ~12:00Z). Ujemny czas z jednostką = trzymaj bez końca (zmierzone: "-1m" → 200, expires_at 2319).
_BEZ_KONCA = "-1m"


def _keep_alive() -> str:
    return _JEV_KEEP_ALIVE_ENV or (_BEZ_KONCA if ":11435" in _url() else "30m")
JEV_TIMEOUT = float(os.getenv("SHADOW_JEV_TIMEOUT", "45"))
# Zimny start modelu to nie jest odruch — to ładowanie 4.7 GB. Rozgrzewka ma własny,
# długi budżet; pytania mają krótki. Inaczej pierwsze pytanie dnia zawsze wygląda
# na awarię (zmierzone 2026-09-20: health() padł na 45 s ReadTimeout po eksmisji modelu).
JEV_WARM_TIMEOUT = float(os.getenv("SHADOW_JEV_WARM_TIMEOUT", "300"))

_SCAN_TOKENS = 8      # ile pozycji tokenów wolno przeskanować w poszukiwaniu odpowiedzi
_TOP_LOGPROBS = 20    # ile kandydatów na pozycję prosimy od serwera
_MIN_MASS = 0.02      # minimalna masa kandydatów na pozycji, gdy klas jest >= 2
_SOLO_MASS = 0.50     # minimalna masa, gdy w top-20 stoi TYLKO jedna klasa (inaczej fałszywa pewność)
_MAX_CHOICE_INLINE = 24   # powyżej tego robimy turniej grupowy (map-reduce)
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWX"
_DIGITS = "000000000"

# Warianty pisowni sumowane do jednej klasy. Polski i angielski, bo organizm myśli w obu.
_TRUE_TOKENS = ("yes", "true", "tak", "y", "1", "prawda")
_FALSE_TOKENS = ("no", "false", "nie", "n", "0", "falsz", "fałsz")


class JevError(RuntimeError):
    """Awaria odruchu. Świadomie głośna — cichy except tu zabija werdykt."""


# ── kontrakt pytania ─────────────────────────────────────────────────────────
Kind = Literal["bool", "choice", "score"]


@dataclass
class Question:
    """Jedno pytanie o stan. `key` jest kluczem kalibracji — trzymaj je stabilne."""
    kind: Kind
    instructions: str
    options: list[str] = field(default_factory=list)   # choice
    levels: list[str] = field(default_factory=list)    # score (uporządkowane rosnąco)
    cost_fp: float = 1.0   # koszt fałszywie pozytywnej (bool)
    cost_fn: float = 1.0   # koszt fałszywie negatywnej (bool)
    margin: float = 0.15   # połowa pasa abstynencji wokół kryterium (bool) / min. pewność (reszta)
    # gemma4 przy pytaniu choice potrafi otworzyć kanał myślenia i w 8 tokenach nie dojść do
    # odpowiedzi (Minecraft 24.09: masa kandydatów 0.008 → wstrzymanie). `reasoning_effort: none`
    # daje czysty rozkład. Domyślnie WYŁĄCZONE: bramki organizmu kalibrowano przy obecnym zachowaniu.
    bez_myslenia: bool = False

    def validate(self, key: str) -> None:
        if self.kind == "choice":
            if len(self.options) < 2:
                raise JevError(f"[{key}] choice wymaga >= 2 opcji")
            if len(self.options) > 255:
                raise JevError(f"[{key}] choice: maks. 255 opcji")
        elif self.kind == "score":
            if not (2 <= len(self.levels) <= 9):
                raise JevError(f"[{key}] score wymaga 2..9 uporządkowanych poziomów")
        elif self.kind != "bool":
            raise JevError(f"[{key}] nieznany typ pytania: {self.kind}")

    @property
    def threshold(self) -> float:
        """Kryterium z asymetrii kosztów (SDT). Symetryczne koszty → 0.5."""
        total = self.cost_fp + self.cost_fn
        return 0.5 if total <= 0 else self.cost_fp / total

    @classmethod
    def parse(cls, raw: "Question | dict") -> "Question":
        if isinstance(raw, Question):
            return raw
        d = dict(raw)
        # zgodność z kontraktem Jeva: `type` == nasze `kind`; "noul" to ich nazwa na bool
        if "kind" not in d and "type" in d:
            d["kind"] = d["type"]
        d.pop("type", None)
        if d.get("kind") == "noul":
            d["kind"] = "bool"
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Judgment:
    key: str
    kind: Kind
    dist: dict[str, float]          # rozkład po klasach (skalibrowany)
    dist_raw: dict[str, float]      # rozkład surowy z logitów
    confidence: float
    abstain: bool
    reason: str = ""
    value: Any = None               # bool: p(true) | choice: etykieta | score: 0..1
    label: str = ""
    method: str = "logprobs"
    model_used: str = ""
    node: str = ""
    latency_ms: int = 0
    temperature_used: float = 1.0
    calibrated: bool = False
    signal: float = 0.0             # masa kandydatów na pozycji odpowiedzi (siła sygnału)
    judgment_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "kind": self.kind, "value": self.value, "label": self.label,
            "confidence": round(self.confidence, 4), "abstain": self.abstain,
            "reason": self.reason, "dist": {k: round(v, 5) for k, v in self.dist.items()},
            "dist_raw": {k: round(v, 5) for k, v in self.dist_raw.items()},
            "method": self.method, "model_used": self.model_used, "node": self.node,
            "latency_ms": self.latency_ms, "calibrated": self.calibrated,
            "temperature_used": round(self.temperature_used, 3), "signal": round(self.signal, 4),
            "judgment_id": self.judgment_id,
        }


# ── baza: sądy, wyniki, kalibracja ───────────────────────────────────────────
async def ensure_tables() -> None:
    async with aiosqlite.connect(_DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jev_judgments (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                qkey TEXT NOT NULL,
                kind TEXT NOT NULL,
                source TEXT DEFAULT '',
                state_hash TEXT DEFAULT '',
                state_head TEXT DEFAULT '',
                question TEXT DEFAULT '',
                dist_raw TEXT DEFAULT '{}',
                dist TEXT DEFAULT '{}',
                value TEXT DEFAULT '',
                label TEXT DEFAULT '',
                confidence REAL DEFAULT 0,
                abstain INTEGER DEFAULT 0,
                reason TEXT DEFAULT '',
                method TEXT DEFAULT '',
                model_used TEXT DEFAULT '',
                node TEXT DEFAULT '',
                latency_ms INTEGER DEFAULT 0,
                temperature_used REAL DEFAULT 1.0,
                calibrated INTEGER DEFAULT 0,
                signal REAL DEFAULT 0
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jev_outcomes (
                judgment_id TEXT PRIMARY KEY,
                qkey TEXT NOT NULL,
                actual TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT DEFAULT ''
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jev_calibration (
                qkey TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL DEFAULT 1.0,
                slope REAL NOT NULL DEFAULT 1.0,
                intercept REAL NOT NULL DEFAULT 0.0,
                n INTEGER NOT NULL DEFAULT 0,
                brier_before REAL, brier_after REAL,
                ece_before REAL, ece_after REAL,
                fitted_at TEXT NOT NULL,
                PRIMARY KEY (qkey, model)
            )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jev_j_key ON jev_judgments(qkey, created_at)")
        # migracja tabeli sprzed wprowadzenia siły sygnału (2026-09-20)
        cols = {r[1] for r in await (await db.execute("PRAGMA table_info(jev_judgments)")).fetchall()}
        if "signal" not in cols:
            await db.execute("ALTER TABLE jev_judgments ADD COLUMN signal REAL DEFAULT 0")
            logger.info("[jev] migracja: dołożona kolumna jev_judgments.signal")
        # 2026-09-21: kalibracja musi wiedzieć, DLA JAKIEGO PYTANIA ją policzono.
        ccols0 = {r[1] for r in await (await db.execute("PRAGMA table_info(jev_calibration)")).fetchall()}
        if ccols0 and "pytanie_hash" not in ccols0:
            await db.execute("ALTER TABLE jev_calibration ADD COLUMN pytanie_hash TEXT DEFAULT ''")
            logger.info("[jev] migracja: dołożona kolumna jev_calibration.pytanie_hash")
        # Kalibracja sprzed 2026-09-20 nie znała modelu ani przesunięcia kryterium.
        # To dane WYLICZONE — kasujemy je zamiast udawać, że pasują do innego modelu.
        ccols = {r[1] for r in await (await db.execute("PRAGMA table_info(jev_calibration)")).fetchall()}
        if ccols and "model" not in ccols:
            await db.execute("DROP TABLE jev_calibration")
            logger.warning("[jev] migracja: STARA kalibracja skasowana (nie wiedziała, jakim modelem "
                           "była liczona). Odtwórz: scripts/jev.py harvest <klucz>")
            await db.execute("""
                CREATE TABLE jev_calibration (
                    qkey TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                    temperature REAL NOT NULL DEFAULT 1.0, slope REAL NOT NULL DEFAULT 1.0,
                    intercept REAL NOT NULL DEFAULT 0.0, n INTEGER NOT NULL DEFAULT 0,
                    brier_before REAL, brier_after REAL, ece_before REAL, ece_after REAL,
                    fitted_at TEXT NOT NULL, PRIMARY KEY (qkey, model))""")
        await db.commit()


_CAL_CACHE: dict[str, tuple[float, float, int]] = {}
_CAL_CACHE_AT = 0.0


def pytanie_hash(instrukcja: str) -> str:
    """Odcisk TREŚCI pytania. Claude-10 2026-09-21: kalibracja była kluczowana (bramka, model),
    więc podmiana sformułowania po cichu zostawiała stare współczynniki opisujące INNE pytanie.
    Dziś to zabolało naprawdę: „wymaga_researchu” przeszło z AUC 0.452 na 0.946 po zmianie zdania,
    a tabela kalibracji nie miała jak tego zauważyć. Od teraz sąd jest `calibrated=True` tylko
    wtedy, gdy współczynniki policzono dla DOKŁADNIE tego pytania.
    """
    return hashlib.sha1((instrukcja or "").strip().encode("utf-8")).hexdigest()[:12]


async def _load_calibration() -> dict[tuple[str, str], tuple[float, float, int, str]]:
    """(qkey, model) → (slope a, intercept b, n, odcisk pytania). Platt: p' = sigma(a*logit(p) + b).

    Klucz zawiera MODEL, bo kalibracja jednego modelu nałożona na drugi to fałszerstwo:
    ten sam prompt dał AUC 0.79 na gemma4:e2b i 0.58 na qwen2.5:7b (zmierzone 2026-09-20).
    """
    global _CAL_CACHE, _CAL_CACHE_AT
    if _CAL_CACHE and (time.monotonic() - _CAL_CACHE_AT) < 60:
        return _CAL_CACHE
    out: dict[tuple[str, str], tuple[float, float, int]] = {}
    try:
        async with aiosqlite.connect(_DB) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT qkey, model, slope, intercept, n, "
                "COALESCE(pytanie_hash,'') AS pytanie_hash FROM jev_calibration")).fetchall()
        out = {(r["qkey"], r["model"]): (float(r["slope"]), float(r["intercept"]), int(r["n"]),
                                         str(r["pytanie_hash"]))
               for r in rows}
    except Exception as exc:  # tabela jeszcze nie istnieje — to nie jest werdykt, to brak danych
        logger.debug("[jev] brak kalibracji: %s", exc)
    _CAL_CACHE, _CAL_CACHE_AT = out, time.monotonic()
    return out


# ── transport ────────────────────────────────────────────────────────────────
_http: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=JEV_TIMEOUT)
    return _http


async def warm(model: str | None = None) -> dict:
    """Wepchnij model do pamięci i trzymaj (keep_alive). Zwraca zmierzony czas."""
    model = model or JEV_MODEL
    t0 = time.monotonic()
    try:
        r = await _client().post(
            f"{_url().rstrip('/')}/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "ok"}],
                  "max_tokens": 1, "temperature": 0, "keep_alive": _keep_alive()},
            timeout=JEV_WARM_TIMEOUT)
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code >= 400:
            return {"ok": False, "model": model, "ms": ms, "error": f"HTTP {r.status_code} {r.text[:160]}"}
        return {"ok": True, "model": str(r.json().get("model") or model), "ms": ms,
                "cold": ms > 3000}
    except Exception as exc:
        return {"ok": False, "model": model, "ms": int((time.monotonic() - t0) * 1000),
                "error": f"{type(exc).__name__}: {str(exc)[:160] or '(timeout bez treści)'}"}


async def _complete(messages: list[dict], model: str, max_tokens: int,
                    logprobs: bool = True, bez_myslenia: bool = False) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "keep_alive": _keep_alive(),
    }
    if bez_myslenia:
        body["reasoning_effort"] = "none"
    if logprobs:
        body["logprobs"] = True
        body["top_logprobs"] = _TOP_LOGPROBS
    r = await _client().post(f"{_url().rstrip('/')}/v1/chat/completions", json=body)
    if r.status_code >= 400:
        raise JevError(f"{model} @ {_url()}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


# ── ekstrakcja rozkładu ──────────────────────────────────────────────────────
def _norm_token(tok: str) -> str:
    return tok.strip().strip("*`'\"”“()[]{}<>:.,;!?-_ ").lower()


def _match_class(tok: str, classes: dict[str, tuple[str, ...]]) -> str | None:
    t = _norm_token(tok)
    if not t:
        return None
    for cls, variants in classes.items():
        if t in variants:
            return cls
    return None


def _distribution(resp: dict, classes: dict[str, tuple[str, ...]]) -> tuple[dict[str, float], float, str]:
    """Pierwsza pozycja tokenu, która NAPRAWDĘ niesie odpowiedź.

    Zwraca (rozkład znormalizowany po klasach, masa surowa na tej pozycji, powód-gdy-pusto).

    Dwa warunki, bo jeden kłamał. Sama masa >= _MIN_MASS wystarczy, gdy w top-20 stoi
    kilka klas — wtedy widzimy spór modelu ze sobą i to jest sygnał. Ale gdy w top-20
    jest JEDNA klasa z masą 0.03, normalizacja robi z niej 1.0 i odruch krzyczy
    „pewność 100%" na podstawie szumu (zmierzone 2026-09-20: `score` zwrócił
    „błaha" z pewnością 1.0, bo na skanowanej pozycji była tylko cyfra „1").
    Dlatego: albo >= 2 klasy w top-20, albo jedna klasa, ale dominująca (>= _SOLO_MASS).
    """
    try:
        content = resp["choices"][0]["logprobs"]["content"]
    except (KeyError, IndexError, TypeError):
        return {}, 0.0, "brak logprobs w odpowiedzi serwera"
    if not content:
        return {}, 0.0, "pusta lista tokenów"

    best_seen = 0.0
    for step in content[:_SCAN_TOKENS]:
        mass: dict[str, float] = {}
        for cand in step.get("top_logprobs") or []:
            cls = _match_class(cand.get("token", ""), classes)
            if cls is None:
                continue
            mass[cls] = mass.get(cls, 0.0) + math.exp(float(cand.get("logprob", -99)))
        total = sum(mass.values())
        best_seen = max(best_seen, total)
        if not mass:
            continue
        if (len(mass) >= 2 and total >= _MIN_MASS) or total >= _SOLO_MASS:
            return {k: v / total for k, v in mass.items()}, total, ""
    return {}, best_seen, (
        f"żaden z {_SCAN_TOKENS} tokenów nie niósł rozstrzygnięcia "
        f"(najlepsza masa kandydatów {best_seen:.3f})")


def _apply_temperature(dist: dict[str, float], t: float) -> dict[str, float]:
    if not dist or t <= 0 or abs(t - 1.0) < 1e-6:
        return dict(dist)
    powered = {k: (v ** (1.0 / t) if v > 0 else 0.0) for k, v in dist.items()}
    s = sum(powered.values())
    return {k: v / s for k, v in powered.items()} if s > 0 else dict(dist)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _platt(p: float, a: float, b: float) -> float:
    """Kalibracja dwuparametrowa. `a` prostuje ROZCIĄGNIĘCIE (czułość, d' w SDT),
    `b` przesuwa KRYTERIUM (c w SDT).

    Jednoparametrowa temperatura tu nie wystarcza i to jest pomiar, nie teoria:
    2026-09-20 na 80 realnych decyzjach z `action_audit` małe modele miały AUC
    0.83–0.85 (świetnie RANKUJĄ ryzyko), ale mówiły „tak" prawie wszystkiemu —
    trafność przy progu 0.5 wynosiła 0.50, Brier 0.41–0.50. Model widział różnicę,
    kryterium jej nie widziało. Sama temperatura skaluje, nie przesuwa — zostawiłaby
    ten sam błąd.
    """
    if abs(a - 1.0) < 1e-9 and abs(b) < 1e-9:
        return p
    return _sigmoid(a * _logit(p) + b)


def _fit_platt(pairs: list[tuple[float, int]], iters: int = 4000, lr: float = 0.25) -> tuple[float, float]:
    """Regresja logistyczna na logicie surowego p. Wypukła, dwa parametry, bez bibliotek."""
    a, b = 1.0, 0.0
    xs = [(_logit(p), y) for p, y in pairs]
    n = max(len(xs), 1)
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in xs:
            e = _sigmoid(a * x + b) - y
            ga += e * x
            gb += e
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def _entropy_confidence(dist: dict[str, float]) -> float:
    if len(dist) < 2:
        return 1.0 if dist else 0.0
    h = -sum(p * math.log(p) for p in dist.values() if p > 0)
    return max(0.0, 1.0 - h / math.log(len(dist)))


# ── budowa promptu ───────────────────────────────────────────────────────────
_SYSTEM = (
    "You are a decision unit, not an assistant. You do not explain, apologize or write prose. "
    "You read the STATE and answer the QUESTION with EXACTLY ONE token from the allowed set. "
    "No punctuation, no reasoning, no other words."
)


def _messages(state: str, q: Question, key: str, options: list[str] | None = None) -> tuple[list[dict], dict[str, tuple[str, ...]], dict[str, str]]:
    """Zwraca (messages, klasy→warianty tokenów, mapowanie klasa→etykieta)."""
    if q.kind == "bool":
        classes = {"true": _TRUE_TOKENS, "false": _FALSE_TOKENS}
        labels = {"true": "tak", "false": "nie"}
        tail = f"QUESTION ({key}): {q.instructions}\nAnswer with one token: yes or no."
    elif q.kind == "choice":
        opts = options if options is not None else q.options
        classes = {L: (L.lower(),) for L in _LETTERS[:len(opts)]}
        labels = {_LETTERS[i]: o for i, o in enumerate(opts)}
        listing = "\n".join(f"{_LETTERS[i]}) {o}" for i, o in enumerate(opts))
        tail = (f"QUESTION ({key}): {q.instructions}\nOptions:\n{listing}\n"
                f"Answer with one token: the letter of the best option.")
    else:  # score
        k = len(q.levels)
        classes = {d: (d,) for d in _DIGITS[:k]}
        labels = {_DIGITS[i]: lv for i, lv in enumerate(q.levels)}
        listing = "\n".join(f"{i + 1} = {lv}" for i, lv in enumerate(q.levels))
        tail = (f"QUESTION ({key}): {q.instructions}\nScale:\n{listing}\n"
                f"Answer with one token: a digit 1-{k}.")
    return (
        [{"role": "system", "content": _SYSTEM},
         {"role": "user", "content": f"STATE:\n{state}\n\n{tail}"}],
        classes, labels,
    )


# ── jedno pytanie ────────────────────────────────────────────────────────────
async def _ask_one(state: str, key: str, q: Question, model: str,
                   cal: dict[tuple[str, str], tuple[float, float, int]]) -> Judgment:
    q.validate(key)
    t0 = time.monotonic()

    if q.kind == "choice" and len(q.options) > _MAX_CHOICE_INLINE:
        return await _ask_choice_tournament(state, key, q, model, cal)

    messages, classes, labels = _messages(state, q, key)
    try:
        resp = await _complete(messages, model, max_tokens=_SCAN_TOKENS, bez_myslenia=q.bez_myslenia)
        retried = False
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        # Zmierzone 2026-09-20: eskalacja do Systemu 2 wciągnęła 18.6 GB (qwen3.8:27b)
        # i wypchnęła model odruchu z VRAM-u — następne pytanie padało na timeout.
        # Odruch ma wstawać sam: jedna rozgrzewka z długim budżetem i JEDNA powtórka.
        logger.info("[jev] %s: %s — rozgrzewam %s i próbuję raz jeszcze", key,
                    type(exc).__name__, model)
        w = await warm(model)
        if not w.get("ok"):
            raise JevError(f"model {model} nie wstał po rozgrzewce: {w.get('error')}") from exc
        resp = await _complete(messages, model, max_tokens=_SCAN_TOKENS, bez_myslenia=q.bez_myslenia)
        retried = True
    used_model = str(resp.get("model") or model)   # model ZWRÓCONY, nie zamówiony
    method = "logprobs+rozgrzewka" if retried else "logprobs"
    raw, signal, why = _distribution(resp, classes)
    latency = int((time.monotonic() - t0) * 1000)

    if not raw:
        return Judgment(key=key, kind=q.kind, dist={}, dist_raw={}, confidence=0.0,
                        abstain=True, reason=why, method=method, model_used=used_model,
                        node=_url(), latency_ms=latency, signal=signal)

    slope, intercept, n_cal, cal_hash = cal.get((key, used_model), cal.get((key, model), (1.0, 0.0, 0, "")))
    biezacy_hash = pytanie_hash(q.instructions[:400])
    if n_cal and cal_hash and cal_hash != biezacy_hash:
        # Treść pytania zmieniła się od czasu kalibracji — stare współczynniki opisują co innego.
        # Mówimy to wprost (calibrated=False) zamiast udawać pewność.
        logger.warning("[jev] %s: kalibracja policzona dla INNEGO sformułowania (%s ≠ %s) — "
                       "sąd leci NIESKALIBROWANY, przelicz żniwa", key, cal_hash, biezacy_hash)
        slope, intercept, n_cal = 1.0, 0.0, 0
    if q.kind == "bool":
        p_cal = _platt(raw.get("true", 0.0), slope, intercept)
        dist = {"true": p_cal, "false": 1.0 - p_cal}
    else:
        # wielo-klasowo przesunięcie kryterium nie ma sensu — skalujemy ostrość rozkładu
        dist = _apply_temperature(raw, 1.0 / slope if slope > 0 else 1.0)
    temp = slope
    j = Judgment(key=key, kind=q.kind, dist=dist, dist_raw=raw, confidence=0.0, abstain=False,
                 method=method, model_used=used_model, node=_url(), latency_ms=latency,
                 temperature_used=temp, calibrated=n_cal > 0, signal=signal)

    if q.kind == "bool":
        p = dist.get("true", 0.0)
        j.value = p
        thr = q.threshold
        j.label = labels["true"] if p >= thr else labels["false"]
        j.confidence = abs(p - thr) / max(thr, 1.0 - thr)   # odległość od kryterium (SDT)
        if abs(p - thr) < q.margin:
            j.abstain, j.reason = True, f"p={p:.3f} w pasie ±{q.margin} wokół kryterium {thr:.2f}"
    elif q.kind == "choice":
        top = max(dist, key=dist.get)
        j.value = labels[top]
        j.label = labels[top]
        j.dist = {labels[k]: v for k, v in dist.items()}
        j.dist_raw = {labels[k]: v for k, v in raw.items()}
        j.confidence = _entropy_confidence(dist)
        if j.confidence < q.margin:
            j.abstain, j.reason = True, f"rozkład rozmyty (pewność {j.confidence:.2f} < {q.margin})"
    else:  # score — wartość OCZEKIWANA, nie token
        k = len(q.levels)
        exp_level = sum(dist[d] * (i + 1) for i, d in enumerate(_DIGITS[:k]) if d in dist)
        j.value = (exp_level - 1.0) / (k - 1)               # 0..1
        nearest = min(range(k), key=lambda i: abs((i + 1) - exp_level))
        j.label = q.levels[nearest]
        j.dist = {labels[d]: v for d, v in dist.items()}
        j.dist_raw = {labels[d]: v for d, v in raw.items()}
        j.confidence = _entropy_confidence(dist)
        if j.confidence < q.margin:
            j.abstain, j.reason = True, f"rozkład rozmyty (pewność {j.confidence:.2f} < {q.margin})"
    return j


async def _ask_choice_tournament(state: str, key: str, q: Question, model: str,
                                 cal: dict[tuple[str, str], tuple[float, float, int]]) -> Judgment:
    """>24 opcji: map-reduce. Grupy po 24, zwycięzcy grup wchodzą do finału.

    Prawdopodobieństwo finalne = p(zwycięzca w grupie) · p(zwycięzca w finale) —
    łańcuch, nie zgadywanka. Do 24×24 = 576 opcji przy dwóch rundach.
    """
    groups = [q.options[i:i + _MAX_CHOICE_INLINE] for i in range(0, len(q.options), _MAX_CHOICE_INLINE)]
    subs = await asyncio.gather(*[
        _ask_one(state, f"{key}#g{i}", Question(kind="choice", instructions=q.instructions,
                                                options=g, margin=0.0), model, cal)
        for i, g in enumerate(groups)
    ], return_exceptions=True)
    winners: list[str] = []
    p_group: dict[str, float] = {}
    for s in subs:
        if isinstance(s, Judgment) and s.label:
            winners.append(s.label)
            p_group[s.label] = s.dist.get(s.label, 0.0)
    if not winners:
        return Judgment(key=key, kind="choice", dist={}, dist_raw={}, confidence=0.0,
                        abstain=True, reason="turniej: żadna grupa nie dała zwycięzcy",
                        method="tournament", node=_url())
    if len(winners) == 1:
        final = Judgment(key=key, kind="choice", dist={winners[0]: p_group[winners[0]]},
                         dist_raw={winners[0]: p_group[winners[0]]},
                         confidence=p_group[winners[0]], abstain=False, value=winners[0],
                         label=winners[0], method="tournament", node=_url())
        return final
    fin = await _ask_one(state, f"{key}#final",
                         Question(kind="choice", instructions=q.instructions, options=winners,
                                  margin=q.margin), model, cal)
    fin.key = key
    fin.method = "tournament"
    fin.dist = {lbl: p * p_group.get(lbl, 1.0) for lbl, p in fin.dist.items()}
    s = sum(fin.dist.values()) or 1.0
    fin.dist = {k2: v / s for k2, v in fin.dist.items()}
    fin.confidence = _entropy_confidence(fin.dist)
    return fin


# ── API modułu ───────────────────────────────────────────────────────────────
async def ask(state: str, questions: dict[str, "Question | dict"], *,
              model: str | None = None, source: str = "", persist: bool = True,
              node: str | None = None, cache: bool | None = None) -> dict[str, dict]:
    """Zadaj N pytań o JEDEN stan. Wszystkie lecą równolegle — dokładanie pytania
    kosztuje tokeny pytania, nie kolejną rundę myślenia. `node` = klucz z JEV_NODES.
    `cache`: None = pamięć sądów tylko dla source 'minecraft*'; True/False wymusza."""
    if not questions:
        return {}
    if node:
        if node not in JEV_NODES:
            raise JevError(f"nieznany węzeł JEV: {node} (znane: {', '.join(JEV_NODES)})")
        _jev_url.set(JEV_NODES[node])   # kontekst tego zadania; gather() dziedziczy kopię
    model = model or JEV_MODEL
    cal = await _load_calibration()
    parsed = {k: Question.parse(v) for k, v in questions.items()}
    use_cache = bool(cache) if cache is not None else str(source or "").startswith("minecraft")
    sh = _state_hash(state) if use_cache else ""
    hits: dict[str, Judgment] = {}
    if use_cache:
        for k in parsed:
            c = _cache_get((sh, k, model))
            if c is not None:
                hits[k] = c
    pending = {k: q for k, q in parsed.items() if k not in hits}
    results = (await asyncio.gather(*[_ask_one(state, k, q, model, cal) for k, q in pending.items()],
                                    return_exceptions=True)) if pending else []
    res_map = dict(zip(pending, results))
    out: dict[str, dict] = {}
    to_store: list[Judgment] = []
    for k, q in parsed.items():
        if k in hits:   # trafienie: bez modelu i bez wiersza w jev_judgments
            j = hits[k]
            j.judgment_id = uuid.uuid4().hex
            out[k] = j.to_dict()
            continue
        res = res_map[k]
        if isinstance(res, BaseException):
            logger.warning("[jev] %s padło: %s", k, res)
            detail = str(res)[:160] or f"(bez treści; budżet {JEV_TIMEOUT:.0f} s — model mógł być zimny)"
            j = Judgment(key=k, kind=q.kind, dist={}, dist_raw={}, confidence=0.0, abstain=True,
                         reason=f"{type(res).__name__}: {detail}", method="error",
                         model_used=model, node=_url())
        else:
            j = res
        j.judgment_id = uuid.uuid4().hex
        out[k] = j.to_dict()
        to_store.append(j)
        if use_cache:
            _cache_put((sh, k, model), j)
    if persist and to_store:
        await _store(state, parsed, to_store, source)
    return out


async def _store(state: str, questions: dict[str, Question], judgments: list[Judgment], source: str) -> None:
    try:
        await ensure_tables()
        now = _now()
        sh = _state_hash(state)
        async with aiosqlite.connect(_DB) as db:
            for j in judgments:
                q = questions.get(j.key)
                await db.execute(
                    """INSERT OR REPLACE INTO jev_judgments
                       (id, created_at, qkey, kind, source, state_hash, state_head, question,
                        dist_raw, dist, value, label, confidence, abstain, reason, method,
                        model_used, node, latency_ms, temperature_used, calibrated, signal)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (j.judgment_id, now, j.key, j.kind, source, sh, state[:400],
                     (q.instructions if q else "")[:400], json.dumps(j.dist_raw),
                     json.dumps(j.dist), json.dumps(j.value), j.label, j.confidence,
                     int(j.abstain), j.reason, j.method, j.model_used, j.node, j.latency_ms,
                     j.temperature_used, int(j.calibrated), j.signal))
            await db.commit()
    except Exception as exc:  # zapis to nie werdykt — ale mówimy głośno
        logger.warning("[jev] nie zapisałem sądów: %s", exc)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def record_outcome(judgment_id: str, actual: str | bool, note: str = "") -> bool:
    """Jak było NAPRAWDĘ. Bez tego kalibracja jest literaturą."""
    await ensure_tables()
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT qkey FROM jev_judgments WHERE id=?", (judgment_id,))).fetchone()
        if not row:
            return False
        val = "true" if actual is True else "false" if actual is False else str(actual)
        await db.execute(
            "INSERT OR REPLACE INTO jev_outcomes (judgment_id, qkey, actual, created_at, note) VALUES (?,?,?,?,?)",
            (judgment_id, row["qkey"], val, _now(), note))
        await db.commit()
    return True


# ── egzamin: TEN SAM zestaw par dla każdego kandydata na odruch ──────────────
# 2026-11-03: jev_outcomes ma 11 666 par sąd → prawdziwy wynik, a trafność obecnego modelu na
# akcja_wymaga_zgody / wymaga_researchu / ten_sam_material jest bliska losowej (55 / 59,5 / 52 %).
# Nie było jak sprawdzić, czy inny model sądziłby lepiej. Zestaw losuje się RAZ i zostaje zamrożony,
# żeby dwa modele dostały te same pytania — inaczej porównanie jest szumem próbki (patrz JEV_MODEL).
_BOOL_TAK = ("tak", "true", "yes", "y", "1", "prawda")
_BOOL_NIE = ("nie", "false", "no", "n", "0", "falsz", "fałsz")


def _norm_etykieta(kind: str, s: Any) -> str:
    """bool → 'tak'/'nie' po normalizacji tak/nie/true/false/1/0; reszta → bez wielkości liter."""
    t = str(s if s is not None else "").strip().lower()
    if kind == "bool":
        if t in _BOOL_TAK:
            return "tak"
        if t in _BOOL_NIE:
            return "nie"
    return t


async def _ensure_egzamin() -> None:
    await ensure_tables()
    async with aiosqlite.connect(_DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jev_egzamin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                judgment_id TEXT NOT NULL UNIQUE,
                qkey TEXT NOT NULL,
                kind TEXT NOT NULL,
                state TEXT DEFAULT '',
                question TEXT DEFAULT '',
                actual TEXT NOT NULL,
                wylosowano TEXT NOT NULL
            )""")
        await db.commit()


async def zestaw_egzaminu(n_na_pytanie: int = 60) -> dict:
    """Jednorazowo losuje po `n_na_pytanie` par z jev_outcomes ⋈ jev_judgments na każde pytanie
    (state_head jako stan, question, kind), bez sądów ze źródeł 'minecraft*'.
    Zestaw jest STAŁY: gdy tabela już coś ma, nic nie dolosowujemy — zwracamy istniejący."""
    await _ensure_egzamin()
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        per = await (await db.execute("SELECT qkey, COUNT(*) n FROM jev_egzamin GROUP BY qkey")).fetchall()
        if per:
            return {"nowy": False, "n": sum(int(r["n"]) for r in per),
                    "per_qkey": {r["qkey"]: int(r["n"]) for r in per}}
        qkeys = [r["qkey"] for r in await (await db.execute(
            "SELECT DISTINCT qkey FROM jev_outcomes ORDER BY qkey")).fetchall()]
        now = _now()
        out: dict[str, int] = {}
        for qk in qkeys:
            rows = await (await db.execute(
                """SELECT o.judgment_id, o.qkey, j.kind, j.state_head, j.question, o.actual
                   FROM jev_outcomes o JOIN jev_judgments j ON j.id = o.judgment_id
                   WHERE o.qkey = ? AND COALESCE(j.source, '') NOT LIKE 'minecraft%'
                   ORDER BY RANDOM() LIMIT ?""", (qk, int(n_na_pytanie)))).fetchall()
            for r in rows:
                await db.execute(
                    "INSERT OR IGNORE INTO jev_egzamin (judgment_id, qkey, kind, state, question, actual, wylosowano)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (r["judgment_id"], r["qkey"], r["kind"] or "bool", r["state_head"] or "",
                     r["question"] or "", str(r["actual"]), now))
            if rows:
                out[qk] = len(rows)
        await db.commit()
    logger.info("[jev] egzamin: wylosowano zestaw %d par (%s)", sum(out.values()), out)
    return {"nowy": True, "n": sum(out.values()), "per_qkey": out}


async def _zapisz_do_areny(**kw) -> Any:
    """Zapis do arena_wyniki przez arena_modeli_2_hand.zapisz_wynik (import leniwy — moduł areny
    nie jest potrzebny do sądzenia). Obsługuje wersję sync i async funkcji."""
    arena = importlib.import_module("app.services.arena_modeli_2_hand")
    r = arena.zapisz_wynik(**kw)
    if asyncio.iscoroutine(r):
        r = await r
    return r


async def egzamin(model: str, node: str | None = None, limit: int | None = None) -> dict:
    """Przepuść model przez zamrożony zestaw. Jedno pytanie naraz (sekwencyjnie), bez zapisu sądów
    (persist=False, source='egzamin'). Błąd pojedynczego sądu = błąd w wyniku, nie przerwanie egzaminu.
    Zwraca trafność per qkey i łącznie, Brier (bool), medianę latency_ms, liczbę błędów;
    wynik łączny ląduje w arena_wyniki (zadanie 'egzamin:jev', skutek 'egzamin')."""
    await zestaw_egzaminu()
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        sql = ("SELECT e.id, e.judgment_id, e.qkey, e.kind, e.state, e.question, e.actual, j.dist "
               "FROM jev_egzamin e LEFT JOIN jev_judgments j ON j.id = e.judgment_id ORDER BY e.qkey, e.id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = await (await db.execute(sql)).fetchall()
    t0 = time.monotonic()
    per: dict[str, dict] = {}
    for r in rows:
        qk, kind = r["qkey"], (r["kind"] or "bool")
        st = per.setdefault(qk, {"n": 0, "trafne": 0, "bledy": 0, "_brier": [], "_lat": []})
        st["n"] += 1
        if kind == "bool":
            q = Question(kind="bool", instructions=r["question"] or "")
        else:
            # jev_judgments.dist dla choice/score jest kluczowany etykietami opcji/poziomów
            try:
                opcje = list(json.loads(r["dist"] or "{}").keys())
            except Exception:
                opcje = []
            if len(opcje) < 2:
                st["bledy"] += 1
                continue
            q = Question(kind=kind, instructions=r["question"] or "",
                         options=opcje if kind == "choice" else [],
                         levels=opcje if kind == "score" else [])
        try:
            res = await ask(r["state"] or "", {qk: q}, model=model, node=node, persist=False, source="egzamin")
            j = res[qk]
        except Exception as exc:
            logger.warning("[jev] egzamin %s/%s padło: %s", qk, r["judgment_id"], exc)
            st["bledy"] += 1
            continue
        if j.get("method") == "error":
            st["bledy"] += 1
            continue
        actual = _norm_etykieta(kind, r["actual"])
        if _norm_etykieta(kind, j.get("label")) == actual:
            st["trafne"] += 1
        st["_lat"].append(int(j.get("latency_ms") or 0))
        if kind == "bool":
            try:
                p = float(j.get("value"))
                st["_brier"].append((p - (1.0 if actual == "tak" else 0.0)) ** 2)
            except (TypeError, ValueError):
                pass
    sekundy = round(time.monotonic() - t0, 3)
    n = sum(s["n"] for s in per.values())
    trafne = sum(s["trafne"] for s in per.values())
    bledy = sum(s["bledy"] for s in per.values())
    briers = [b for s in per.values() for b in s["_brier"]]
    laty = [v for s in per.values() for v in s["_lat"]]
    per_out = {
        qk: {"n": s["n"], "trafne": s["trafne"], "bledy": s["bledy"],
             "trafnosc": round(s["trafne"] / s["n"], 4) if s["n"] else None,
             "brier": round(sum(s["_brier"]) / len(s["_brier"]), 4) if s["_brier"] else None,
             "latency_ms_mediana": int(statistics.median(s["_lat"])) if s["_lat"] else None}
        for qk, s in per.items()
    }
    wezel = node or "domyslny"
    trafnosc = round(trafne / n, 4) if n else 0.0
    wynik = {
        "model": model, "wezel": wezel, "n": n, "trafne": trafne, "bledy": bledy,
        "trafnosc": trafnosc,
        "brier": round(sum(briers) / len(briers), 4) if briers else None,
        "latency_ms_mediana": int(statistics.median(laty)) if laty else None,
        "sekundy": sekundy, "per_qkey": per_out,
    }
    try:
        wynik["arena"] = await _zapisz_do_areny(
            zadanie="egzamin:jev", model=model, wezel=wezel, ocena=trafnosc, zrodlo="egzamin",
            zrodlo_id=f"{model}@{_now()}", opis=json.dumps(per_out, ensure_ascii=False),
            sekundy=sekundy, skutek="egzamin")
    except Exception as exc:  # brak zapisu w arenie nie unieważnia pomiaru — ale mówimy głośno
        logger.warning("[jev] egzamin: nie zapisałem wyniku w arenie: %s", exc)
        wynik["arena"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return wynik


# ── kalibracja: temperature scaling na zebranych wynikach ────────────────────
def _brier(pairs: Iterable[tuple[float, int]]) -> float:
    pairs = list(pairs)
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def _ece(pairs: list[tuple[float, int]], bins: int = 10) -> float:
    if not pairs:
        return float("nan")
    tot, err = len(pairs), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, y) for p, y in pairs if (p >= lo and (p < hi or (b == bins - 1 and p <= hi)))]
        if not sel:
            continue
        conf = sum(p for p, _ in sel) / len(sel)
        acc = sum(y for _, y in sel) / len(sel)
        err += (len(sel) / tot) * abs(conf - acc)
    return err


def _nll(pairs: list[tuple[float, int]], t: float) -> float:
    s = 0.0
    for p, y in pairs:
        q = _apply_temperature({"a": p, "b": 1 - p}, t)["a"]
        q = min(max(q, 1e-6), 1 - 1e-6)
        s -= (math.log(q) if y else math.log(1 - q))
    return s / max(len(pairs), 1)


async def fit_calibration(qkey: str, min_n: int = 30, model: str | None = None,
                          base_rate: float | None = None, question: str | None = None,
                          min_klasy: int | None = None) -> dict:
    """Dopasuj (a, b) dla klucza na zebranych wynikach. Mniej niż `min_n` → NIE kalibrujemy.

    Nie wymyślamy kalibracji z powietrza: bez odpowiedzi zwrotnych sąd zostaje surowy
    i leci z `calibrated=False`, a nie z udawaną pewnością.
    """
    await ensure_tables()
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT j.dist_raw, j.kind, j.label, o.actual
               FROM jev_judgments j JOIN jev_outcomes o ON o.judgment_id = j.id
               WHERE j.qkey = ? AND j.model_used = ?
                 AND j.method IN ('logprobs','logprobs+rozgrzewka','tournament')
                 AND (? = '' OR j.question = ?)""",
            (qkey, model or JEV_MODEL, (question or "")[:400], (question or "")[:400]))).fetchall()
    pairs: list[tuple[float, int]] = []
    for r in rows:
        try:
            dist = json.loads(r["dist_raw"] or "{}")
        except Exception:
            continue
        if not dist:
            continue
        if r["kind"] == "bool":
            p = float(dist.get("true", 0.0))
            y = 1 if str(r["actual"]).lower() in ("true", "tak", "1", "yes") else 0
        else:
            top = max(dist, key=dist.get)
            p = float(dist[top])
            y = 1 if str(r["actual"]).strip().lower() == str(top).strip().lower() else 0
        pairs.append((p, y))

    if len(pairs) < min_n:
        return {"qkey": qkey, "model": model or JEV_MODEL, "fitted": False, "n": len(pairs),
                "reason": f"za mało wyników dla modelu {model or JEV_MODEL} "
                          f"({len(pairs)} < {min_n}) — sąd zostaje NIESKALIBROWANY"}

    # próg na KAŻDĄ klasę z jednego słownika (jev_gates.MIN_KLASY) — pilnuje go harmonogram, CLI i API naraz
    if min_klasy is None:
        from app.services.jev_gates import MIN_KLASY
        min_klasy = MIN_KLASY.get(qkey, 0)
    n_tak = sum(y for _, y in pairs)
    if min_klasy and min(n_tak, len(pairs) - n_tak) < min_klasy:
        return {"qkey": qkey, "model": model or JEV_MODEL, "fitted": False, "n": len(pairs),
                "reason": f"za mało wyników jednej klasy ({n_tak} tak / {len(pairs) - n_tak} nie, "
                          f"potrzeba ≥{min_klasy} każdej) — sąd zostaje NIESKALIBROWANY"}

    a, b = _fit_platt(pairs)
    # Jakość kalibracji mierzymy ZAWSZE na rozkładzie, z którego wzięta jest próbka —
    # inaczej poprawka na prior (niżej) wygląda jak pogorszenie, choć nim nie jest.
    cal_pairs = [(_platt(p, a, b), y) for p, y in pairs]

    # Poprawka na dobór próby (case-control). Żniwa biorą po równo „tak" i „nie",
    # żeby model zobaczył obie klasy — ale w życiu klasy nie są po równo: w historii
    # bramki potwierdzeń jest 605 zatrzymań na 5392 przepuszczenia (~10%, nie 50%).
    # Bez tej korekty sąd dziedziczy prior zbioru treningowego i pyta o wszystko
    # (zmierzone: `list_models` dostawał „tak"). b' = b + logit(prior) − logit(próbka).
    prior_note = None
    if base_rate is not None and 0.0 < base_rate < 1.0:
        sample_rate = sum(y for _, y in pairs) / len(pairs)
        if 0.0 < sample_rate < 1.0:
            b = b + _logit(base_rate) - _logit(sample_rate)
            prior_note = (f"udział TAK w próbce {sample_rate:.2f} → w życiu {base_rate:.3f}; "
                          f"metryki niżej zmierzone PRZED tym przesunięciem, na zbalansowanej "
                          f"próbce — po przesunięciu bramka celuje w prawdziwy prior, nie w próbkę")
    res = {
        "qkey": qkey, "model": model or JEV_MODEL, "fitted": True, "n": len(pairs),
        "slope": round(a, 4), "intercept": round(b, 4), "prior_correction": prior_note or None,
        "brier_before": round(_brier(pairs), 4), "brier_after": round(_brier(cal_pairs), 4),
        "ece_before": round(_ece(pairs), 4), "ece_after": round(_ece(cal_pairs), 4),
        "acc_before": round(sum((p >= 0.5) == bool(y) for p, y in pairs) / len(pairs), 4),
        "acc_after": round(sum((p >= 0.5) == bool(y) for p, y in cal_pairs) / len(cal_pairs), 4),
    }
    async with aiosqlite.connect(_DB) as db:
        await db.execute(
            """INSERT OR REPLACE INTO jev_calibration
               (qkey, model, temperature, slope, intercept, n, brier_before, brier_after,
                ece_before, ece_after, fitted_at, pytanie_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (qkey, model or JEV_MODEL, a, a, b, len(pairs), res["brier_before"],
             res["brier_after"], res["ece_before"], res["ece_after"], _now(),
             pytanie_hash(question[:400]) if question else ""))
        await db.commit()
    global _CAL_CACHE_AT
    _CAL_CACHE_AT = 0.0
    return res


async def report(qkey: str | None = None) -> dict:
    """Metapoznanie organizmu: ile sądów, ile abstynencji, jak kalibrowane, czym."""
    await ensure_tables()
    where, args = ("WHERE qkey = ?", (qkey,)) if qkey else ("", ())
    async with aiosqlite.connect(_DB) as db:
        db.row_factory = aiosqlite.Row
        keys = await (await db.execute(f"""
            SELECT qkey, COUNT(*) n, AVG(abstain) abst, AVG(latency_ms) lat,
                   AVG(confidence) conf, SUM(method='error') errs,
                   GROUP_CONCAT(DISTINCT model_used) models
            FROM jev_judgments {where} GROUP BY qkey ORDER BY n DESC""", args)).fetchall()
        cal = await (await db.execute("SELECT * FROM jev_calibration")).fetchall()
        outc = await (await db.execute("SELECT qkey, COUNT(*) n FROM jev_outcomes GROUP BY qkey")).fetchall()
    cal_map = {r["qkey"]: dict(r) for r in cal}   # klucz→ostatnia kalibracja (pełna lista w tabeli)
    out_map = {r["qkey"]: r["n"] for r in outc}
    return {
        "keys": [{
            "qkey": r["qkey"], "n": r["n"], "abstain_rate": round(r["abst"] or 0, 3),
            "avg_latency_ms": int(r["lat"] or 0), "avg_confidence": round(r["conf"] or 0, 3),
            "errors": r["errs"], "models": (r["models"] or "").split(","),
            "outcomes": out_map.get(r["qkey"], 0),
            "calibration": cal_map.get(r["qkey"]),
        } for r in keys],
        "model_default": JEV_MODEL, "node": JEV_URL,
    }


async def health() -> dict:
    """Czy odruch żyje i ile trwa. Mierzone, nie deklarowane."""
    w = await warm()
    t0 = time.monotonic()
    try:
        res = await ask("Kocioł się pali.", {"__health": Question(kind="bool", instructions="Czy to jest pilne?")},
                        persist=False)
        j = res["__health"]
        return {"ok": j["method"] != "error", "latency_ms": int((time.monotonic() - t0) * 1000),
                "model_used": j["model_used"], "method": j["method"], "p_true": j["value"],
                "abstain": j["abstain"], "reason": j["reason"], "warm": w,
                "node": JEV_URL, "model_requested": JEV_MODEL}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "node": JEV_URL, "warm": w,
                "model_requested": JEV_MODEL, "latency_ms": int((time.monotonic() - t0) * 1000)}
