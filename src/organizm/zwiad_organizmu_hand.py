"""Ręka zwiad_organizmu (08.11.2026: kuźnia 4× nieudana — recenzent; wersja ucznia Claude z próby 5 zwiad_organizmu_4, przejrzana i wpięta ręcznie przez sesję Claude na polecenie operatora „1”): gdzie w organizmie Shadowa (węzeł .10) jest coś o danej nazwie. Tylko odczyt, bez LLM.

Słowa: z polecenia słowa dłuższe niż 3 znaki, bez: gdzie, jest, znajdz, zwiad, organizmie, ciebie, twoj, twoja
(porównanie bez polskich znaków, więc „znajdź” też odpada). Pięć miejsc naraz, każde z limitem CZAS = 15 s:
 1) pliki .py .sh .md .json pod KATALOGI (app, scripts, handoff), nazwa zawiera słowo; bez .bak, __pycache__,
    node_modules, .venv; do 15,
 2) grep -rIl -i -F (słowo dosłownie) po .py pod KATALOGI["app"], do 15 plików, przy każdym pierwsza linia z trafieniem
    (grep: kod 1 = brak trafień, kod ≥ 2 = „grep nie odpowiada: <stderr>”),
 3) tabele bazy BAZA otwartej z mode=ro, nazwa zawiera słowo, z liczbą wierszy,
 4) kolekcje Qdrant z GET QDRANT_URL/collections, nazwa zawiera słowo, z points_count,
 5) jednostki systemd: systemctl list-units --all, systemctl list-timers --all, systemctl --user list-units --all.
Gdy polecenie zewnętrzne kończy się kodem ≠ 0 albo wyjątkiem, sekcja mówi „nie odpowiada: <stderr>”, a nie
„nic pod tą nazwą”. Ręka nie zgaduje ścieżek i niczego nie zapisuje (dysk, baza) — „wykonaj” nie jest potrzebne.
Granice I/O: _proces (asyncio.create_subprocess_exec), _pobierz (httpx.AsyncClient), _tabele_sync (sqlite mode=ro),
_pliki_sync (os.walk).
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from urllib.parse import quote

import httpx

WZORZEC = (r"\b(?:zwiad\w*|znajd[źz]\w*\s+(?:w\s+)?(?:twoim\s+)?organizm\w*|"
           r"gdzie\s+(?:u\s+ciebie|w\s+(?:twoim\s+)?organizmie)\s+jest|"
           r"gdzie\s+(?:u\s+ciebie\s+)?jest\s+[\w-]*_[\w-]*)")
GOTOWA = True

KATALOGI = {
    "app": "/opt/shadow/shadow/backend/app",
    "scripts": "/opt/shadow/shadow/scripts",
    "handoff": "/opt/shadow/shadow/handoff",
}
BAZA = "/opt/shadow/shadow/backend/shadowai.db"
QDRANT_URL = "http://127.0.0.1:6333"

STOP = frozenset({"gdzie", "jest", "znajdz", "zwiad", "organizmie", "ciebie", "twoj", "twoja"})
ROZSZ = (".py", ".sh", ".md", ".json")
POMIN_KAT = frozenset({"__pycache__", "node_modules", ".venv"})
LIMIT_PLIKOW, LIMIT_INNYCH, MAX_SLOW = 15, 30, 8
CZAS = 15.0
ZNACZNIKI = ("●", "○", "*", "×")
_PIPE = asyncio.subprocess.PIPE
SYSTEMD = (
    ("systemctl list-units", "system", False,
     ["systemctl", "list-units", "--all", "--no-legend", "--no-pager", "--plain"]),
    ("systemctl list-timers", "system", True,
     ["systemctl", "list-timers", "--all", "--no-legend", "--no-pager"]),
    ("systemctl --user list-units", "user", False,
     ["systemctl", "--user", "list-units", "--all", "--no-legend", "--no-pager", "--plain"]),
)


# ---------------------------------------------------------------- drobne

def _k(s, n: int = 300) -> str:
    return " ".join(str(s or "").split())[:n]


def _blad(e) -> str:
    m = _k(e)
    return f"{m} ({type(e).__name__})" if m else type(e).__name__


def _opis(kod, err) -> str:
    return _k(err) or f"kod wyjścia {kod}, bez komunikatu na stderr"


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _odm(n, jeden, kilka, wiele):
    return jeden if n == 1 else (kilka if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else wiele)


def _pasuje(nazwa, slowa) -> bool:
    n = str(nazwa).lower()
    return any(s in n for s in slowa)


def _w(traf=(), bledy=(), uwagi=()) -> dict:
    return {"traf": list(traf), "bledy": list(bledy), "uwagi": list(uwagi)}


def _slowa(cmd: str, params: dict) -> list:
    zrodlo = params.get("slowa")
    if isinstance(zrodlo, (list, tuple)):
        zrodlo = " ".join(str(x) for x in zrodlo)
    tekst = str(zrodlo) if zrodlo else cmd
    out = []
    for t in re.findall(r"[\w.-]+", tekst):
        t = t.strip(".-").lower()
        if len(t) > 3 and _norm(t) not in STOP and t not in out:
            out.append(t)
    return out[:MAX_SLOW]


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + env["XDG_RUNTIME_DIR"] + "/bus")
    env["SYSTEMD_COLORS"] = "0"
    env["SYSTEMD_PAGER"] = ""
    return env


# ---------------------------------------------------------------- granice I/O

async def _proces(argv: list) -> tuple:
    """Uruchamia polecenie (tylko odczyt); zwraca (kod, stdout, stderr). Przy anulowaniu zabija proces."""
    proc = await asyncio.create_subprocess_exec(*[str(a) for a in argv], stdin=asyncio.subprocess.DEVNULL,
                                                stdout=_PIPE, stderr=_PIPE, env=_env())
    try:
        out, err = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise
    return (proc.returncode, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"))


async def _pobierz(url: str):
    async with httpx.AsyncClient(timeout=CZAS) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- 1) pliki

def _pliki_sync(slowa: list) -> dict:
    traf, uwagi, brak = [], [], []
    katalogi = dict(KATALOGI)
    for klucz, kat in katalogi.items():
        p = Path(kat)
        if not p.is_dir():
            brak.append(f"{p} ({klucz})")
            continue
        bl = []
        for root, dirs, files in os.walk(p, onerror=bl.append):
            dirs[:] = sorted(d for d in dirs if d not in POMIN_KAT and ".bak" not in d.lower())
            for f in sorted(files):
                fl = f.lower()
                if ".bak" in fl or not fl.endswith(ROZSZ):
                    continue
                if _pasuje(f, slowa):
                    traf.append(str(Path(root) / f))
        for e in bl[:3]:
            plik = getattr(e, "filename", None) or "?"
            uwagi.append(f"nie czytam {plik}: {getattr(e, 'strerror', None) or e}")
    if brak and len(brak) == len(katalogi):
        return _w(bledy=["os.walk nie odpowiada: żaden katalog nie istnieje — " + ", ".join(brak)])
    uwagi += [f"brak katalogu {x} — nie przeszukany" for x in brak]
    if len(traf) > LIMIT_PLIKOW:
        uwagi.append(f"pokazuję {LIMIT_PLIKOW} z {len(traf)} — zawęź słowo")
    return _w(traf[:LIMIT_PLIKOW], [], uwagi)


# ---------------------------------------------------------------- 2) grep

def _pierwsza_linia(sciezka: str, slowa: list):
    try:
        with open(sciezka, encoding="utf-8", errors="replace") as f:
            for i, linia in enumerate(f, 1):
                ll = linia.lower()
                if any(s in ll for s in slowa):
                    return i
    except OSError:
        return None
    return None


async def _grep(slowa: list) -> dict:
    app = KATALOGI.get("app")
    if not app or not Path(app).is_dir():
        return _w(bledy=[f"grep nie odpowiada: brak katalogu backend/app ({app}) — innego nie zgaduję"])
    argv = ["grep", "-rIl", "-i", "-F", "--include=*.py", "--exclude=*.bak*"]
    argv += [f"--exclude-dir={d}" for d in sorted(POMIN_KAT)]
    for s in slowa:
        argv += ["-e", s]
    argv += ["--", str(app)]
    kod, out, err = await _proces(argv)
    pliki = sorted({x for x in out.splitlines() if x.strip()})
    bledy = [] if kod in (0, 1) else [f"grep nie odpowiada: {_opis(kod, err)}"]
    if not pliki:
        return _w(bledy=bledy)
    pokaz = pliki[:LIMIT_PLIKOW]
    numery = await asyncio.to_thread(lambda: [_pierwsza_linia(p, slowa) for p in pokaz])
    traf = [f"{p}:{n}" if n else f"{p} (numeru linii nie odczytałem)" for p, n in zip(pokaz, numery)]
    uwagi = [f"pokazuję {LIMIT_PLIKOW} z {len(pliki)} plików — zawęź słowo"] if len(pliki) > LIMIT_PLIKOW else []
    return _w(traf, bledy, uwagi)


# ---------------------------------------------------------------- 3) tabele bazy

def _ident(n: str) -> str:
    return '"' + n.replace('"', '""') + '"'


def _tabele_sync(slowa: list) -> dict:
    p = Path(BAZA)
    if not p.is_file():
        return _w(bledy=[f"baza nie odpowiada: brak pliku {p} — innej ścieżki nie zgaduję"])
    con = sqlite3.connect(f"file:{quote(str(p.resolve()))}?mode=ro", uri=True, timeout=5)
    try:
        nazwy = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        traf = []
        for n in nazwy:
            if not _pasuje(n, slowa):
                continue
            try:
                ile = con.execute("SELECT COUNT(*) FROM " + _ident(n)).fetchone()[0]
                traf.append(f"{n}: {ile} {_odm(ile, 'wiersz', 'wiersze', 'wierszy')}")
            except sqlite3.Error as e:
                traf.append(f"{n}: liczby wierszy nie odczytam ({_blad(e)})")
    finally:
        con.close()
    uwagi = [f"pokazuję {LIMIT_INNYCH} z {len(traf)}"] if len(traf) > LIMIT_INNYCH else []
    return _w(traf[:LIMIT_INNYCH], [], uwagi)


# ---------------------------------------------------------------- 4) Qdrant

async def _qdrant(slowa: list) -> dict:
    baza = QDRANT_URL.rstrip("/")
    d = await _pobierz(f"{baza}/collections")
    wynik = d.get("result") if isinstance(d, dict) else None
    kol = wynik.get("collections") if isinstance(wynik, dict) else None
    if not isinstance(kol, list):
        return _w(bledy=["Qdrant nie odpowiada poprawnie: brak result.collections w odpowiedzi /collections"])
    nazwy = sorted({x["name"] for x in kol if isinstance(x, dict) and isinstance(x.get("name"), str)})
    pasujace = [n for n in nazwy if _pasuje(n, slowa)]
    traf = []
    for n in pasujace[:LIMIT_INNYCH]:
        url = f"{baza}/collections/{quote(n, safe='')}"
        try:
            d2 = await _pobierz(url)
            r2 = d2.get("result") if isinstance(d2, dict) else None
            pc = r2.get("points_count") if isinstance(r2, dict) else None
            traf.append(f"{n} — points_count {pc}" if pc is not None else f"{n} — points_count: Qdrant nie podał")
        except Exception as e:  # noqa: BLE001
            traf.append(f"{n} — points_count: Qdrant nie odpowiada: {_blad(e)}")
    uwagi = [f"pokazuję {LIMIT_INNYCH} z {len(pasujace)}"] if len(pasujace) > LIMIT_INNYCH else []
    return _w(traf, [], uwagi)


# ---------------------------------------------------------------- 5) systemd

async def _systemd(slowa: list) -> dict:
    wyniki = await asyncio.gather(*(_proces(s[3]) for s in SYSTEMD), return_exceptions=True)
    jednostki, bledy = {}, []
    for (etykieta, zakres, timery, _argv), r in zip(SYSTEMD, wyniki):
        if isinstance(r, BaseException):
            bledy.append(f"{etykieta} nie odpowiada: {_blad(r)}")
            continue
        kod, out, err = r
        if kod != 0:
            bledy.append(f"{etykieta} nie odpowiada: {_opis(kod, err)}")
            continue
        for linia in out.splitlines():
            t = linia.split()
            if t and t[0] in ZNACZNIKI:
                t = t[1:]
            if not t:
                continue
            if timery:
                i = next((j for j, x in enumerate(t) if x.endswith(".timer")), None)
                if i is None or not _pasuje(t[i], slowa):
                    continue
                akt = t[i + 1] if i + 1 < len(t) else "?"
                klucz = (zakres, t[i])
                if klucz in jednostki:
                    jednostki[klucz] += f"; uruchamia {akt}"
                else:
                    jednostki[klucz] = f"{t[i]} [{zakres}; timer, uruchamia {akt}]"
            elif _pasuje(t[0], slowa):
                klucz = (zakres, t[0])
                if klucz not in jednostki:
                    stan, opis = " ".join(t[1:4]), " ".join(t[4:])
                    jednostki[klucz] = f"{t[0]} [{zakres}; {stan}]" + (f" — {opis}" if opis else "")
    traf = list(jednostki.values())
    uwagi = [f"pokazuję {LIMIT_INNYCH} z {len(traf)}"] if len(traf) > LIMIT_INNYCH else []
    return _w(traf[:LIMIT_INNYCH], bledy, uwagi)


# ---------------------------------------------------------------- składanie

async def _miejsce(etykieta: str, coro) -> dict:
    try:
        return await asyncio.wait_for(coro, CZAS)
    except asyncio.TimeoutError:
        return _w(bledy=[f"{etykieta} nie odpowiada: brak odpowiedzi w {CZAS:g} s"])
    except Exception as e:  # noqa: BLE001
        return _w(bledy=[f"{etykieta} nie odpowiada: {_blad(e)}"])


def _sekcja(nr: int, tytul: str, w: dict, slowa: list) -> list:
    li = [f"{nr}) {tytul}"]
    li += [f"  • {x}" for x in w["traf"]]
    if not w["traf"] and not w["bledy"]:
        li.append(f"  nic pod tą nazwą (szukane słowa: {', '.join(slowa)})")
    li += [f"  {x}" for x in w["bledy"]]
    li += [f"  ({x})" for x in w["uwagi"]]
    return li


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest zwiad organizmu (np. „zwiad rag_codebase”, "
                                       "„znajdź w organizmie executor”)."}
    slowa = _slowa(cmd, params or {})
    if not slowa:
        return {"ok": False, "reason": "Nie wiem, czego szukać — podaj nazwę dłuższą niż 3 znaki "
                                       "(np. „zwiad rag_codebase”). Niczego nie zgaduję."}
    wyniki = await asyncio.gather(
        _miejsce("os.walk", asyncio.to_thread(_pliki_sync, slowa)),
        _miejsce("grep", _grep(slowa)),
        _miejsce("baza", asyncio.to_thread(_tabele_sync, slowa)),
        _miejsce("Qdrant", _qdrant(slowa)),
        _miejsce("systemctl", _systemd(slowa)),
    )
    tytuly = (
        "Pliki .py .sh .md .json z tym słowem w nazwie (" + ", ".join(str(x) for x in KATALOGI.values()) + "):",
        f"grep -rIl -i -F w plikach .py pod {KATALOGI.get('app')}:",
        f"Tabele bazy {BAZA} (mode=ro):",
        f"Kolekcje Qdrant ({QDRANT_URL.rstrip('/')}/collections):",
        "Jednostki systemd (list-units --all, list-timers --all, --user list-units --all):",
    )
    li = [f"Zwiad organizmu na węźle .10 — szukane słowa: {', '.join(slowa)}."]
    for nr, (tytul, w) in enumerate(zip(tytuly, wyniki), 1):
        li += _sekcja(nr, tytul, w, slowa)
    li.append("Tylko odczyt, bez LLM — niczego nie zapisałem na dysk ani do bazy.")
    raport = "\n".join(li)
    if all(not w["traf"] and w["bledy"] for w in wyniki):
        return {"ok": False, "reason": "Żadne miejsce nie odpowiedziało — niczego nie zgaduję.\n" + raport}
    return {"ok": True, "reply": raport}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka zwiad_organizmu padła: {_blad(e)}"}


async def handler_zwiad_organizmu(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}
