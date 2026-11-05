"""Czujki organizmu: sondy, pamięć usterek, alarm (tor 1, 2026-09-18).

DLACZEGO (2026-09-18, zlecenie „Rada mechów”): jednego wieczoru padły trzy rzeczy i Shadow
żadnej nie zauważył, a zapytany „ogarniasz wszystko?” oddał akapit autoprezentacji:
 (a) llama-server z Bielikiem v3 na .172:8081 nie wstał po restarcie maszyny — router ma ten
     węzeł (llamacpp_node), ale audyt organizmu go nie sondował, więc faults było [];
 (b) ręka Suno padła „nie znalazłem menu utworu” — porażka była jedną linią
     „SHADOW_UTWOR BLAD: ...” w pliku, którego nikt nie czyta;
 (c) (poza tym modułem) render poszedł na zajęte GPU.
Ten moduł domyka pętlę: WYKRYJ (sondy) → POWIEDZ (alarm) → ZAPROPONUJ (action_spec dla
lan_actuator) → SPRAWDŹ, czy naprawa pomogła (pamięć usterek + status propozycji w bazie).

ZASADY:
 - Sondy są CZYSTE: tylko odczyt, żadnego zapisu, żadnej mutacji routera. Plik pamięci pisze
   wyłącznie tick (_organism_self_heal_tick), bo GET /api/network/organism-audit też woła audyt
   i inaczej zjadałby zdarzenia „nowa”/„ustąpiła” sprzed nosa alarmu.
 - FAIL-LOUD: brak pomiaru to ok=None + usterka kind="probe_unknown" z powodem. Nigdy ok=True
   przy braku danych i nigdy „ustąpiła”, gdy sonda po prostu nie zmierzyła.
 - Wszystko z limitem czasu. Limit przez asyncio.wait + cancel, NIE asyncio.wait_for —
   scheduler.py (komentarz przy _run) opisuje, jak wait_for wisi na to_thread.
 - Import bez efektów ubocznych: router, settings i httpx ładują się dopiero w sondach.

Samodzielny pomiar:  backend/.venv/bin/python -m app.services.organism_probes
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger("shadowai.organism_probes")

# ── STAŁE ────────────────────────────────────────────────────────────────────
BACKEND_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_DIR / "data"
PLIK_PAMIECI = DATA_DIR / "organism_faults.json"
PLIK_KANARKA_SUNO = DATA_DIR / "kanarki" / "suno.json"
PLIK_BAZY = BACKEND_DIR / "shadowai.db"

OKNO_PORAZEK_S = 6 * 3600
LIMIT_PORAZEK = 10
KANAREK_MAX_WIEK_S = 48 * 3600
ALARM_DEDUP_S = 6 * 3600
PROPOZYCJA_PONOW_S = 24 * 3600
# Tyle po APPLIED czekamy, zanim orzekniemy „nie pomogło” — llama-server ładuje model ~1-2 min,
# a tick chodzi co 30 min; 10 min to zapas, nie pomiar.
REMEDIUM_KARENCJA_S = 600
GC_USTAPIONYCH_S = 7 * 86400

T_HTTP = 4.0
T_TCP = 2.0
T_SUBPROC = 10.0
T_SONDA = 12.0
T_CALOSC = 25.0
T_WYSYLKA = 20.0

# Skan plików: budżety, żeby audyt nie czytał gigabajtów (2026-09-18: w logs/ leży 158 plików).
ROZSZERZENIA_LOGOW = (".log", ".txt", ".out")
PLIK_CALY_MAX = 8 * 1024 * 1024
PLIK_OGON = 1024 * 1024
MAX_PLIKOW = 200
BUDZET_BAJTOW = 64 * 1024 * 1024
BUDZET_SKANU_S = 8.0
# logs/ to SYMLINK do /srv/data/shadowai-runtime/audit — stąd followlinks=True niżej.
KORZEN_LOGOW = Path("/opt/shadow/shadow/logs")
GLOB_TMP = "/srv/data/tmp/*/*.log"
# NIE skanujemy backend/logs/shadowai.log ani ~/logs/*.log — to rewir log_sentinel.py
# (własne offsety i attention_queue); dwie czujki na jednym pliku = dwa alarmy o tym samym.

JEDNOSTKI_WAZNE = ("shadowai-backend", "shadowai-discord", "shadow-suno-vnc", "shadow-xtts",
                   "shadow-gameplay-jas-receiver")

# Decyzja 2026-09-18 (do odwrócenia jedną stałą): cała maszyna operatora/rodziny wyłączona
# (granie, noc, laptop Dziecko w plecaku) to zwykle decyzja człowieka, nie awaria. Trafia do
# pamięci i do audytu, ale NIE budzi Discorda — inaczej każdy wieczór to NOWA + USTĄPIŁA
# i operator nauczy się kanał ignorować (log_sentinel już to przerabiał: „NAWRÓT 58×”).
ALARMUJ_WYLACZONE_MASZYNY_OPERATORA = False

STATUSY_AKTYWNE = ("nowa", "trwa", "remedium_nie_pomoglo")
STATUSY_ZAKONCZONE = ("ustapila", "remedium_zastosowane_i_pomoglo")

_WAGA = {"high": 0, "medium": 1, "low": 2}


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _z_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── KSZTAŁTY ─────────────────────────────────────────────────────────────────
def _fault(*, key: str, probe: str, czesc: str, kind: str, severity: str, evidence: str,
           service_key: Optional[str] = None, target_node: Optional[str] = None,
           suggested_action: Optional[str] = None, action_spec: Optional[dict] = None,
           target_ip: Optional[str] = None, owner: str = "shadow", zdarzenie: bool = False,
           file: Optional[str] = None, line: Optional[int] = None, ts_us: Optional[int] = None,
           size: Optional[int] = None, cicha: bool = False) -> dict[str, Any]:
    """Usterka = nadzbiór faults z organism_audit.

    kind + service_key są OBOWIĄZKOWE i różne dla każdej usterki: organism_audit._fault_key liczy
    klucz dedupu propozycji z kind|target_node|service_key|suggested_action. `key` jest dla pamięci.
    """
    return {
        "key": key, "ok": False, "probe": probe, "czesc": czesc, "kind": kind,
        "severity": severity, "target_node": target_node, "service_key": service_key or key,
        "evidence": str(evidence)[:300], "suggested_action": suggested_action,
        "action_spec": dict(action_spec or {}), "target_ip": target_ip, "owner": owner,
        "rag_hits": [], "zdarzenie": zdarzenie, "file": file, "line": line, "ts_us": ts_us,
        "size": size, "cicha": cicha,
    }


def _nie_wiem(probe: str, czesc: str, powod: str) -> dict[str, Any]:
    # probe="meta": usterka „nie wiem” sama nigdy nie jest „niewiadoma” — znika, gdy sonda
    # znów mierzy, i wtedy uczciwie przechodzi w „ustąpiła”.
    return _fault(key=f"probe_unknown:{probe}:{czesc}", probe="meta", czesc=czesc,
                  kind="probe_unknown", severity="medium",
                  evidence=f"sonda {probe}/{czesc}: nie wiem — {powod}")


def _wynik(probe: str, t0: float, checked: int, faults: list[dict], notes: list[str],
           niewiadome: list[str]) -> dict[str, Any]:
    prawdziwe = [f for f in faults if f.get("kind") != "probe_unknown"]
    ok: Optional[bool] = False if prawdziwe else (None if niewiadome else True)
    return {"probe": probe, "ok": ok, "checked": checked,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "faults": faults, "notes": notes, "niewiadome": niewiadome}


def klucz_usterki(f: dict[str, Any]) -> str:
    """Klucz pamięci. Stare faults z audytu (organ_offline, service_offline, CTL) nie mają `key`."""
    return str(f.get("key") or "|".join([str(f.get("kind") or ""), str(f.get("target_node") or ""),
                                         str(f.get("service_key") or "")]))


# ── NARZĘDZIA Z LIMITEM CZASU ────────────────────────────────────────────────
async def _z_limitem(coro: Awaitable[Any], limit_s: float) -> tuple[bool, Any]:
    """(True, wynik) albo (False, powód). Wzorzec scheduler._run: ensure_future + wait + cancel."""
    t = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({t}, timeout=limit_s)
    if not done:
        t.cancel()
        return False, f"limit {limit_s:g} s"
    try:
        return True, t.result()
    except asyncio.CancelledError:
        return False, "anulowane"
    except Exception as exc:  # noqa: BLE001 — sonda nie może wywrócić audytu
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


async def _subproc(cmd: list[str], env: Optional[dict] = None, limit_s: float = T_SUBPROC) -> tuple[int, str, str]:
    """rc, stdout, stderr. rc=-9 = ubity po limicie, rc=-1 = nie dało się odpalić."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"{type(exc).__name__}: {exc}"
    t = asyncio.ensure_future(proc.communicate())
    done, _ = await asyncio.wait({t}, timeout=limit_s)
    if not done:
        try:
            proc.kill()  # nasz własny proces potomny (systemctl/journalctl), nie usługa
        except ProcessLookupError:
            pass
        t.cancel()
        return -9, "", f"limit {limit_s:g} s"
    out, err = t.result()
    return int(proc.returncode or 0), out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _env_user() -> dict[str, str]:
    # 2026-09-18: proces backendu NIE ma XDG_RUNTIME_DIR (sprawdzone w /proc/PID/environ);
    # bez niego `systemctl --user` oddaje „Failed to connect to bus”.
    return {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


async def _tcp_otwarty(host: str, port: int, limit_s: float = T_TCP) -> bool:
    ok, res = await _z_limitem(asyncio.open_connection(host, port), limit_s)
    if not ok:
        return False
    _, w = res
    w.close()
    return True


# ── SONDA 1: węzły LLM ───────────────────────────────────────────────────────
_LLAMA_TASK = "ShadowLlamaServer172"
_LLAMA_SKRYPT = (
    f"Enable-ScheduledTask -TaskName '{_LLAMA_TASK}' | Out-Null; "
    f"Start-ScheduledTask -TaskName '{_LLAMA_TASK}'; Start-Sleep -Seconds 5; "
    f"(Get-ScheduledTask -TaskName '{_LLAMA_TASK}').State"
)
# rollback_plan jest KONIECZNY: validate_lan_action odrzuca STATE_CHANGE_SHARED bez niego
# (tak 06.09 padł guest_exec audytu: „rollback_plan required”). winrm_exec nie ma
# inherent_rollback, więc auto-exec S16 go nie ruszy — zostaje PROPOZYCJĄ dla operatora.
_LLAMA_ROLLBACK = (
    f"Stop-ScheduledTask -TaskName '{_LLAMA_TASK}' — zadanie tylko startuje llama-server; "
    "zatrzymanie wraca do stanu sprzed, a router i tak ma fallback_model na Ollamie"
)
_SEVERITY_WEZLA = {"gpu": "high", "cpu": "high", "z13": "high", "z13-llamacpp": "high",
                   "guest": "medium", "win": "medium"}


def _wezel_i_wlasciciel(host: str) -> tuple[Optional[str], str]:
    if host in ("127.0.0.1", "localhost", "::1"):
        host = "10.0.0.10"
    try:
        from app.services.node_hardware_truth import NODES
    except Exception:  # noqa: BLE001
        return None, "shadow"
    for klucz, meta in NODES.items():
        if meta.get("ip") == host:
            return str(klucz), str(meta.get("owner") or "shadow")
    return None, "shadow"


async def sonda_wezly_llm(pomin_adresy: Optional[set] = None) -> dict[str, Any]:
    """Każdy WŁĄCZONY w konfiguracji endpoint LLM ma odpowiadać. „Włączony, a milczy” = usterka.

    NIE wołamy node.ping(): mutuje stan routera i listę modeli, a ma 8 s limitu. Własny GET.
    """
    t0 = time.monotonic()
    probe = "wezly_llm"
    notes: list[str] = []
    faults: list[dict] = []
    niewiadome: list[str] = []
    try:
        import httpx
        from app.config import settings
        from app.services.node_client import auth_headers
        from app.services.ollama_router import ollama_router
    except Exception as exc:  # noqa: BLE001
        powod = f"import routera padł: {type(exc).__name__}: {str(exc)[:120]}"
        return _wynik(probe, t0, 0, [_nie_wiem(probe, "konfiguracja", powod)], [powod], [probe])

    cele: list[tuple[Any, str, str]] = []  # (node, url_sondy, rodzaj)
    for n in list(getattr(ollama_router, "_nodes", []) or []):
        if not getattr(n, "enabled", False):
            notes.append(f"{n.id}: wyłączony w konfiguracji/przez gaming-gate — nie sonduję")
            continue
        cele.append((n, f"{str(n.url).rstrip('/')}/api/tags", "ollama"))
    lc = getattr(ollama_router, "llamacpp", None)
    if lc is not None:
        if getattr(lc, "enabled", False):
            cele.append((lc, f"{str(lc.url).rstrip('/')}/v1/models", "llamacpp"))
        else:
            notes.append(f"{lc.id}: wyłączony w konfiguracji — nie sonduję")
    if not cele:
        powod = "router nie ma żadnego włączonego węzła LLM"
        return _wynik(probe, t0, 0, [_nie_wiem(probe, "konfiguracja", powod)], notes + [powod], [probe])

    # Router trzyma Ollamę GPU jako 127.0.0.1, runtime_topology jako 10.0.0.10 — ten sam
    # proces; bez normalizacji jedna awaria dawałaby dwa faults (2026-09-18).
    def _nh(h: Any) -> str:
        return "10.0.0.10" if str(h) in ("127.0.0.1", "localhost", "::1") else str(h)
    pomin = {(_nh(h), int(pt)) for h, pt in (pomin_adresy or set()) if h and pt}
    naglowki = auth_headers()

    async def _get(klient, url: str) -> tuple[Optional[int], str]:
        try:
            r = await klient.get(url, headers=naglowki)
            return r.status_code, ""
        except Exception as exc:  # noqa: BLE001
            return None, type(exc).__name__

    sondowane: list[tuple[Any, str, str, str, int]] = []
    for n, url, rodzaj in cele:
        p = urlparse(url)
        host, port = p.hostname or "", int(p.port or 80)
        if (_nh(host), port) in pomin:
            # runtime_topology JUŻ zgłosiło tę usługę jako offline — jedna awaria, jeden fault.
            notes.append(f"{n.id}: {host}:{port} zgłoszone już przez runtime_topology — pomijam")
            continue
        sondowane.append((n, url, rodzaj, host, port))

    async with httpx.AsyncClient(timeout=T_HTTP) as klient:
        wyniki = await asyncio.gather(*[_get(klient, url) for _, url, _, _, _ in sondowane])

    for (n, url, rodzaj, host, port), (status, blad) in zip(sondowane, wyniki):
        wezel, wlasciciel = _wezel_i_wlasciciel(host)
        if status == 200:
            if getattr(n, "runner_down", False):
                faults.append(_fault(
                    key=f"llm:{n.id}:runner", probe=probe, czesc="ping", kind="llm_runner_down",
                    severity="medium", service_key=f"llm_{n.id}_runner", target_node=wezel,
                    target_ip=None, owner=wlasciciel,
                    evidence=(f"{n.id} {n.url} odpowiada HTTP 200, ale router trzyma runner_down: "
                              f"{str(getattr(n, '_runner_error', '') or getattr(n, '_last_error', ''))[:160]}")))
            continue
        powod = blad or f"HTTP {status}"
        if rodzaj == "llamacpp":
            from app.services.llamacpp_node import w_uzyciu
            if not await asyncio.to_thread(w_uzyciu):
                notes.append(f"{n.id}: działa na żądanie")
                continue
        f = _fault(
            key=f"llm:{n.id}", probe=probe, czesc="ping", kind="llm_endpoint_offline",
            severity=_SEVERITY_WEZLA.get(str(n.id), "medium"), service_key=f"llm_{n.id}",
            target_node=wezel, owner=wlasciciel,
            evidence=f"{n.id} {n.url} włączony w konfiguracji, a nie odpowiada: {powod}")
        if rodzaj == "llamacpp":
            if await _tcp_otwarty(host, int(settings.winrm_port)):
                f["suggested_action"] = "winrm_exec"
                f["target_ip"] = host
                f["action_spec"] = {"script": _LLAMA_SKRYPT, "rollback_plan": _LLAMA_ROLLBACK}
            else:
                f["evidence"] = (f["evidence"] + " | maszyna nie odpowiada na WinRM — pewnie wyłączona")[:300]
                f["cicha"] = wlasciciel != "shadow"
        faults.append(f)
    return _wynik(probe, t0, len(sondowane), faults, notes, niewiadome)


# ── SONDA 2: porażki zadań ───────────────────────────────────────────────────
_WZORCE = [
    ("fail_loud", re.compile(r"FAIL-LOUD")),
    ("shadow_blad", re.compile(r"^SHADOW_[A-Z0-9_]+ BLAD")),
    ("shadow_rc", re.compile(r"^SHADOW_\w+_RC=(?!0\b)\d+")),
    ("traceback", re.compile(r"Traceback \(most recent call last\)")),
]
_ZALECZENIE = re.compile(r"^SHADOW_\w+ DONE|_RC=0\b")
_CZAS_Z_PRZODU = re.compile(
    r"^\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d[.,\d]*(?:Z|[+-]\d\d:?\d\d)?\]?\s*|^\[?\d\d:\d\d:\d\d[.,\d]*\]?\s*")
# 2026-09-18: Traceback z backendu (starlette + ExceptionGroup, łańcuch „direct cause”) ma ~200 linii,
# zanim padnie właściwe „sqlite3.OperationalError: database is locked”; 60 linii nie wystarczało.
TB_ZASIEG = 250
_TB_POMIN = (" ", "\t", "|", "Traceback", "During handling", "The above exception")


def _bez_czasu(linia: str) -> str:
    return _CZAS_Z_PRZODU.sub("", linia, count=1)


def _norma(tekst: str) -> str:
    t = _bez_czasu(tekst)
    t = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}\b|\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{12,}\b", "H", t)
    t = re.sub(r"\d{3,}", "N", t)
    return t.lower().strip()[:160]


def _dopasuj(linie: list[str], i: int) -> Optional[tuple[str, str]]:
    """(nazwa wzorca, tekst dowodu) albo None. Dla Traceback dowodem jest OSTATNIA linia wyjątku."""
    goly = _bez_czasu(linie[i])
    for nazwa, rx in _WZORCE:
        if not rx.search(goly):
            continue
        if nazwa != "traceback":
            return nazwa, goly.strip()
        for j in range(i + 1, min(i + 1 + TB_ZASIEG, len(linie))):
            kand = _bez_czasu(linie[j])
            if kand.strip() and not kand.startswith(_TB_POMIN):
                return nazwa, kand.strip()
        return nazwa, f"Traceback bez linii wyjątku w następnych {TB_ZASIEG} liniach"
    return None


def _lista_plikow(teraz_ts: float) -> tuple[list[Path], list[str]]:
    notes: list[str] = []
    kand: list[tuple[float, Path]] = []
    widziane: set[str] = set()

    def _dodaj(p: Path) -> None:
        try:
            st = p.stat()
        except OSError:
            return
        if not p.is_file() or p.suffix.lower() not in ROZSZERZENIA_LOGOW:
            return
        if teraz_ts - st.st_mtime > OKNO_PORAZEK_S:
            return
        rp = str(p.resolve())
        if rp not in widziane:
            widziane.add(rp)
            kand.append((st.st_mtime, p))

    if KORZEN_LOGOW.exists():
        for katalog, _, pliki in os.walk(KORZEN_LOGOW, followlinks=True):
            for nazwa in pliki:
                _dodaj(Path(katalog) / nazwa)
    else:
        notes.append(f"korzeń {KORZEN_LOGOW} nie istnieje")
    for s in glob.glob(GLOB_TMP):
        _dodaj(Path(s))
    kand.sort(key=lambda x: -x[0])
    if len(kand) > MAX_PLIKOW:
        notes.append(f"plików w oknie {len(kand)}, czytam {MAX_PLIKOW} najświeższych")
    return [p for _, p in kand[:MAX_PLIKOW]], notes


def _skanuj_pliki(teraz_ts: float) -> dict[str, Any]:
    """Synchroniczne (idzie przez to_thread). Zwraca {faults, notes, checked, przerwany}."""
    t0 = time.monotonic()
    pliki, notes = _lista_plikow(teraz_ts)
    znalezione: dict[str, dict] = {}
    przeczytane = 0
    bajtow = 0
    przerwany = ""
    for p in pliki:
        if time.monotonic() - t0 > BUDZET_SKANU_S:
            przerwany = f"budżet {BUDZET_SKANU_S:g} s wyczerpany po {przeczytane}/{len(pliki)} plikach"
            break
        if bajtow > BUDZET_BAJTOW:
            przerwany = f"budżet {BUDZET_BAJTOW >> 20} MB wyczerpany po {przeczytane}/{len(pliki)} plikach"
            break
        try:
            rozmiar = p.stat().st_size
            mtime = p.stat().st_mtime
            przesuniecie = 0
            with open(p, "rb") as fh:
                if rozmiar > PLIK_CALY_MAX:
                    # Ogon 1 MB; numer linii doliczamy zliczając \n w pominiętej części,
                    # żeby max_line w pamięci znaczył to samo przy każdym skanie.
                    do_pominiecia = rozmiar - PLIK_OGON
                    while do_pominiecia > 0:
                        kawalek = fh.read(min(4 * 1024 * 1024, do_pominiecia))
                        if not kawalek:
                            break
                        przesuniecie += kawalek.count(b"\n")
                        do_pominiecia -= len(kawalek)
                    dane = fh.read()
                    bajtow += PLIK_OGON
                else:
                    dane = fh.read()
                    bajtow += len(dane)
        except OSError as exc:
            notes.append(f"{p}: nie przeczytałem ({type(exc).__name__})")
            continue
        przeczytane += 1
        linie = dane.decode("utf-8", "replace").splitlines()
        if rozmiar > PLIK_CALY_MAX and linie:
            linie = linie[1:]  # pierwsza linia ogona jest urwana w połowie
            przesuniecie += 1
        trafienia: list[tuple[int, str, str]] = []
        for i in range(len(linie)):
            m = _dopasuj(linie, i)
            if m:
                trafienia.append((i, m[0], m[1]))
        if not trafienia:
            continue
        zaleczenia = [i for i, ln in enumerate(linie) if _ZALECZENIE.search(_bez_czasu(ln))]
        ostatnie_zal = zaleczenia[-1] if zaleczenia else -1
        for i, nazwa, tekst in trafienia:
            norma = _norma(tekst)
            key = "log:" + hashlib.sha1(f"{p}|{norma}".encode()).hexdigest()[:12]
            severity = "medium" if nazwa == "traceback" else "high"
            dopisek = ""
            if ostatnie_zal > i:
                # Dalej w TYM SAMYM pliku stoi DONE/RC=0 — drugi przebieg się udał
                # (tak wygląda /srv/data/tmp/rada_mechow/suno.log z 18.09).
                severity = "low"
                dopisek = " (później w pliku DONE)"
            nr = przesuniecie + i + 1
            znalezione[key] = _fault(  # późniejsze wystąpienie nadpisuje wcześniejsze
                key=key, probe="porazki_zadan", czesc="pliki", kind="log_failure",
                severity=severity, service_key=key, target_node="10", owner="shadow",
                zdarzenie=True, file=str(p), line=nr, size=rozmiar,
                evidence=f"{p}:{nr}: {tekst[:200]}{dopisek}")
            znalezione[key]["_mtime"] = mtime
    if przerwany:
        notes.append(f"skan plików przerwany: {przerwany}")
    return {"faults": list(znalezione.values()), "notes": notes, "checked": przeczytane,
            "przerwany": przerwany}


async def _jednostki_failed(scope: str) -> tuple[Optional[list[str]], str]:
    if scope == "system":
        cmd = ["systemctl", "list-units", "--all", "--plain", "--no-legend", "--state=failed",
               "shadow*"]
        env = None
    else:
        cmd = ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
               "--state=failed"]
        env = _env_user()
    rc, out, err = await _subproc(cmd, env)
    if rc != 0 or "Failed to connect to bus" in err:
        return None, f"systemctl ({scope}) rc={rc}: {(err or out).strip()[:140]}"
    return [ln.split()[0] for ln in out.splitlines() if ln.split()], ""


async def _journal(scope: str) -> tuple[Optional[list[dict]], str]:
    # 2026-09-30: 16 tys. wpisów w 6 h (głównie logi dostępu uvicorna) — w ticku 19:39Z journal systemowy nie
    # zmieścił się w T_SUBPROC i sonda została „nie wiem”. Tylko potrzebne pola (18,8 → 7,9 MB) i parsowanie
    # w wątku: ~1,3 s json.loads szło wprost na pętli zdarzeń backendu i blokowało wszystkie zapytania.
    cmd = ["journalctl"] + (["--user"] if scope == "user" else []) + [
        "--since", "-6h", "--no-pager", "-q", "-o", "json",
        "--output-fields=MESSAGE,_SYSTEMD_UNIT,_SYSTEMD_USER_UNIT,UNIT"]
    cmd += ["-u", "shadow*"] if scope == "user" else ["-u", "shadow-*", "-u", "shadowai-*"]
    rc, out, err = await _subproc(cmd, _env_user() if scope == "user" else None)
    if rc != 0:
        return None, f"journalctl ({scope}) rc={rc}: {err.strip()[:140]}"
    return await asyncio.to_thread(_parsuj_journal, out), ""


def _parsuj_journal(out: str) -> list[dict]:
    wpisy: list[dict] = []
    for ln in out.splitlines():
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        msg = d.get("MESSAGE")
        if isinstance(msg, list):  # journal oddaje bajty jako listę liczb, gdy treść nie jest UTF-8
            try:
                msg = bytes(msg).decode("utf-8", "replace")
            except (ValueError, TypeError):
                msg = str(msg)
        unit = d.get("_SYSTEMD_UNIT") or d.get("_SYSTEMD_USER_UNIT") or d.get("UNIT") or "?"
        try:
            ts = int(d.get("__REALTIME_TIMESTAMP") or 0)
        except (TypeError, ValueError):
            ts = 0
        wpisy.append({"unit": str(unit), "ts": ts, "msg": str(msg or "")})
    return wpisy


def _porazki_z_journala(wpisy: list[dict], scope: str) -> list[dict]:
    znalezione: dict[str, dict] = {}
    per_unit: dict[str, list[dict]] = {}
    for w in wpisy:
        per_unit.setdefault(w["unit"], []).append(w)
    for unit, lista in per_unit.items():
        # Jeden wpis journala bywa wieloliniowy (cały Traceback w jednym MESSAGE).
        linie: list[str] = []
        czasy: list[int] = []
        for w in lista:
            for kawalek in (w["msg"].splitlines() or [""]):
                linie.append(kawalek)
                czasy.append(w["ts"])
        for i in range(len(linie)):
            m = _dopasuj(linie, i)
            if not m:
                continue
            nazwa, tekst = m
            norma = _norma(tekst)
            key = "log:" + hashlib.sha1(f"journal:{scope}:{unit}|{norma}".encode()).hexdigest()[:12]
            if nazwa == "traceback":
                # ~20 Tracebacków z backendu na 6 h to szum tła (zmierzone 18.09); high zalałby kanał.
                severity = "low" if unit.startswith("shadowai-backend") else "medium"
            else:
                severity = "high"
            kiedy = datetime.fromtimestamp(czasy[i] / 1e6, timezone.utc).strftime("%H:%M:%SZ") if czasy[i] else "?"
            znalezione[key] = _fault(
                key=key, probe="porazki_zadan", czesc=f"journal_{scope}", kind="log_failure",
                severity=severity, service_key=key, target_node="10", owner="shadow",
                zdarzenie=True, file=f"journal:{scope}:{unit}", line=None, ts_us=czasy[i] or None,
                evidence=f"journal {unit} {kiedy}: {tekst[:200]}")
            znalezione[key]["_mtime"] = czasy[i] / 1e6
    return list(znalezione.values())


async def sonda_porazki_zadan() -> dict[str, Any]:
    """Padnięte jednostki systemd + świeże linie porażek w plikach rąk i w journalu (okno 6 h)."""
    t0 = time.monotonic()
    probe = "porazki_zadan"
    notes: list[str] = []
    faults: list[dict] = []
    niewiadome: list[str] = []
    checked = 0

    (sys_u, sys_err), (usr_u, usr_err), (j_sys, j_sys_err), (j_usr, j_usr_err) = await asyncio.gather(
        _jednostki_failed("system"), _jednostki_failed("user"), _journal("system"), _journal("user"))
    ok_skan, skan = await _z_limitem(asyncio.to_thread(_skanuj_pliki, time.time()), BUDZET_SKANU_S + 2.0)

    # (a) jednostki
    for scope, lista, blad in (("system", sys_u, sys_err), ("user", usr_u, usr_err)):
        czesc = f"jednostki_{scope}"
        if lista is None:
            notes.append(blad)
            niewiadome.append(f"{probe}:{czesc}")
            faults.append(_nie_wiem(probe, czesc, blad))
            continue
        checked += 1
        for nazwa in lista:
            goly = nazwa.rsplit(".", 1)[0]
            f = _fault(
                key=f"unit:{scope}:{nazwa}", probe=probe, czesc=czesc, kind="unit_failed",
                severity="high" if goly in JEDNOSTKI_WAZNE else "medium",
                service_key=f"unit_{scope}_{goly}", target_node="10", owner="shadow",
                evidence=f"jednostka systemd ({scope}) {nazwa} w stanie failed")
            # Remedium tylko tam, gdzie ręka NAPRAWDĘ sięga: lan_actuator robi `sudo systemctl
            # restart` i wpuszcza wyłącznie shadowai-* — czyli systemowe, nie --user. Backendu nie
            # proponujemy: restart samego siebie z własnego apply to osobna ścieżka (l. ~1511).
            if scope == "system" and goly.startswith("shadowai-") and goly != "shadowai-backend":
                f["suggested_action"] = "restart_systemd_unit"
                f["target_ip"] = "10.0.0.10"
                f["action_spec"] = {
                    "unit": goly,
                    "rollback_plan": f"sudo systemctl stop {goly} — jednostka już leży (failed), "
                                     "restart nie zmienia konfiguracji; stop wraca do stanu sprzed"}
            faults.append(f)

    zdarzenia: list[dict] = []
    # (b) pliki
    if not ok_skan:
        powod = f"skan plików: {skan}"
        notes.append(powod)
        niewiadome.append(f"{probe}:pliki")
        faults.append(_nie_wiem(probe, "pliki", powod))
    else:
        notes.extend(skan["notes"])
        checked += int(skan["checked"])
        zdarzenia.extend(skan["faults"])
        if skan["przerwany"]:
            # Skan częściowy = o reszcie plików NIE WIEM; pamięć nie może orzec „ustąpiła”.
            niewiadome.append(f"{probe}:pliki")
            faults.append(_nie_wiem(probe, "pliki", skan["przerwany"]))
    # (c) journal
    for scope, wpisy, blad in (("system", j_sys, j_sys_err), ("user", j_usr, j_usr_err)):
        czesc = f"journal_{scope}"
        if wpisy is None:
            notes.append(blad)
            niewiadome.append(f"{probe}:{czesc}")
            faults.append(_nie_wiem(probe, czesc, blad))
            continue
        checked += 1
        notes.append(f"journal {scope}: {len(wpisy)} wpisów w 6 h")
        zdarzenia.extend(_porazki_z_journala(wpisy, scope))

    zdarzenia.sort(key=lambda f: (_WAGA.get(f["severity"], 3), -float(f.get("_mtime") or 0)))
    if len(zdarzenia) > LIMIT_PORAZEK:
        uciete = zdarzenia[LIMIT_PORAZEK:]
        notes.append(f"porażek w logach {len(zdarzenia)}, pokazuję {LIMIT_PORAZEK}; ucięte: "
                     + ", ".join(f"{sum(1 for u in uciete if u['severity'] == s)}×{s}"
                                 for s in ("high", "medium", "low")))
        if any(u["severity"] == "high" for u in uciete):
            # Ucięto porażki HIGH — o nich pamięć nie może orzec „ustąpiła”.
            niewiadome.append(f"{probe}:uciete")
        zdarzenia = zdarzenia[:LIMIT_PORAZEK]
    for z in zdarzenia:
        z.pop("_mtime", None)
    faults.extend(zdarzenia)
    return _wynik(probe, t0, checked, faults, notes, niewiadome)


# ── SONDA 3: kanarki rąk ─────────────────────────────────────────────────────
async def sonda_kanarki_rak(plik: Optional[Path] = None) -> dict[str, Any]:
    """Czyta TYLKO plik stanu kanarka (scripts/kanarek_suno.py). Żadnej przeglądarki ani sieci."""
    t0 = time.monotonic()
    probe = "kanarki_rak"
    plik = Path(plik or os.environ.get("KANAREK_SUNO_PATH") or PLIK_KANARKA_SUNO)
    wspolne = dict(probe=probe, czesc="suno", service_key="kanarek_suno", target_node="10", owner="shadow")
    if not plik.exists():
        return _wynik(probe, t0, 0, [_fault(
            key="kanarek:suno", kind="hand_canary_stale", severity="medium",
            evidence=f"kanarek Suno nigdy nie biegł (brak pliku {plik})", **wspolne)], [], [])
    try:
        stan = json.loads(plik.read_text(encoding="utf-8"))
        if not isinstance(stan, dict):
            raise ValueError("nie słownik")
    except (OSError, ValueError) as exc:
        powod = f"plik kanarka nieczytelny: {type(exc).__name__}: {str(exc)[:100]}"
        return _wynik(probe, t0, 1, [_nie_wiem(probe, "suno", powod)], [powod], [f"{probe}:suno"])
    proba = stan.get("ostatnia_proba") or {}
    pomiar = stan.get("ostatni_pomiar") or {}
    ts = _z_iso(pomiar.get("ts"))
    if ts is None:
        # Kanarek próbował, ale nigdy nie ZMIERZYŁ (same pominięcia/błędy) — to nie zielony.
        return _wynik(probe, t0, 1, [_fault(
            key="kanarek:suno", kind="hand_canary_stale", severity="medium",
            evidence=("kanarek Suno nie ma żadnego pomiaru; ostatnia próba: "
                      f"{proba.get('status') or '?'} {proba.get('ts') or ''} — {proba.get('powod') or 'bez powodu'}"),
            **wspolne)], [], [])
    faults: list[dict] = []
    wiek_s = (_teraz() - ts).total_seconds()
    if str(pomiar.get("status")) == "czerwony":
        brak = ", ".join(str(b) for b in (pomiar.get("brakujace") or [])) or "?"
        faults.append(_fault(
            key="kanarek:suno", kind="hand_canary_red", severity="high",
            evidence=f"kanarek Suno CZERWONY ({_iso(ts)}): brak selektorów: {brak}", **wspolne))
    elif str(pomiar.get("status")) != "zielony":
        powod = f"nieznany status pomiaru: {pomiar.get('status')!r}"
        return _wynik(probe, t0, 1, [_nie_wiem(probe, "suno", powod)], [powod], [f"{probe}:suno"])
    elif wiek_s > KANAREK_MAX_WIEK_S:
        faults.append(_fault(
            key="kanarek:suno", kind="hand_canary_stale", severity="medium",
            evidence=(f"kanarek Suno zielony, ale sprzed {wiek_s / 3600:.0f} h (limit 48 h); ostatnia próba: "
                      f"{proba.get('status') or '?'} — {proba.get('powod') or 'bez powodu'}"), **wspolne))
    return _wynik(probe, t0, 1, faults, [f"pomiar {pomiar.get('status')} {_iso(ts)}"], [])


# ── SONDA 4: ciągłość (kopie, Syncthing, kanały publikacji, restarty, nasłuchy LAN) ──
# Claude-10 2026-09-30 (plan napraw §8, weryfikacja organizmu): audyt widział tylko węzły LLM
# i linie błędów w logach. Kopie nie szły od 04.07, Syncthing do .253 leżał od 07.09, TikTok/IG
# padały dobę, backend wstał 102× od 19.09, a :8088/:18765/:21 wisiały na LAN — i nic z tego.
# Każda część ma własny try: ślepa część to „nie wiem” o niej, nie o całej sondzie.
KATALOG_KOPII = Path("/srv/data/shadow_backups")
KOPIA_MAX_WIEK_S = 30 * 3600            # cron 03:30 codziennie + zapas
KOPIA_RDZEN = ("SQLite", "Qdrant", "Postgres", "Sekrety", "Kod", "FAISS")
PLIK_SYNCTHING = Path("/opt/shadow/.local/state/syncthing/config.xml")
KANALY_PUBLIKACJI = ("tiktok", "instagram", "facebook", "facebook_music", "x",
                     "youtube", "youtube_gaming_en", "youtube_music", "youtube_music_en", "youtube_art")
KANAL_STOI_S = 36 * 3600
KANAL_STOI_HIGH_S = 72 * 3600
RESTARTY_MAX_24H = 4
_MSG_UNIT_STARTED = "39f53479d3a045ac8e11786248231fbf"   # systemd: „Started <unit>”
_PUBLISHED = re.compile(r"PUBLISHED (\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?")
# Rejestr nasłuchów LAN na .10 (pomiar 30.09 po sprzątaniu). Port spoza listy = usterka — tak
# wisiały porzucone serwery ad hoc i anonimowy FTP. KAŻDĄ nową usługę na LAN dopisz tutaj.
NASLUCHY_ZNANE = {
    139: "Samba", 445: "Samba", 2222: "SSH", 6742: "OpenRGB", 8000: "ACE-Step",
    8001: "backend ShadowAI", 8021: "VPU organ (docker)", 8055: "XTTS", 8077: "Shadow Strike",
    8090: "shadow_site", 8188: "ComfyUI", 8790: "odbiornik gameplay Dziecko",
    9000: "SonarQube (docker)", 9090: "Prometheus (docker)", 11434: "Ollama GPU",
    22000: "Syncthing (synchronizacja)", 25565: "Minecraft Shadow Craft",
}
# Znane, ale czekają na decyzję operatora (weryfikacja 30.09) — notatka, nie usterka.
NASLUCHY_DECYZJA_OPERATORA = {32768: "vast.ai sshd", 6443: "k3s API", 10250: "k3s kubelet"}


def _kopie(teraz: datetime) -> tuple[list[dict], list[str]]:
    probe, czesc = "ciaglosc", "kopie"
    wspolne = dict(probe=probe, czesc=czesc, target_node="10", owner="shadow")
    katalogi = sorted((p for p in KATALOG_KOPII.iterdir()
                       if p.is_dir() and re.fullmatch(r"\d{8}_\d{6}", p.name)), reverse=True)
    # Najnowszy katalog bez podsumowania i świeższy niż 15 min = kopia właśnie trwa — bierz poprzedni.
    for kat in katalogi:
        pods = kat / "PODSUMOWANIE.txt"
        if pods.exists() or time.time() - kat.stat().st_mtime > 900:
            break
    else:
        return [_fault(key="kopia:brak", kind="backup_missing", severity="high", service_key="kopia",
                       evidence=f"brak jakiejkolwiek kopii w {KATALOG_KOPII}", **wspolne)], []
    if not pods.exists():
        return [_fault(key="kopia:przerwana", kind="backup_partial", severity="high", service_key="kopia",
                       evidence=f"kopia {kat.name} bez PODSUMOWANIE.txt — skrypt padł w trakcie", **wspolne)], []
    wiek = teraz.timestamp() - pods.stat().st_mtime
    faults: list[dict] = []
    if wiek > KOPIA_MAX_WIEK_S:
        faults.append(_fault(key="kopia:nieswieza", kind="backup_stale", severity="high", service_key="kopia",
                             evidence=f"ostatnia kopia {kat.name} sprzed {wiek / 3600:.0f} h (limit 30 h; cron 03:30)",
                             **wspolne))
    nieudane = [ln.strip().lstrip("✗").strip() for ln in pods.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip().startswith("✗")]
    if nieudane:
        rdzen = any(any(r in n for r in KOPIA_RDZEN) for n in nieudane)
        faults.append(_fault(key="kopia:czesciowa", kind="backup_partial", severity="high" if rdzen else "medium",
                             service_key="kopia_czesciowa",
                             evidence=f"kopia {kat.name} częściowa: " + "; ".join(nieudane), **wspolne))
    return faults, [f"kopia {kat.name}: {len(nieudane)} nieudanych pozycji, wiek {wiek / 3600:.1f} h"]


async def _syncthing() -> tuple[list[dict], list[str]]:
    import httpx
    klucz = re.search(r"<apikey>([^<]+)</apikey>", PLIK_SYNCTHING.read_text(encoding="utf-8"))
    if not klucz:
        raise RuntimeError(f"brak <apikey> w {PLIK_SYNCTHING}")
    h = {"X-API-Key": klucz.group(1)}
    async with httpx.AsyncClient(timeout=T_HTTP, base_url="http://127.0.0.1:8384") as k:
        conn = (await k.get("/rest/system/connections", headers=h)).raise_for_status().json()
        urz = (await k.get("/rest/config/devices", headers=h)).raise_for_status().json()
        staty = (await k.get("/rest/stats/device", headers=h)).raise_for_status().json()
    nazwy = {d.get("deviceID"): d.get("name") or d.get("deviceID", "")[:7] for d in urz}
    faults: list[dict] = []
    notes: list[str] = []
    for dev, c in (conn.get("connections") or {}).items():
        nazwa = nazwy.get(dev, dev[:7])
        if c.get("paused"):
            notes.append(f"Syncthing: {nazwa} wstrzymany (świadomie)")
            continue
        if not c.get("connected"):
            ost = (staty.get(dev) or {}).get("lastSeen") or "?"
            faults.append(_fault(
                key=f"syncthing:{dev[:7]}", probe="ciaglosc", czesc="syncthing", kind="sync_peer_down",
                severity="medium", service_key=f"syncthing_{dev[:7]}", target_node="10", owner="shadow",
                evidence=f"Syncthing: {nazwa} rozłączony, ostatnio widziany {ost[:16]}"))
    notes.append(f"Syncthing: {len(conn.get('connections') or {})} urządzeń, rozłączonych {len(faults)}")
    return faults, notes


def _kanaly(teraz: datetime) -> tuple[list[dict], list[str]]:
    con = sqlite3.connect(f"file:{PLIK_BAZY}?mode=ro", uri=True, timeout=3)
    try:
        rows = con.execute(
            "SELECT channel, status, notes, scheduled_at FROM publication_queue WHERE status IN "
            f"('published','approved','needs_operator') AND channel IN ({','.join('?' * len(KANALY_PUBLIKACJI))}) "
            "AND staged_at >= ?", (*KANALY_PUBLIKACJI, _iso(teraz - timedelta(days=90)))).fetchall()
    finally:
        con.close()
    ostatnia: dict[str, datetime] = {}
    gotowe: dict[str, int] = {}
    blad: dict[str, int] = {}
    for kanal, status, notes, sched in rows:
        if status == "published":
            for d, g in _PUBLISHED.findall(notes or ""):
                ts = _z_iso(f"{d}T{g or '00:00'}:00+00:00")
                if ts and (kanal not in ostatnia or ts > ostatnia[kanal]):
                    ostatnia[kanal] = ts
        elif status == "approved":
            termin = _z_iso(sched) if sched else None
            if termin is None or termin <= teraz:
                gotowe[kanal] = gotowe.get(kanal, 0) + 1
        else:
            blad[kanal] = blad.get(kanal, 0) + 1
    faults: list[dict] = []
    for kanal, n in sorted(gotowe.items()):
        ost = ostatnia.get(kanal)
        cisza = (teraz - ost).total_seconds() if ost else None
        if cisza is not None and cisza <= KANAL_STOI_S:
            continue
        faults.append(_fault(
            key=f"kanal:{kanal}", probe="ciaglosc", czesc="kanaly", kind="publish_channel_stalled",
            severity="high" if cisza is None or cisza > KANAL_STOI_HIGH_S else "medium",
            service_key=f"kanal_{kanal}", target_node="10", owner="shadow",
            evidence=(f"{kanal}: {n} gotowych do publikacji, a ostatnia udana "
                      + (f"{cisza / 3600:.0f} h temu ({_iso(ost)})" if ost else "— brak w 90 dniach")
                      + (f"; {blad[kanal]} czeka na operatora po błędzie ręki" if blad.get(kanal) else ""))))
    notes = [f"kanały: gotowe {sum(gotowe.values())}, czeka na operatora {sum(blad.values())}, "
             f"stoi {len(faults)}"]
    return faults, notes


async def _restarty() -> tuple[list[dict], list[str]]:
    rc, out, err = await _subproc(["journalctl", "--since", "-24h", "-q", "--no-pager", "-o", "short-iso",
                                   f"MESSAGE_ID={_MSG_UNIT_STARTED}", "UNIT=shadowai-backend.service"])
    if rc != 0:
        raise RuntimeError(f"journalctl rc={rc}: {err.strip()[:140]}")
    starty = [ln.split()[0][11:16] for ln in out.splitlines() if ln.strip()]
    if len(starty) <= RESTARTY_MAX_24H:
        return [], [f"backend: {len(starty)} startów w 24 h"]
    return [_fault(key="backend:restarty", probe="ciaglosc", czesc="restarty", kind="backend_restart_storm",
                   severity="medium", service_key="backend_restarty", target_node="10", owner="shadow",
                   evidence=(f"backend wstawał {len(starty)}× w 24 h (limit {RESTARTY_MAX_24H}; "
                             f"godziny UTC: {', '.join(starty[-12:])}) — zbieraj zmiany, jeden restart"))], []


async def _nasluchy() -> tuple[list[dict], list[str]]:
    rc, out, err = await _subproc(["ss", "-tlnH"])
    if rc != 0:
        raise RuntimeError(f"ss rc={rc}: {err.strip()[:140]}")
    obce: dict[int, set[str]] = {}
    decyzje: set[str] = set()
    for ln in out.splitlines():
        pola = ln.split()
        if len(pola) < 4 or ":" not in pola[3]:
            continue
        host, _, port_s = pola[3].rpartition(":")
        host = host.strip("[]").split("%")[0]
        if host.startswith("127.") or host == "::1" or host.startswith("172.") or not port_s.isdigit():
            continue  # loopback i mosty dockera — nieosiągalne z LAN
        port = int(port_s)
        if port in NASLUCHY_ZNANE:
            continue
        if port in NASLUCHY_DECYZJA_OPERATORA:
            decyzje.add(f"{port} {NASLUCHY_DECYZJA_OPERATORA[port]}")
            continue
        obce.setdefault(port, set()).add(host)
    faults = [_fault(
        key=f"nasluch:{port}", probe="ciaglosc", czesc="nasluchy", kind="lan_listener_unregistered",
        severity="medium", service_key=f"nasluch_{port}", target_node="10", owner="shadow",
        evidence=(f"port {port} słucha na {', '.join(sorted(hosty))} — poza rejestrem nasłuchów "
                  "(organism_probes.NASLUCHY_ZNANE); porzucony serwer albo nowa usługa bez wpisu"))
        for port, hosty in sorted(obce.items())]
    notes = [f"nasłuchy LAN czekające na decyzję operatora: {', '.join(sorted(decyzje))}"] if decyzje else []
    return faults, notes


async def sonda_ciaglosc() -> dict[str, Any]:
    """Kopie, Syncthing, kanały publikacji, restarty backendu, nasłuchy LAN. Tylko odczyt."""
    t0 = time.monotonic()
    probe = "ciaglosc"
    teraz = _teraz()
    notes: list[str] = []
    faults: list[dict] = []
    niewiadome: list[str] = []
    checked = 0
    czesci: list[tuple[str, Awaitable[Any]]] = [
        ("kopie", asyncio.to_thread(_kopie, teraz)),
        ("syncthing", _syncthing()),
        ("kanaly", asyncio.to_thread(_kanaly, teraz)),
        ("restarty", _restarty()),
        ("nasluchy", _nasluchy()),
    ]
    wyniki = await asyncio.gather(*[_z_limitem(c, T_SONDA - 2.0) for _, c in czesci])
    for (czesc, _), (ok, res) in zip(czesci, wyniki):
        if not ok:
            notes.append(f"{czesc}: {res}")
            niewiadome.append(f"{probe}:{czesc}")
            faults.append(_nie_wiem(probe, czesc, str(res)))
            continue
        checked += 1
        f, n = res
        faults.extend(f)
        notes.extend(n)
    return _wynik(probe, t0, checked, faults, notes, niewiadome)


# ── SONDA 5: pętle firmowe (push serwisu, JPK, KSeF) ─────────────────────────
# Claude-10 2026-09-30 (plan napraw §8): żadna czujka nie znała pętli firmowych. Push serwisu
# zapisywał „wysłane” przy porażce (8 z 22 dni września), JPK 06–08 przeleżało terminy, a lista
# zakupów KSeF stała 4 dni i nikt tego nie widział (najnowsza_fv ginęła w uciętym logu).
PUSH_GOTOWY_PL = (9, 15)            # okno pushu 7:15–9:00 PL + zapas
IB_TAX_TICK_MAX_S = 26 * 3600       # tick raz na dobę
KSEF_TICK_MAX_S = 14 * 3600         # tick co 6 h
KSEF_FV_MAX_DNI_ROBOCZYCH = 4


def _brain_modes_ro(prefiksy: tuple[str, ...]) -> dict[str, str]:
    con = sqlite3.connect(f"file:{PLIK_BAZY}?mode=ro", uri=True, timeout=3)
    try:
        war = " OR ".join("key LIKE ?" for _ in prefiksy)
        return {k: v for k, v in con.execute(f"SELECT key, value FROM brain_modes WHERE {war}",
                                             tuple(p + "%" for p in prefiksy)).fetchall()}
    finally:
        con.close()


def _dni_robocze_miedzy(od, do) -> int:
    n, d = 0, od
    while d < do:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


async def _push_serwisu(teraz: datetime) -> tuple[list[dict], list[str]]:
    from zoneinfo import ZoneInfo
    from app.routers.serwis import SERWISANCI
    pl = teraz.astimezone(ZoneInfo("Europe/Warsaw"))
    d = pl.date()
    if pl.weekday() > 4 or (pl.hour, pl.minute) < PUSH_GOTOWY_PL:
        d -= timedelta(days=1)
        while d.weekday() > 4:
            d -= timedelta(days=1)
    oczekiwany = d.isoformat()
    bm = await asyncio.to_thread(_brain_modes_ro, ("serwis_push_ostatni",))
    per = {k.split(":", 1)[1]: v for k, v in bm.items() if ":" in k}
    wspolne = dict(probe="petle_firmy", czesc="serwis_push", kind="firm_loop_push_missed",
                   severity="medium", target_node="10", owner="shadow")
    if not per:  # klucze per serwisant są od 30.09 wieczór — do tego czasu tylko wspólny
        ost = bm.get("serwis_push_ostatni")
        if ost is None:
            raise RuntimeError("brak serwis_push_ostatni w brain_modes")
        if ost >= oczekiwany:
            return [], [f"push serwisu: ostatnio {ost} (wspólny znacznik)"]
        return [_fault(key="serwis_push:wszyscy", service_key="serwis_push",
                       evidence=f"poranny push serwisu: ostatnie doręczenie {ost}, oczekiwane {oczekiwany}",
                       **wspolne)], []
    faults = [_fault(key=f"serwis_push:{kto}", service_key=f"serwis_push_{kto}",
                     evidence=(f"poranny push serwisu do {kto}: ostatnie doręczenie {per.get(kto) or 'nigdy'}, "
                               f"oczekiwane {oczekiwany}"), **wspolne)
              for kto in SERWISANCI if (per.get(kto) or "") < oczekiwany]
    return faults, [f"push serwisu: oczekiwany {oczekiwany}, " + ", ".join(f"{k}={v}" for k, v in sorted(per.items()))]


async def _jpk(teraz: datetime) -> tuple[list[dict], list[str]]:
    from zoneinfo import ZoneInfo
    from app.services.ib_tax import deklarowane_kody, obligations, zlozone_kody
    dzis = teraz.astimezone(ZoneInfo("Europe/Warsaw")).date()
    dekl = set(await deklarowane_kody())
    po = [o for o in obligations(dzis, await zlozone_kody()) if o.status == "po_terminie"]
    bm = await asyncio.to_thread(_brain_modes_ro, ("ib_tax_ostatni_tick",))
    faults: list[dict] = []
    notes: list[str] = []
    if po:
        faults.append(_fault(
            key="firma:jpk_po_terminie", probe="petle_firmy", czesc="ib_tax", kind="firm_tax_overdue",
            severity="medium", service_key="ib_tax_po_terminie", target_node="10", owner="operator",
            evidence="Instal Busters po terminie: " + ", ".join(
                f"{o.kod} (termin {o.termin.isoformat()}{', deklarowane bez UPO' if o.kod in dekl else ''})"
                for o in po)))
    tick = _z_iso(bm.get("ib_tax_ostatni_tick"))
    if tick is None:
        notes.append("ib_tax: tick nie zapisał jeszcze znacznika (zapis od 30.09)")
    elif (teraz - tick).total_seconds() > IB_TAX_TICK_MAX_S:
        faults.append(_fault(
            key="firma:ib_tax_tick", probe="petle_firmy", czesc="ib_tax", kind="firm_loop_stalled",
            severity="medium", service_key="ib_tax_tick", target_node="10", owner="shadow",
            evidence=f"tick terminów podatkowych nie chodził od {_iso(tick)} — cisza ≠ „nic po terminie”"))
    return faults, notes + [f"ib_tax: po terminie {len(po)}"]


async def _ksef(teraz: datetime) -> tuple[list[dict], list[str]]:
    bm = await asyncio.to_thread(_brain_modes_ro, ("ksef_",))
    ost = _z_iso(bm.get("ksef_ostatni_przebieg"))
    if ost is None:
        return [], ["KSeF: tick nie zapisał jeszcze znacznika (zapis od 30.09)"]
    faults: list[dict] = []
    wspolne = dict(probe="petle_firmy", czesc="ksef", target_node="10", owner="shadow")
    if (teraz - ost).total_seconds() > KSEF_TICK_MAX_S:
        faults.append(_fault(key="firma:ksef_tick", kind="firm_loop_stalled", severity="medium",
                             service_key="ksef_tick", evidence=f"pobieranie zakupów z KSeF stoi od {_iso(ost)}",
                             **wspolne))
    fv = str(bm.get("ksef_najnowsza_fv") or "")
    if re.fullmatch(r"\d{8}", fv):
        from datetime import date as _date
        d_fv = _date(int(fv[:4]), int(fv[4:6]), int(fv[6:]))
        dni = _dni_robocze_miedzy(d_fv, teraz.date())
        if dni > KSEF_FV_MAX_DNI_ROBOCZYCH:
            faults.append(_fault(
                key="firma:ksef_lista", kind="firm_ksef_list_stale", severity="low", service_key="ksef_lista",
                evidence=(f"najnowsza faktura zakupowa z KSeF: {d_fv.isoformat()} ({dni} dni roboczych) — "
                          "lista stoi albo naprawdę nie było zakupów"), **wspolne))
    return faults, [f"KSeF: przebieg {_iso(ost)}, najnowsza FV {fv or '?'}"]


# Filtr echa działa od restartu 30.09 19:27:50Z — wcześniejsze wpisy to stare echo, nie nawrót.
KALENDARZ_FILTR_OD = datetime(2026, 9, 30, 19, 27, 50, tzinfo=timezone.utc)


def _kalendarz_ro(teraz: datetime, pomin: tuple[str, ...]) -> tuple[int, int, Optional[str], int]:
    con = sqlite3.connect(f"file:{PLIK_BAZY}?mode=ro", uri=True, timeout=3)
    try:
        znaki = ",".join("?" * len(pomin))
        od = max(teraz - timedelta(hours=24), KALENDARZ_FILTR_OD)
        echo24 = con.execute(f"SELECT COUNT(*) FROM voice_calendar_events WHERE created_at >= ? AND caller_id IN ({znaki})",
                             (od.isoformat(), *pomin)).fetchone()[0]
        czeka, najstarszy = con.execute(
            "SELECT COUNT(*), MIN(created_at) FROM voice_calendar_events WHERE status='pending'").fetchone()
        echo_stare = con.execute(f"SELECT COUNT(*) FROM voice_calendar_events WHERE caller_id IN ({znaki})",
                                 pomin).fetchone()[0]
        return int(echo24 or 0), int(czeka or 0), najstarszy, int(echo_stare or 0)
    finally:
        con.close()


async def _kalendarz(teraz: datetime) -> tuple[list[dict], list[str]]:
    # 30.09: 183 z 222 wpisów kalendarza wizyt to było echo własnych raportów Shadowa. Filtr siedzi
    # w whatsapp._kalendarz_pomija — ta część pilnuje, żeby echo nie wróciło (np. nowy numer mostu).
    from app.routers.whatsapp import _KALENDARZ_POMIN_NR
    echo24, czeka, najstarszy, echo_stare = await asyncio.to_thread(
        _kalendarz_ro, teraz, tuple(sorted(_KALENDARZ_POMIN_NR)))
    faults = []
    if echo24:
        faults.append(_fault(
            key="firma:kalendarz_echo", probe="petle_firmy", czesc="kalendarz", kind="firm_calendar_echo",
            severity="medium", service_key="kalendarz_echo", target_node="10", owner="shadow",
            evidence=(f"kalendarz wizyt z WA: {echo24} wpisów-echa własnych raportów w 24 h — "
                      "filtr whatsapp._kalendarz_pomija przestał działać")))
    return faults, [f"kalendarz: {czeka} czeka (najstarszy {str(najstarszy or '?')[:10]}), "
                    f"w tym {echo_stare} starego echa do usunięcia (decyzja operatora)"]


async def sonda_petle_firmy() -> dict[str, Any]:
    """Push serwisu, terminy JPK (+ żywotność ticku), świeżość KSeF, echo kalendarza. Tylko odczyt."""
    t0 = time.monotonic()
    probe = "petle_firmy"
    teraz = _teraz()
    notes: list[str] = []
    faults: list[dict] = []
    niewiadome: list[str] = []
    checked = 0
    czesci = [("serwis_push", _push_serwisu(teraz)), ("ib_tax", _jpk(teraz)), ("ksef", _ksef(teraz)),
              ("kalendarz", _kalendarz(teraz))]
    wyniki = await asyncio.gather(*[_z_limitem(c, T_SONDA - 2.0) for _, c in czesci])
    for (czesc, _), (ok, res) in zip(czesci, wyniki):
        if not ok:
            notes.append(f"{czesc}: {res}")
            niewiadome.append(f"{probe}:{czesc}")
            faults.append(_nie_wiem(probe, czesc, str(res)))
            continue
        checked += 1
        f, n = res
        faults.extend(f)
        notes.extend(n)
    return _wynik(probe, t0, checked, faults, notes, niewiadome)


# ── SONDA 6: JEV (odruch, System 1) ──────────────────────────────────────────
# Claude-10 2026-09-30 (operator: „widzisz JEV?”): odruch siedzi w bramce akcji i przy dedupie
# publikacji, a żadna czujka go nie znała. Projekt 26.09: węzeł CPU :11435, sąd 0,76–0,94 s,
# kalibracje przeliczane przez jev_pielegnacja co ~6 h. health() pyta na żywo, BEZ zapisu do bazy.
JEV_SAD_WOLNY_MS = 5000
JEV_KALIBRACJA_MAX_S = 30 * 3600


def _jev_kalibracje() -> tuple[Optional[str], int]:
    con = sqlite3.connect(f"file:{PLIK_BAZY}?mode=ro", uri=True, timeout=3)
    try:
        ost = con.execute("SELECT MAX(fitted_at) FROM jev_calibration").fetchone()[0]
        n24 = con.execute("SELECT COUNT(*) FROM jev_judgments WHERE created_at >= ?",
                          (_iso(_teraz() - timedelta(hours=24)),)).fetchone()[0]
        return ost, int(n24 or 0)
    finally:
        con.close()


async def sonda_jev() -> dict[str, Any]:
    t0 = time.monotonic()
    probe = "jev"
    wspolne = dict(probe=probe, target_node="10", owner="shadow")
    try:
        from app.services import jev_service
        h = await jev_service.health()
        ost, n24 = await asyncio.to_thread(_jev_kalibracje)
    except Exception as exc:  # noqa: BLE001
        powod = f"{type(exc).__name__}: {str(exc)[:160]}"
        return _wynik(probe, t0, 0, [_nie_wiem(probe, "calosc", powod)], [powod], [probe])
    faults: list[dict] = []
    if not h.get("ok"):
        faults.append(_fault(key="jev:offline", czesc="health", kind="jev_offline", severity="medium",
                             service_key="jev", evidence=(f"JEV nie odpowiada ({h.get('node')}, "
                                                          f"{h.get('model_requested')}): {h.get('error') or h.get('method')}"),
                             **wspolne))
    elif int(h.get("latency_ms") or 0) > JEV_SAD_WOLNY_MS and not (h.get("warm") or {}).get("cold"):
        faults.append(_fault(key="jev:wolny", czesc="health", kind="jev_slow", severity="low", service_key="jev_wolny",
                             evidence=f"sąd JEV na ciepłym modelu trwa {h.get('latency_ms')} ms (projekt 26.09: 0,76–0,94 s)",
                             **wspolne))
    wiek = (_teraz() - _z_iso(ost)).total_seconds() if _z_iso(ost) else None
    if wiek is None or wiek > JEV_KALIBRACJA_MAX_S:
        faults.append(_fault(key="jev:kalibracja", czesc="kalibracja", kind="jev_calibration_stale", severity="low",
                             service_key="jev_kalibracja",
                             evidence=f"kalibracje JEV nieprzeliczane od {ost or 'nigdy'} (jev_pielegnacja co ~6 h)",
                             **wspolne))
    return _wynik(probe, t0, 2, faults,
                  [f"JEV {h.get('model_used') or h.get('model_requested')} @ {h.get('node')}: "
                   f"{h.get('latency_ms')} ms, kalibracja {str(ost or '?')[:16]}, sądów w 24 h: {n24}"], [])


# ── REJESTR I PRZEBIEG ───────────────────────────────────────────────────────
PROBES: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "wezly_llm": sonda_wezly_llm,
    "porazki_zadan": sonda_porazki_zadan,
    "kanarki_rak": sonda_kanarki_rak,
    "ciaglosc": sonda_ciaglosc,
    "petle_firmy": sonda_petle_firmy,
    "jev": sonda_jev,
}


async def run_probes(*, tylko: Optional[list[str]] = None,
                     pomin_adresy: Optional[set] = None) -> dict[str, Any]:
    """Wszystkie sondy równolegle, każda pod limitem T_SONDA. Tylko odczyt."""
    t0 = time.monotonic()
    nazwy = [n for n in PROBES if not tylko or n in tylko]

    async def _jedna(nazwa: str) -> dict[str, Any]:
        t1 = time.monotonic()
        fn = PROBES[nazwa]
        coro = fn(pomin_adresy) if nazwa == "wezly_llm" else fn()
        ok, res = await _z_limitem(coro, T_SONDA)
        if ok and isinstance(res, dict):
            return res
        powod = str(res) if not ok else f"sonda oddała {type(res).__name__} zamiast słownika"
        logger.warning("[organism_probes] sonda %s: nie wiem — %s", nazwa, powod)
        return _wynik(nazwa, t1, 0, [_nie_wiem(nazwa, "calosc", powod)], [powod], [nazwa])

    ok, res = await _z_limitem(asyncio.gather(*[_jedna(n) for n in nazwy]), T_CALOSC)
    if ok:
        probes = list(res)
    else:
        probes = [_wynik(n, t0, 0, [_nie_wiem(n, "calosc", f"run_probes: {res}")], [str(res)], [n])
                  for n in nazwy]
    faults = [f for p in probes for f in p["faults"]]
    unknown = [u for p in probes for u in p.get("niewiadome") or []]
    return {"ts": _iso(_teraz()), "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "probes": probes, "faults": faults, "unknown": unknown}


# ── PAMIĘĆ USTEREK ───────────────────────────────────────────────────────────
def _sciezka_pamieci(sciezka: Optional[Path] = None) -> Path:
    return Path(sciezka or os.environ.get("ORGANISM_FAULTS_PATH") or PLIK_PAMIECI)


def _pusta_pamiec() -> dict[str, Any]:
    return {"wersja": 1, "zapis": None, "usterki": {}}


def wczytaj_pamiec(sciezka: Optional[Path] = None) -> dict[str, Any]:
    """Brak pliku = pusta pamięć. Zepsuty plik = pusta pamięć + „_blad”, a plik idzie NA BOK
    (.zepsuty_<ts>), nie do kosza — to jedyny ślad po tym, co Shadow wiedział."""
    p = _sciezka_pamieci(sciezka)
    if not p.exists():
        return _pusta_pamiec()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or not isinstance(d.get("usterki"), dict):
            raise ValueError("zły kształt")
        return d
    except (OSError, ValueError) as exc:
        bok = p.with_name(p.name + f".zepsuty_{int(time.time())}")
        try:
            os.replace(p, bok)
        except OSError:
            bok = p
        pusta = _pusta_pamiec()
        pusta["_blad"] = f"pamięć usterek nieczytelna ({type(exc).__name__}); odłożona jako {bok.name}"
        logger.error("[organism_probes] %s", pusta["_blad"])
        return pusta


def zapisz_pamiec(pamiec: dict[str, Any], sciezka: Optional[Path] = None) -> None:
    p = _sciezka_pamieci(sciezka)
    p.parent.mkdir(parents=True, exist_ok=True)
    do_zapisu = {k: v for k, v in pamiec.items() if not k.startswith("_")}
    do_zapisu["zapis"] = _iso(_teraz())
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(do_zapisu, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


async def _status_propozycji(proposal_id: str) -> Optional[dict[str, Any]]:
    """Status propozycji z bazy, TYLKO ODCZYT (mode=ro). None = nie wiem."""
    def _czytaj() -> Optional[dict[str, Any]]:
        con = sqlite3.connect(f"file:{PLIK_BAZY}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT status, updated_at FROM lan_action_proposals WHERE id=?",
                              (proposal_id,)).fetchone()
        finally:
            con.close()
        return {"status": row[0], "updated_at": row[1]} if row else None
    ok, res = await _z_limitem(asyncio.to_thread(_czytaj), 6.0)
    if not ok:
        logger.warning("[organism_probes] status propozycji %s: %s", proposal_id, res)
        return None
    return res


def _czy_niewiadoma(rec: dict[str, Any], niewiadome: set[str]) -> bool:
    probe = str(rec.get("probe") or "audyt")
    if "wszystkie" in niewiadome or probe in niewiadome:
        return True
    if f"{probe}:{rec.get('czesc')}" in niewiadome:
        return True
    # Ucięte porażki high: nie wiemy, czy zdarzenie zniknęło, czy tylko nie zmieściło się w limicie.
    return bool(rec.get("zdarzenie")) and f"{probe}:uciete" in niewiadome


async def aktualizuj_pamiec(
    faults: list[dict[str, Any]], niewiadome_sondy: list[str], *,
    teraz: Optional[datetime] = None,
    status_propozycji: Callable[[str], Awaitable[Optional[dict]]] = _status_propozycji,
    sciezka: Optional[Path] = None, pamiec: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Porównuje bieżące usterki z pamięcią i oddaje przejścia. NIE zapisuje pliku — robi to
    tick po alarmie (zapisz_pamiec), żeby nieudana wysyłka nie zgubiła alarmu."""
    teraz = teraz or _teraz()
    t_iso = _iso(teraz)
    p = pamiec if pamiec is not None else wczytaj_pamiec(sciezka)
    usterki: dict[str, dict] = p.setdefault("usterki", {})
    niewiadome = set(niewiadome_sondy or [])
    zm: dict[str, Any] = {"nowe": [], "trwaja": [], "ustapily": [], "pomoglo": [], "nie_pomoglo": [],
                          "pamiec": p}
    biezace: dict[str, dict] = {}
    for f in faults or []:
        biezace[klucz_usterki(f)] = f

    async def _applied(rec: dict) -> Optional[datetime]:
        """Czas zastosowania remedium albo None (brak propozycji / nie applied / nie wiem)."""
        pid = rec.get("proposal_id")
        if not pid:
            return None
        st = await status_propozycji(str(pid))
        if not st or str(st.get("status")) != "applied":
            return None
        return _z_iso(st.get("updated_at")) or teraz

    for key, f in biezace.items():
        rec = usterki.get(key)
        zdarzenie = bool(f.get("zdarzenie"))
        if rec is not None and zdarzenie:
            # Plik nadpisany od zera (krótszy niż zapamiętany) — stare numery linii nic nie znaczą.
            if f.get("size") is not None and rec.get("rozmiar") is not None and f["size"] < rec["rozmiar"]:
                rec["max_line"] = 0
            swieze = ((f.get("line") or 0) > int(rec.get("max_line") or 0)
                      or (f.get("ts_us") or 0) > int(rec.get("last_ts") or 0))
        else:
            swieze = True
        if rec is None or (rec.get("status") in STATUSY_ZAKONCZONE and swieze):
            poprzedni = rec or {}
            rec = {
                "key": key, "kind": f.get("kind"), "probe": f.get("probe") or "audyt",
                "czesc": f.get("czesc"), "severity": f.get("severity"), "owner": f.get("owner"),
                "evidence": f.get("evidence"), "suggested_action": f.get("suggested_action"),
                "target_ip": f.get("target_ip"), "cicha": bool(f.get("cicha")),
                "status": "nowa", "first_seen": t_iso, "last_seen": t_iso, "ustapila_ts": None,
                "tickow": 1, "zdarzenie": zdarzenie, "file": f.get("file"),
                "max_line": max(int(f.get("line") or 0), 0), "last_ts": int(f.get("ts_us") or 0),
                "rozmiar": f.get("size"), "proposal_id": None, "proposal_ts": None,
                # alarm_ts PRZEŻYWA epizody: to on trzyma dedup 6 h przy usterce, która miga.
                "alarm_ts": dict(poprzedni.get("alarm_ts") or {"nowa": None, "ustapila": None, "nie_pomoglo": None}),
                "do_alarmu": ["nowa"], "epizod_alarmowany": False,
                "epizodow": int(poprzedni.get("epizodow") or 0) + 1,
            }
            usterki[key] = rec
            zm["nowe"].append(rec)
            continue
        if rec.get("status") in STATUSY_ZAKONCZONE:
            # Zdarzenie już widziane (line <= max_line): nagrobek. To NIE jest nowa porażka —
            # tak log_sentinel wpadł w „NAWRÓT 58×”. Odświeżamy last_seen, żeby GC nie zdjął
            # nagrobka, póki linia wciąż wraca w oknie.
            rec["last_seen"] = t_iso
            continue
        # aktywna i nadal obecna
        rec["last_seen"] = t_iso
        rec["tickow"] = int(rec.get("tickow") or 0) + 1
        rec["evidence"] = f.get("evidence")
        rec["severity"] = f.get("severity")
        rec["cicha"] = bool(f.get("cicha"))
        rec["suggested_action"] = f.get("suggested_action")
        rec["target_ip"] = f.get("target_ip")
        rec["max_line"] = max(int(rec.get("max_line") or 0), int(f.get("line") or 0))
        rec["last_ts"] = max(int(rec.get("last_ts") or 0), int(f.get("ts_us") or 0))
        if f.get("size") is not None:
            rec["rozmiar"] = f.get("size")
        kiedy = await _applied(rec)
        if kiedy is not None and (teraz - kiedy).total_seconds() >= REMEDIUM_KARENCJA_S:
            if rec.get("status") != "remedium_nie_pomoglo":
                rec["status"] = "remedium_nie_pomoglo"
                rec["remedium_ts"] = _iso(kiedy)
                rec.setdefault("do_alarmu", [])
                if "nie_pomoglo" not in rec["do_alarmu"]:
                    rec["do_alarmu"].append("nie_pomoglo")
            zm["nie_pomoglo"].append(rec)
        else:
            if rec.get("status") == "nowa":
                rec["status"] = "trwa"
            zm["trwaja"].append(rec)

    for key, rec in list(usterki.items()):
        if key in biezace:
            continue
        if rec.get("status") in STATUSY_AKTYWNE:
            if _czy_niewiadoma(rec, niewiadome):
                continue  # brak pomiaru to NIE wyzdrowienie
            kiedy = await _applied(rec)
            rec["ustapila_ts"] = t_iso
            pending = [a for a in (rec.get("do_alarmu") or []) if a not in ("nowa", "nie_pomoglo")]
            # O ustąpieniu mówimy tylko, gdy operator SŁYSZAŁ o tym epizodzie; zdarzenia z logów
            # wychodzą z okna po cichu (nikt ich nie „naprawił”, po prostu minęło 6 h).
            if rec.get("epizod_alarmowany") and not rec.get("zdarzenie"):
                pending.append("ustapila")
            rec["do_alarmu"] = pending
            if kiedy is not None:
                rec["status"] = "remedium_zastosowane_i_pomoglo"
                rec["remedium_ts"] = _iso(kiedy)
                zm["pomoglo"].append(rec)
            else:
                rec["status"] = "ustapila"
                zm["ustapily"].append(rec)
        else:
            ost = _z_iso(rec.get("last_seen")) or _z_iso(rec.get("ustapila_ts"))
            if ost is not None and (teraz - ost).total_seconds() > GC_USTAPIONYCH_S and not rec.get("do_alarmu"):
                usterki.pop(key, None)
    return zm


def do_zaproponowania(faults: list[dict[str, Any]], pamiec: dict[str, Any],
                      teraz: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Trwały dedup propozycji. Dedup propose_remedies żyje tylko w procesie i trwa 6 h — stąd
    wake_on_lan .197 zaproponowane 61 razy (wszystkie wygasły), a mentor wetuje CTL przy 4×/60 min."""
    teraz = teraz or _teraz()
    out: list[dict[str, Any]] = []
    for f in faults or []:
        if not f.get("suggested_action"):
            continue
        rec = (pamiec.get("usterki") or {}).get(klucz_usterki(f))
        if rec is None:
            out.append(f)  # nie ma w pamięci (nie powinno się zdarzyć po aktualizuj_pamiec) — proponuj
            continue
        pts = _z_iso(rec.get("proposal_ts"))
        if pts is None or (teraz - pts).total_seconds() >= PROPOZYCJA_PONOW_S:
            out.append(f)
    return out


def mapa_propozycji(actionable: list[dict[str, Any]], proposed: list[dict[str, Any]],
                    fault_key: Callable[[dict], str]) -> dict[str, str]:
    """klucz pamięci → proposal_id. propose_remedies oddaje klucz organism_audit._fault_key
    (z suggested_action w środku), pamięć ma swój — tu jest jedyne miejsce przekładu."""
    po_audycie = {fault_key(f): klucz_usterki(f) for f in actionable or []}
    out: dict[str, str] = {}
    for pr in proposed or []:
        if pr.get("proposed") and pr.get("proposal_id") and pr.get("fault") in po_audycie:
            out[po_audycie[pr["fault"]]] = str(pr["proposal_id"])
    return out


def zapisz_propozycje(pamiec: dict[str, Any], mapa: dict[str, str],
                      teraz: Optional[datetime] = None) -> None:
    t_iso = _iso(teraz or _teraz())
    for key, pid in (mapa or {}).items():
        rec = (pamiec.get("usterki") or {}).get(key)
        if rec is not None:
            rec["proposal_id"] = pid
            rec["proposal_ts"] = t_iso


# ── ALARM ────────────────────────────────────────────────────────────────────
def _cicha(rec: dict[str, Any]) -> bool:
    if ALARMUJ_WYLACZONE_MASZYNY_OPERATORA:
        return False
    if rec.get("cicha"):
        return True
    return rec.get("kind") == "organ_offline" and str(rec.get("owner") or "shadow") != "shadow"


def _ile_czasu(od_iso: Any, do: datetime) -> str:
    od = _z_iso(od_iso)
    if od is None:
        return "?"
    s = max(0, int((do - od).total_seconds()))
    if s < 5400:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} h"


async def alarmuj(zmiany: dict[str, Any], wyslij: Callable[[str], Awaitable[bool]], *,
                  teraz: Optional[datetime] = None) -> dict[str, Any]:
    """Jedna zbiorcza wiadomość o NOWYCH usterkach high, USTĄPIENIACH i remediach, które NIE POMOGŁY.

    `wyslij` przychodzi z zewnątrz (w ticku: scheduler._discord_say). alarm_ts ustawiamy TYLKO,
    gdy wysyłka oddała True — awaria kanału nie kasuje alarmu (lekcja „awaria mózgu nie kasuje
    widza”): kandydaci bierzemy z CAŁEJ pamięci po polu do_alarmu, nie tylko z bieżących przejść.
    """
    teraz = teraz or _teraz()
    usterki: dict[str, dict] = (zmiany.get("pamiec") or {}).get("usterki") or {}
    pozycje: list[tuple[dict, str, str]] = []
    for rec in usterki.values():
        for typ in list(rec.get("do_alarmu") or []):
            if typ == "nowa" and (rec.get("severity") != "high" or _cicha(rec)
                                  or rec.get("status") not in STATUSY_AKTYWNE):
                continue
            if typ == "ustapila" and rec.get("status") not in STATUSY_ZAKONCZONE:
                continue
            ost = _z_iso((rec.get("alarm_ts") or {}).get(typ))
            if ost is not None and (teraz - ost).total_seconds() < ALARM_DEDUP_S:
                continue
            if typ == "nowa":
                linia = f"NOWA: {rec.get('evidence')}"
                if rec.get("suggested_action"):
                    linia += f" | proponuję: {rec['suggested_action']}"
                    if rec.get("target_ip"):
                        linia += f" na {rec['target_ip']}"
                    if rec.get("proposal_id"):
                        linia += (f" (propozycja {str(rec['proposal_id'])[:8]}, czeka na "
                                  "zatwierdzenie, wygasa po 60 min)")
                    else:
                        linia += " (propozycji jeszcze nie ma w bazie)"
            elif typ == "ustapila":
                linia = f"USTĄPIŁA: {rec.get('key')} po {_ile_czasu(rec.get('first_seen'), _z_iso(rec.get('ustapila_ts')) or teraz)}"
                if rec.get("status") == "remedium_zastosowane_i_pomoglo":
                    linia += " (po zastosowaniu remedium)"
            else:
                linia = (f"NIE POMOGŁO: {rec.get('key')} — remedium zastosowane "
                         f"{rec.get('remedium_ts') or '?'}, usterka trwa: {str(rec.get('evidence'))[:160]}")
            pozycje.append((rec, typ, linia))
    if not pozycje:
        return {"wyslano": False, "pozycji": 0, "tekst": "", "powod": "nic do zgłoszenia"}

    kolejnosc = {"nie_pomoglo": 0, "nowa": 1, "ustapila": 2}
    pozycje.sort(key=lambda x: kolejnosc.get(x[1], 3))
    naglowek = "Shadow — audyt organizmu:"
    tekst = naglowek
    wyslane: list[tuple[dict, str]] = []
    for rec, typ, linia in pozycje:
        if len(tekst) + len(linia) + 1 > 1750:
            continue  # nie zmieściło się: zostaje w do_alarmu na następny tick
        tekst += "\n" + linia
        wyslane.append((rec, typ))
    reszta = len(pozycje) - len(wyslane)
    if reszta:
        tekst += f"\n(+{reszta} w następnym komunikacie)"
    if not wyslane:  # jedna pozycja dłuższa niż limit — tniemy, ale mówimy
        rec, typ, linia = pozycje[0]
        tekst = (naglowek + "\n" + linia)[:1790]
        wyslane = [(rec, typ)]

    ok, res = await _z_limitem(wyslij(tekst), T_WYSYLKA)
    poszlo = bool(ok and res is True)
    if poszlo:
        t_iso = _iso(teraz)
        for rec, typ in wyslane:
            rec.setdefault("alarm_ts", {})[typ] = t_iso
            rec["do_alarmu"] = [a for a in (rec.get("do_alarmu") or []) if a != typ]
            if typ == "nowa":
                rec["epizod_alarmowany"] = True
    else:
        logger.warning("[organism_probes] alarm NIE wyszedł (%s) — zostaje na następny tick", res)
    return {"wyslano": poszlo, "pozycji": len(wyslane), "tekst": tekst,
            "powod": "" if poszlo else f"wysyłka: {res}"}


# ── SAMODZIELNY POMIAR ───────────────────────────────────────────────────────
async def _main() -> None:
    import sys
    wynik = await run_probes()
    skrot = {
        "ts": wynik["ts"], "elapsed_ms": wynik["elapsed_ms"], "unknown": wynik["unknown"],
        "probes": [{k: p[k] for k in ("probe", "ok", "checked", "elapsed_ms", "notes")} | {"faults": len(p["faults"])}
                   for p in wynik["probes"]],
        "faults": [{k: f[k] for k in ("key", "kind", "severity", "evidence", "suggested_action", "target_ip", "owner")}
                   for f in wynik["faults"]],
    }
    print(json.dumps(wynik if "--pelny" in sys.argv else skrot, ensure_ascii=False, indent=1))
    # Decyzja „co by poszło na alarm” — na PUSTEJ pamięci w RAM, bez zapisu i bez wysyłki.
    zm = await aktualizuj_pamiec(wynik["faults"], wynik["unknown"], pamiec=_pusta_pamiec())
    zapisane: list[str] = []

    async def _atrapa(t: str) -> bool:
        zapisane.append(t)
        return True
    al = await alarmuj(zm, _atrapa)
    print("\n--- DECYZJA (atrapa wysyłki, pamięć w RAM, nic nie zapisano) ---")
    print(f"nowe={len(zm['nowe'])} do_zaproponowania={len(do_zaproponowania(wynik['faults'], zm['pamiec']))} "
          f"alarm_pozycji={al['pozycji']}")
    print(al["tekst"] or "(cisza: żadnej nowej usterki high)")


if __name__ == "__main__":
    asyncio.run(_main())
