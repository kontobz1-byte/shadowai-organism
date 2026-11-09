# plik: odbior_2_hand.py
"""Ręka odbior_2: ODBIÓR NA D: — plik albo katalog z .10 ląduje w D:\\ShadowAICont\\_persona\\<persona>\\<paczka>
na .253 i otwiera się tam w Eksploratorze.

Organ: app.services.odbior_d.odloz_sync(pliki, persona, paczka) (SMB, dowód = rozmiar co do bajta; paczka może mieć
podkatalogi po \\), nazwy części przez odbior_d.nazwa_paczki. Otwarcie: POST
http://127.0.0.1:8001/api/tech/desktop/launch {"app": "explorer", "file": <katalog na D:>}.

Źródło: ścieżka z polecenia (zaczyna się od /opt/shadow/; spacje w nazwie obsłużone przez dopasowanie najdłuższej
istniejącej ścieżki) albo params["sciezka"]. Bez ścieżki ręka pyta o nią jednym zdaniem.
Persona: z sekcji rozmowy (tech, creative, Firma, family, mentor), domyślnie creative.
Paczka: nazwa katalogu źródła + _RRRR-MM-DD (dla pojedynczego pliku: katalog, w którym leży).
Katalog: struktura zachowana — każdy podkatalog z plikami to osobne odloz_sync z paczką <paczka>\\<pod>\\<pod>.
Wynik odloz_sync, którego nie umiem odczytać, NIE liczy się jako odłożony (brak zgadywania sukcesu).

Bez bramki „wykonaj”: zdanie operatora („skopiuj … na d: 253”, „wrzuć to na D na 253”) jest samym poleceniem
kopii; ręka tylko DOKŁADA pliki na .253 i niczego nie kasuje na .10.

KONTRAKT: start(params, cmd) ZAWSZE zwraca {"ok": bool, "reply": str}.
Granice I/O: _odloz (odloz_sync), _otworz (desktop/launch), _nazwa_czesci (nazwa_paczki), _dzis (zegar).
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

WZORZEC = (r"\b(?:s?kopiuj|wrzu[ćc]|od[łl][óo][żz]|przerzu[ćc])\b.{0,300}?"
           r"\bna\s+(?:d\s*:|d\b.{0,15}?\b253\b|(?:192\.168\.50)?\.?253\b)"
           r"|/[\w.-]+/\S*\s+(?:i\s+)?otw[óo]rz\s+(?:go\s+|j[ąa]\s+|to\s+)?na\s+(?:d\b|(?:192\.168\.50)?\.?253\b)")
GOTOWA = True

PREFIKS = "/opt/shadow/"
BAZA_D = "D:\\ShadowAICont\\_persona"
URL_LAUNCH = "http://127.0.0.1:8001/api/tech/desktop/launch"
TIMEOUT_LAUNCH = 15.0
PERSONY = ("tech", "creative", "Firma", "family", "mentor")
DOMYSLNA = "creative"
PL = ZoneInfo("Europe/Warsaw")
_KONCOWKI = ".,;:!?)]}\"'”“»«„"
_ZLE_ZNAKI = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_KLUCZE_OK = ("odlozone", "odlozono", "zapisane", "skopiowane", "ok_pliki", "ok")
_KLUCZE_BRAK = ("brak", "brakuje", "bledy", "nieudane", "missing", "failed")
_KLUCZE_LISTA = ("pliki", "wyniki", "files", "results")


# ---------------------------------------------------------------- drobne

def _k(v, n: int = 300) -> str:
    return " ".join(str(v).split())[:n]


def _blad(e) -> str:
    m = _k(e)
    return f"{type(e).__name__}: {m}" if m else type(e).__name__


def _odm(n, jeden, kilka, wiele):
    return jeden if n == 1 else (kilka if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else wiele)


def _pliku(n: int) -> str:
    return "pliku" if n == 1 else "plików"


def _lokalna_nazwa(s: str) -> str:
    return _ZLE_ZNAKI.sub("_", str(s)).strip().rstrip(". ") or "paczka"


# ---------------------------------------------------------------- granice I/O

def _dzis() -> date:
    return datetime.now(PL).date()


def _nazwa_czesci(s: str) -> str:
    """Nazwa części paczki przez odbior_d.nazwa_paczki; bez organu — tylko usunięcie znaków zakazanych w Windows."""
    try:
        from app.services import odbior_d
        fn = odbior_d.nazwa_paczki
    except (ImportError, AttributeError):
        fn = None
    if fn is not None:
        try:
            w = fn(s)
        except Exception:  # noqa: BLE001 — inna sygnatura organu nie może zatrzymać odbioru
            w = None
        if isinstance(w, str) and w.strip():
            return w.strip()
    return _lokalna_nazwa(s)


async def _odloz(pliki: list, persona: str, paczka: str):
    """odloz_sync z backendu (SMB na .253, dowód = rozmiar co do bajta); synchroniczny, więc w wątku."""
    from app.services import odbior_d
    return await asyncio.to_thread(odbior_d.odloz_sync, list(pliki), persona, paczka)


async def _otworz(katalog: str):
    """Eksplorator na .253 przez desktop/launch (backend proxuje do win_agent)."""
    async with httpx.AsyncClient(timeout=TIMEOUT_LAUNCH) as c:
        r = await c.post(URL_LAUNCH, json={"app": "explorer", "file": katalog})
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"tekst": r.text[:200]}


# ---------------------------------------------------------------- źródło, persona, grupy

def _sciezka_z_cmd(cmd: str):
    i = cmd.find(PREFIKS)
    if i < 0:
        return None
    tokeny = cmd[i:].split()
    for n in range(len(tokeny), 0, -1):
        surowa = " ".join(tokeny[:n])
        for kand in (surowa, surowa.rstrip(_KONCOWKI)):
            if len(kand) > len(PREFIKS) and os.path.exists(kand):
                return kand
    pierwsza = tokeny[0].rstrip(_KONCOWKI) if tokeny else ""
    return pierwsza if len(pierwsza) > len(PREFIKS) else None


def _persona(params: dict):
    for klucz in ("persona", "section", "sekcja"):
        v = str(params.get(klucz) or "").strip().lower()
        if not v:
            continue
        slowa = re.split(r"[^a-z]+", v)
        for p in PERSONY:
            if p in slowa:
                return p, None
        return DOMYSLNA, f"sekcja „{v}” nie jest personą ({', '.join(PERSONY)}) — odkładam do {DOMYSLNA}"
    return DOMYSLNA, None


def _grupy(p: Path, baza: str):
    """[(paczka, [pliki])] — katalog główny i każdy podkatalog z plikami osobno; plus liczba pustych katalogów."""
    if p.is_file():
        return [(baza, [str(p)])], 0
    grupy, puste = [], 0
    for root, dirs, files in os.walk(p):
        dirs.sort()
        pliki = [os.path.join(root, f) for f in sorted(files) if os.path.isfile(os.path.join(root, f))]
        rel = os.path.relpath(root, p)
        if not pliki:
            if rel != "." and not dirs:
                puste += 1
            continue
        czesci = [] if rel == "." else [_nazwa_czesci(c) for c in Path(rel).parts]
        grupy.append(("\\".join([baza] + czesci), pliki))
    return grupy, puste


def _rel(f: str, p: Path) -> str:
    return os.path.relpath(f, p) if p.is_dir() else os.path.basename(f)


# ---------------------------------------------------------------- odczyt dowodu odloz_sync

def _ile(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple, set, dict)):
        return len(v)
    return None


def _nazwy(v) -> list:
    if isinstance(v, dict):
        v = list(v)
    if not isinstance(v, (list, tuple, set)):
        return []
    out = []
    for x in v:
        if isinstance(x, dict):
            x = x.get("plik") or x.get("nazwa") or x.get("name") or x.get("path") or "?"
        out.append(os.path.basename(str(x)) or str(x))
    return out


def _pozycja_ok(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, dict):
        for k in ("ok", "zgodny", "zgodne", "match"):
            if isinstance(x.get(k), bool):
                return x[k]
        st = str(x.get("status") or "").strip().lower()
        if st:
            return st in ("ok", "zgodny", "zgodne", "done")
    return None


def _policz(wynik, pliki: list):
    """(odłożone, brakujące, nazwy brakujących, uwaga). Nieczytelny wynik = nic nie odłożone."""
    n = len(pliki)
    wszystkie = [os.path.basename(f) for f in pliki]
    blad = ""
    if isinstance(wynik, dict):
        blad = _k(wynik.get("blad") or wynik.get("error") or wynik.get("reason") or "", 200)
        ok = next((_ile(wynik[k]) for k in _KLUCZE_OK if _ile(wynik.get(k)) is not None), None)
        bk = next((k for k in _KLUCZE_BRAK if _ile(wynik.get(k)) is not None), None)
        if ok is not None or bk is not None:
            if ok is None:
                ok = n - _ile(wynik[bk])
            ok = max(0, min(ok, n))
            return ok, n - ok, (_nazwy(wynik[bk]) if bk else [])[: n - ok], None
        lista = next((wynik[k] for k in _KLUCZE_LISTA if isinstance(wynik.get(k), list)), None)
        if lista is not None:
            wynik = lista
        elif isinstance(wynik.get("ok"), bool):
            wynik = wynik["ok"]
    if isinstance(wynik, bool):
        if wynik:
            return n, 0, [], None
        return 0, n, wszystkie, "odloz_sync zgłosił niepowodzenie" + (f": {blad}" if blad else "")
    if isinstance(wynik, list):
        stany = [_pozycja_ok(x) for x in wynik]
        ok = min(sum(1 for s in stany if s is True), n)
        zle = [x for x, s in zip(wynik, stany) if s is not True]
        uw = None if None not in stany else "część wpisów odloz_sync bez statusu — liczę je jako brakujące"
        return ok, n - ok, _nazwy(zle), uw
    return 0, n, wszystkie, (f"nieczytelny wynik odloz_sync ({type(wynik).__name__}) — "
                             "nie liczę tych plików jako odłożonych")


# ---------------------------------------------------------------- główna logika

async def wykonaj(params: dict, cmd: str) -> dict:
    params = params if isinstance(params, dict) else {}
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie odbioru na D: (np. „skopiuj /opt/shadow/… na d: 253”)."}
    zrodlo = str(params.get("sciezka") or params.get("path") or "").strip() or _sciezka_z_cmd(cmd)
    if not zrodlo:
        return {"ok": False, "reason": f"Podaj pełną ścieżkę pliku albo katalogu na .10 (zaczyna się od {PREFIKS}), "
                                       f"bo nie wiem, który z rozmowy masz na myśli."}
    pref, real = os.path.realpath(PREFIKS), os.path.realpath(zrodlo)
    if not zrodlo.startswith(PREFIKS) or not real.startswith(pref + os.sep):
        return {"ok": False, "reason": f"Źródło {zrodlo} nie leży wewnątrz {PREFIKS} — odkładam tylko stamtąd "
                                       f"(i nie cały katalog domowy). Niczego nie skopiowałem."}
    if not os.path.exists(zrodlo):
        return {"ok": False, "reason": f"Na .10 nie ma {zrodlo} — niczego nie odłożyłem. Podaj poprawną ścieżkę."}

    persona, uwaga_persony = _persona(params)
    p = Path(zrodlo)
    nazwa = p.name if p.is_dir() else p.parent.name
    baza = _nazwa_czesci(f"{nazwa}_{_dzis():%Y-%m-%d}")
    cel = f"{BAZA_D}\\{persona}\\{baza}"
    grupy, puste = _grupy(p, baza)
    if not grupy:
        return {"ok": False, "reason": f"W {zrodlo} nie ma żadnego pliku — nic do odłożenia na D:."}
    total = sum(len(g) for _, g in grupy)

    ok_n = brak_n = 0
    brakujace, bledy, uwagi = [], [], []
    for paczka, pliki in grupy:
        try:
            wynik = await _odloz(pliki, persona, paczka)
        except Exception as e:  # noqa: BLE001
            brak_n += len(pliki)
            brakujace += [_rel(f, p) for f in pliki]
            bledy.append(f"{paczka}: {_blad(e)}")
            continue
        o, b, nz, u = _policz(wynik, pliki)
        ok_n, brak_n = ok_n + o, brak_n + b
        brakujace += nz
        if u:
            uwagi.append(u)
    uwagi = list(dict.fromkeys(uwagi))

    if ok_n == 0:
        powod = "; ".join(bledy + uwagi) or "odloz_sync nie potwierdził żadnego pliku"
        return {"ok": False, "reason": f"Nie odłożyłem żadnego z {total} {_pliku(total)} do {cel} ({powod}). "
                                       f"Eksploratora nie otwieram; na .10 niczego nie skasowałem."}

    blad_launch = None
    try:
        r = await _otworz(cel)
        if isinstance(r, dict) and (r.get("ok") is False or r.get("error") or r.get("blad")):
            blad_launch = _k(r.get("error") or r.get("blad") or r.get("detail") or "launch zwrócił ok=false", 200)
    except Exception as e:  # noqa: BLE001
        blad_launch = _blad(e)

    li = [f"ODBIÓR NA D: (.253) — {zrodlo} → {cel}",
          f"Odłożono {ok_n} z {total} {_pliku(total)} (dowód odloz_sync: rozmiar co do bajta), brakuje {brak_n}."]
    if len(grupy) > 1:
        li.append(f"Struktura zachowana: {len(grupy)} {_odm(len(grupy), 'katalog', 'katalogi', 'katalogów')} "
                  f"z plikami, każdy osobnym odloz_sync.")
    if brakujace:
        li.append("Brakuje: " + ", ".join(brakujace[:8])
                  + (f" i {len(brakujace) - 8} innych" if len(brakujace) > 8 else "") + ".")
    li += [f"Błąd odkładania {b}." for b in bledy]
    li += [f"Uwaga: {u}." for u in uwagi]
    if puste:
        li.append(f"Pominąłem {puste} {_odm(puste, 'pusty katalog', 'puste katalogi', 'pustych katalogów')} "
                  f"(odloz_sync odkłada pliki).")
    if uwaga_persony:
        li.append(f"Uwaga: {uwaga_persony}.")
    li.append(f"Otworzyłem {cel} w Eksploratorze na .253." if blad_launch is None
              else f"Nie otworzyłem Eksploratora ({blad_launch}) — pliki leżą w {cel}.")
    li.append("Na .10 niczego nie skasowałem.")
    return {"ok": True, "reply": "\n".join(li)}


# ---------------------------------------------------------------- kontrakt

async def start(params: dict | None = None, cmd: str = "") -> dict:
    """ZAWSZE {"ok": bool, "reply": str} — także przy błędzie."""
    try:
        w = await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reply": f"Ręka odbior_2 padła: {_blad(e)}. Na .10 niczego nie skasowałem."}
    w = w if isinstance(w, dict) else {}
    return {"ok": bool(w.get("ok")),
            "reply": str(w.get("reply") or w.get("reason") or "Ręka odbior_2 nie podała odpowiedzi.")}


async def handler_odbior_2(params: dict, cmd: str, section: str = "") -> dict:
    p = dict(params or {})
    if section and not p.get("section"):
        p["section"] = section
    w = await start(p, cmd)
    return {"reply": w["reply"], "data": w}
