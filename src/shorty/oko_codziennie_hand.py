# plik: oko_codziennie_hand.py
"""Codzienna obsługa OKA MASZYNY. Import nie wykonuje I/O.

CLI „przebieg” jest wejściem wykonawczym dla autoryzowanego timera.
Wszystkie operacje zewnętrzne przechodzą przez Klient _pobierz.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

WZORZEC = r"\A\s*(?:shadow[\s,:]+)?(?:zrób\s+odcinki\s+oka\s+maszyny|oko\s+maszyny\s+na\s+dziś|ile\s+odcinków\s+oka\s+maszyny\s+czeka\s+w\s+kolejce)(?:\s+(?:wykonaj|wyślij|zatwierdź))?\s*[.!]?\s*\Z"
GOTOWA = True

REPO = Path("/opt/shadow/shadow")
PYTHON = REPO / "backend/.venv/bin/python"
DB = REPO / "backend/shadowai.db"
EXPORT = REPO / "exports/oko_maszyny"
STAN = EXPORT / "stan_serii.json"
LOG = EXPORT / "typy_log.jsonl"
ZAMEK = EXPORT / ".oko_codziennie.lock"
KLIPY = Path("/mnt/usb_toshiba/Dziecko/highlights")
OLLAMA = "http://10.0.0.10:11434/api/generate"
STREFA = ZoneInfo("Europe/Warsaw")
ZGODA = re.compile(r"\b(?:wykonaj|wyślij|zatwierdź)\b", re.I)


class Odmowa(RuntimeError):
    pass


def _czytaj_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _czytaj_log():
    # Brak pliku nie oznacza pustej historii działającej serii.
    wpisy = []
    for nr, linia in enumerate(LOG.read_text(encoding="utf-8").splitlines(), 1):
        if not linia.strip():
            continue
        wpis = json.loads(linia)
        if not isinstance(wpis, dict):
            raise Odmowa(f"typy_log.jsonl: wiersz {nr} nie jest obiektem.")
        wpisy.append(wpis)
    return wpisy


def _wiersze():
    with sqlite3.connect(
        DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
    ) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row) for row in conn.execute(
                "SELECT title, channel, status, scheduled_at "
                "FROM publication_queue WHERE channel = ?",
                ("youtube",),
            )
        ]


def _proces(argv):
    """Argumenty są listą; bez powłoki, bez automatycznych ponowień."""
    try:
        wynik = subprocess.run(
            [str(x) for x in argv],
            cwd=str(REPO),
            timeout=3600,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Odmowa("Proces przekroczył limit 3600 s.") from exc
    return {
        "rc": wynik.returncode,
        "log": (wynik.stdout + "\n" + wynik.stderr)[-6000:],
    }


def _blokada():
    ZAMEK.parent.mkdir(parents=True, exist_ok=True)
    uchwyt = ZAMEK.open("a+", encoding="utf-8")
    try:
        fcntl.flock(uchwyt, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        uchwyt.close()
        raise Odmowa("Poprzedni przebieg OKA MASZYNY nadal trwa.") from exc
    except BaseException:
        uchwyt.close()
        raise
    # Pliku nie usuwamy: usunięcie umożliwiałoby blokowanie różnych inode.
    return uchwyt


def _odblokuj(uchwyt):
    try:
        fcntl.flock(uchwyt, fcntl.LOCK_UN)
    finally:
        uchwyt.close()


def _pliki_klipow():
    if not KLIPY.is_dir():
        raise Odmowa(f"Źródło klipów jest niedostępne: {KLIPY}.")
    return sorted(str(p) for p in KLIPY.glob("dz_*.mp4") if p.is_file())


async def _zwolnij_model():
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            OLLAMA,
            json={"model": "qwen2.5vl:7b", "keep_alive": 0},
        )
        response.raise_for_status()
        dane = response.json()
        if not isinstance(dane, dict) or dane.get("done") is not True:
            raise Odmowa("Ollama nie potwierdziła zwolnienia modelu.")
        return dane


def _io(op, *args):
    if op == "wiersze":
        return _wiersze()
    if op == "stan":
        return _czytaj_json(STAN)
    if op == "log":
        return _czytaj_log()
    if op == "klipy":
        return _pliki_klipow()
    if op == "proces":
        return _proces(args[0])
    if op == "blokada":
        return _blokada()
    if op == "odblokuj":
        return _odblokuj(args[0])
    if op == "teraz":
        return dt.datetime.now(dt.timezone.utc)
    raise Odmowa(f"Nieznana operacja: {op}.")


async def _pobierz(op, *args):
    if op == "zwolnij":
        return await _zwolnij_model()
    return await asyncio.to_thread(_io, op, *args)


def _termin(wartosc):
    if not isinstance(wartosc, str) or not wartosc.strip():
        raise Odmowa("Odcinek w kolejce nie ma poprawnego scheduled_at.")
    try:
        data = dt.datetime.fromisoformat(wartosc.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise Odmowa(f"Niepoprawny scheduled_at: {wartosc}.") from exc
    # Daty bez offsetu w publication_queue to UTC (sloty OKA: PL 18:30Z, EN 19:30Z — scripts/oko_kolejka.py).
    return data.replace(tzinfo=dt.timezone.utc) if data.tzinfo is None else data


def _odcinki_w_kolejce(wiersze):
    return [
        w for w in wiersze
        if w.get("channel") == "youtube"
        and "oko maszyny #" in str(w.get("title", "")).casefold()
    ]


def _plan(wiersze, teraz):
    odcinki = _odcinki_w_kolejce(wiersze)
    czekajace = [
        w for w in odcinki
        if w.get("status") in {"approved", "staged"}
        and _termin(w.get("scheduled_at")) > teraz
    ]
    dzien = teraz.astimezone(STREFA).date() + dt.timedelta(days=1)
    # Każdy istniejący termin OKA rezerwuje dzień, niezależnie od statusu.
    zajete = {
        _termin(w["scheduled_at"]).astimezone(STREFA).date()
        for w in odcinki if w.get("scheduled_at")
    }
    while dzien in zajete:
        dzien += dt.timedelta(days=1)
    return len(czekajace), max(0, 3 - len(czekajace)), dzien.isoformat()


def _nieujemna(wartosc):
    return type(wartosc) is int and wartosc >= 0


def _tablica(stan):
    if not isinstance(stan, dict):
        raise Odmowa("stan_serii.json nie zawiera obiektu.")
    for obiekt in (
        stan,
        *(stan.get(k) for k in ("tablica", "wynik", "score", "scoreboard")),
    ):
        if isinstance(obiekt, dict):
            pola = {str(k).casefold(): v for k, v in obiekt.items()}
            oko = pola.get("oko")
            jas = pola.get("dziecko", pola.get("dziecko"))
            if _nieujemna(oko) and _nieujemna(jas):
                return f"OKO {oko} : {jas} Dziecko"
        if isinstance(obiekt, str):
            m = re.fullmatch(
                r"\s*OKO\s+(\d+)\s*:\s*(\d+)\s+JA[ŚS]\s*",
                obiekt, re.I,
            ) or re.fullmatch(
                r"\s*OKO\s+(\d+)\s*:\s*JA[ŚS]\s+(\d+)\s*",
                obiekt, re.I,
            )
            if m:
                return f"OKO {int(m[1])} : {int(m[2])} Dziecko"
    raise Odmowa("Brak rozpoznawalnej tablicy OKO/Dziecko w stan_serii.json.")


def _opis_wpisu(wpis):
    numer = next(
        (wpis[k] for k in ("nr", "numer", "odcinek", "episode", "episode_no")
         if k in wpis),
        None,
    )
    if isinstance(numer, str):
        m = re.fullmatch(r"(?:OKO MASZYNY\s*)?#?(\d+)", numer.strip(), re.I)
        numer = int(m[1]) if m else None
    typ = wpis.get("typ", wpis.get("type"))
    if type(numer) is not int or numer < 1:
        raise Odmowa("Nowy wpis typy_log.jsonl nie zawiera numeru odcinka.")
    if not isinstance(typ, str) or not typ.strip():
        raise Odmowa(f"Brak typu odcinka #{numer} w typy_log.jsonl.")
    return numer, typ.strip()


def _nazwy_klipow(obiekt):
    wynik = set()
    if isinstance(obiekt, str):
        wynik.update(re.findall(r"dz_[^/\\\s\"<>]+\.mp4", obiekt, re.I))
    elif isinstance(obiekt, dict):
        for wartosc in obiekt.values():
            wynik.update(_nazwy_klipow(wartosc))
    elif isinstance(obiekt, list):
        for wartosc in obiekt:
            wynik.update(_nazwy_klipow(wartosc))
    return wynik


def _ewidencja_klipow(stan, log):
    """Brak ewidencji nie jest dowodem, że wszystkie klipy są nieużyte."""
    uzyte = _nazwy_klipow(stan) | _nazwy_klipow(log)
    jawna_lista = any(
        isinstance(stan.get(k), list)
        for k in ("uzyte_klipy", "used_clips", "used")
    )
    if not uzyte and not jawna_lista:
        raise Odmowa(
            "Brak ewidencji użytych klipów w stanie i logu; "
            "nie mogę uczciwie wskazać nieużytych materiałów."
        )
    return uzyte


async def _podglad(cmd):
    teraz = await _pobierz("teraz")
    ile, brak, od = _plan(await _pobierz("wiersze"), teraz)
    stan = await _pobierz("stan")
    tablica = _tablica(stan)
    tekst = (
        f"PODGLĄD: w przyszłej kolejce PL czeka {ile} odcinków; "
        f"do utworzenia: {brak}. Pierwszy wolny dzień: {od}.\n{tablica}"
    )
    if re.search(r"\bile\s+odcinków\b", cmd, re.I) or not brak:
        return {"ok": True, "reply": tekst}
    log = await _pobierz("log")
    uzyte = _ewidencja_klipow(stan, log)
    kandydaci = [
        p for p in await _pobierz("klipy")
        if Path(p).name not in uzyte
    ]
    if not kandydaci:
        raise Odmowa(f"{tekst}\nBrak nieużytych klipów w źródle.")
    # --auto wybiera materiały wewnątrz skryptu. Nie udajemy jego wyniku.
    return {
        "ok": True,
        "reply": (
            tekst
            + f"\nDostępne nieużyte klipy: {len(kandydaci)}; 10 najnowszych:\n"
            + "\n".join(Path(p).name for p in kandydaci[-10:])
            + "\nTo pula materiałów; ostateczny wybór wykonuje skrypt --auto."
            + "\nDodaj „wykonaj”, aby uruchomić generowanie i kolejkę PL/EN."
        ),
    }


def _sprawdz_proces(wynik, nazwa):
    if not isinstance(wynik, dict) or wynik.get("rc") != 0:
        log = wynik.get("log", "") if isinstance(wynik, dict) else str(wynik)
        rc = wynik.get("rc") if isinstance(wynik, dict) else "brak"
        raise Odmowa(f"{nazwa}: rc={rc}; {log}")


async def _przebieg_pod_blokada():
    teraz = await _pobierz("teraz")
    ile, brak, _ = _plan(await _pobierz("wiersze"), teraz)
    if not brak:
        return {
            "ok": True,
            "reply": (
                f"W kolejce czeka {ile} odcinków. Nowych odcinków: 0.\n"
                + _tablica(await _pobierz("stan"))
            ),
        }

    stare = await _pobierz("log")
    # Sprawdzenie źródła przed uruchomieniem kosztownego procesu.
    _tablica(await _pobierz("stan"))
    wynik = None
    blad_zwolnienia = None
    try:
        try:
            generator = await _pobierz("proces", [
                PYTHON, "scripts/oko_maszyny_odcinek.py",
                "--auto", "--ile", str(brak),
            ])
            _sprawdz_proces(generator, "Generator OKA MASZYNY")
            # Ponowny odczyt uwzględnia zmiany kolejki podczas renderowania.
            _, _, od = _plan(
                await _pobierz("wiersze"), await _pobierz("teraz")
            )
            kolejka = await _pobierz("proces", [
                PYTHON, "scripts/oko_kolejka.py", "--od", od,
            ])
            _sprawdz_proces(kolejka, "Kolejkowanie OKA MASZYNY")

            nowe = await _pobierz("log")
            if nowe[:len(stare)] != stare:
                raise Odmowa("Historia typy_log.jsonl zmieniła się nieprzyrostowo.")
            # typy_log.jsonl ma na odcinek wpis typu (czas_typu + typ) i wpis oceny (czas_oceny, bez typ), bywają też
            # UNIEWAZNIONY/PRZELICZONY — odcinki liczymy po wpisach typu; ostatni typ danego numeru wygrywa (Claude 21.10).
            typy = {}
            for w in nowe[len(stare):]:
                if w.get("typ") and not any(k in w for k in ("czas_oceny", "UNIEWAZNIONY", "PRZELICZONY")):
                    nr, typ = _opis_wpisu(w)
                    typy[nr] = typ
            opisy = sorted(typy.items())
            if len(opisy) != brak:
                raise Odmowa(
                    f"Zlecono {brak} odcinków, ale log nie potwierdza "
                    "tej liczby unikalnych nowych odcinków."
                )
            tablica = _tablica(await _pobierz("stan"))
            wynik = {
                "ok": True,
                "reply": (
                    f"Nowych odcinków: {len(opisy)}.\n"
                    + "\n".join(f"#{nr}: {typ}" for nr, typ in opisy)
                    + f"\n{tablica}\n"
                    f"Skrypt kolejki PL/EN zakończył się poprawnie; --od {od}."
                ),
            }
        except Exception as exc:
            wynik = {
                "ok": False,
                "reason": (
                    f"Przebieg przerwany: {type(exc).__name__}: {exc}. "
                    "Część plików lub wpisów kolejki mogła już powstać; "
                    "sprawdź stan przed ponowieniem."
                ),
            }
    finally:
        # Także po rc != 0, timeout lub błędzie kolejki.
        try:
            await _pobierz("zwolnij")
        except Exception as exc:
            blad_zwolnienia = (
                "Nie udało się zwolnić modelu qwen2.5vl:7b na .10: "
                f"{type(exc).__name__}: {exc}"
            )

    if blad_zwolnienia:
        szczegoly = wynik.get("reply", wynik.get("reason", ""))
        return {"ok": False, "reason": szczegoly + "\n" + blad_zwolnienia}
    if wynik["ok"]:
        wynik["reply"] += "\nModel oka na .10 został zwolniony."
    return wynik


async def przebieg() -> dict:
    uchwyt = None
    wynik = None
    try:
        uchwyt = await _pobierz("blokada")
        wynik = await _przebieg_pod_blokada()
    except Exception as exc:
        wynik = {
            "ok": False,
            "reason": (
                "Źródło danych lub usługa nie odpowiada albo zwróciła błąd: "
                f"{type(exc).__name__}: {exc}"
            ),
        }
    finally:
        if uchwyt is not None:
            try:
                await _pobierz("odblokuj", uchwyt)
            except Exception as exc:
                szczegoly = wynik or {}
                wynik = {
                    "ok": False,
                    "reason": (
                        szczegoly.get("reply", szczegoly.get("reason", ""))
                        + f"\nBłąd zwalniania zamka: {exc}"
                    ),
                }
    return wynik


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(params, dict) or not isinstance(cmd, str):
        return {"ok": False, "reason": "params musi być słownikiem, cmd tekstem."}
    if len(cmd) > 500 or not re.fullmatch(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "Nie rozpoznaję polecenia OKA MASZYNY."}
    # Pytanie o kolejkę pozostaje odczytem również z dopiskiem zgody.
    if ZGODA.search(cmd) and not re.search(r"\bile\s+odcinków\b", cmd, re.I):
        return await przebieg()
    try:
        return await _podglad(cmd)
    except Exception as exc:
        return {
            "ok": False,
            "reason": (
                "Nie można przygotować podglądu: źródło danych "
                f"nie odpowiada lub jest niepoprawne: {type(exc).__name__}: {exc}"
            ),
        }


if __name__ == "__main__":
    if sys.argv[1:] != ["przebieg"]:
        raise SystemExit("Użycie: python -m app.services.oko_codziennie_hand przebieg")
    rezultat = asyncio.run(przebieg())
    print(json.dumps(rezultat, ensure_ascii=False))
    raise SystemExit(0 if rezultat["ok"] else 1)

SLUG = 'oko_codziennie'


async def start(params: dict | None = None, cmd: str = "") -> dict:
    """Wejście dopisane przez kuźnię — handler woła start(), ręka ma wykonaj()."""
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka {SLUG} padła: {exc}"}
