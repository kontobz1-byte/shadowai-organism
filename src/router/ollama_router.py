"""
OllamaRouter — smart routing across two Ollama nodes + cloud fallback.

Maintains two Ollama nodes:
  - GPU  (shadow, Linux 10.0.0.10:11434)
        qwen2.5 | deepseek-r1 | qwen3-embedding | qwen2.5:14b | qwen2.5:32b
        Heavy reasoning + embeddings. Always available.
  - GUEST (SHADOWGUEST, Windows RTX 5070 12GB, 10.0.0.130:11434)
        llama3.1:8b | phi4 | gemma3:12b | qwen2.5-coder:14b-q8 | deepseek-r1:14b-q8
        Wpiety 2026-09-06. Wczesniej maszyna miala 6 modeli i odpowiadala na :11434,
        ale nie byla wezlem routera — /api/ps stale puste, GPU bezczynne. Online non-stop.
        12 GB VRAM: mixtral:8x7b i qwen3.8:27b sie NIE mieszcza (zostaja na WIN).
  - WIN  (shadow, Windows RTX 5080, 10.0.0.253:11434)
        llama3:8b | llama3.2:3b | phi4:14b | gemma3:4b | gemma3n:e4b
        | granite3.3:8b | deepseek-coder:6.7b | mixtral:8x7b
        Lighter/medium models. AUTO-DISABLED by gaming trigger when game detected.

Model → node resolution:
  1. If request specifies a model that belongs to a node → use that node (if enabled)
  2. If the primary node is down / disabled → fall back to the other node with its default model
  3. If no model specified → GPU node, default model
  4. If ALL local nodes are offline → fall back to OpenAI cloud (if OPENAI_API_KEY is set
     and cloud_fallback_enabled=True in config/env)

Gaming trigger:
  gaming.py calls set_win_enabled(False) when a game process starts on shadow.
  All WIN-exclusive models fall back to GPU during gaming.
  set_win_enabled(True) is called when the game exits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

import httpx
from fastapi import HTTPException

from app.config import settings
from app.services.node_client import auth_headers  # inter-node auth: x-admin-token do ollama-proxy (faza 2)

logger = logging.getLogger(__name__)

NodeId = Literal["gpu", "guest", "win", "cpu", "cloud"]  # cpu = .10 RAM-only ollama-cpu.service :11435 (deep brain, no VRAM)

# ── Cloud model dispatch map ───────────────────────────────────────────────────
# model_name → provider (must match create_adapter() provider string in model_adapters.py)
_CLOUD_MODELS: dict[str, str] = {
    # Operator 2026-09-08: Gemini API włączone na Firma.Operator; stare nazwy
    # (2.0-flash, 1.5-*) zwracają 404 „no longer available" — żywe: 2.5-flash/pro, 3.6-flash.
    "gemini-2.5-flash":          "gemini",
    "gemini-2.5-pro":            "gemini",
    "gemini-3.6-flash":          "gemini",
    "llama-3.3-70b-versatile":   "groq",
    "llama-3.1-70b-versatile":   "groq",
    "mixtral-8x7b-32768":        "groq",
    "gemma2-9b-it":              "groq",
    "mistral-large-latest":      "mistral",
    "mistral-medium-latest":     "mistral",
    "gpt-4o":                    "openai",
    "gpt-4o-mini":               "openai",
    "command-r-plus":            "cohere",
    "command-r":                 "cohere",
}


# Operator 2026-07-26: jak dlugo wezel z padlym runnerem jest omijany, zanim dostanie
# kolejna szanse. Krotko, bo koszt bledu = jeden nieudany request; dlugo bylo by rownie zle
# jak brak bezpiecznika (wezel wypada z floty na godziny po jednym mignieciu).
RUNNER_COOLDOWN_SEC = 300.0
# Claude-10 2026-09-30: ReadTimeout przy KRÓTKIM budżecie wołającego (np. 12 s tieru 2.5 namysłu) to nie
# dowód zawieszonego runnera — zimny model ładuje się dłużej. Bezpiecznik „WISI” dopiero od tego progu.
WISI_MIN_TIMEOUT_S = 60.0

# ── TRÓJKĄT W ROUTINGU (Claude-10 2026-09-30) ─────────────────────────────────
# Operator: „trajektoria decyzyjna, trójkąt nieeuklidesowy i przenoszenie w czasie, nie tylko time cost”,
# „via żelazo, sieć, organizm”, „tu wchodzi gra — rozgrzewa się gdzie indziej”. Do dziś router znał tylko
# kolejkę (transport_cost(t) z model_tiering), a pełne równanie z node_hardware_truth (teraźniejszość ×
# przyszłość × przeszłość) miało jednego konsumenta: podgląd /api/network/relocate (CLAUDE.md §8).
_WEZEL_ROUTERA_NA_KLUCZ = {"gpu": "10", "cpu": "10", "guest": "130", "win": "253", "z13": "172"}
# Decyzja operatora 30.09: na .10 czatuje e4b (obok XTTS + Whisper toru telefonicznego auta .237);
# gdy trójkąt wskaże .10 dla 12b, bierzemy e4b tam, gdzie już siedzi, zamiast przenosić 12b i thrashować.
_ZASTEPSTWO_NA_WEZLE = {("gemma4:12b", "gpu"): "gemma4:e4b"}
# Gdzie model w ogóle może mieszkać (decyzja operatora 30.09: 12b na .130, zapas = e4b na .10 — NIE .253,
# maszyna operatora do gry). Trójkąt wybiera tylko w obrębie tej listy; brak wpisu = każdy właściciel.
_WEZLY_MODELU = {
    # Decyzja operatora 26.10 („stawiaj z13”, „czemu nie na 172 czat”): gdy gość gra na .130, 12b idzie na Z13.
    "gemma4:12b": ("guest", "z13", "gpu"),
    # 22:06Z trójkąt przeniósł e4b na .253 (był tam ciepły, więc tańszy) — a decyzja operatora: e4b mieszka na .10.
    "gemma4:e4b": ("gpu",),
}
# Decyzja operatora 26.10: DETERMINISTYCZNY zapas czatu guest -> z13 (dopiero potem .10/e4b). .10 też ma 12b
# (owns_exact True) i stoi w self._nodes przed z13, więc kolejność bierzemy z tej krotki, nie z pick_node/_nodes.
_ZAPAS_KOLEJNO = {"gemma4:12b": ("guest", "z13")}
# Decyzja operatora 30.09: „.10 = tylko e4b (+XTTS/Whisper)” — na VRAM .10 czatuje wyłącznie e4b. 22:20Z po restarcie
# qwen3.8:27b (8,8 GB) wylądował na .10 (pierwsze zapytanie przed migawką trójkąta szło do 1. właściciela z listy)
# i wypchnął e4b: namysł łapał timeouty, pytania szły 75–78 s. Reguła działa niezależnie od migawki.
_MODELE_NA_WEZLE = {"gpu": ("gemma4:e4b", "gemma4:e4b-it-qat")}


def _wolno_na_wezle(node_id: str, model: str) -> bool:
    """Czy tekstowy model może liczyć na węźle (lista _MODELE_NA_WEZLE; brak wpisu = wszystko wolno).

    30.09 22:38Z: `owns()` widzi KAŻDY model zainstalowany na .10 (_reconcile_models dopisuje tagi z /api/tags),
    więc qwen3.8:27b z zajętego Z13 odbijał się na .10 (odbicie tła, przekierowanie budżetu, zapas po awarii)
    i wypychał e4b — Ollama na .10 trzyma JEDEN model (OLLAMA_MAX_LOADED_MODELS=1). Wizja (chat_vision)
    tej listy nie przechodzi — qwen2.5vl na .10 czeka na decyzję operatora."""
    _mw = _MODELE_NA_WEZLE.get(node_id)
    return not _mw or (model or "").strip().lower() in _mw
# Bez świeżej migawki trójkąta: kolejność zapasowa właścicieli — maszyna Shadowa, potem Z13, na końcu .253 (gra operatora).
_KOLEJNOSC_ZAPASOWA = ("guest", "z13", "win")
MALY_MODEL_GB = 6.0           # do tej wielkości model z .10 zastępuje e4b zamiast wędrować po organizmie
# 30.09: jeden kontekst e4b na .10 = domyślny serwera (drop-in ollama 20-kontekst-e4b.conf, OLLAMA_CONTEXT_LENGTH).
# Zmiana num_ctx restartuje runner (llama-server -c N) i kasuje cache prefiksu — dziś 46 przeładowań e4b przy
# 4096/8192/16384 (namysł, awatary dzieci i preload wołają bez num_ctx). e4b przy 16384 = 3,24 GB.
KONTEKST_E4B_10 = 16384
# 29.10: Z13 (10.0.0.172, Ubuntu 24.04) — ollama.service z OLLAMA_CONTEXT_LENGTH=16384.
# Zmiana num_ctx w zapytaniu restartuje runner (llama-server -c N) i kasuje KV-cache:
# 29.10 20:14–20:35Z: 9 przeładowań qwen3.8:27b w 21 min (rotacja 4096/8192/16384).
# Stały kontekst = domyślny serwera → wołający bez num_ctx i z num_ctx nie przeładowują.
KONTEKST_Z13 = 16384
_TROJKAT_SWIEZOSC_S = 20.0    # co tyle najwyżej odświeżamy prawdę sprzętową w tle
_TROJKAT_WAZNOSC_S = 90.0     # starsza migawka nie decyduje (brak prawdy = zostaje dotychczasowy wybór)
_SKUTEK_OK_CO_S = 300.0       # sukces do trajektorii najwyżej raz na 5 min na węzeł (porażki zawsze)
#: Tyle HTTP 500 pod rząd (bez sygnatury silnika) wyłącza węzeł na RUNNER_COOLDOWN_SEC.
SERIA_500_PROG = 3

# ── Operator/Claude 2026-08-09: wezel, ktory ZNIKA z LAN (operator zabiera Z13) ──
# Zmierzone przy zablokowanym .172: proba polaczenia z nieobecnym wezlem konczyla sie
# `httpx.ConnectTimeout` po 52.5 s (a przy timeout=None — ConnectError po 133 s), bo goly
# float podany jako `timeout=` httpx rozciaga TAKZE na faze polaczenia. Connect dostaje
# wlasny, krotki sufit; reszta budzetu zostaje dla generacji, ktora na 70B naprawde trwa.
CONNECT_TIMEOUT_SEC = 8.0

# Wezel nieobecny w sieci zglasza sie NA DWA sposoby, zaleznie od tego, czy siec odmawia
# (RST/ICMP -> ConnectError), czy milczy (pakiety w prozni -> ConnectTimeout). ConnectTimeout
# NIE jest podklasa ConnectError ani ReadTimeout (sprawdzone, httpx 0.28.1), wiec lapany osobno
# — inaczej wylatuje ponad blok `except` i omija petle fallbacku pod nim.
NODE_UNREACHABLE = (httpx.ConnectError, httpx.ConnectTimeout)


def _model_mysli(model: str) -> bool:
    """Czy model najpierw rozumuje, a dopiero potem odpowiada (ollama: pole `think`)."""
    low = (model or "").lower()
    # gemma4 (e4b/12b/26b/31b) też raportuje `thinking` w /api/show — coach gamingowy
    # wołał e4b-it-qat na zrzutach ekranu i płacił rozumowanie za każdy kadr.
    return any(m in low for m in ("qwen3.6", "qwen3.8", "deepseek-r1", "qwq",
                                  "mellum2-thinking", "gemma4"))


def _bounded_timeout(value, default):
    """Skalarny timeout -> httpx.Timeout z osobnym, krotkim limitem na faze connect."""
    if value is None:
        return default
    if isinstance(value, httpx.Timeout):
        return value
    return httpx.Timeout(value, connect=min(CONNECT_TIMEOUT_SEC, float(value)))

# Sygnatury awarii SILNIKA (nie modelu, nie promptu): ollama odpowiada HTTP 500 z takim
# body, gdy proces runnera padl na sterowniku. Rownowaznik Xid 109 po stronie NVIDIA.
_RUNNER_FAIL_MARKERS = (
    "model runner has unexpectedly stopped",
    "rocm error",
    "cuda error",
    "unspecified launch failure",
    "no cuda-capable device",
    "out of memory",
)


def _is_runner_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _RUNNER_FAIL_MARKERS)


async def _juz_zaladowany(node, model: str) -> bool:
    """Czy `model` jest JUŻ załadowany na węźle (GET {node.url}/api/ps, limit 2 s; błąd = False).

    08.11: żywy budżet (z13_ram, guest_vram) liczy wolne jako pulę minus modele z /api/ps, więc
    rezerwacja pełnych MB dla modelu rezydentnego odejmowała go drugi raz (6 wymuszeń z13_ram,
    odrzucony kandydat .253 gemma4:12b, choć siedział w pamięci .253)."""
    if not model or not getattr(node, "url", ""):
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0, headers=auth_headers()) as client:
            r = await client.get(f"{node.url}/api/ps")
        if r.status_code != 200:
            return False
        cel = node.canonical_model(model).strip().lower()
        for m in (r.json() or {}).get("models", []) or []:
            nazwa = (m.get("name") or m.get("model") or "").strip()
            if nazwa and node.canonical_model(nazwa).strip().lower() == cel:
                return True
    except Exception as exc:
        logger.debug("[pool-budget] /api/ps %s nieudane (%s) — model traktowany jako niezaładowany",
                     getattr(node, "id", "?"), exc)
    return False


@dataclass
class OllamaNode:
    id: NodeId
    url: str
    models: list[str]
    enabled: bool = True
    _online: bool = field(default=False, init=False, repr=False)
    _ever_pinged: bool = field(default=False, init=False, repr=False)
    # ── Operator 2026-07-26: bezpiecznik padlego runnera ────────────────────
    # z13 przez ~24 h serwowal /api/tags i /api/ps (wiec ping() = online), ale KAZDA
    # generacja padala na "model runner has unexpectedly stopped ... ROCm error".
    # Router dalej wybieral go jako primary, health raportowal 4/4 online.
    # ping() NIE moze tego wykryc (tags zyje) — stad osobna flaga z czasem waznosci:
    # po RUNNER_COOLDOWN_SEC wezel dostaje kolejna szanse i albo wraca, albo znow padnie.
    _runner_failed_at: float = field(default=0.0, init=False, repr=False)
    _runner_error: str = field(default="", init=False, repr=False)
    # Claude-10 2026-09-30 (plan napraw §3): .253 od 29.09 19:27Z odpowiadał 500 na KAŻDĄ generację
    # (brak lib\ollama), ale treść błędu nie pasowała do _RUNNER_FAIL_MARKERS — węzeł zostawał
    # pierwszy w kolejce i każde zapytanie traciło na nim czas (166× 500, /reply do 585 s).
    _seria_500: int = field(default=0, init=False, repr=False)
    _ostatecznosc: Callable[[], bool] | None = field(default=None, init=False, repr=False)

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""

    @staticmethod
    def _short(name: str) -> str:
        """Strip :latest / :tag so 'qwen2.5:latest' matches config 'qwen2.5'."""
        return name.split(":")[0].lower()

    def owns_exact(self, model: str) -> bool:
        model_norm = model.strip().lower()
        return any(model_norm == configured.strip().lower() for configured in self.models)

    def owns(self, model: str) -> bool:
        model_short = self._short(model)
        return any(model_short == self._short(m) for m in self.models)

    def canonical_model(self, model: str) -> str:
        # After ping/_reconcile_models, self.models holds the tags ACTUALLY
        # installed on the node. Always prefer those over the requested tag,
        # otherwise we 404 on requests like 'deepseek-r1:8b' when only
        # 'deepseek-r1:latest' is installed (heartbeat anti-loop incident
        # 2026-04-27).
        model_norm = model.strip().lower()
        for configured in self.models:
            if model_norm == configured.strip().lower():
                return configured
        model_short = self._short(model)
        for configured in self.models:
            if model_short == self._short(configured):
                return configured
        return model

    async def ping(self) -> bool:
        self._ever_pinged = True
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=auth_headers()) as client:  # Operator 2026-06-08: 3→8s probe
                r = await client.get(f"{self.url}/api/tags")
                self._online = r.status_code == 200
                if self._online:
                    try:
                        self._reconcile_models(r.json())
                    except Exception:
                        pass
        except Exception:
            self._online = False
        return self._online

    def _reconcile_models(self, tags_json: dict) -> None:
        """Sync configured model names with canonical names installed on the node.

        Ollama 404s when asked for `llama3` while only `llama3:8b` is
        installed. After /api/tags we keep configured order for models
        that are actually present, upgrade short names to tagged variants,
        drop missing entries, and append any extra installed tags.
        """
        installed = [m.get("name", "") for m in tags_json.get("models", []) if m.get("name")]
        if not installed:
            return
        # Operator/Claude-10 2026-06-11 (CURSED FIX): dedup po PEŁNYM tagu, nie po _short.
        # Stary `seen_short` kolapsował gemma4:26b≡gemma4:e4b≡"gemma4" → drugi wariant wypadał,
        # canonical_model(e4b)→26b, FAST e4b po cichu odpalał 26b na GPU VRAM. Matching short→tag
        # (upgrade bare-name) ZOSTAJE; dedupujemy realny tag, więc same-family/różny-rozmiar oba żyją.
        reconciled: list[str] = []
        seen: set[str] = set()
        for configured in self.models:
            if configured in installed:
                match = configured
            else:
                short = self._short(configured)
                match = next((i for i in installed if self._short(i) == short), None)
            if match and match not in seen:
                reconciled.append(match)
                seen.add(match)

        for actual in installed:
            if actual not in seen:
                reconciled.append(actual)
                seen.add(actual)
        if reconciled:
            self.models = reconciled

    async def resolve_default_model(self) -> str:
        """Return default_model guaranteed reconciled against /api/tags.

        Triggers ping() on first use so self.models is upgraded to
        canonical names before returning default_model.
        """
        if not self._ever_pinged:
            await self.ping()
        return self.default_model

    @property
    def online(self) -> bool:
        return self._online

    @property
    def requests_in_flight(self) -> int:
        return getattr(self, "_requests_in_flight", 0)

    @property
    def available(self) -> bool:
        if self.runner_down:
            return False
        if self._ostatecznosc is not None and self._ostatecznosc():
            return False
        if not self._ever_pinged:
            return self.enabled
        return self.enabled and self._online

    @property
    def runner_down(self) -> bool:
        """True gdy silnik generacji padl, mimo ze HTTP wezla odpowiada (patrz _runner_failed_at)."""
        if not self._runner_failed_at:
            return False
        import time as _t
        if _t.monotonic() - self._runner_failed_at >= RUNNER_COOLDOWN_SEC:
            return False
        return True

    def mark_runner_failed(self, error: str) -> None:
        import time as _t
        self._runner_failed_at = _t.monotonic()
        self._runner_error = (error or "")[:300]

    def mark_runner_ok(self) -> None:
        self._runner_failed_at = 0.0
        self._runner_error = ""
        self._seria_500 = 0

    def zanotuj_500(self, body: str) -> bool:
        """Kolejne HTTP 500 bez znanej sygnatury silnika. Po SERIA_500_PROG pod rząd węzeł idzie
        na ten sam bezpiecznik co padnięty runner (RUNNER_COOLDOWN_SEC). True = właśnie wyłączony."""
        self._seria_500 += 1
        if self._seria_500 >= SERIA_500_PROG:
            self.mark_runner_failed(f"seria {self._seria_500}× HTTP 500: {(body or '').strip()[:200]}")
            self._seria_500 = 0
            return True
        return False


def _ten_sam_model_inny_tag(zadany: str, kandydat: str) -> bool:
    """Czy `kandydat` to TEN SAM model pod innym tagiem — a nie inny rozmiar rodziny.

    Claude-10 2026-09-06. Luzny fallback po `owns()` (dopasowanie po RODZINIE) wpuszczal
    podmiane ROZMIARU: przy martwym .10 prosba o `gemma4:e4b` trafiala na node `cpu`
    (models=['gemma4:26b']) i odpowiadal 26B na instancji RAM-only — dokladnie regresja
    "e4b po cichu odpalal 26b", ktora fix owns()->owns_exact z 2026-05-29 mial zabic.
    Przepuszczamy wiec tylko roznice TAGU przy tej samej bazie:
      phi4:14b == phi4:latest      (TAK — zweryfikowane /api/show: oba phi3 14.7B Q4_K_M)
      gemma4:e4b != gemma4:26b     (NIE — inny rozmiar, inna maszyna, inny koszt)
    """
    baza_z, _, tag_z = zadany.partition(":")
    baza_k, _, tag_k = kandydat.partition(":")
    if baza_z.strip().lower() != baza_k.strip().lower():
        return False
    tag_z, tag_k = tag_z.strip().lower(), tag_k.strip().lower()
    if tag_z == tag_k:
        return True
    # "latest" jest z natury nieokreslony — traktujemy jak ten sam model.
    return not tag_z or not tag_k or "latest" in (tag_z, tag_k)


def _parse_models(raw: str) -> list[str]:
    return [m.strip() for m in raw.split(",") if m.strip()]


class _CountingOllamaClient:
    """Liczy POST-y generacji na rzeczywistym węźle, także podczas fallbacku."""

    def __init__(self, client, nodes):
        self._client = client
        self._nodes = nodes

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def post(self, url, **kwargs):
        node = next(
            (n for n in self._nodes
             if str(url) in (f"{n.url}/api/generate", f"{n.url}/api/chat")),
            None,
        )
        if node is None:
            return await self._client.post(url, **kwargs)
        others_before = node.requests_in_flight
        node._requests_in_flight = others_before + 1
        try:
            return await self._client.post(url, **kwargs)
        except httpx.ReadTimeout as exc:
            exc._ollama_node = node
            exc._ollama_other_requests = max(
                others_before, node.requests_in_flight - 1,
            )
            raise
        finally:
            node._requests_in_flight -= 1


class OllamaRouter:
    def _handle_read_timeout(self, node, used_model, timeout, exc) -> None:
        node = getattr(exc, "_ollama_node", node)
        others = max(
            getattr(exc, "_ollama_other_requests", 0),
            node.requests_in_flight,
        )
        if others:
            logger.warning(
                "Ollama '%s': timeout kolejki — inne zapytania tego procesu: %d "
                "— bez bezpiecznika",
                node.id, others,
            )
        elif timeout is None or float(timeout) >= WISI_MIN_TIMEOUT_S:
            node.mark_runner_failed(f"generation timeout on {node.id}")
            self._zapisz_skutek(node, False, f"generacja wisi ({used_model})", model=used_model)
            logger.error(
                "Ollama '%s': generacja WISI mimo zywego HTTP — wezel omijany przez %.0fs",
                node.id, RUNNER_COOLDOWN_SEC,
            )
        else:
            logger.warning(
                "Ollama '%s': brak odpowiedzi w budżecie wywołania %.1f s — bez bezpiecznika "
                "(za krótko, by orzec zawieszenie)", node.id, float(timeout),
            )

    def __init__(self) -> None:
        self.gpu = OllamaNode(
            id="gpu",
            url=settings.ollama_gpu_url,
            models=_parse_models(settings.ollama_gpu_models),
        )
        self.win = OllamaNode(
            id="win",
            url=settings.ollama_win_url,
            models=_parse_models(settings.ollama_win_models),
            enabled=settings.ollama_win_enabled,
        )
        # CPU node: osobna Ollama instance na :11435 z OLLAMA_NUM_GPU=0 (.10 RAM-only).
        # Operator 2026-05-24: ".10 ma 64GB RAM cały dla Shadow". Deep brain (gemma4:26b)
        # tutaj pinned RAM-only, nie blokuje VRAM dla ACE-Step/ComfyUI.
        _cpu_url = getattr(settings, "ollama_cpu_url", "") or "http://127.0.0.1:11435"
        _deep = getattr(settings, "deep_brain_model", "") or "gemma4:26b"
        self.cpu = OllamaNode(
            id="cpu",
            url=_cpu_url,
            models=[_deep],
            enabled=bool(_cpu_url and _deep),
        )
        self.z13 = OllamaNode(
            id="z13",
            url=settings.ollama_z13_url,
            models=_parse_models(settings.ollama_z13_models),
            enabled=settings.ollama_z13_enabled,
        )
        self.guest = OllamaNode(
            id="guest",
            url=settings.ollama_guest_url,
            models=_parse_models(settings.ollama_guest_models),
            enabled=settings.ollama_guest_enabled,
        )
        # Claude 2026-09-15: llama-server (.172:8081, API OpenAI) — Bielik v3 poza Ollamą.
        # NIE wchodzi do self._nodes (to lista OllamaNode z /api/tags, /api/ps i budżetem VRAM);
        # obsługa = dispatch bezpośredni w chat()/chat_messages() przed _resolve_with_vram,
        # a przy awarii zejście na `fallback_model` (Bielik v2.6 Q8 na Ollamie).
        from app.services.llamacpp_node import LlamaCppNode
        self.llamacpp = LlamaCppNode(
            url=getattr(settings, "llamacpp_z13_url", "") or "http://10.0.0.172:8081",
            models=_parse_models(getattr(settings, "llamacpp_z13_models", "") or "bielik-11b-v3"),
            enabled=bool(getattr(settings, "llamacpp_z13_enabled", True)),
            fallback_model=getattr(settings, "llamacpp_z13_fallback_model", "") or "",
        )
        # Kolejnosc ma znaczenie: _resolve skanuje _nodes po kolei, wiec dla modelu
        # ktory maja OBA (phi4) wygrywa ten wczesniejszy. guest PRZED win swiadomie —
        # .130 stoi online non-stop, .253 gasnie przy kazdej grze (gaming trigger).
        self._nodes = [self.gpu, self.guest, self.win, self.cpu, self.z13]
        # 26.10: zapas kolejno (guest -> z13) idzie PRZED trójkątem — każde wywołanie _wybierz_trojkatem
        # przechodzi przez _wybierz_z_zapasem; rdzeń trójkąta dla 12b nie widzi z13 (z13 nigdy przez pick_node).
        self._trojkat_rdzen = self._wybierz_trojkatem
        self._wybierz_trojkatem = self._wybierz_z_zapasem
        _get_http = self._get_http

        def _get_counted_http():
            return _CountingOllamaClient(_get_http(), self._nodes)

        self._get_http = _get_counted_http
        self._trojkat: dict = {"ts": 0.0, "nodes": [], "w_toku": False}
        self._skutek_ok_ts: dict = {}
        # Cluster Concurrency — 5 independent pools (2 slots each):
        #   _gpu_semaphore     = GPU-VRAM (shadow RTX 5060 Ti)
        #   _win_semaphore     = WIN-VRAM (shadow RTX 5080)
        #   _z13_semaphore     = Z13 (ROG Flow Z13, 59GB RAM/VRAM)
        #   _gpu_cpu_semaphore = GPU-RAM  (shadow, 50GB headroom) — was lumped under _cpu_semaphore
        #   _win_cpu_semaphore = WIN-RAM  (shadow, 24GB headroom)
        import asyncio as _aio_init
        self._gpu_semaphore = _aio_init.Semaphore(2)
        self._win_semaphore = _aio_init.Semaphore(2)
        self._z13_semaphore = _aio_init.Semaphore(2)
        self._guest_semaphore = _aio_init.Semaphore(2)
        self._guest_cpu_semaphore = _aio_init.Semaphore(2)
        self._gpu_cpu_semaphore = _aio_init.Semaphore(2)
        self._win_cpu_semaphore = _aio_init.Semaphore(2)
        # Back-compat alias — some diagnostic code still reads _cpu_semaphore
        self._cpu_semaphore = self._win_cpu_semaphore
        self._gpu_busy = False
        # Operator 2026-07-01 (operator: "nie jedz na skroty" — semafor liczy
        # SLOTY, nie MB): semafor(2) pozwalal 2 requestom wejsc rownolegle na ten
        # sam pool nawet gdy oba razem fizycznie nie miescily sie w VRAM/UMA
        # (np. z13: model_tiering.py mowi "JEDEN slot heavy", a semafor mial 2 —
        # dwa ciezkie modele (qwen3.6:35b + deepseek-r1:70b) moglyby wejsc naraz
        # na tej samej migawce budzetu). Prawdziwa rezerwacja: przed kazdym
        # _do_request rezerwujemy MB w odpowiednim poolu (gpu_vram/gpu_ram/
        # win_vram/win_ram/z13_ram), po zakonczeniu zwalniamy. Check jest
        # ATOMOWY (lock) wiec dwa rownolegle requesty widza siebie nawzajem,
        # nie tylko martwa migawke nvidia-smi/api-ps sprzed chwili.
        self._pool_reserved_mb: dict[str, float] = {
            "gpu_vram": 0.0, "gpu_ram": 0.0,
            "win_vram": 0.0, "win_ram": 0.0,
            "z13_ram": 0.0,
            "guest_vram": 0.0, "guest_ram": 0.0,
        }
        self._pool_lock = _aio_init.Lock()
        # Ustawiane per-tick przez _resolve_with_vram tuz przed kazdym return —
        # tyle MB rezerwuje TA konkretna decyzja (mirror wzorca self._last_num_gpu).
        self._last_reserved_mb: float = 0.0
        # Debounce for _maybe_free_heavy_for — prevents comfyui/ace-step
        # /free spam (8×/30min in the wild) when multiple qwen3:14b requests
        # arrive back-to-back. The actual unload is idempotent; we just
        # stop logging + re-hitting the HTTP endpoints.
        self._last_free_heavy_ts: float = 0.0
        # Shared HTTP client — avoids creating 100+ AsyncClient instances per command
        self._http: httpx.AsyncClient | None = None

    def _zapas_kolejno(self, model: str, bez=()) -> "OllamaNode | None":
        """Pierwszy żywy węzeł z _ZAPAS_KOLEJNO[model] w KOLEJNOŚCI KROTKI (decyzja operatora 26.10)."""
        _kol = _ZAPAS_KOLEJNO.get((model or "").strip().lower())
        if not _kol:
            return None
        _po_id = {n.id: n for n in self._nodes}
        for _id in _kol:
            if _id in bez:
                continue
            n = _po_id.get(_id)
            if n is not None and n.available and n.enabled and n.owns_exact(model):
                return n
        return None

    def _sloty_zajete(self, node_id: str) -> bool:
        """Czy wszystkie sloty semafora węzła są zajęte (zapytanie musiałoby czekać)."""
        sem = getattr(self, f"_{node_id}_semaphore", None)
        try:
            return bool(sem is not None and sem.locked())
        except Exception:
            return False

    def _wybierz_z_zapasem(self, model, need, bez=(), **kw):
        """Początek _wybierz_trojkatem. 2026-11-05: żywi właściciele z _ZAPAS_KOLEJNO rankowani przez
        node_hardware_truth.pick_node (min transport_cost); kolejność guest -> z13 tylko przy remisie
        albo gdy pick_node milczy. Zapytanie interaktywne (czat/WA/Avatar) nie czeka na sloty zajęte
        przez tło — od razu następny węzeł z rankingu. Gdy nikt nie żyje — trójkąt bez z13."""
        interaktywne = bool(kw.pop("interaktywne", False))
        _klucz = (model or "").strip().lower()
        if _klucz in _ZAPAS_KOLEJNO:
            # Ta gałąź może wrócić przed rdzeniem trójkąta, więc odśwież migawkę przed odczytem kosztów.
            self._trojkat_odswiez_w_tle()
            from app.services import operator_modele as _om
            interaktywne = interaktywne or _om.jest_interaktywne()
            _po_id = {n.id: n for n in self._nodes}
            kandydaci = []
            for _id in _ZAPAS_KOLEJNO[_klucz]:
                n = _po_id.get(_id)
                if _id in bez or n is None:
                    continue
                if n.available and n.enabled and n.owns_exact(model):
                    kandydaci.append(n)
            if kandydaci:
                koszty = _om.koszty_transportu(
                    [{"node": n.id, "id": n.id, "klucz": _WEZEL_ROUTERA_NA_KLUCZ.get(n.id), "url": n.url,
                      "model": model, "sloty_zajete": self._sloty_zajete(n.id)} for n in kandydaci],
                    float(need or 0),
                    prawda=_om.prawda_z_migawki(self._trojkat),
                    prefer_model=model,
                )
                ranking = [n for _, n in sorted(
                    enumerate(kandydaci),
                    key=lambda p: (koszty.get(p[1].id, float("inf")), p[0]))]
                wybor = ranking[0]
                pominiety = None
                if interaktywne and self._sloty_zajete(wybor.id):
                    wolny = next((n for n in ranking if not self._sloty_zajete(n.id)), None)
                    if wolny is not None:
                        pominiety, wybor = wybor.id, wolny
                if koszty:
                    powod = "min transport_cost %s=%s (pick_node; remis -> guest->z13)" % (
                        wybor.id, koszty.get(wybor.id))
                else:
                    powod = "zapas kolejno guest->z13 (pick_node milczy)"
                if pominiety:
                    powod += f"; interaktywne: sloty {pominiety} zajęte przez tło -> {wybor.id} bez czekania"
                return {"node": wybor, "reason": powod}
            bez = tuple(bez) + ("z13",)
        return self._trojkat_rdzen(model, need, bez=bez, **kw)

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            # Connect should be fast in LAN. Operator 2026-06-08: read 180→600s —
            # sprzęt skromny (.172 Z13 bandwidth-bound ~5 tok/s, 70B odpowiada minutami),
            # 180s ścinał działające-ale-wolne generacje → plaga ReadTimeout. connect 3→8s.
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=8.0), headers=auth_headers())
        return self._http

    def _cloud_available(self) -> bool:
        return (
            settings.cloud_fallback_enabled
            and bool(settings.openai_api_key and settings.openai_api_key.strip())
        )

    def _resolve(self, model: str | None, preferred_node: str | None = None) -> tuple[OllamaNode, str]:
        """Return (node, model) to use for this request.

        preferred_node (Operator/Claude-10 2026-07-01, unifikacja dual-decider): id węzła
        wskazanego przez transport-równanie SmartRoutera. Dla modeli MULTI-HOMED (np.
        gemma4:12b na gpu+win) `_resolve` brał pierwszego z listy `self._nodes`, ignorując
        węzeł który wygrał transport_cost(t) → decyzja SmartRoutera i egzekucja się rozjeżdżały.
        Teraz: gdy preferred_node żyje i owns_exact(model) — wygrywa. Additive, None=stare.
        """
        if isinstance(model, str) and model.strip().lower() in {"auto", "default", "routing"}:
            model = None
        # Honoruj węzeł z transport-równania PRZED skanem po kolejności listy.
        if model and preferred_node:
            _pn = {"gpu": self.gpu, "guest": self.guest, "win": self.win, "cpu": self.cpu, "z13": self.z13}.get(preferred_node)
            if _pn is not None and _pn.available and _pn.enabled and _pn.owns_exact(model):
                return _pn, _pn.canonical_model(model)
        # CPU node first-match: jeśli zapytanie WPROST o deep brain (gemma4:26b) → CPU instance .10 (RAM-only).
        # Operator 2026-05-24: 64GB RAM dla Shadow, multimedia ma wolne VRAM.
        # 2026-05-29 FIX (Claude-10, root planner/chat hang): owns() -> owns_exact().
        # cpu.models=['gemma4:26b'], a owns() robi luźny family-match → 'gemma4:e4b' też pasował →
        # KAŻDE gemma4 (w tym lekki e4b) było porywane na cpu i canonical_model mapowało na 26b →
        # 26b na CPU/RAM = ślimak → router.chat(e4b) wisiał. owns_exact: tylko DOKŁADNE 'gemma4:26b' → cpu.
        # 2026-06-24 (Claude-10 ground-truth): agent-codegen zostawił TU placeholder
        # '[POPRAWIONA_WERSJA_ZAPROJEKTOWANA_W_KROKU_3]' → NameError przy KAŻDYM _resolve →
        # backendowy LLM-routing + mentor auto_evaluate_with_llm martwe (0 lekcji od 2026-06-23 20:41).
        # Placeholder USUNIĘTY (przywrócenie do działania == HEAD). CPU-first-match dla DOKŁADNEGO
        # gemma4:26b do właściwej reimplementacji przez Critical Review Shadow — modele explicit
        # i tak resolvują w bloku niżej, więc usunięcie jest bezpieczne, nie redesign.
        if not model:
            preferred = (settings.primary_model or "").strip()
            # (1) Exact match preferred on ANY node (prioritize Z13 for heavy, GPU for light if both have it)
            if preferred:
                for node in [self.gpu, self.z13, self.guest, self.win, self.cpu]:
                    if node.available and node.enabled and node.owns_exact(preferred):
                        return node, node.canonical_model(preferred)
            
            # (2) Family match fallback (prefer GPU for speed)
            if preferred and self.gpu.available and self.gpu.owns(preferred):
                return self.gpu, self.gpu.canonical_model(preferred)
            
            # (3) Last resort defaults
            if self.gpu.available:
                return self.gpu, self.gpu.default_model
            if self.guest.available:
                return self.guest, self.guest.default_model
            if self.win.available:
                return self.win, self.win.default_model
            if self.z13.available:
                return self.z13, self.z13.default_model
            if self._cloud_available():
                raise _CloudFallbackSignal()
            raise HTTPException(503, "All Ollama nodes are offline or disabled.")

        if model:
            # Exact model names win before short-name matching.
            for node in self._nodes:
                if node.owns_exact(model):
                    if node.available:
                        return node, node.canonical_model(model)
                    # Operator/Claude-10 2026-06-11: właściciel down (np. .253 w grze) → preferuj
                    # INNEGO WŁAŚCICIELA tego samego modelu (np. Bielik .253→.172) zanim zejdziesz
                    # na czyjś default. Bez tego gaming gubił Bielika na gpu-default (gemma4:e4b).
                    for other in self._nodes:
                        if other is not node and other.available and other.owns_exact(model):
                            return other, other.canonical_model(model)
                    # Claude-10 2026-09-06 (przy wpinaniu .130): wlasciciel-EXACT moze nie
                    # istniec, bo ten sam model stoi na drugim wezle pod INNYM TAGIEM —
                    # win ma 'phi4:14b', guest ma 'phi4:latest' (zweryfikowane: oba phi3
                    # 14.7B Q4_K_M, ten sam plik). Bez tego kroku prosba o phi4 przy
                    # martwym .253 schodzila od razu na CUDZY default_model i router po
                    # cichu odpowiadal calkiem innym modelem niz zamowiony — klasa bledu
                    # "cicha podmiana", nie widac jej w logu wolajacego. Najpierw wiec
                    # szukamy wlasciciela po rodzinie (owns), dopiero potem default.
                    for other in self._nodes:
                        if (other is not node and other.available and other.owns(model)
                                and _ten_sam_model_inny_tag(model, other.canonical_model(model))):
                            return other, other.canonical_model(model)
                    for fallback in self._nodes:
                        if fallback is not node and fallback.available:
                            return fallback, fallback.default_model
                    if self._cloud_available():
                        raise _CloudFallbackSignal()
                    raise HTTPException(503, f"Model '{model}' node is offline/disabled and no fallback available.")

            for node in self._nodes:
                if node.owns(model):
                    if node.available:
                        return node, node.canonical_model(model)
                    # Ta sama zasada co wyzej: inny wlasciciel tej samej rodziny bije
                    # zejscie na czyjs default_model.
                    for other in self._nodes:
                        if (other is not node and other.available and other.owns(model)
                                and _ten_sam_model_inny_tag(model, other.canonical_model(model))):
                            return other, other.canonical_model(model)
                    for fallback in self._nodes:
                        if fallback is not node and fallback.available:
                            return fallback, fallback.default_model
                    if self._cloud_available():
                        raise _CloudFallbackSignal()
                    raise HTTPException(503, f"Model '{model}' node is offline/disabled and no fallback available.")

        for node in self._nodes:
            if node.available:
                return node, node.default_model

        if self._cloud_available():
            raise _CloudFallbackSignal()

        raise HTTPException(503, "All Ollama nodes are offline or disabled.")

    @staticmethod
    def _get_keep_alive(model: str) -> str:
        """Get optimal keep_alive for VRAM management."""
        try:
            from app.services.vram_manager import get_keep_alive
            return get_keep_alive(model)
        except Exception:
            return "5m"

    @staticmethod
    def _estimate_num_ctx(prompt: str | None = None, messages: list | None = None) -> int:
        """Auto-select num_ctx based on input size. Saves VRAM on short prompts."""
        if messages:
            total_chars = sum(len(m.get("content", "")) for m in messages)
        elif prompt:
            total_chars = len(prompt)
        else:
            total_chars = 0

        estimated_tokens = total_chars // 3

        needed = estimated_tokens + 1500

        if needed < 1500:
            return 2048
        elif needed < 3000:
            return 4096
        elif needed < 6000:
            return 8192
        elif needed < 15000:
            return 16384
        elif needed < 30000:
            return 32768
        else:
            return 65536

    def _stable_num_ctx(self, model: str, estimated: int) -> int:
        """Sticky num_ctx per model (Operator 2026-07-25, „avatar nieprzewidywalny").

        Zmiana num_ctx między requestami do TEGO SAMEGO modelu restartuje runner
        ollamy (llama-server -c N) i kasuje cały KV-cache: interaktywne 32k tuż po
        heartbeatowym 8k = przeładowanie + pełny re-eval ~20k tokenów promptu
        (30-70s zamiast 3s). Journal 13:52-14:00: 3 restarty jednego modelu,
        -c 32768↔8192. Sticky = nie schodzimy poniżej najwyższego dotąd użytego
        num_ctx (cap 32768) — runner stabilny, prefix-cache przeżywa requesty.
        Jednorazowy olbrzym (>32768) dostaje swoje, ale nie podnosi sticky.
        """
        sticky = getattr(self, "_sticky_num_ctx_by_model", None)
        if sticky is None:
            sticky = self._sticky_num_ctx_by_model = {}
        val = max(int(estimated or 0), sticky.get(model, 0))
        if (model or "").strip().lower() in _MODELE_NA_WEZLE.get("gpu", ()):
            val = max(val, KONTEKST_E4B_10)   # ten sam -c co wołający bez num_ctx — bez przeładowań runnera
        # 29.10: model TYLKO na z13 (nie na gpu/win/guest/cpu) — stały kontekst = OLLAMA_CONTEXT_LENGTH
        # z ollama.service na Z13. Zmiana num_ctx restartuje runner i kasuje KV-cache (9 przeładowań
        # qwen3.8:27b w 21 min, 29.10 20:14–20:35Z). Modele z kilkoma właścicielami (np. gemma4:12b)
        # i modele innych węzłów — bez zmian.
        if (model and self.z13.owns(model)
                and not self.gpu.owns(model)
                and not self.win.owns(model)
                and not self.guest.owns(model)
                and not self.cpu.owns(model)):
            val = max(val, KONTEKST_Z13)
        # Claude-10 2026-08-27 (polecenie operatora, koszt ZMIERZONY przed zmianą):
        # sufit 32768 ustawiono bez pomiaru, „na wszelki wypadek". Zmierzone dziś:
        #   qwen3.8:27b @z13  8k → 17,29 GB, 64k → 17,53 GB  (+0,24 GB)
        #   gemma4:12b @.10   32k → 8,09 GB, 64k → 8,13 GB   (+0,04 GB, CAŁY w VRAM)
        # Karta 16 GB przy 64k nadal trzyma model w całości na GPU (13,3 → 13,8 GB
        # zajęte). Sufit idzie więc na 65536 — tyle, ile i tak zwraca górny próg
        # `_estimate_num_ctx`, więc sticky przestaje ucinać własne szacunki.
        # Uwaga: to NIE jest natywne 262144 modelu qwen3.8 — tamto działa (zmierzone,
        # 1 min 54 s), ale eksmituje ciepłą kotwicę person z z13, więc zostaje trybem
        # na wyraźne żądanie, nie domyślnym.
        sticky[model] = min(val, 65536)
        return val

    # ── 4-Resource Cascade ──────────────────────────────────────────────────
    # Priority: GPU VRAM → WIN VRAM → GPU RAM (CPU) → WIN RAM (CPU) → Cloud
    #
    # Each model can run in 2 modes:
    #   num_gpu=-1 or omit  = full GPU (VRAM)
    #   num_gpu=0           = full CPU (RAM only, slower but always fits)

    _MODEL_VRAM_MB = {
        # Z13 Heavy Brain
        "deepseek-r1:70b": 42000, "qwen2.5:72b": 44000,
        "llama3.3:70b": 42000, "gemma4:31b": 22000,
        # Claude-10 2026-08-27: qwen3.8:27b nie miał tu wpisu, więc przy ZIMNYM
        # cache model_tiering kaskada liczyła go po domyślnych 5000 MB — 3,5× za
        # mało wobec zmierzonych 17,3-17,5 GB (Q4_K_M + wieża wizyjna). Planowanie
        # na tej liczbie mogło go wpuścić na kartę 16 GB, gdzie się nie mieści.
        "qwen3.8:27b": 17600, "qwen3.6:35b": 24500,
        # GPU / WIN
        "qwen3:14b": 6500, "qwen2.5": 4700, "deepseek-r1": 5200,
        "llava:7b": 4700, "moondream": 1700, "gemma3:4b": 3100,
        "granite3.3:8b": 4600, "phi4:14b": 8400, "deepseek-coder:6.7b": 3600,
        "llama3:8b": 4300, "gemma3n:e4b": 7000,
        "llama3.2:3b": 1900, "mixtral:8x7b": 24600,
        "qwen3-embedding:0.6b": 640,
        # Gemma 4 family
        "gemma4:26b": 16000, "gemma4:e4b": 9600,
        "nemotron-3-nano:4b-bf16": 2500,
        # Operator/Claude-10 2026-06-11: dopełnione (były na 5000-default → zły placement)
        "mellum2-instruct": 8100, "mellum2-thinking": 8100,
        "gemma4:12b": 7600, "qwen2.5-coder:7b": 4700, "llava-phi3": 2900,
        "deepseek-coder": 900, "qwen3-embedding": 4700, "llama3.2-vision:11b": 7800,
        "hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF": 6700,
        # Q8_0 wazy prawie 2x tyle — bez tego wpisu router liczy domyslne 5000
        # i wciska model tam, gdzie sie nie miesci ([[feedback_aspirational_vs_real]]).
        "hf.co/speakleash/Bielik-11B-v2.6-Instruct-GGUF": 12400,
        # Gemma 4 QAT (2026-06-11, -72% vs full) — fast PL tier / multimodal coach
        "gemma4:e2b-it-qat": 4340, "gemma4:e4b-it-qat": 5000,
        "gemma4:12b-it-qat": 7000, "gemma4:26b-it-qat": 15000,
    }

    _MODEL_RAM_MB = {
        # Heavy models (RAM equivalent for Z13/CPU)
        "deepseek-r1:70b": 48000, "qwen2.5:72b": 50000,
        "llama3.3:70b": 48000, "gemma4:31b": 28000,
        # RAM needed for CPU-only inference (model + runtime overhead)
        "qwen3:14b": 12000, "qwen2.5": 6000, "deepseek-r1": 7000,
        "granite3.3:8b": 6000, "phi4:14b": 10000, "deepseek-coder:6.7b": 5000,
        "llama3:8b": 6000, "gemma3:4b": 4500, "llama3.2:3b": 2500,
        "mixtral:8x7b": 28000, "qwen3-embedding:0.6b": 1000,
        # Gemma 4 family
        "gemma4:26b": 18000, "gemma4:e4b": 12000,
        "nemotron-3-nano:4b-bf16": 3500,
        # Operator/Claude-10 2026-06-11: dopełnione (były na 8000-default)
        "mellum2-instruct": 10000, "mellum2-thinking": 10000,
        "gemma4:12b": 9000, "qwen2.5-coder:7b": 6000, "llava-phi3": 4000,
        "deepseek-coder": 1500, "qwen3-embedding": 6000, "llama3.2-vision:11b": 9500,
        "gemma3n:e4b": 9000, "moondream": 2500, "llava:7b": 6000,
        "hf.co/speakleash/Bielik-11B-v2.3-Instruct-GGUF": 8000,
        "hf.co/speakleash/Bielik-11B-v2.6-Instruct-GGUF": 14000,
        # Gemma 4 QAT (2026-06-11)
        "gemma4:e2b-it-qat": 5500, "gemma4:e4b-it-qat": 6500,
        "gemma4:12b-it-qat": 9000, "gemma4:26b-it-qat": 17000,
    }

    # GPU: 16GB VRAM, 64GB RAM | WIN: 16GB VRAM, 32GB RAM
    # Fallback budgets when live VRAM probe is unavailable.
    _NODE_BUDGET = {
        "gpu_vram": 14000,  # 16GB - 2GB OS
        "gpu_ram": 50000,   # 64GB - 14GB OS/services
        "win_vram": 14000,  # RTX 5080 16GB - 2GB OS (był 5070 12GB→10000, Operator 2026-06-22)
        "win_ram": 24000,   # 32GB - 8GB OS/gaming headroom
        "z13_ram": 38500,   # GTT 52 GiB − Bielik v3 rezydentny 14,5 GB (Z13 Linux od 29.10)
        "guest_vram": 11000,  # RTX 5070 12227 MiB - ~1,2GB pulpit/OS (SHADOWGUEST .130)
        "guest_ram": 22000,   # 31,1GB - 9GB OS
    }

    # Live VRAM cache — vram_manager.get_node_vram_free calls nvidia-smi/api/ps;
    # cache 10 s to avoid spam when multiple requests arrive back-to-back.
    _LIVE_VRAM_TTL_SEC = 10.0
    _live_vram_cache: dict = {}
    # Z13 Linux (29.10): pula GPU = GTT, Bielik rezydentny poza Ollamą.
    _Z13_PULA_MB = 53248
    _Z13_BIELIK_MB = 14500
    _Z13_BIELIK_URL = "http://10.0.0.172:8081"

    async def _get_live_budget(self, resource_key: str) -> int:
        """Return live free budget (MB) for one of the 4 resource tiers.

        resource_key ∈ {"gpu_vram", "win_vram", "gpu_ram", "win_ram"}.
        Falls back to _NODE_BUDGET hardcode on any failure (feature flag
        ROUTING_LIVE_VRAM=false also forces the hardcoded path).
        """
        # Sprawdź TTL bramki gry .130 — jeśli wygasła, automatycznie ją wyłącz
        if resource_key in ("guest_vram", "guest_ram"):
            try:
                from app.routers.gaming import check_guest_gaming_ttl
                check_guest_gaming_ttl()
            except Exception:
                pass

        fallback = self._NODE_BUDGET[resource_key]
        try:
            if not getattr(settings, "routing_live_vram", True):
                return fallback
        except Exception:
            pass

        import time as _t
        now = _t.monotonic()
        cached = self._live_vram_cache.get(resource_key)
        if cached and (now - cached[0]) < self._LIVE_VRAM_TTL_SEC:
            return cached[1]

        # Operator/Claude-10 2026-06-11: Z13 (.172, AMD 395 UMA — brak nvidia-smi) live-probe
        # przez /api/ps: free = pula - suma rozmiarów załadowanych modeli.
        # Claude 2026-10-29: Z13 na Linuksie — pula = GTT amdgpu 53248 MB; Bielik v3 (llama-server :8081,
        # poza Ollamą, enabled na stałe) zajmuje ~14,5 GB, więc odejmujemy go, gdy /health odpowiada.
        if resource_key == "z13_ram":
            try:
                import httpx as _hx_z
                async with _hx_z.AsyncClient(timeout=2.5, headers=auth_headers()) as _cl_z:
                    _r = await _cl_z.get(f"{self.z13.url}/api/ps")
                    try:
                        _bielik = (await _cl_z.get(f"{self._Z13_BIELIK_URL}/health", timeout=1.5)).status_code == 200
                    except Exception:
                        _bielik = False
                if _r.status_code == 200:
                    _used_mb = sum(
                        int(m.get("size", 0)) for m in (_r.json() or {}).get("models", [])
                    ) // (1024 * 1024)
                    value = max(0, self._Z13_PULA_MB - _used_mb - (self._Z13_BIELIK_MB if _bielik else 0))
                    self._live_vram_cache[resource_key] = (now, value)
                    return value
            except Exception as exc:
                logger.debug("[live-vram] z13_ram probe failed: %s — hardcoded %d", exc, fallback)
            return fallback

        # SHADOWGUEST (.130) — node_hardware_truth nie ma dla niej sondy (zna .10 i .253),
        # wiec liczymy jak dla z13: wolne = uzyteczne 11000 MB - suma rozmiarow modeli w /api/ps.
        # Uwaga na ograniczenie tej metody: widzi TYLKO VRAM zajety przez ollame. Gdy przy .130
        # siedzi gosc i gra, gra nie pojawi sie w /api/ps — stad bramka na winagencie (mode/inference).
        if resource_key == "guest_vram":
            try:
                import httpx as _hx_g
                async with _hx_g.AsyncClient(timeout=2.5, headers=auth_headers()) as _cl_g:
                    _rg = await _cl_g.get(f"{self.guest.url}/api/ps")
                if _rg.status_code == 200:
                    _used_g = sum(
                        int(m.get("size", 0)) for m in (_rg.json() or {}).get("models", [])
                    ) // (1024 * 1024)
                    value = max(0, 11000 - _used_g)
                    self._live_vram_cache[resource_key] = (now, value)
                    return value
            except Exception as exc:
                logger.debug("[live-vram] guest_vram probe failed: %s — hardcoded %d", exc, fallback)
            return fallback

        # Only GPU/WIN VRAM tiers have an nvidia-smi probe — other RAM tiers stay hardcoded.
        if resource_key not in ("gpu_vram", "win_vram"):
            return fallback

        # Operator/Claude-10 2026-06-18 (authoritative real-source): preferuj zunifikowany
        # node_hardware_truth (.10 nvidia-smi+Gigabyte lm-sensors, .253 LHM :8085 — szybkie HTTP,
        # dokładne, zamiast wolnego winrm) nad vram_manager. Fallback do vram_manager zachowany.
        try:
            from app.services import node_hardware_truth as _nht
            _probe = _nht._probe_10 if resource_key == "gpu_vram" else _nht._probe_253
            _tr = await _probe()
            if _tr.get("ok") and _tr.get("free_gb") is not None:
                value = max(0, int(float(_tr["free_gb"]) * 1024))
                self._live_vram_cache[resource_key] = (now, value)
                return value
        except Exception as exc:
            logger.debug("[live-vram] node_hardware_truth %s failed: %s — fallback vram_manager",
                         resource_key, exc)

        node_id = "gpu" if resource_key == "gpu_vram" else "win"
        try:
            from app.services.vram_manager import get_node_vram_free
            free_mb = await get_node_vram_free(node_id)
            value = max(0, int(free_mb))
            self._live_vram_cache[resource_key] = (now, value)
            return value
        except Exception as exc:
            logger.debug("[live-vram] %s probe failed: %s — using hardcoded %d",
                         resource_key, exc, fallback)
            return fallback

    @staticmethod
    def _pool_key_for(node_id: str, is_cpu: bool) -> str:
        """Map (node, tier) -> jeden z 5 realnych poolow pamieci (_NODE_BUDGET
        ma dokladnie te klucze: gpu_vram/gpu_ram/win_vram/win_ram/z13_ram).
        Z13 to UMA — VRAM i RAM to fizycznie ta sama pula, wiec zawsze 'z13_ram'.
        Operator 2026-07-01 (operator: 'cpu tez ma miec modele na .10' —
        dedykowany node 'cpu' (osobna instancja ollama-CPU dla gemma4:26b,
        patrz __init__ self.cpu) fizycznie dzieli RAM z .10 GPU-node CPU-fallback
        — TA SAMA pamiec, nie osobna piata pula. Bez tego mapowania KeyError na
        _NODE_BUDGET['cpu_ram'] przy pierwszym zapytaniu o gemma4:26b."""
        if node_id == "z13":
            return "z13_ram"
        if node_id == "cpu":
            return "gpu_ram"
        return f"{node_id}_{'ram' if is_cpu else 'vram'}"

    async def _acquire_pool_budget(self, pool: str, mb_needed: float) -> bool:
        """Atomowy check-and-reserve: czy (zywy wolny budzet) - (juz zarezerwowane
        przez inne w-locie requesty na TYM poolu) >= mb_needed. Lock gwarantuje ze
        dwa rownolegle requesty widza sie nawzajem, nie tylko martwa migawke
        nvidia-smi/api-ps sprzed chwili (to byl root-cause: semafor liczyl sloty,
        nie MB). Zwraca True i REZERWUJE gdy sie miesci — caller MUSI zwolnic
        przez _release_pool_budget w finally."""
        if mb_needed <= 0:
            return True
        async with self._pool_lock:
            live_free = await self._get_live_budget(pool)
            already = self._pool_reserved_mb.get(pool, 0.0)
            if mb_needed <= max(0.0, live_free - already):
                self._pool_reserved_mb[pool] = already + mb_needed
                return True
            return False

    async def _release_pool_budget(self, pool: str, mb_needed: float) -> None:
        if mb_needed <= 0:
            return
        async with self._pool_lock:
            self._pool_reserved_mb[pool] = max(0.0, self._pool_reserved_mb.get(pool, 0.0) - mb_needed)

    def _pool_busy_nodes(self) -> dict[str, int]:
        """Realna rezerwacja (nie dlugosc kolejki semafora) jako busy_nodes dla
        best_available() — rownanie transport_cost(t) ma widziec faktyczny ucisk
        pamieci, nie tylko liczbe oczekujacych person (Operator 2026-07-01,
        operator: 'nie tylko transport cost')."""
        busy: dict[str, int] = {}
        for pool, mb in self._pool_reserved_mb.items():
            if mb > 0:
                node_id = pool.split("_")[0]
                busy[node_id] = busy.get(node_id, 0) + 1
        return busy

    async def _admit_with_reservation(
        self,
        node: "OllamaNode",
        used_model: str,
        is_cpu: bool,
        *,
        role_class: str = "heavy",
    ) -> tuple["OllamaNode", str, bool, str, float]:
        """Rezerwuje realny budzet pamieci dla (node, used_model) PRZED wejsciem
        do semafora. Gdy pool jest juz zajety przez inny w-locie request (nie
        martwa migawke, tylko faktyczna rezerwacja) — pyta best_available()
        o alternatywny wezel (nieeuklidesowy wybor po transport_cost(t), z
        busy_nodes=realna rezerwacja) ZANIM w ogole rozwaza czekanie. Dopiero
        gdy alternatywy nie ma — bounded wait, a na koncu wymuszenie z fail-loud
        warningiem (ksiegujemy realnie, zeby kolejne requesty widzialy prawde,
        nie milczaco nadpisywaly). Zwraca finalny (node, model, is_cpu, pool, mb)
        — mb JEST juz zarezerwowane, caller MUSI zwolnic w finally."""
        mb = self._last_reserved_mb
        pool = self._pool_key_for(node.id, is_cpu)
        # 08.11: model już w pamięci węzła jest odjęty w żywym budżecie (/api/ps) — nie rezerwuj go drugi raz.
        if mb > 0 and await _juz_zaladowany(node, used_model):
            logger.debug("[pool-budget] %s już załadowany na %s — rezerwacja 0MB (było %.0fMB)",
                         used_model, node.id, mb)
            mb = 0.0
        if await self._acquire_pool_budget(pool, mb):
            return node, used_model, is_cpu, pool, mb

        logger.debug("[pool-budget] %s zajety (need=%.0fMB) — pytam best_available o alternatywe", pool, mb)
        _powod: str | None = None
        try:
            _przypiecie = _WEZLY_MODELU.get(used_model)
            if _przypiecie:
                _wezly_txt = (_przypiecie if isinstance(_przypiecie, str)
                              else "/".join(str(_w) for _w in _przypiecie))
                _powod = f"model przypięty do {_wezly_txt} — przekierowanie pominięte"
                # Model przypięty decyzją operatora — przekierowanie wypchnęłoby go poza jego węzły
                # (np. e4b na Z13); zostaje czekanie/wymuszenie na własnym węźle.
                raise LookupError(f"{used_model} przypięty do {_WEZLY_MODELU[used_model]}")
            from app.services.model_tiering import best_available, cached_est_mem_mb
            _pick = await best_available(role_class=role_class, busy_nodes=self._pool_busy_nodes())
            if _pick:
                _alt_model, _alt_node_id = _pick
                _alt_node = {"gpu": self.gpu, "win": self.win, "z13": self.z13}.get(_alt_node_id)
                if _alt_node is None:
                    _powod = f"kandydat {_alt_node_id} to węzeł niedostępny lub wyłączony"
                elif _alt_node.id == node.id:
                    _powod = f"kandydat {_alt_node_id} to ten sam węzeł"
                elif not _alt_node.available or not _alt_node.enabled:
                    _powod = f"kandydat {_alt_node_id} to węzeł niedostępny lub wyłączony"
                else:
                    _alt_model_final = _alt_node.canonical_model(_alt_model)
                    if not _wolno_na_wezle(_alt_node.id, _alt_model_final):
                        _powod = (f"reguła listy modeli węzła {_alt_node.id} "
                                  f"nie pozwala kandydatowi {_alt_model_final}")
                        # 22:05Z „przekierowano z13 -> gpu/gemma4:12b” — .10 = tylko e4b (decyzja operatora 30.09)
                        raise LookupError(f"{_alt_model_final} nie liczy na {_alt_node.id}")
                    if await _juz_zaladowany(_alt_node, _alt_model_final):
                        # 08.11: kandydat już rezydentny na swoim węźle — jego MB są już poza żywym budżetem.
                        _alt_mb = 0.0
                    else:
                        _alt_mb = float(cached_est_mem_mb(_alt_model_final) or mb)
                    # best_available() zawsze wybiera zywy/VRAM-tier kandydat (nie CPU-fallback
                    # rebalans) — is_cpu=False dla gpu/win; z13 to UMA, wlasny pool bez tego rozroznienia.
                    _alt_is_cpu = False if _alt_node.id in ("gpu", "win") else is_cpu
                    _alt_pool = self._pool_key_for(_alt_node.id, _alt_is_cpu)
                    if await self._acquire_pool_budget(_alt_pool, _alt_mb):
                        logger.info(
                            "[pool-budget] przekierowano %s -> %s/%s (transport_cost(t), nie slepe czekanie)",
                            node.id, _alt_node.id, _alt_model_final,
                        )
                        return _alt_node, _alt_model_final, _alt_is_cpu, _alt_pool, _alt_mb
                    _powod = (f"pula kandydata {_alt_pool} bez budżetu "
                              f"(kandydat {_alt_node.id}/{_alt_model_final}, need={_alt_mb:.0f}MB)")
            else:
                _powod = "funkcja kandydata (best_available) nie dała kandydata"
        except Exception as exc:
            logger.debug("[pool-budget] best_available redirect failed: %s", exc)
            if _powod is None:
                _powod = f"wyjątek przy przekierowaniu: {exc}"

        # Brak lepszej alternatywy — bounded wait (moze w-locie request zaraz zwolni).
        for _ in range(4):
            await asyncio.sleep(1.5)
            if await self._acquire_pool_budget(pool, mb):
                logger.info("[pool-budget] %s zwolnil sie po oczekiwaniu", pool)
                return node, used_model, is_cpu, pool, mb

        try:
            _wolne = await self._get_live_budget(pool)
        except Exception as exc:
            logger.debug("[pool-budget] odczyt wolnej pamieci %s do logu nieudany: %s", pool, exc)
            _wolne = None
        _wolne_txt = "None" if _wolne is None else f"{float(_wolne):.0f}"
        _zarezerwowane = self._pool_reserved_mb.get(pool, 0.0)
        logger.warning(
            "[pool-budget] wymuszam %s mimo przekroczenia budzetu "
            "(need=%.0fMB, wolne=%s, zarezerwowane=%.0fMB, model=%s, węzeł=%s; powód: %s) "
            "— ryzyko OOM/thrashing",
            pool, mb, _wolne_txt, _zarezerwowane, used_model, node.id, _powod or "nieznany",
        )
        async with self._pool_lock:
            self._pool_reserved_mb[pool] = self._pool_reserved_mb.get(pool, 0.0) + mb
        return node, used_model, is_cpu, pool, mb

    async def _resolve_with_vram(
        self,
        model: str | None,
        num_ctx: int,
        *,
        allow_cpu_fallback: bool = True,
        persona_preference: str = "auto",
        force_cpu: bool = False,
        priority: str = "normal",
        preferred_node: str | None = None,
    ) -> tuple['OllamaNode', str]:
        """4-resource cascade: GPU VRAM → WIN VRAM → GPU RAM → WIN RAM → Cloud.

        Returns (node, model). Sets self._last_num_gpu for the caller to use.

        allow_cpu_fallback=False restricts to Tier 1/2 (VRAM only) —
            background tasks that could tolerate CPU should pass True (default),
            foreground user-facing calls that must be fast can pass False and
            cascade hands off to cloud when VRAM is exhausted.
        persona_preference ∈ {"gpu", "win", "auto"} — reorders Tier 1/2 so the
            preferred node is tried first. Never excludes the other node — only
            reorders.
        force_cpu=True skips Tier 0/1/2 and jumps straight to Tier 3 (RAM).
        """
        self._last_num_gpu = -1  # default: use GPU layers

        # Operator 2026-07-01: "primary" chat (model=None) szedl przez staly
        # settings.primary_model string-match -> zawsze ten sam model/node, bez
        # wiedzy o warm/cold/kolejce. best_available() (model_tiering.py) juz to
        # liczy transport_cost(t)-owo dla rosteru schedulera — tu podpinamy TEN
        # SAM mozg pod zwykly czat, zamiast trzymac dwa rozjechane. role_class
        # z priority: background (heartbeat/refleksja) = light, normal = heavy
        # (operator chce najlepszej odpowiedzi). Fallback na stary _resolve()
        # gdy best_available nic nie zwroci (flota niedostepna) — fail-soft, nie
        # fail-loud, bo to tylko dobor, nie krytyczna sciezka.
        node = used_model = None
        if model is None:
            try:
                from app.services.model_tiering import best_available
                _role = "light" if priority == "background" else "heavy"
                # Operator 2026-07-01 (operator: "nie tylko transport cost" — rownanie
                # ma widziec realny ucisk, nie tylko dlugosc kolejki person): busy_nodes
                # teraz pochodzi z faktycznej rezerwacji pooli (_pool_reserved_mb), nie
                # z szacunku semaforow.
                _pick = await best_available(role_class=_role, busy_nodes=self._pool_busy_nodes())
                if _pick:
                    _picked_model, _picked_node_id = _pick
                    _picked_node = {"gpu": self.gpu, "win": self.win, "z13": self.z13}.get(_picked_node_id)
                    if _picked_node is not None and _picked_node.available and _picked_node.enabled:
                        node, used_model = _picked_node, _picked_node.canonical_model(_picked_model)
                        logger.debug("[best_available] role=%s -> %s/%s", _role, _picked_node_id, _picked_model)
            except Exception as _ba_exc:
                logger.warning("best_available() failed, falling back to static primary_model: %s", _ba_exc)

        if node is None:
            try:
                node, used_model = self._resolve(model, preferred_node=preferred_node)
            except _CloudFallbackSignal:
                raise
            except HTTPException:
                raise

        # Trójkąt decyzyjny (Claude-10 2026-09-30): model stojący na kilku węzłach idzie tam, gdzie pełne
        # równanie (teraźniejszość × przyszłość × przeszłość) daje najniższy koszt — nie tylko kolejka.
        # Jawny preferred_node (SmartRouter) i force_cpu mają pierwszeństwo.
        if node is not None and used_model and not force_cpu and "embed" not in used_model.lower():
            try:
                from app.services.model_tiering import cached_est_mem_mb as _cem
                _need = float(_cem(used_model) or self._MODEL_VRAM_MB.get(used_model, 5000)) / 1024.0
            except Exception:
                _need = float(self._MODEL_VRAM_MB.get(used_model, 5000)) / 1024.0
            if preferred_node is None:
                try:
                    _w = self._wybierz_trojkatem(used_model, _need)
                    if _w and _w["node"] is not node:
                        logger.info("[trojkat] %s: %s -> %s (%s)", used_model, node.id, _w["node"].id, _w["reason"][:140])
                        node, used_model = _w["node"], _w["node"].canonical_model(used_model)
                except Exception as _te:
                    logger.debug("[trojkat] wybór padł: %s", _te)
            # Węzeł z listą dozwolonych modeli (.10 = tylko e4b): inny model idzie do innego żywego właściciela
            # (trójkątem, gdy jest ich kilku), a gdy nikogo nie ma — zastępuje go dozwolony model węzła.
            _mw = _MODELE_NA_WEZLE.get(node.id)
            if _mw and used_model not in _mw and not _WEZLY_MODELU.get(used_model):
                # Mały model (≤ MALY_MODEL_GB) zastępuje e4b na miejscu — przenoszenie np. qwen2.5 persony kończyło się
                # w kaskadzie jako 27b na Z13; duży (27b…) idzie do swojego właściciela.
                _inni = [n for n in self._nodes if n.id not in ("cpu", node.id) and n.available and n.enabled
                         and n.owns_exact(used_model)] if _need > MALY_MODEL_GB else []
                _cel = None
                if len(_inni) > 1:
                    try:
                        _w2 = self._wybierz_trojkatem(used_model, _need, bez=(node.id,))
                        _cel = _w2["node"] if _w2 else None
                    except Exception:
                        _cel = None
                if _cel is None and _inni:
                    _cel = sorted(_inni, key=lambda n: _KOLEJNOSC_ZAPASOWA.index(n.id)
                                  if n.id in _KOLEJNOSC_ZAPASOWA else 99)[0]
                if _cel is not None:
                    logger.info("[trojkat] %s: %s tylko dla %s -> %s (decyzja operatora 30.09)",
                                used_model, node.id, _mw, _cel.id)
                    node, used_model = _cel, _cel.canonical_model(used_model)
                elif node.owns_exact(_mw[0]):
                    logger.info("[trojkat] %s na %s -> %s (brak innego właściciela; .10 = tylko e4b)",
                                used_model, node.id, _mw[0])
                    used_model = node.canonical_model(_mw[0])
            # Model poza swoją listą węzłów (np. e4b wybrany na .253 przez best_available) wraca na dozwolony.
            _dozw = _WEZLY_MODELU.get(used_model)
            if _dozw and node.id not in _dozw:
                _cel = self._zapas_kolejno(used_model)
                if _cel is None:
                    _cel = next((n for n in self._nodes if n.id in _dozw and n.id != "z13" and n.available
                                 and n.enabled and n.owns_exact(used_model)), None)
                if _cel is not None:
                    logger.info("[trojkat] %s: %s poza dozwolonymi %s -> %s (decyzja operatora 30.09)",
                                used_model, node.id, _dozw, _cel.id)
                    node, used_model = _cel, _cel.canonical_model(used_model)
            _zast = _ZASTEPSTWO_NA_WEZLE.get((used_model, node.id))
            if _zast and node.owns_exact(_zast):
                logger.info("[trojkat] %s na %s -> %s (decyzja operatora 30.09: na .10 czatuje e4b)",
                            used_model, node.id, _zast)
                used_model = node.canonical_model(_zast)

        base_model = used_model.split(":")[0] if used_model else ""

        # Embeddings always CPU.
        # Claude-10 2026-09-06: bylo `"embedding" in ...` — zlapie qwen3-embedding (.10/.172),
        # ale NIE nomic-embed-text z .130. Organizm ma DWA rodzaje embeddingow o roznych
        # nazwach i roznej wymiarowosci; dopasowanie po "embed" lapie oba.
        if "embed" in used_model.lower():
            self._last_num_gpu = 0
            self._last_reserved_mb = 0.0
            return node, used_model

        # Estimate VRAM/RAM needed. Operator 2026-07-01 (operator: flota ma 38
        # modeli/node z realnymi rozmiarami z /api/tags — nie zgaduj statyczna
        # tabela gdy fakt jest znany): preferuj model_tiering.cached_est_mem_mb
        # (zywy probe fleet), spadaj do _MODEL_VRAM_MB/_MODEL_RAM_MB tylko gdy
        # model jeszcze nie byl probowany (cache pusty/zimny).
        def _est_mb(_m: str, _bm: str, _static: dict, _default: int) -> int:
            try:
                from app.services.model_tiering import cached_est_mem_mb
                _live = cached_est_mem_mb(_m)
                if _live is not None:
                    return _live
            except Exception:
                pass
            return _static.get(_m, _static.get(_bm, _default))

        model_vram = _est_mb(used_model, base_model, self._MODEL_VRAM_MB, 5000)
        model_ram = _est_mb(used_model, base_model, self._MODEL_RAM_MB, 8000)
        kv_cache_mb = num_ctx * 8 // 1024
        vram_needed = model_vram + kv_cache_mb
        ram_needed = model_ram + kv_cache_mb

        win_ok = self.win.available and self.win.enabled
        z13_ok = self.z13.available and self.z13.enabled
        guest_ok = self.guest.available and self.guest.enabled

        # Live budgets — replaces hardcoded _NODE_BUDGET with nvidia-smi / api/ps.
        gpu_vram_free = await self._get_live_budget("gpu_vram")
        win_vram_free = await self._get_live_budget("win_vram") if win_ok else 0
        z13_ram_free = await self._get_live_budget("z13_ram") if z13_ok else 0
        guest_vram_free = await self._get_live_budget("guest_vram") if guest_ok else 0
        gpu_ram_free = self._NODE_BUDGET["gpu_ram"]
        win_ram_free = self._NODE_BUDGET["win_ram"]
        # Claude-10 2026-09-06: JAWNA mapa node->budzet zamiast zagniezdzonego ternary.
        # Stare `a if id=="gpu" else (b if id=="win" else c)` mapowalo KAZDY nieznany
        # node na z13_ram_free — czyli liczylo budzet zupelnie innej maszyny. Ten sam
        # blad jest juz raz opisany wyzej dla node'a "cpu" (komentarz z 2026-07-01);
        # przy dokladaniu "guest" powtorzylby sie po raz drugi, wiec wzorzec znika.
        _budgets = {
            "gpu": gpu_vram_free,
            "win": win_vram_free,
            "z13": z13_ram_free,
            "guest": guest_vram_free,
        }

        if force_cpu:
            logger.debug("[force-cpu] skipping VRAM tiers for %s", used_model)
        else:
            # ── Persona preference override ────────────────────────────────────
            def _recompute_sizes(_m: str) -> tuple[int, int]:
                _bm = _m.split(":")[0] if _m else ""
                _v = _est_mb(_m, _bm, self._MODEL_VRAM_MB, 5000) + kv_cache_mb
                _r = _est_mb(_m, _bm, self._MODEL_RAM_MB, 8000) + kv_cache_mb
                return _v, _r

            if persona_preference == "win" and node.id != "win" and win_ok:
                if self.win.owns_exact(used_model):
                    node = self.win
                    used_model = self.win.canonical_model(used_model)
                else:
                    node = self.win
                    used_model = self.win.default_model
                    vram_needed, ram_needed = _recompute_sizes(used_model)
                logger.debug("[persona-pref] override to WIN: model=%s", used_model)
            elif persona_preference == "gpu" and node.id != "gpu":
                if self.gpu.owns_exact(used_model):
                    node = self.gpu
                    used_model = self.gpu.canonical_model(used_model)
                else:
                    node = self.gpu
                    used_model = self.gpu.default_model
                    vram_needed, ram_needed = _recompute_sizes(used_model)
                logger.debug("[persona-pref] override to GPU: model=%s", used_model)

            # ── Tier 0: model already loaded on primary node — bypass VRAM math ──
            # True tylko gdy /api/ps odpowiedziało statusem 200 i JSON dał się odczytać.
            _ps_odpowiedz = False
            try:
                import httpx as _hx_ps
                async with _hx_ps.AsyncClient(timeout=2.0, headers=auth_headers()) as _cl_ps:
                    _psr = await _cl_ps.get(f"{node.url}/api/ps")
                    if _psr.status_code == 200:
                        _ps_data = _psr.json() or {}
                        _ps_odpowiedz = True
                        for _loaded in _ps_data.get("models", []):
                            _lm = (_loaded.get("name") or "").lower()
                            _um = used_model.lower()
                            # 30.09: było `startswith(rodzina + ":")` — e4b w pamięci uchodził za „już
                            # załadowany” przy prośbie o 12b: rezerwacja 0 MB, a Ollama wymieniała modele
                            # (thrashing .10 zgłoszony przez sesję rozliczenia Klient). Tylko ten sam tag.
                            if _lm == _um or _lm == _um + ":latest" or _um == _lm + ":latest":
                                logger.debug(
                                    "[4-route] Tier 0: %s already loaded for %s — reuse runner",
                                    node.id.upper(), used_model,
                                )
                                self._last_reserved_mb = 0.0
                                return node, used_model
            except Exception:
                pass  # /api/ps check is best-effort

            # ── Tier 1: primary node VRAM ──────────────────────────────────────
            # Operator 2026-07-01 (operator: precyzja — "cpu" node (dedykowana
            # instancja ollama-CPU dla gemma4:26b) nie mial WLASNEJ galezi tutaj:
            # nested-ternary defaultowal do z13_ram_free (kompletnie zla liczba,
            # sprawdzala budzet INNEJ maszyny). "cpu" jest RAM-only (brak VRAM w
            # ogole), wiec porownuje sie po ram_needed/gpu_ram_free (fizycznie
            # ten sam RAM co CPU-fallback GPU-node'a na .10), nie po vram_needed.
            if node.id == "cpu":
                if ram_needed <= gpu_ram_free:
                    self._last_num_gpu = 0
                    logger.debug(
                        "[4-route] Tier 1: CPU RAM for %s (need=%dMB free=%dMB)",
                        used_model, ram_needed, gpu_ram_free,
                    )
                    self._last_reserved_mb = ram_needed
                    return node, used_model
                primary_vram_budget = -1  # brak fitu na primary — leci dalej w kaskadzie
            else:
                # -1 = nieznany node → brak fitu na primary, leci dalej w kaskade
                # (bezpieczniej niz zgadnac cudzy budzet).
                primary_vram_budget = _budgets.get(node.id, -1)
            if vram_needed <= primary_vram_budget:
                logger.debug(
                    "[4-route] Tier 1: %s %s for %s (need=%dMB free=%dMB)",
                    node.id.upper(), "VRAM" if node.id != "z13" else "Memory",
                    used_model, vram_needed, primary_vram_budget,
                )
                self._last_reserved_mb = vram_needed
                return node, used_model

            # 30.09: model przypięty decyzją operatora zostaje na swoim węźle mimo braku budżetu — Ollama zwolni
            # miejsce, wyrzucając inny model (np. e4b na .10 wypiera 27b). Tier 2 zamieniał e4b na domyślny model Z13.
            _pin_dozw = _WEZLY_MODELU.get(used_model)
            if _pin_dozw and node.id in _pin_dozw:
                _pin_powod = "rezydencja nieznana, /api/ps nie odpowiedział"
                if _ps_odpowiedz:
                    _pin_spr = []
                    _pin_po_id = {n.id: n for n in self._nodes}
                    for _pin_id in _pin_dozw:
                        if _pin_id == node.id:
                            continue
                        _pin_n = _pin_po_id.get(_pin_id)
                        _pin_free = _budgets.get(_pin_id, 0) or 0
                        if _pin_n is None:
                            _pin_spr.append(f"{_pin_id} free={int(_pin_free)}MB brak węzła")
                            continue
                        if not (_pin_n.available and _pin_n.enabled):
                            _pin_spr.append(f"{_pin_id} free={int(_pin_free)}MB niedostępny")
                            continue
                        if not _pin_n.owns_exact(used_model):
                            _pin_spr.append(f"{_pin_id} free={int(_pin_free)}MB nie ma modelu")
                            continue
                        _pin_model = _pin_n.canonical_model(used_model)
                        if not _wolno_na_wezle(_pin_id, _pin_model):
                            _pin_spr.append(f"{_pin_id} free={int(_pin_free)}MB reguła węzła zabrania")
                            continue
                        if vram_needed > _pin_free:
                            _pin_spr.append(f"{_pin_id} free={int(_pin_free)}MB nie mieści")
                            continue
                        logger.info(
                            "[4-route] %s: %s nie mieści, need=%dMB free=%dMB, brak w /api/ps -> %s free=%dMB",
                            used_model, node.id, int(vram_needed), int(primary_vram_budget),
                            _pin_id, int(_pin_free),
                        )
                        self._last_reserved_mb = vram_needed
                        return _pin_n, _pin_model
                    _pin_powod = "; ".join(_pin_spr) or "brak innego dozwolonego węzła"
                logger.info(
                    "[4-route] %s przypięty do %s — zostaje (need=%dMB, free=%dMB), Ollama zwolni miejsce; powód: %s",
                    used_model, node.id, int(vram_needed), int(primary_vram_budget), _pin_powod,
                )
                self._last_reserved_mb = vram_needed
                return node, used_model

            # ── Tier 2: alternate node VRAM/Memory — exact owner or swap to alt default ──
            for other in [self.gpu, self.guest, self.win, self.z13]:
                if other is node:
                    continue
                _other_ok = (
                    (other.id == "gpu")
                    or (other.id == "guest" and guest_ok)
                    or (other.id == "win" and win_ok)
                    or (other.id == "z13" and z13_ok)
                )
                if not _other_ok:
                    continue

                other_budget = _budgets.get(other.id, 0)
                if other.owns_exact(used_model) and vram_needed <= other_budget:
                    logger.debug(
                        "[4-route] Tier 2: %s %s for %s (alt owner, need=%dMB free=%dMB)",
                        other.id.upper(), "VRAM" if other.id != "z13" else "Memory",
                        used_model, vram_needed, other_budget,
                    )
                    self._last_reserved_mb = vram_needed
                    return other, other.canonical_model(used_model)

                # Alt's default model fallback
                alt_default = other.default_model
                alt_vram_needed, _ = _recompute_sizes(alt_default)
                if alt_vram_needed <= other_budget:
                    logger.debug(
                        "[4-route] Tier 2: %s %s with %s (primary %s exhausted for %s)",
                        other.id.upper(), "VRAM" if other.id != "z13" else "Memory",
                        alt_default, node.id, used_model,
                    )
                    self._last_reserved_mb = alt_vram_needed
                    return other, alt_default

        if not allow_cpu_fallback and not force_cpu:
            # Foreground task that refuses CPU tier — hand off to cloud if any.
            logger.debug(
                "[4-route] VRAM exhausted and allow_cpu_fallback=False — signalling cloud",
            )
            if self._cloud_available():
                raise _CloudFallbackSignal()

            # Operator 2026-04-13: cloud disabled → don't fall through to CPU
            # (30-60s blocking). Instead scan both nodes for any already-loaded
            # model and reuse it. Pinned models (qwen3:14b, qwen3-embedding)
            # are ALWAYS available — better to answer with wrong model than
            # block for 60s on CPU fallback.
            try:
                import httpx as _hx_reuse
                for _check_node in [self.gpu, self.guest, self.win]:
                    if _check_node is self.win and not win_ok:
                        continue
                    if _check_node is self.guest and not guest_ok:
                        continue
                    try:
                        async with _hx_reuse.AsyncClient(timeout=2.0, headers=auth_headers()) as _cl_r:
                            _rr = await _cl_r.get(f"{_check_node.url}/api/ps")
                            if _rr.status_code != 200:
                                continue
                            _loaded_models = (_rr.json() or {}).get("models", []) or []
                            # Prefer deep models (qwen3:14b, deepseek-r1) first
                            _sorted = sorted(
                                _loaded_models,
                                key=lambda m: (
                                    0 if "qwen3:14b" in m.get("name", "") else
                                    1 if "deepseek-r1" in m.get("name", "") else
                                    2 if "embed" not in m.get("name", "").lower() else
                                    99  # embedding last — not a chat model
                                ),
                            )
                            for _m in _sorted:
                                _name = _m.get("name", "")
                                if "embed" in _name.lower():
                                    # Claude-10 2026-09-06: bylo "embedding" — filtr przepuszczal
                                    # `nomic-embed-text` z .130 (dodanej dzis do tego skanu) jako
                                    # model czatowy. Router oddalby embedder do generacji tekstu.
                                    continue  # embeddings cannot answer chat
                                logger.debug(
                                    "[4-route] Tier 2.5: reusing loaded %s on %s "
                                    "(refused %s on CPU)",
                                    _name, _check_node.id.upper(), used_model,
                                )
                                self._last_reserved_mb = 0.0
                                return _check_node, _name
                    except Exception:
                        continue
            except Exception:
                pass

            # Last resort: raise 503 instead of 60s CPU block
            from fastapi import HTTPException as _HE
            raise _HE(
                status_code=503,
                detail=(
                    f"All GPU slots saturated and no loaded chat model available. "
                    f"Requested: {used_model}. Try again in 30s or use background "
                    f"priority=allow_cpu_fallback=True for non-urgent work."
                ),
            )

        # ── Tier 3: node with MORE free RAM (CPU) ──────────────────────────
        # Rebalance across 4 resource pools: for CPU fallback, pick the node
        # with more headroom instead of blindly using primary. Persona preference
        # only governs VRAM tiers — CPU path optimizes total-RAM utilization.
        # Respects persona_preference if explicitly "gpu" or "win".
        if force_cpu and persona_preference not in ("gpu", "win"):
            gpu_cpu_ok = self.gpu.available and ram_needed <= gpu_ram_free
            win_cpu_ok = win_ok and ram_needed <= win_ram_free
            if gpu_cpu_ok and win_cpu_ok:
                chosen_cpu = self.gpu if gpu_ram_free >= win_ram_free else self.win
            elif gpu_cpu_ok:
                chosen_cpu = self.gpu
            elif win_cpu_ok:
                chosen_cpu = self.win
            else:
                chosen_cpu = node  # neither fits — fall through to Tier 5
            if chosen_cpu.id != node.id:
                node = chosen_cpu
                other = self.win if chosen_cpu.id == "gpu" else self.gpu
                other_ok = (other.id == "gpu") or win_ok
                used_model = (
                    chosen_cpu.canonical_model(used_model)
                    if chosen_cpu.owns_exact(used_model)
                    else chosen_cpu.default_model
                )
                logger.debug(
                    "[4-route] Tier 3 rebalance: CPU → %s (gpu_ram=%dMB, win_ram=%dMB)",
                    chosen_cpu.id.upper(), gpu_ram_free, win_ram_free,
                )
        other = self.win if node.id == "gpu" else self.gpu
        other_ok = (other.id == "gpu") or win_ok
        # Claude-10 2026-09-06: ten sam blad co przy VRAM 200 linii wyzej, tylko dla RAM —
        # `gpu if id=="gpu" else win` wrzucalo guest, z13 ORAZ cpu na budzet RAM .253.
        # Mapa jest teraz spojna z _pool_key_for (cpu fizycznie dzieli RAM z .10).
        _ram_budgets = {
            "gpu": gpu_ram_free,
            "cpu": gpu_ram_free,
            "win": win_ram_free,
            "z13": z13_ram_free,
            "guest": self._NODE_BUDGET["guest_ram"],
        }
        primary_ram_budget = _ram_budgets.get(node.id, 0)
        if ram_needed <= primary_ram_budget:
            self._last_num_gpu = 0
            logger.debug(
                "[4-route] Tier 3: %s RAM/CPU for %s (%dMB RAM, num_gpu=0)",
                node.id.upper(), used_model, ram_needed,
            )
            self._last_reserved_mb = ram_needed
            return node, used_model

        # ── Tier 4: alternate node RAM (CPU) ───────────────────────────────
        if other_ok:
            alt_ram_budget = win_ram_free if other.id == "win" else gpu_ram_free
            if ram_needed <= alt_ram_budget:
                self._last_num_gpu = 0
                alt_model = (
                    other.canonical_model(used_model)
                    if other.owns_exact(used_model)
                    else other.default_model
                )
                logger.debug(
                    "[4-route] Tier 4: %s RAM/CPU for %s (%dMB RAM, num_gpu=0)",
                    other.id.upper(), alt_model, ram_needed,
                )
                self._last_reserved_mb = ram_needed
                return other, alt_model

        # Tier 5: Force primary (might be slow/OOM but try)
        logger.warning(
            "[4-route] No tier fits %s (%dMB VRAM, %dMB RAM) — forcing %s",
            used_model, vram_needed, ram_needed, node.id,
        )
        self._last_reserved_mb = ram_needed if self._last_num_gpu == 0 else vram_needed
        return node, used_model

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """Strip <think>...</think> reasoning blocks from model output (qwen3, deepseek-r1)."""
        import re
        text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
        text = re.sub(r"<\|begin_of_thought\|>[\s\S]*?<\|end_of_thought\|>", "", text).strip()
        text = re.sub(r"</?think>", "", text).strip()
        return text

    async def _notify_model_used(self, model: str) -> None:
        """Track model usage for VRAM lifecycle management."""
        try:
            from app.services.vram_manager import notify_model_used
            await notify_model_used(model)
        except Exception:
            pass

    async def _maybe_free_heavy_for(self, node: OllamaNode, model: str) -> None:
        """Pre-emptively free ACE-Step / ComfyUI VRAM if we're about to load a
        heavy LLM on the GPU node. Best-effort, never raises.

        Debounced to once per 30s per router instance — back-to-back
        qwen3:14b requests don't need to hit comfyui /free each time
        (it's idempotent, but the repeated calls were spamming logs and
        showed up as "VRAM flap" in audits).
        """
        if node.id != "gpu":
            return
        m = (model or "").lower()
        # Only deep / large GPU models — small ones (qwen2.5, embeddings) fit fine
        if "qwen3:14b" not in m and "deepseek-r1" not in m:
            return
        import time as _t
        now = _t.time()
        if now - self._last_free_heavy_ts < 30.0:
            return
        self._last_free_heavy_ts = now
        try:
            from app.services.vram_manager import free_heavy_gpu_services
            await free_heavy_gpu_services()
        except Exception:
            pass


    async def autonomous_model_expire(self, idle_threshold_sec: float = 300) -> dict:
        """AGI Evolution: Automatically unload models from VRAM when system is idle.
        Prevents VRAM fragmentation on Nodes .10 and .253 when operator is away."""
        try:
            from app.services.vram_manager import cleanup_if_idle
            return await cleanup_if_idle(idle_threshold_sec=idle_threshold_sec)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def chat(
        self,
        prompt: str,
        model: str | None = None,
        priority: str = "normal",
        *,
        allow_cpu_fallback: bool | None = None,
        persona_preference: str = "auto",
        persona_name: str = "",
        force_cpu: bool = False,
        timeout: float | None = None,
        think: bool | None = None,
    ) -> tuple[str, str, str]:
        """Returns (response_text, used_model, used_node_id).

        think (30.09, prośba sesji rozliczenia Klient): None = jak dotąd (Ollama decyduje; gemma4 myśli),
            False = odpowiedz od razu. 22:08Z główna odpowiedź agenta na gemma4:12b zjadła cały
            num_predict=4096 na 'thinking' i wróciła pusta.

        allow_cpu_fallback — when False, the cascade refuses Tier 3/4 (CPU)
            and escalates to cloud instead. Foreground user-facing calls that
            must be fast should pass False; background / batch work defaults
            to True.
        persona_preference — "gpu" | "win" | "auto". Reorders VRAM tier
            preference without excluding the other node.
        persona_name — used for persona-aware cloud fallback tone guard.
        force_cpu — skips Tier 0/1/2 and jumps straight to Tier 3 (RAM).
        """
        # Claude-10 2026-09-30: `timeout` to budżet CAŁEGO wywołania — fallbacki dostawały każdy pełny
        # timeout od nowa (gemma4:e4b 51 s przy timeout=12 s, zgłoszone przez sesję rozliczenia Klient).
        _koniec = (time.monotonic() + float(timeout)) if timeout else None

        # ── Identity Guard Injection ──────────────────────────────────────────
        # Operator 2026-05-29: manifest-bleed fix. Ciężki manifest tożsamości jest
        # dla rozmów z operatorem (Shadow≠Firma). Dla priority="background"
        # (heartbeat/reflection) manifest zalewał prompt i ZŻERAŁ current_focus —
        # mały model recytował manifest zamiast myśleć nad fokusem (pętla w miejscu).
        # Heartbeat niesie własną tożsamość+focus w promptcie, więc go pomijamy.
        if priority == "background":
            full_prompt = prompt
        else:
            try:
                from app.services.brain_core import get_identity_manifest_prompt
                identity_manifest = get_identity_manifest_prompt()
                # Wrap original prompt with identity manifest
                full_prompt = f"{identity_manifest}\n\n[USER COMMAND]: {prompt}"
            except Exception:
                full_prompt = prompt

        # ── llama-server (.172:8081) direct dispatch — Claude 2026-09-15 ────────
        # Model z węzła OpenAI-compat idzie wprost, bez Ollamy i bez rezerwacji VRAM.
        # Awaria węzła = zejście na jego fallback_model przez zwykłą kaskadę niżej.
        if model and self.llamacpp.enabled and self.llamacpp.owns(model):
            try:
                return await self.llamacpp.chat(full_prompt, model, timeout=float(timeout or 300.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[llamacpp] %s padł (%s) — schodzę na Ollamę: %s",
                               model, str(exc)[:120], self.llamacpp.fallback_model or "default")
                model = self.llamacpp.fallback_model or None

        _num_ctx = self._estimate_num_ctx(prompt=full_prompt)
        # Auto-decide CPU fallback based on priority:
        # - background (heartbeat, training): CPU OK
        # - normal (operator command): NO CPU — wait for GPU or retry
        # Operator-visible requests never go to CPU anymore — Operator
        # 2026-04-13 incident: cascade pushed every operator query to CPU
        # when it misread 4 GB free VRAM as "pressure" and blocked for 30-60s.
        _cpu_fallback = (
            allow_cpu_fallback if allow_cpu_fallback is not None
            else (priority == "background")
        )
        try:
            node, used_model = await self._resolve_with_vram(
                model, _num_ctx,
                allow_cpu_fallback=_cpu_fallback,
                persona_preference=persona_preference,
                force_cpu=force_cpu,
                priority=priority,
            )
        except _CloudFallbackSignal:
            return await self._cloud_chat(prompt=full_prompt, persona_name=persona_name)

        await self._maybe_free_heavy_for(node, used_model)

        is_cpu = getattr(self, "_last_num_gpu", -1) == 0

        # Operator 2026-07-01 (operator: "nie jedz na skroty" — Semaphore(2)
        # liczyl SLOTY nie MB, dwa ciezkie modele mogly wejsc naraz i fizycznie
        # sie nie zmiescic, zwlaszcza z13 gdzie model_tiering.py zaklada JEDEN
        # slot heavy). Prawdziwa rezerwacja przed semaforem; gdy nie miesci sie,
        # pyta best_available (transport_cost(t), nie slepe czekanie) o alternatywe.
        node, used_model, is_cpu, _pool, _reserved_mb = await self._admit_with_reservation(
            node, used_model, is_cpu,
            role_class=("light" if priority == "background" else "heavy"),
        )

        ka = self._get_keep_alive(used_model)
        _opts = {"num_ctx": self._stable_num_ctx(used_model, _num_ctx), "num_predict": 4096}
        if is_cpu:
            _opts["num_gpu"] = 0

        # ── Cluster Concurrency Logic — 4 independent pools ───────────────────
        # GPU-VRAM / GPU-CPU / WIN-VRAM / WIN-CPU / Z13 — each 2 slots (SLOTY, nie
        # MB — realny budzet pamieci pilnowany osobno przez _admit_with_reservation
        # powyzej; semafor tu nadal ogranicza rownoczesnosc requestow per pool).
        if node.id == "win":
            active_semaphore = self._win_cpu_semaphore if is_cpu else self._win_semaphore
        elif node.id == "z13":
            active_semaphore = self._z13_semaphore
        elif node.id == "guest":
            # Claude-10 2026-09-06: bez tej galezi guest wpadal do `else` i bral slot
            # puli GPU (.10) — limitowalby maszyne, na ktorej nic sie nie dzieje,
            # a .130 puscil bez zadnego limitu rownoczesnosci.
            active_semaphore = self._guest_cpu_semaphore if is_cpu else self._guest_semaphore
        else:  # gpu node
            active_semaphore = self._gpu_cpu_semaphore if is_cpu else self._gpu_semaphore

        # Automatic deflection for background tasks if primary resource is busy.
        # Pick the first available alternate pool — prefer same-class (VRAM→VRAM, CPU→CPU).
        # 30.09: modeli przypiętych decyzją operatora (_WEZLY_MODELU) nie przerzucamy — deflection
        # podmieniał model na domyślny węzła zastępczego (e4b z .10 wylądował na Z13 jako inny model).
        if priority == "background" and active_semaphore.locked() and not _WEZLY_MODELU.get(used_model):
            alt_semaphore = None
            alt_node = None
            # 30.09 23:05Z: odbicia tła NA .10 zajmowały jedyne miejsce Ollamy (NUM_PARALLEL=1) 15-sekundowymi
            # generacjami (Bielik z zajętego Z13 → e4b), a namysł czekał za nimi 21 s. Decyzja operatora: .10 = e4b
            # dla toru telefonicznego i klasyfikatorów, które odpowiadają w sekundy — zadanie tła czeka na swój węzeł
            # albo idzie gdzie indziej, ale nie na .10.
            if node.id == "gpu":
                if self.guest.available and not self._guest_semaphore.locked():
                    alt_semaphore = self._guest_cpu_semaphore if is_cpu else self._guest_semaphore
                    alt_node = self.guest
                elif self.z13.available and not self._z13_semaphore.locked():
                    alt_semaphore = self._z13_semaphore
                    alt_node = self.z13
                elif self.win.available:
                    alt_semaphore = self._win_cpu_semaphore if is_cpu else self._win_semaphore
                    alt_node = self.win
            elif node.id == "guest":
                if self.z13.available:
                    alt_semaphore = self._z13_semaphore
                    alt_node = self.z13
            elif node.id == "win":
                if self.z13.available:
                    alt_semaphore = self._z13_semaphore
                    alt_node = self.z13
            elif node.id == "z13":
                if self.win.available:
                    alt_semaphore = self._win_cpu_semaphore if is_cpu else self._win_semaphore
                    alt_node = self.win
            
            if alt_semaphore is not None and not alt_semaphore.locked() and alt_node is not None:
                _odbity = (alt_node.canonical_model(used_model) if alt_node.owns(used_model)
                           else await alt_node.resolve_default_model())
                if not _wolno_na_wezle(alt_node.id, _odbity):
                    # 22:38Z: 27b z zajętego Z13 odbity na .10 (owned — bo zainstalowany) → 127 s, e4b wypchnięty.
                    logger.info("[resource] odbicie tła %s→%s pominięte — %s nie liczy na %s (decyzja operatora 30.09)",
                                node.id.upper(), alt_node.id.upper(), _odbity, alt_node.id)
                else:
                    logger.debug(
                        "[resource] Background task deflected %s→%s model=%s (%s, cpu=%s)",
                        node.id.upper(), alt_node.id.upper(), _odbity,
                        "owned" if alt_node.owns(used_model) else "default", is_cpu,
                    )
                    used_model = _odbity
                    node = alt_node
                    active_semaphore = alt_semaphore

        async def _do_request(target_node, target_model, req_timeout: float | None = None):
            if target_node.id == "gpu" and _opts.get("num_gpu") == 0 and self.cpu.available:
                # 30.09 23:25Z: kaskada (Tier 3/4 „RAM .10”) wysyłała num_gpu=0 do Ollamy KARTY :11434 — przy
                # MAX_LOADED_MODELS=1 przeładowywało to e4b na CPU (deepseek-r1:70b z Z13 → e4b, 2 min 1 s).
                # Tryb CPU na .10 idzie do osobnej instancji ollama-cpu :11435; karta zostaje dla e4b.
                logger.info("[cpu-10] %s w trybie CPU -> ollama-cpu :11435 (karta .10 zostaje dla e4b)", target_model)
                target_node = self.cpu
                # BEZ canonical_model: przed pingiem węzeł cpu zna tylko gemma4:26b, a dopasowanie po rodzinie
                # zamieniłoby e4b w 26b (pułapka „CURSED FIX” z 11.06). :11435 ma e4b pod dokładną nazwą.
            if not _wolno_na_wezle(target_node.id, target_model):
                _zast_m = _MODELE_NA_WEZLE[target_node.id][0]
                logger.warning("[straz-wezla] %s na %s -> %s (decyzja operatora 30.09: .10 = tylko e4b)",
                               target_model, target_node.id, _zast_m)
                target_model = _zast_m
            # 30.09 23:05Z: po odbiciu / zapasie / straży model się zmieniał, a num_ctx i keep_alive zostawały
            # policzone dla poprzedniego (Bielik 4096 → e4b przeładowany z 16384 na 4096 i z powrotem).
            _opts_req = dict(_opts, num_ctx=self._stable_num_ctx(target_model, _num_ctx))
            client = self._get_http()
            resp = await client.post(
                f"{target_node.url}/api/generate",
                json={"model": target_model, "prompt": full_prompt, "stream": False,
                      "keep_alive": self._get_keep_alive(target_model), "options": _opts_req,
                      **({"think": bool(think)} if think is not None else {})},
                timeout=_bounded_timeout(req_timeout, client.timeout),
            )
            resp.raise_for_status()
            _j = resp.json()
            # Operator 2026-07-26: ollama potrafi oddac 200 z polem "error" zamiast 500 —
            # bez tego bloku padly silnik przeszedlby jako "sukces z pusta odpowiedzia".
            _e = str(_j.get("error") or "")
            if _e and _is_runner_failure(_e):
                target_node.mark_runner_failed(_e)
                logger.error("Ollama '%s': SILNIK PADL (200+error: %s)", target_node.id, _e[:160])
                raise httpx.HTTPStatusError(_e, request=resp.request, response=resp)
            text = self._strip_think_tags(str(_j.get("response", "")))

            # Operator 2026-08-19: KANAREK na modele myslace.
            # Nowa ollama oddaje rozumowanie w OSOBNYM polu "thinking", nie w
            # <think> w tresci — _strip_think_tags go nie widzi. Gdy num_predict
            # wyczerpie sie na myslenie, wraca 200 + response="" i organizm
            # traktuje to jak poprawna pusta odpowiedz. Tak wlasnie avatar
            # mowil operatorowi "jestem zajety": model liczyl poprawnie, tylko
            # caly budzet zszedl na mysli. Zero sladu w logu przez miesiace.
            _think = str(_j.get("thinking") or "")
            if not text.strip() and _think.strip():
                logger.error(
                    "Ollama '%s': PUSTA ODPOWIEDZ z modelu myslacego %s — caly budzet "
                    "poszedl na 'thinking' (%d znakow, done_reason=%s, eval=%s tok). "
                    "PODNIES num_predict albo wylacz myslenie dla tej sciezki.",
                    target_node.id, target_model, len(_think),
                    _j.get("done_reason"), _j.get("eval_count"),
                )

            target_node.mark_runner_ok()  # Operator 2026-07-26: silnik dowiozl → bezpiecznik zdjety
            self._zapisz_skutek(target_node, True, target_model)
            await self._notify_model_used(target_model)
            return text, target_model, target_node.id

        try:
            async with active_semaphore:
                if active_semaphore == self._gpu_semaphore:
                    self._gpu_busy = True
                try:
                    return await _do_request(node, used_model, req_timeout=timeout)
                finally:
                    if active_semaphore == self._gpu_semaphore:
                        self._gpu_busy = False
        except NODE_UNREACHABLE as exc:
            # Wezel naprawde nieosiagalny — ping() sam go przywroci gdy wstanie.
            node._online = False
            logger.warning(
                "Ollama node '%s' unreachable (%s), trying fallback…",
                node.id, type(exc).__name__,
            )
        except httpx.ReadTimeout as exc:
            self._handle_read_timeout(node, used_model, timeout, exc)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # Operator 2026-07-26: odroznij "model/prompt zly" od "silnik padl". Dotad KAZDE
            # 500 tylko logowalo i szlo do fallbacku, wiec wezel z martwym runnerem zostawal
            # primary i zzeral kazda kolejna synteze (z13, ~24 h ciszy w metrykach).
            _body = ""
            try:
                _body = exc.response.text or ""
            except Exception:
                pass
            if _is_runner_failure(_body):
                node.mark_runner_failed(_body)
                self._zapisz_skutek(node, False, f"silnik padł: {_body.strip()[:100]}")
                logger.error(
                    "Ollama '%s': SILNIK PADL (%s) — wezel omijany przez %.0fs",
                    node.id, _body.strip()[:160], RUNNER_COOLDOWN_SEC,
                )
            else:
                logger.warning("Ollama '%s' HTTP %s — trying fallback", node.id, status)
                if status == 500 and node.zanotuj_500(_body):
                    self._zapisz_skutek(node, False, f"seria HTTP 500: {_body.strip()[:100]}")
                    logger.error("Ollama '%s': seria %d× HTTP 500 (%s) — wezel omijany przez %.0fs",
                                 node.id, SERIA_500_PROG, _body.strip()[:120], RUNNER_COOLDOWN_SEC)
            if status not in (404, 500, 502, 503):
                raise HTTPException(503, f"Ollama '{node.id}' error: {status}") from exc
        finally:
            # Zwolnij rezerwacje z _admit_with_reservation niezaleznie od wyniku
            # (sukces przez return powyzej, blad ktory leci dalej do fallbacku, albo
            # HTTPException ktory wylatuje wyzej) — inaczej pool "przecieka" i kolejne
            # requesty widza fantomowo zajety budzet w nieskonczonosc.
            await self._release_pool_budget(_pool, _reserved_mb)
        # ── fallback to the other local node ─────────────────────────────────
        for fallback in self._nodes:
            if fallback is not node and fallback.available:
                _zostalo = None if _koniec is None else _koniec - time.monotonic()
                if _zostalo is not None and _zostalo < 1.0:
                    break  # budżet wołającego wyczerpany — kolejna próba tylko by go przekroczyła
                try:
                    return await _do_request(fallback, fallback.default_model,
                                             req_timeout=_zostalo if _zostalo is not None else timeout)
                except Exception:
                    pass
        if self._cloud_available():
            return await self._cloud_chat(prompt=prompt, persona_name=persona_name)
        raise HTTPException(503, f"Cannot reach Ollama node '{node.id}' and no fallback available.")

    async def chat_messages(
        self,
        messages: list[dict],
        model: str | None = None,
        options_override: dict | None = None,
        *,
        priority: str = "normal",
        allow_cpu_fallback: bool | None = None,
        persona_preference: str = "auto",
        persona_name: str = "",
        force_cpu: bool = False,
        timeout: float | None = None,
        preferred_node: str | None = None,
        format: dict | str | None = None,
        think: bool | None = None,
    ) -> tuple[str, str, str]:
        """Chat-style (messages list). Returns (response_text, used_model, used_node_id).

        think: sterowanie rozumowaniem modeli myślących (qwen3.x, deepseek-r1, qwq).
        None = nie wysyłamy pola, ollama decyduje sama (dla tych modeli = WŁĄCZONE).
        False = odpowiadaj od razu. Claude-10 2026-08-27: router NIGDY nie wysyłał tej
        flagi, więc każda rozmowa z modelem myślącym płaciła pełne rozumowanie —
        stąd „185 s i jedenaście znaków" na avatarze (agent.py:1608). `num_predict`
        ogranicza SUMĘ myślenia i odpowiedzi, więc budżet potrafił pójść w całości
        w rozumowanie i wracała pustka. Zmierzone z think=False: „2+2 to 4" w 45 s
        RAZEM z zimnym ładowaniem 17 GB.

        format: Ollama structured output — "json" lub JSON Schema (dict).
        Constrained decoding gwarantuje czysty JSON bez ogona prozy.
        Uwaga: .172 ma ollamę 0.24 — tam gwarancje słabsze, caller
        powinien mieć parser odporny na śmieci (raw_decode).

        preferred_node: id węzła z transport-równania SmartRoutera (unifikacja dual-decider
        2026-07-01) — dla modeli multi-homed egzekucja idzie na węzeł, który WYGRAŁ decyzję,
        nie na pierwszego z listy. None = stare zachowanie (route sam wybiera).

        options_override: merged into Ollama `options` (e.g. temperature, top_p)
        — used by loop self-correction to force local retry with a jittered
        sampler before escalating to cloud.

        allow_cpu_fallback / persona_preference / persona_name: see chat().
        force_cpu: skips Tier 0/1/2 and jumps straight to Tier 3 (RAM).
        """
        # Claude-10 2026-09-30: jak w chat() — `timeout` to budżet CAŁEGO wywołania, także fallbacków.
        _koniec = (time.monotonic() + float(timeout)) if timeout else None

        # ── Identity Guard Injection ──────────────────────────────────────────
        # Operator 2026-09-17: to samo co w chat() — dla priority="background"
        # (heartbeat/refleksja notatnika) manifest NIE wchodzi. Bliźniak chat() pomijał
        # go od 05-29 (manifest zżerał focus małym modelom), ta strona pary nie —
        # refleksja przepięta na chat_messages(think=False) dostałaby go z powrotem.
        if priority == "background":
            full_messages = messages
        else:
            try:
                from app.services.brain_core import get_identity_manifest_prompt
                identity_manifest = get_identity_manifest_prompt()
                # Inject as FIRST system message
                full_messages = [{"role": "system", "content": identity_manifest}] + messages
            except Exception:
                full_messages = messages

        # ── llama-server (.172:8081) direct dispatch — Claude 2026-09-15 ────────
        if model and self.llamacpp.enabled and self.llamacpp.owns(model):
            try:
                return await self.llamacpp.chat_messages(
                    full_messages, model, options=options_override, timeout=float(timeout or 300.0))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[llamacpp] %s padł (%s) — schodzę na Ollamę: %s",
                               model, str(exc)[:120], self.llamacpp.fallback_model or "default")
                model = self.llamacpp.fallback_model or None

        _num_ctx = self._estimate_num_ctx(messages=full_messages)
        # ── Cloud model direct dispatch (bypasses Ollama entirely) ───────────
        if model and model in _CLOUD_MODELS:
            try:
                return await self._dispatch_cloud_model(model, _CLOUD_MODELS[model], full_messages)
            except Exception as exc:
                logger.warning("[cloud_dispatch] %s failed (%s), falling back to local", model, exc)
        # Same CPU fallback default as chat(): operator calls never go to CPU
        # unless priority is explicitly "background".
        _cpu_fallback_m = (
            allow_cpu_fallback if allow_cpu_fallback is not None
            else (priority == "background")
        )
        try:
            node, used_model = await self._resolve_with_vram(
                model, _num_ctx,
                allow_cpu_fallback=_cpu_fallback_m,
                persona_preference=persona_preference,
                force_cpu=force_cpu,
                priority=priority,
                preferred_node=preferred_node,
            )
        except _CloudFallbackSignal:
            return await self._cloud_chat_messages(messages=full_messages, persona_name=persona_name)

        if node.id == "gpu" and getattr(self, "_last_num_gpu", -1) == 0 and self.cpu.available:
            # tryb CPU na .10 → ollama-cpu :11435 (patrz chat()._do_request, 30.09 23:25Z)
            logger.info("[cpu-10] %s w trybie CPU -> ollama-cpu :11435 (karta .10 zostaje dla e4b)", used_model)
            node = self.cpu   # model bez canonical_model — patrz chat()._do_request (dopasowanie po rodzinie)
        if not _wolno_na_wezle(node.id, used_model):
            _zast_m = _MODELE_NA_WEZLE[node.id][0]
            logger.warning("[straz-wezla] %s na %s -> %s (decyzja operatora 30.09: .10 = tylko e4b)",
                           used_model, node.id, _zast_m)
            used_model = _zast_m

        await self._maybe_free_heavy_for(node, used_model)

        ka = self._get_keep_alive(used_model)
        _opts_m = {"num_ctx": self._stable_num_ctx(used_model, _num_ctx)}
        if getattr(self, "_last_num_gpu", -1) == 0:
            _opts_m["num_gpu"] = 0
        if options_override:
            _opts_m.update(options_override)
        if (used_model or "").strip().lower() in _MODELE_NA_WEZLE.get("gpu", ()):
            _opts_m["num_ctx"] = max(int(_opts_m.get("num_ctx") or 0), KONTEKST_E4B_10)
        _payload = {"model": used_model, "messages": full_messages, "stream": False, "options": _opts_m}
        if format is not None:
            _payload["format"] = format
        if think is not None:
            _payload["think"] = bool(think)
        try:
            client = self._get_http()
            resp = await client.post(
                f"{node.url}/api/chat",
                json=_payload,
                timeout=_bounded_timeout(timeout, client.timeout),
            )

            resp.raise_for_status()
            text = self._strip_think_tags(str(resp.json()["message"]["content"]))
            node.mark_runner_ok()  # Operator 2026-07-26: silnik dowiozl → bezpiecznik zdjety
            self._zapisz_skutek(node, True, used_model)
            await self._notify_model_used(used_model)
            return text, used_model, node.id
        except NODE_UNREACHABLE as exc:
            node._online = False
            logger.warning(
                "Ollama node '%s' unreachable (%s), trying fallback…",
                node.id, type(exc).__name__,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # Operator 2026-07-26: odroznij "model/prompt zly" od "silnik padl". Dotad KAZDE
            # 500 tylko logowalo i szlo do fallbacku, wiec wezel z martwym runnerem zostawal
            # primary i zzeral kazda kolejna synteze (z13, ~24 h ciszy w metrykach).
            _body = ""
            try:
                _body = exc.response.text or ""
            except Exception:
                pass
            if _is_runner_failure(_body):
                node.mark_runner_failed(_body)
                self._zapisz_skutek(node, False, f"silnik padł: {_body.strip()[:100]}")
                logger.error(
                    "Ollama '%s': SILNIK PADL (%s) — wezel omijany przez %.0fs",
                    node.id, _body.strip()[:160], RUNNER_COOLDOWN_SEC,
                )
            else:
                logger.warning("Ollama '%s' HTTP %s — trying fallback", node.id, status)
                if status == 500 and node.zanotuj_500(_body):
                    self._zapisz_skutek(node, False, f"seria HTTP 500: {_body.strip()[:100]}")
                    logger.error("Ollama '%s': seria %d× HTTP 500 (%s) — wezel omijany przez %.0fs",
                                 node.id, SERIA_500_PROG, _body.strip()[:120], RUNNER_COOLDOWN_SEC)
            if status not in (404, 500, 502, 503):
                raise HTTPException(503, f"Ollama '{node.id}' error: {status}") from exc
        # ── fallback to the other local node ─────────────────────────────────
        for fallback in self._nodes:
            if fallback is not node and fallback.available:
                _zostalo = None if _koniec is None else _koniec - time.monotonic()
                if _zostalo is not None and _zostalo < 1.0:
                    break  # budżet wołającego wyczerpany
                try:
                    client = self._get_http()
                    # Operator 2026-07-17: było node.url — fallback strzelał w węzeł, który właśnie padł
                    # Claude-10 2026-09-06 (przeglad, HIGH): `json=_payload` wysylal do fallbacku
                    # MODEL padnietego wezla (np. gemma4:e4b z .10 do .253) → 404 → except: pass
                    # → petla nigdy nie dowozila; a gdy fallback akurat mial ten tag, zwrot raportowal
                    # fallback.default_model, czyli INNY model niz ten, ktory naprawde odpowiedzial.
                    # Teraz: ten sam model pod tagiem fallbacku gdy go ma (tylko roznica tagu, nie
                    # rozmiaru — patrz _ten_sam_model_inny_tag), inaczej jego default; i zawsze
                    # raportujemy model, ktory FAKTYCZNIE poszedl w request.
                    _fb_can = fallback.canonical_model(used_model)
                    _fb_model = (
                        _fb_can
                        if fallback.owns(used_model) and _ten_sam_model_inny_tag(used_model, _fb_can)
                        else fallback.default_model
                    )
                    if not _wolno_na_wezle(fallback.id, _fb_model):
                        _fb_model = _MODELE_NA_WEZLE[fallback.id][0]   # 27b z padłego Z13 nie ląduje na .10
                    _fb_opts = dict(_opts_m, num_ctx=max(int((options_override or {}).get("num_ctx") or 0),
                                                         self._stable_num_ctx(_fb_model, _num_ctx)))
                    if fallback.id == "gpu":
                        _fb_opts.pop("num_gpu", None)   # zapas na .10 liczy na karcie (e4b), nie CPU w Ollamie karty
                    _fb_payload = dict(_payload, model=_fb_model, options=_fb_opts)
                    resp = await client.post(
                        f"{fallback.url}/api/chat",
                        json=_fb_payload,
                        # _bounded_timeout jak w glownym wywolaniu: goly float szedl tez na faze
                        # connect, a to najgoretsza sciezka dla ConnectTimeout (wezel wlasnie padl)
                        timeout=_bounded_timeout(_zostalo if _zostalo is not None else timeout, client.timeout),
                    )

                    resp.raise_for_status()
                    fallback.mark_runner_ok()
                    self._zapisz_skutek(fallback, True, _fb_model)
                    logger.info("[chat_messages] fallback %s→%s model=%s (zamawiano %s)",
                                node.id, fallback.id, _fb_model, used_model)
                    return self._strip_think_tags(str(resp.json()["message"]["content"])), _fb_model, fallback.id
                except Exception:
                    pass
        if self._cloud_available():
            return await self._cloud_chat_messages(messages=full_messages, persona_name=persona_name)
        raise HTTPException(503, f"Cannot reach Ollama node '{node.id}' and no fallback available.")

    async def chat_vision(
        self,
        prompt: str,
        image_b64: str,
        model: str = "llava-phi3",
    ) -> tuple[str, str, str]:
        """Send image + text to a multimodal Ollama model."""
        
        # ── BEZ manifestu tożsamości (Claude-10 2026-08-27, ZMIERZONE) ────────
        # Był tu Identity Guard: 7305 znaków opisu organizmu doklejane PRZED pytaniem
        # o obraz. Dla modelu wizyjnego to nie jest kontekst, tylko konkurencyjna treść
        # — ten sam zrzut (`handoff/screen_oauth.png`) na qwen3.8:27b:
        #   z manifestem  → 353 s i CZYSTA KONFABULACJA (terminal, `ollama serve`,
        #                   „operator Operator diagnozuje węzeł .10" — wprost z manifestu)
        #   bez manifestu →  45 s i poprawny odczyt (okno logowania Google, komunikat
        #                   o niezweryfikowanej aplikacji, pulpit Windows)
        # qwen2.5vl:7b znosił manifest bez szkody, ale też nic z niego nie zyskiwał.
        # Opis obrazu ma opisywać OBRAZ — tożsamość organizmu należy do ścieżek czatu.
        full_prompt = prompt

        # Operator 2026-08-06 ZMIERZONE (nie hipoteza): qwen2.5vl:7b na z13
        # (.172 AMD UMA) zwraca HTTP 200 z bełkotem ("jahjah..." repetition) —
        # ten sam obraz+prompt na .10 daje poprawny JSON. Garbage ≠ wyjątek,
        # więc fallback by nie zadziałał — z13 CELOWO wykluczony z vision.
        # Tekstowa kognicja dalej routuje na .172; vision = gpu/win.
        # Claude-10 2026-08-27 ZMIERZONE: powyższa reguła dotyczy MODELU `qwen2.5vl:7b`,
        # a stosowana była do całego WĘZŁA. Ten sam zrzut (`handoff/screen_oauth.png`)
        # na tym samym .172: `qwen3.8:27b` odczytał komunikat Google, pulpit Windows
        # i datę z rogu ekranu — a `llava:7b` na .10 wymyślił czerwony pasek i „dane
        # użytkownika", których na obrazku nie ma. Qwen3.8 waży 17,7 GB, więc NIE mieści
        # się na kartach 16 GB (.10/.253) — dla niego z13 to jedyny węzeł, nie preferencja.
        if (model or "").startswith("qwen3.8"):
            ordered_nodes = [self.z13]
        else:
            ordered_nodes = [self.gpu, self.win]
        for node in ordered_nodes:
            if not node.available:
                continue
            try:
                client = self._get_http()
                resp = await client.post(
                    f"{node.url}/api/generate",
                    json={
                        "model": model,
                        "prompt": full_prompt,
                        "images": [image_b64],
                        "stream": False,
                        # Opis obrazu nie potrzebuje rozumowania, a modele myślące
                        # przepalają na nie cały budżet tokenów i zwracają pustkę.
                        # Flagę wysyłamy TYLKO im — llava/moondream jej nie znają.
                        **({"think": False} if _model_mysli(model) else {}),
                    },
                    timeout=_bounded_timeout(600.0, None),  # Operator 2026-06-08: 180→600 (vision na skromnym GPU)
                )
                resp.raise_for_status()
                text = self._strip_think_tags(str(resp.json().get("response", "")))
                return text, model, node.id
            except NODE_UNREACHABLE as exc:
                node._online = False
                logger.warning(
                    "Vision: node '%s' unreachable (%s), trying next…",
                    node.id, type(exc).__name__,
                )
            except httpx.HTTPStatusError as exc:
                logger.warning("Vision: node '%s' HTTP %s", node.id, exc.response.status_code)
        raise HTTPException(503, f"Vision model '{model}' unavailable — no online node found.")

    def _get_persona_cloud_config(self, persona_name: str) -> tuple[str | None, str]:
        """Return (cloud_model, tone_guard_prompt) for a persona.

        None model → cloud fallback forbidden for this persona (e.g. family).
        Empty string or missing → use global settings.cloud_fallback_model.
        """
        if not persona_name:
            return settings.cloud_fallback_model, ""
        try:
            from app.services.persona_config import PERSONAS_BY_SECTION
            pcfg = PERSONAS_BY_SECTION.get(persona_name)
            if pcfg is None:
                return settings.cloud_fallback_model, ""
            cloud_model = getattr(pcfg, "cloud_model", "")
            tone = getattr(pcfg, "cloud_system_prompt", "") or ""
            if cloud_model is None:
                return None, tone
            if not cloud_model:
                return settings.cloud_fallback_model, tone
            return cloud_model, tone
        except Exception:
            return settings.cloud_fallback_model, ""

    async def _cloud_chat(self, prompt: str, persona_name: str = "") -> tuple[str, str, str]:
        from app.services.openai_service import openai_service
        cloud_model, tone = self._get_persona_cloud_config(persona_name)
        if cloud_model is None:
            logger.warning(
                "[cloud-guard] persona=%s has cloud fallback disabled — refusing to escalate",
                persona_name,
            )
            raise HTTPException(
                503,
                f"Local resources exhausted and cloud fallback is disabled for persona '{persona_name}'.",
            )
        logger.info(
            "Using cloud fallback (%s) for chat prompt (persona=%s).",
            cloud_model, persona_name or "default",
        )
        guarded_prompt = f"{tone}\n\n{prompt}" if tone else prompt
        text = await openai_service.chat(guarded_prompt, model=cloud_model)
        return text, cloud_model, "cloud"

    async def _cloud_chat_messages(
        self,
        messages: list[dict],
        persona_name: str = "",
    ) -> tuple[str, str, str]:
        from app.services.openai_service import openai_service
        cloud_model, tone = self._get_persona_cloud_config(persona_name)
        if cloud_model is None:
            logger.warning(
                "[cloud-guard] persona=%s has cloud fallback disabled — refusing to escalate",
                persona_name,
            )
            raise HTTPException(
                503,
                f"Local resources exhausted and cloud fallback is disabled for persona '{persona_name}'.",
            )
        logger.info(
            "Using cloud fallback (%s) for chat_messages (persona=%s).",
            cloud_model, persona_name or "default",
        )
        guarded_messages = list(messages)
        if tone:
            guarded_messages = [{"role": "system", "content": tone}] + guarded_messages
        text = await openai_service.chat_messages(guarded_messages, model=cloud_model)
        return text, cloud_model, "cloud"

    async def _dispatch_cloud_model(self, model: str, provider: str, messages: list[dict]) -> tuple[str, str, str]:
        """Route to the appropriate cloud Klient for a named cloud model."""
        import uuid
        from app.services.model_adapters import create_adapter
        pid = str(uuid.uuid4())
        Klient = create_adapter(pid, model, "assistant", provider)
        content, _tokens, _lat = await Klient.chat_raw_messages(messages)
        logger.info("[cloud_dispatch] model=%s provider=%s len=%d", model, provider, len(content))
        return content, model, provider

    async def refresh_status(self) -> None:
        """Ping all local nodes to see who is online.

        Operator/Claude-10 2026-07-01 (z13 jako REALNY wierzchołek): pingowaliśmy TYLKO
        gpu+win → z13 (.172) i cpu (.10:11435) nigdy nie miały `_online=True`, więc każda
        warstwa patrząca na `online` była na nie ślepa (deep-brain niewidzialny). Teraz
        pingujemy WSZYSTKIE włączone nody organizmu — dynamicznie z self._nodes, nie hardkod.
        """
        try:
            pings = [n.ping() for n in self._nodes if n.enabled]
            if self.llamacpp.enabled:
                pings.append(self.llamacpp.ping())
            await asyncio.gather(*pings, return_exceptions=True)
        except Exception as e:
            logger.error("Failed to refresh status of Ollama nodes: %s", e)

    def node_status(self) -> list[dict]:
        nodes = [
            {
                "id": n.id,
                "url": n.url,
                "models": n.models,
                "enabled": n.enabled,
                "online": n.online,
                "available": n.available,
                # Operator 2026-07-26: HTTP zyje, ale silnik generacji padl. Bez tego pola
                # health liczyl taki wezel jako sprawny (z13, ROCm launch failure).
                "runner_down": n.runner_down,
                "runner_error": n._runner_error if n.runner_down else "",
                "default_model": n.default_model,
            }
            for n in self._nodes
        ]
        nodes.append({
            "id": self.llamacpp.id,
            "url": self.llamacpp.url,
            "models": list(self.llamacpp.models),
            "enabled": self.llamacpp.enabled,
            "online": self.llamacpp.online,
            "available": self.llamacpp.available,
            "runner_down": self.llamacpp.runner_down,
            "runner_error": self.llamacpp._last_error if self.llamacpp.runner_down else "",
            "default_model": self.llamacpp.default_model,
            "kind": "openai",
        })
        nodes.append({
            "id": "cloud",
            "url": "https://api.openai.com",
            "models": [settings.cloud_fallback_model],
            "enabled": settings.cloud_fallback_enabled,
            "online": self._cloud_available(),
            "available": self._cloud_available(),
            "default_model": settings.cloud_fallback_model,
        })
        return nodes

    def resource_status(self) -> dict:
        """Current resource allocation state for UI/monitoring."""
        return {
            "gpu_busy": self._gpu_busy,
            "gpu_node": {"online": self.gpu.online, "enabled": self.gpu.enabled},
            "win_node": {"online": self.win.online, "enabled": self.win.enabled},
            "strategy": "4-tier cascade: GPU VRAM → WIN VRAM → GPU RAM → WIN RAM",
            "default_primary_model": settings.primary_model,
            "background_deflection": "background priority tasks → WIN when GPU busy",
        }

    # ── trójkąt decyzyjny w routingu ────────────────────────────────────────
    def _trojkat_odswiez_w_tle(self) -> None:
        """Migawka prawdy sprzętowej (node_truth ~0,1–2,5 s) — w tle, nigdy na ścieżce zapytania."""
        st = self._trojkat
        if st["w_toku"] or time.monotonic() - st["ts"] < _TROJKAT_SWIEZOSC_S:
            return

        async def _odswiez() -> None:
            try:
                from app.services.node_hardware_truth import node_truth
                t = await node_truth()
                st["nodes"] = list(t.get("nodes") or [])
                st["ts"] = time.monotonic()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[trojkat] odświeżenie prawdy padło: %s", exc)
            finally:
                st["w_toku"] = False

        try:
            asyncio.get_running_loop().create_task(_odswiez())
            st["w_toku"] = True
        except RuntimeError:
            pass

    def _wybierz_trojkatem(self, model: str, need_gb: float, *, bez: tuple = (),
                           zapisz: bool = False) -> dict | None:
        """Spośród ŻYWYCH właścicieli modelu wybierz węzeł pełnym trójkątem (żelazo × sieć × trajektoria).
        None = jeden właściciel albo brak świeżej prawdy — zostaje dotychczasowy wybór (nie zgadujemy)."""
        dozwolone = _WEZLY_MODELU.get(model)
        wl = [n for n in self._nodes if n.id != "cpu" and n.id not in bez and n.available and n.enabled
              and n.owns_exact(model) and (dozwolone is None or n.id in dozwolone)]
        if len(wl) < 2 and not bez:
            return None
        self._trojkat_odswiez_w_tle()
        st = self._trojkat
        if not wl or not st["nodes"] or time.monotonic() - st["ts"] > _TROJKAT_WAZNOSC_S:
            return None
        po_kluczu = {_WEZEL_ROUTERA_NA_KLUCZ[n.id]: n for n in wl}
        # Router WIE, że ci właściciele mają runtime (odpowiadają na /api/tags) — tabela NODES bywa
        # starsza od organizmu (.130 wpisany jako „inference: False”, a trzyma gemma4:12b).
        prawda = [dict(x, inference=True) for x in st["nodes"] if x.get("node") in po_kluczu]
        from app.services.node_hardware_truth import pick_node
        w = pick_node(prawda, need_gb=need_gb, prefer_model=model, zapisz_wybor=zapisz)
        cel = po_kluczu.get(str(w.get("decision") or ""))
        return {"node": cel, "reason": str(w.get("reason") or ""), "vertex": w.get("vertex")} if cel else None

    def _zapisz_skutek(self, node: "OllamaNode", ok: bool, note: str, model: str | None = None) -> None:
        """Człon PRZESZŁOŚCI uczy się z realnej pracy: porażka zawsze, sukces najwyżej raz na 5 min
        na węzeł (inaczej tysiące 'ok' z heartbeatów wypchnęłyby z okna prawdziwe porażki).
        need_gb (05.11): szacunek pamięci modelu węzła (cached_est_mem_mb / 1024) — bez liczby
        _traj_wpisy pomija porażkę i człon przeszłości jej nie widzi; nieznany szacunek = None."""
        klucz = _WEZEL_ROUTERA_NA_KLUCZ.get(node.id)
        if not klucz:
            return
        teraz = time.monotonic()
        if ok:
            if teraz - self._skutek_ok_ts.get(klucz, 0.0) < _SKUTEK_OK_CO_S:
                return
            self._skutek_ok_ts[klucz] = teraz
        _m = model or (note if ok else None) or getattr(node, "default_model", None)
        need_gb = None
        if _m:
            try:
                from app.services.model_tiering import cached_est_mem_mb
                _mb = cached_est_mem_mb(_m)
                if _mb:
                    need_gb = round(float(_mb) / 1024.0, 3)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[trojkat] szacunek pamięci %s padł: %s", _m, exc)
        try:
            from app.services.node_hardware_truth import record_outcome
            _wynik = "ok" if ok else "fail"
            _opis = f"llm {node.id}: {note}"[:300]
            try:
                record_outcome(klucz, _wynik, _opis, need_gb=need_gb)
            except TypeError:
                record_outcome(klucz, _wynik, _opis)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[trojkat] zapis skutku padł: %s", exc)

    async def _przenies_rozgrzanie(self, node: "OllamaNode") -> None:
        """Operator 30.09: „tu wchodzi gra — rozgrzewa się gdzie indziej”. Węzeł właśnie oddaje GPU człowiekowi:
        każdy model czatu, który tam był ciepły, trójkąt przenosi zawczasu na najtańszego innego właściciela."""
        try:
            async with httpx.AsyncClient(timeout=3.0, headers=auth_headers()) as c:
                r = await c.get(f"{node.url}/api/ps")
                cieple = [m.get("name") for m in (r.json() or {}).get("models", []) if m.get("name")]
        except Exception as exc:  # noqa: BLE001
            logger.info("[trojkat] gra na %s: nie odczytałem ciepłych modeli (%s)", node.id, exc)
            return
        self._trojkat["ts"] = 0.0          # prawda sprzed gry jest już nieaktualna
        self._trojkat_odswiez_w_tle()
        for _ in range(20):                 # poczekaj na świeżą migawkę (do ~5 s)
            if self._trojkat["ts"]:
                break
            await asyncio.sleep(0.25)
        for model in cieple:
            if "embed" in model.lower():
                continue
            try:
                from app.services.model_tiering import cached_est_mem_mb
                need = float(cached_est_mem_mb(model) or self._MODEL_VRAM_MB.get(model, 6000)) / 1024.0
            except Exception:
                need = float(self._MODEL_VRAM_MB.get(model, 6000)) / 1024.0
            w = self._wybierz_trojkatem(model, need, bez=(node.id,), zapisz=True)
            zast = next(((wz, zm) for (m, wz), zm in _ZASTEPSTWO_NA_WEZLE.items() if m == model), None)
            if not w and zast:
                # Nikt nie udźwignie tego modelu — operator wskazał zastępstwo (12b → e4b na .10).
                zn = {"gpu": self.gpu, "guest": self.guest, "win": self.win, "z13": self.z13}.get(zast[0])
                w = {"node": zn, "reason": f"zastępstwo operatora: {model} -> {zast[1]}"} if zn and zn.available else None
            if not w:
                logger.info("[trojkat] gra na %s: %s nie ma dokąd pójść (brak zdolnego właściciela)", node.id, model)
                continue
            cel, cel_model = w["node"], w["node"].canonical_model(model)
            cel_model = _ZASTEPSTWO_NA_WEZLE.get((model, cel.id), cel_model)
            logger.info("[trojkat] gra na %s: %s rozgrzewa się na %s/%s (%s)", node.id, model, cel.id,
                        cel_model, w["reason"][:120])
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0), headers=auth_headers()) as c:
                    await c.post(f"{cel.url}/api/generate", json={"model": cel_model, "prompt": "", "keep_alive": "30m"})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[trojkat] rozgrzanie %s na %s padło: %s", cel_model, cel.id, exc)

    def _gdy_gra(self, node: "OllamaNode", enabled: bool) -> None:
        if enabled or not node.enabled:
            return                          # tylko przejście włączony → wyłączony (wchodzi gra)
        try:
            asyncio.get_running_loop().create_task(self._przenies_rozgrzanie(node))
        except RuntimeError:
            pass

    def set_win_enabled(self, enabled: bool) -> None:
        self._gdy_gra(self.win, enabled)
        self.win.enabled = enabled

    def set_guest_enabled(self, enabled: bool) -> None:
        """Bramka dla .130 — symetryczna do set_win_enabled.

        Operator 2026-09-06: SHADOWGUEST to stanowisko dla GOSCIA, przy ktorym ktos
        siada, gdy gracie razem. Poza tym to normalny wezel organizmu. Gdy gosc gra,
        GPU jest zajete, a sonda guest_vram tego NIE zobaczy — /api/ps pokazuje tylko
        pamiec wzieta przez ollame, nie przez gre. Stad jawna bramka.
        """
        self._gdy_gra(self.guest, enabled)
        self.guest.enabled = enabled

    async def clear_gpu(self) -> None:
        """Unload all models from local nodes to free VRAM."""
        for node in [self.gpu, self.guest, self.win]:
            if not node.online:
                continue
            for model in node.models:
                try:
                    async with httpx.AsyncClient(timeout=10.0, headers=auth_headers()) as client:
                        # sending keep_alive: 0 unloads the model from memory
                        await client.post(
                            f"{node.url}/api/generate",
                            json={"model": model, "keep_alive": 0}
                        )
                except Exception as exc:
                    logger.error(f"Failed to unload model {model} from {node.id}: {exc}")


class _CloudFallbackSignal(Exception):
    """Internal signal: skip local Ollama, go straight to cloud."""


ollama_router = OllamaRouter()
