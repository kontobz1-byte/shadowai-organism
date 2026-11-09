# plik: shorty_gaming_2_hand.py
"""Montaż i kolejkowanie Shortów Fortnite. Import nie wykonuje operacji I/O."""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import fcntl
import importlib.util
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path

import httpx

WZORZEC = r"^\s*(?:shadow[\s,:]+)?(?:zrób\s+shorty\s+z\s+gry|shorty\s+gaming\s+z\s+highlightów)(?:\s+(?P<zgoda>wykonaj|wyślij|zatwierdź))?\s*[.!?]*\s*$"
GOTOWA = True
SLUG = "shorty_gaming_2"
CAPABILITY = "shorty_gaming"

REPO = Path("/opt/shadow/shadow")
BACKEND = REPO / "backend"
PYTHON = BACKEND / ".venv/bin/python"
BAZA = BACKEND / "shadowai.db"
UZYTE = BACKEND / "data/shorty_gaming_uzyte.json"
EXPORT = REPO / "exports/shorty_gaming"
PION = REPO / "scripts/pion_ostry.py"
FONT = Path("/opt/shadow/.local/share/fonts/film/BebasNeue-Regular.ttf")
KLUCZ = "/opt/shadow/.ssh/shadowai_gpu"
HOST = "user@example.com"
ZDALNY = "D:/_shadowtest/Highlights/Fortnite"
MODEL_URL = "http://10.0.0.130:11434/api/generate"
MODEL = "gemma4:12b"
KANALY = {"youtube": "PL", "youtube_gaming_en": "EN"}
AKTYWNE = {"approved", "staged", "needs_operator"}
WŁASNE = {
    "fortnite", "epic", "games", "battle", "royale", "tilted", "towers",
    "jonesy", "peely", "slurp", "loot", "noob", "gg",
}
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p,crop=2560:1440"
)
NAZWA = re.compile(
    r"Fortnite (?P<data>\d{4}\.\d{2}\.\d{2}) - "
    r"(?P<czas>\d{2}\.\d{2}\.\d{2})\.(?P<numer>\d+)\."
    r"(?P<zdarzenie>[^/\\]+)\.DVR\.mp4", re.I,
)


class Odmowa(Exception):
    pass


# Granice systemu: procesy, HTTP, baza, pliki, import narzędzia i WhatsApp.
def _proces(args: list, stdin: str | None = None, timeout: int = 600) -> str:
    try:
        p = subprocess.run(
            list(map(str, args)), input=stdin, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Odmowa(f"Narzędzie {args[0]} nie odpowiedziało: {exc}") from exc
    if p.returncode:
        raise Odmowa(
            f"{args[0]} zakończył się kodem {p.returncode}: "
            f"{(p.stderr or p.stdout)[-2000:]}"
        )
    return p.stdout + "\n" + p.stderr


async def _http(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=180, trust_env=False) as klient:
        r = await klient.post(MODEL_URL, json=payload)
        r.raise_for_status()
        wynik = r.json()
    if not isinstance(wynik, dict):
        raise Odmowa("Gemma nie zwróciła obiektu JSON.")
    return wynik


@contextlib.contextmanager
def _baza(zapis: bool = False):
    # mode=rw nie tworzy pustej bazy przy błędnej ścieżce.
    conn = sqlite3.connect(
        f"file:{BAZA}?mode={'rw' if zapis else 'ro'}",
        uri=True, timeout=30, isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _czytaj(plik: Path, domyslnie=None):
    try:
        return json.loads(plik.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        if domyslnie is not None:
            return domyslnie
        raise Odmowa(f"Brak pliku źródłowego: {plik}")
    except (OSError, ValueError) as exc:
        raise Odmowa(f"Nie można odczytać {plik}: {exc}") from exc


def _zapisz(plik: Path, dane) -> None:
    plik.parent.mkdir(parents=True, exist_ok=True)
    nazwa = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=plik.parent, delete=False,
        ) as f:
            nazwa = Path(f.name)
            json.dump(dane, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
        nazwa.replace(plik)
    finally:
        if nazwa is not None:
            nazwa.unlink(missing_ok=True)


def _tekst(plik: Path, tekst: str) -> None:
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_text(tekst, encoding="utf-8")


def _mkdir(plik: Path) -> None:
    plik.mkdir(parents=True, exist_ok=True)


def _plik(plik: Path) -> bool:
    return plik.is_file() and plik.stat().st_size > 0


@contextlib.contextmanager
def _blokada():
    plik = BACKEND / "data/shorty_gaming.lock"
    plik.parent.mkdir(parents=True, exist_ok=True)
    with plik.open("a+b") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Odmowa("Inny przebieg shorty gaming już trwa.") from exc
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _pion(wideo: Path, out: Path, sek: float) -> dict:
    spec = importlib.util.spec_from_file_location("shadow_pion_ostry", PION)
    if spec is None or spec.loader is None:
        raise Odmowa(f"Nie można załadować modułu pionu: {PION}")
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    wynik = modul.zrob_pion(
        str(wideo), str(out), sek=sek, audio=None, ass=None,
        start=0, uklad="pelny", fade_in=0, fade_out=0,
    )
    if not isinstance(wynik, dict):
        raise Odmowa("zrob_pion nie zwrócił słownika wyniku.")
    if wynik.get("ok") is False or wynik.get("error"):
        raise Odmowa(f"zrob_pion: {wynik}")
    return wynik


def _rh(plik: Path, sek: float) -> float:
    import cv2

    cap = cv2.VideoCapture(str(plik))
    try:
        if not cap.isOpened():
            raise Odmowa(f"Nie można otworzyć obrazu do oceny: {plik}")
        cap.set(cv2.CAP_PROP_POS_MSEC, sek * 500)
        ok, obraz = cap.read()
        if not ok:
            raise Odmowa("Nie można odczytać środkowej klatki.")
        szary = cv2.cvtColor(obraz, cv2.COLOR_BGR2GRAY)
        maly = cv2.resize(
            szary, (szary.shape[1] // 2, szary.shape[0] // 2),
            interpolation=cv2.INTER_AREA,
        )
        licznik = float(cv2.Laplacian(szary, cv2.CV_64F).var())
        mianownik = float(cv2.Laplacian(maly, cv2.CV_64F).var())
        if mianownik <= 0:
            raise Odmowa("Nie można obliczyć rh: zerowa wariancja odniesienia.")
        return licznik / mianownik
    finally:
        cap.release()


async def _wa(tekst: str) -> None:
    from app.services.wa_documents import OPERATOR_WA, send_wa_text

    await send_wa_text(OPERATOR_WA, tekst)


def _teraz() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ssh(skrypt: str) -> str:
    return _proces(
        [
            "ssh", "-i", KLUCZ, "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
            HOST, "powershell -NoProfile -Command -",
        ],
        stdin=(
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
            + skrypt + "\n\n"
        ),
        timeout=45,
    ).strip()


def _stan_zdalny() -> dict:
    tekst = _ssh(
        "$gra = @(Get-Process FortniteClient*, EasyAntiCheat* "
        "-ErrorAction SilentlyContinue)\n"
        "if ($gra.Count -gt 0) {\n"
        "  @{gra=$true; pliki=@()} | ConvertTo-Json -Compress\n"
        "} else {\n"
        "  $pliki = @(Get-ChildItem -LiteralPath "
        "'D:\\_shadowtest\\Highlights\\Fortnite' -Filter '*.mp4' "
        "-File | ForEach-Object {$_.Name})\n"
        "  @{gra=$false; pliki=$pliki} | ConvertTo-Json -Compress\n"
        "}\n"
    )
    try:
        dane = json.loads(tekst)
    except ValueError as exc:
        raise Odmowa(".253 nie zwrócił poprawnego stanu JSON.") from exc
    if (
        not isinstance(dane, dict) or type(dane.get("gra")) is not bool
        or not isinstance(dane.get("pliki"), list)
        or not all(isinstance(s, str) for s in dane["pliki"])
    ):
        raise Odmowa(".253 nie zwrócił kompletnego stanu gry i plików.")
    return dane


def _pobierz_klip(nazwa: str, cel: Path) -> None:
    # Ponowna kontrola tuż przed każdym transferem.
    if _stan_zdalny()["gra"]:
        raise Odmowa("Gra działa na .253. Czekam; nie pobieram klipu.")
    if NAZWA.fullmatch(nazwa) is None:
        raise Odmowa("Niepoprawna nazwa klipu do pobrania.")
    _proces(
        [
            "scp", "-i", KLUCZ, "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
            f"{HOST}:{ZDALNY}/{nazwa}", cel,
        ],
        timeout=300,
    )
    if not _plik(cel):
        raise Odmowa(f"SCP nie dostarczyło pliku {nazwa}.")


def _kolejka() -> list[dict]:
    with _baza() as conn:
        return [
            dict(r) for r in conn.execute("SELECT * FROM publication_queue")
        ]


def _normalizuj(tekst: str) -> str:
    tekst = re.sub(r"#shorts\b", "", tekst, flags=re.I)
    tekst = tekst.casefold().replace("ł", "l")
    tekst = unicodedata.normalize("NFKD", tekst)
    return "".join(z for z in tekst if z.isalnum())


def _termin(wiersz: dict) -> dt.datetime | None:
    try:
        wartosc = dt.datetime.fromisoformat(
            str(wiersz.get("scheduled_at", "")).replace("Z", "+00:00")
        )
        if wartosc.tzinfo is None:
            wartosc = wartosc.replace(tzinfo=dt.timezone.utc)
        return wartosc.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def _plan(wiersze: list[dict], teraz: dt.datetime) -> dict:
    wynik = {}
    for kanal in KANALY:
        zajete = set()
        nadchodzace = 0
        for r in wiersze:
            if r.get("channel") != kanal:
                continue
            termin = _termin(r)
            if (
                r.get("status") in AKTYWNE
                and "#shorts" in str(r.get("title", "")).casefold()
            ):
                if termin is None:
                    raise Odmowa(
                        f"Kolejka {kanal}: short bez poprawnego scheduled_at."
                    )
                zajete.add(termin.date())
            if (
                r.get("capability") == CAPABILITY
                and r.get("status") in AKTYWNE
            ):
                if termin is None:
                    raise Odmowa(
                        f"Kolejka {kanal}: brak daty shorta tej ręki."
                    )
                if termin >= teraz:
                    nadchodzace += 1
        ile = min(3, max(0, 5 - nadchodzace))
        dzien = teraz.date() + dt.timedelta(days=1)
        terminy = []
        while len(terminy) < ile:
            if dzien not in zajete:
                terminy.append(f"{dzien.isoformat()}T16:00")
            dzien += dt.timedelta(days=1)
        wynik[kanal] = {
            "nadchodzace": nadchodzace, "terminy": terminy,
        }
    return wynik


def _zrodla_z_kolejki(wiersze: list[dict]) -> set[str]:
    wynik = set()
    for r in wiersze:
        if r.get("capability") != CAPABILITY:
            continue
        try:
            notes = json.loads(r.get("notes") or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(notes, dict):
            zrodla = notes.get("zrodla", [])
            if isinstance(zrodla, list):
                wynik.update(x for x in zrodla if isinstance(x, str))
    return wynik


def _klipy(nazwy: list[str], uzyte: set[str]) -> list[dict]:
    wynik = []
    for nazwa in sorted(set(nazwy)):
        m = NAZWA.fullmatch(nazwa)
        if m is None or nazwa in uzyte:
            continue
        try:
            czas = dt.datetime.strptime(
                f"{m['data']} {m['czas']}", "%Y.%m.%d %H.%M.%S"
            )
        except ValueError:
            continue
        if (
            czas.date() == dt.date(2026, 10, 16)
            and czas.time() <= dt.time(10, 30)
        ):
            continue
        zdarzenie = m["zdarzenie"].casefold()
        if "zwycięstwo" in zdarzenie:
            priorytet = 0
        elif "odniesione likwidacje" in zdarzenie:
            priorytet = 1
        elif "likwidacja" in zdarzenie:
            priorytet = 2
        else:
            continue
        wynik.append({
            "nazwa": nazwa, "czas": czas, "zdarzenie": m["zdarzenie"],
            "priorytet": priorytet,
        })
    return sorted(wynik, key=lambda k: (k["czas"], k["nazwa"]))


def _probe(plik: Path) -> dict:
    tekst = _proces(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", plik,
        ], timeout=60,
    )
    try:
        dane = json.loads(tekst)
        strumienie = dane["streams"]
        wideo = next(s for s in strumienie if s["codec_type"] == "video")
        sek = float(dane["format"]["duration"])
        if not math.isfinite(sek) or sek <= 0:
            raise ValueError("niepoprawna długość")
        return {
            "sek": sek, "w": int(wideo["width"]), "h": int(wideo["height"]),
            "audio": any(s["codec_type"] == "audio" for s in strumienie),
        }
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise Odmowa(f"ffprobe: brak wiarygodnych danych dla {plik}.") from exc


def _szczyt(plik: Path) -> float:
    tekst = _proces(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-i", plik,
            "-vn", "-af", "astats=metadata=0:reset=0",
            "-f", "null", "-",
        ], timeout=300,
    )
    wartosci = re.findall(
        r"Peak level dB:\s*(-?inf|[-+]?\d+(?:\.\d+)?)", tekst, re.I,
    )
    if not wartosci:
        raise Odmowa(f"astats nie zwrócił szczytu dźwięku: {plik.name}")
    liczby = [float(v) for v in wartosci]
    if any(math.isnan(v) for v in liczby):
        raise Odmowa(f"astats zwrócił NaN: {plik.name}")
    return max(liczby)


def _grupy_leniwe(klipy: list[dict], limit: int, bledy: list[str]):
    """Najpierw nazwy; pomiary tylko w granicach budżetu źródeł."""
    ranking = sorted(
        klipy, key=lambda k: (k["priorytet"], k["czas"], k["nazwa"]),
    )
    likwidacje = [k for k in ranking if k["priorytet"] == 2][:12]
    do_pomiaru = {k["nazwa"] for k in likwidacje}
    indeks = {k["nazwa"]: i for i, k in enumerate(klipy)}
    sprawdzone = {}
    zajete = set()
    pozostale = 2 * limit
    zrodla = EXPORT / "_zrodla"
    _mkdir(zrodla)

    def przygotuj(klip):
        nonlocal pozostale
        nazwa = klip["nazwa"]
        if nazwa in sprawdzone:
            return sprawdzone[nazwa]
        if klip["priorytet"] == 2:
            if nazwa not in do_pomiaru:
                return None
        else:
            if pozostale <= 0:
                return None
            pozostale -= 1
        plik = zrodla / nazwa
        if not _plik(plik):
            _pobierz_klip(nazwa, plik)
        info = _probe(plik)
        sprawdzone[nazwa] = None
        if not info["audio"] or (info["w"], info["h"]) not in {
            (5120, 1440), (3840, 1080),
        }:
            bledy.append(
                f"Pominięto {nazwa}: wymiary {info['w']}×{info['h']}, "
                f"audio={info['audio']}."
            )
            return None
        wynik = {**klip, "plik": plik, "sek": info["sek"]}
        if klip["priorytet"] == 2:
            wynik["szczyt"] = _szczyt(plik)
        sprawdzone[nazwa] = wynik
        return wynik

    def kandydaci():
        yield from (k for k in ranking if k["priorytet"] != 2)
        zmierzone = []
        for klip in likwidacje:
            if klip["nazwa"] not in zajete:
                wynik = przygotuj(klip)
                if wynik is not None:
                    zmierzone.append(wynik)
        yield from sorted(
            zmierzone, key=lambda k: (-k["szczyt"], k["czas"], k["nazwa"]),
        )

    ile = 0
    for kandydat in kandydaci():
        if kandydat["nazwa"] in zajete:
            continue
        klip = przygotuj(kandydat)
        if klip is None:
            continue
        grupa = [klip]
        if klip["sek"] < 15:
            i = indeks[klip["nazwa"]]
            for j in (i - 1, i + 1):
                if not 0 <= j < len(klipy):
                    continue
                sasiad = klipy[j]
                if (
                    sasiad["nazwa"] in zajete
                    or abs((sasiad["czas"] - klip["czas"]).total_seconds()) > 180
                ):
                    continue
                drugi = przygotuj(sasiad)
                if drugi is not None and 15 <= klip["sek"] + drugi["sek"] <= 45:
                    grupa.append(drugi)
                    break
            if len(grupa) == 1:
                continue
        grupa.sort(key=lambda k: k["czas"])
        zajete.update(k["nazwa"] for k in grupa)
        yield grupa
        ile += 1
        if ile >= limit:
            return


def _grupy(klipy: list[dict]) -> list[list[dict]]:
    """Sąsiedztwo określa kolejność klipów, a nie ich ranking."""
    indeks = {k["nazwa"]: i for i, k in enumerate(klipy)}
    ranking = sorted(
        klipy,
        key=lambda k: (
            k["priorytet"],
            -k.get("szczyt", -math.inf) if k["priorytet"] == 2 else 0,
            k["czas"],
        ),
    )
    zajete = set()
    wynik = []
    for klip in ranking:
        if klip["nazwa"] in zajete:
            continue
        grupa = [klip]
        if klip["sek"] < 15:
            i = indeks[klip["nazwa"]]
            sasiedzi = []
            for j in (i - 1, i + 1):
                if 0 <= j < len(klipy):
                    drugi = klipy[j]
                    odstep = abs((drugi["czas"] - klip["czas"]).total_seconds())
                    if (
                        drugi["nazwa"] not in zajete and odstep <= 180
                        and 15 <= klip["sek"] + drugi["sek"] <= 45
                    ):
                        sasiedzi.append(drugi)
            if not sasiedzi:
                continue
            grupa.append(min(sasiedzi, key=lambda k: k["czas"]))
        grupa.sort(key=lambda k: k["czas"])
        wynik.append(grupa)
        zajete.update(k["nazwa"] for k in grupa)
    return wynik


def _pisownia(tekst: str, jezyk: str) -> list[str]:
    slowa = re.findall(r"[^\W\d_]+(?:[-’][^\W\d_]+)*", tekst, re.UNICODE)
    slowa = [s for s in slowa if s.casefold() not in WŁASNE]
    if not slowa:
        return []
    wynik = _proces(
        ["hunspell", "-d", "pl_PL" if jezyk == "PL" else "en_US", "-l"],
        stdin="\n".join(slowa) + "\n", timeout=30,
    )
    return sorted({
        s.strip() for s in wynik.splitlines()
        if s.strip() and s.strip().casefold() not in WŁASNE
    })


def _waliduj_meta(dane, jezyk: str, tytuly: set[str]) -> dict:
    if not isinstance(dane, dict):
        raise Odmowa("Metadane nie są obiektem JSON.")
    for pole in ("hook", "title", "description"):
        if not isinstance(dane.get(pole), str) or not dane[pole].strip():
            raise Odmowa(f"Metadane: brak pola {pole}.")
    hook = dane["hook"].strip()
    tytul = re.sub(r"\s*#shorts\s*$", "", dane["title"], flags=re.I).strip()
    if len(hook) > 90 or len(tytul + " #shorts") > 100:
        raise Odmowa("Hak lub tytuł jest za długi.")
    if "\n" in hook or "\n" in tytul:
        raise Odmowa("Hak i tytuł muszą mieścić się w jednym wierszu.")
    if re.search(r"\b(?:AI|model\w*|Claude)\b", tytul, re.I):
        raise Odmowa("Tytuł zawiera zabronione słowo.")
    if any(
        unicodedata.category(z) in {"So", "Cs", "Cc"}
        or z in "\ufe0f\u200d\u20e3"
        for z in tytul
    ):
        raise Odmowa("Tytuł zawiera emoji lub znak sterujący.")
    if not _normalizuj(tytul) or _normalizuj(tytul) in tytuly:
        raise Odmowa("Tytuł jest pusty lub powtarza tytuł z kolejki.")
    tagi = dane.get("tags")
    if (
        not isinstance(tagi, list) or not 3 <= len(tagi) <= 5
        or not all(
            isinstance(t, str) and re.fullmatch(r"#[^\W_]\w*", t)
            for t in tagi
        )
        or len({t.casefold() for t in tagi}) != len(tagi)
    ):
        raise Odmowa("Model musi zwrócić 3–5 różnych hashtagów.")
    bledy = _pisownia(hook + "\n" + tytul, jezyk)
    if bledy:
        raise Odmowa("Hunspell odrzucił słowa: " + ", ".join(bledy))
    return {
        "hook": hook, "title": tytul + " #shorts",
        "description": dane["description"].strip(), "tags": tagi,
    }


WIZJA = "qwen2.5vl:7b"
WIZJA_URL = "http://10.0.0.10:11434/api/generate"
KLATKI_ULAMKI = (0.25, 0.50, 0.85)


def _klatki(montaz) -> list[bytes]:
    """Pobiera trzy klatki JPEG z montażu przez ffmpeg."""
    import subprocess

    sciezka = str(montaz)
    wynik = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", sciezka,
        ],
        capture_output=True, check=True, timeout=60,
    )
    dlugosc = float(wynik.stdout.decode("utf-8", "replace").strip())
    if dlugosc <= 0:
        raise ValueError("Montaż bez długości.")

    klatki = []
    for ulamek in KLATKI_ULAMKI:
        wynik = subprocess.run(
            [
                "ffmpeg", "-v", "error", "-ss", f"{dlugosc * ulamek:.3f}",
                "-i", sciezka, "-frames:v", "1", "-vf", "scale=640:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            capture_output=True, check=True, timeout=60,
        )
        if not wynik.stdout:
            raise ValueError("ffmpeg nie zwrócił klatki.")
        klatki.append(wynik.stdout)
    return klatki


async def _opis_sceny(montaz) -> str:
    """Zwraca krótki opis sceny po angielsku albo pusty tekst."""
    import asyncio
    import base64

    try:
        klatki = await asyncio.to_thread(_klatki, montaz)
        payload = {
            "model": WIZJA, "stream": False, "keep_alive": 0,
            "prompt": (
                "These are three frames from one Fortnite gameplay clip. "
                "In one English sentence describe only what is visible: "
                "location, action, weapon, and vehicle. Do not guess."
            ),
            "images": [
                base64.b64encode(klatka).decode("ascii")
                for klatka in klatki
            ],
        }
        async with httpx.AsyncClient(timeout=180, trust_env=False) as klient:
            r = await klient.post(WIZJA_URL, json=payload)
            r.raise_for_status()
            wynik = r.json()
        return " ".join(str(wynik["response"]).split())[:300]
    except Exception:
        return ""


async def _metadane(
    grupa: list[dict], jezyk: str, tytuly: set[str],
    montaz=None, scena: str | None = None,
) -> dict:
    if scena is None:
        scena = await _opis_sceny(montaz) if montaz is not None else ""

    zrodlo = [
        {"event": k["zdarzenie"], "time": k["czas"].isoformat()}
        for k in grupa
    ]
    if scena:
        zrodlo.append({"scene": scena})

    blad = ""
    for _ in range(5):
        wynik = await _http({
            "model": MODEL, "stream": False, "think": False, "format": "json",
            "options": {"seed": uuid.uuid4().int % 2_147_483_647},
            "prompt": (
                "Create metadata for a Fortnite gaming short. "
                f"Language: {'Polish' if jezyk == 'PL' else 'English'}. "
                "Return JSON: hook (max 90 characters), title (max 92 "
                "characters, no emoji, no AI/model/Claude), description, "
                "tags (3–5 distinct hashtags beginning with #). "
                "Use correctly spelled dictionary words; do not invent kills, "
                "scores, weapons or dialogue. Base claims only on the supplied "
                "event names, recording times"
                + (" and the scene description from sampled montage frames"
                   if scena else "")
                + ". Do not include #shorts in the title; it will be appended. "
                "Source data:\n"
                + json.dumps(zrodlo, ensure_ascii=False)
                + "\nPrevious validation error: " + blad
                + "\nForbidden normalized titles: "
                + json.dumps(sorted(tytuly), ensure_ascii=False)
            ),
        })
        try:
            dane = json.loads(wynik["response"])
            return _waliduj_meta(dane, jezyk, tytuly)
        except (KeyError, ValueError, TypeError, Odmowa) as exc:
            blad = str(exc)
    raise Odmowa("Gemma: pięć odrzuconych prób metadanych. " + blad)


def _ass(hook: str) -> str:
    # Ucieczki ASS uniemożliwiają wstawienie tagów przez wygenerowany tekst.
    hook = hook.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Hak,Bebas Neue,76,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&H00000000,0,0,0,0,100,100,0,0,1,4,0,8,70,70,150,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:02.50,Hak,,0,0,0,,{hook}\n"
    )


def _montuj(grupa: list[dict], katalog: Path, slug: str) -> tuple[Path, float]:
    czesci = []
    filtr = TONEMAP.replace(
        "crop=2560:1440", "crop=iw/2:ih,scale=2560:1440:flags=lanczos",
    ) + ",setsar=1,fps=60"
    for i, klip in enumerate(grupa):
        cel = katalog / f"{slug}_sdr_{i}.mp4"
        _proces([
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", klip["plik"],
            "-t", str(min(45.0, klip["sek"])),
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", filtr,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", cel,
        ], timeout=1800)
        if not _plik(cel):
            raise Odmowa(f"Brak znormalizowanej części: {cel}")
        czesci.append(cel)
    baza = katalog / f"{slug}_baza.mp4"
    args = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    for plik in czesci:
        args += ["-i", plik]
    wejscia = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(len(czesci)))
    _proces([
        *args, "-filter_complex",
        f"{wejscia}concat=n={len(czesci)}:v=1:a=1[v][a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "18",
        "-c:a", "aac", baza,
    ], timeout=1800)
    info = _probe(baza)
    if not 15 <= info["sek"] <= 45 or not info["audio"]:
        raise Odmowa("Po połączeniu: wymagane 15–45 s i dźwięk gry.")
    pion = katalog / f"{slug}_pion.mp4"
    _pion(baza, pion, info["sek"])
    if not _plik(pion):
        raise Odmowa("Moduł pionu nie utworzył filmu.")
    return pion, info["sek"]


def _wersja(
    pion: Path, baza: Path, out: Path, meta: dict, sek: float,
) -> None:
    if not _plik(FONT):
        raise Odmowa(f"Brak wymaganego fontu: {FONT}")
    ass = out.with_suffix(".ass")
    _tekst(ass, _ass(meta["hook"]))
    # Ścieżki pochodzą wyłącznie ze stałych i wygenerowanych slugów.
    filtr = f"ass=filename={ass}:fontsdir={FONT.parent}"
    _proces([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-i", pion, "-i", baza, "-t", str(sek),
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", filtr, "-af", "loudnorm=I=-14:TP=-1.5",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out,
    ], timeout=1800)


def _bramki(plik: Path) -> dict:
    if not _plik(plik):
        raise Odmowa(f"Brak pliku wynikowego: {plik}")
    info = _probe(plik)
    ostrosc = _rh(plik, info["sek"])
    log = _proces([
        "ffmpeg", "-nostdin", "-hide_banner", "-i", plik,
        "-vf", "freezedetect=n=0.001:d=0.5", "-an", "-f", "null", "-",
    ], timeout=600)
    zamrozenia = len(re.findall(r"\bfreeze_start\s*:", log))
    wynik = {
        **info, "rh": ostrosc, "freeze_count": zamrozenia,
        "pisownia": "hunspell: zaliczona",
        "tytul": "bez emoji, zabronionych słów i duplikatu",
    }
    if (
        (info["w"], info["h"]) != (1080, 1920)
        or not 15 <= info["sek"] <= 45
        or not info["audio"]
        or not math.isfinite(ostrosc) or ostrosc < 0.25
        or zamrozenia != 0
    ):
        raise Odmowa("FAIL-LOUD: " + json.dumps(wynik, ensure_ascii=False))
    return wynik


def _wstaw(
    grupa: list[dict], slug: str, wersje: dict, teraz: dt.datetime,
) -> list[dict]:
    """Ponownie sprawdza limity i tytuły pod blokadą zapisu SQLite."""
    with _baza(True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            stare = [
                dict(r) for r in conn.execute("SELECT * FROM publication_queue")
            ]
            plan = _plan(stare, teraz)
            zrodla = [k["nazwa"] for k in grupa]
            if set(zrodla) & _zrodla_z_kolejki(stare):
                raise Odmowa("Źródło zostało już wykorzystane przez inny przebieg.")
            wpisy = []
            for kanal, wersja in wersje.items():
                terminy = plan[kanal]["terminy"]
                if not terminy:
                    continue
                meta = wersja["meta"]
                if _normalizuj(meta["title"]) in {
                    _normalizuj(r.get("title") or "")
                    for r in stare if r.get("channel") == kanal
                }:
                    raise Odmowa(f"Duplikat tytułu w kanale {kanal}.")
                wpis = {
                    "id": uuid.uuid4().hex,
                    "opportunity_id": "",
                    "capability": CAPABILITY,
                    "kind": "staged_asset",
                    "title": meta["title"],
                    "asset_paths": json.dumps([str(wersja["plik"])]),
                    "seo": json.dumps({
                        "title": meta["title"],
                        "description": meta["description"],
                        "tags": meta["tags"],
                    }, ensure_ascii=False),
                    "first_step": "auto-publish po scheduled_at (status approved)",
                    "channel": kanal,
                    "status": "approved",
                    "notes": json.dumps({
                        "reka": SLUG, "slug": slug, "zrodla": zrodla,
                        "model": MODEL, "bramki": wersja["bramki"],
                    }, ensure_ascii=False),
                    "staged_at": teraz.isoformat(),
                    "scheduled_at": terminy[0],
                }
                kolumny = list(wpis)
                conn.execute(
                    f"INSERT INTO publication_queue ({','.join(kolumny)}) "
                    f"VALUES ({','.join('?' for _ in kolumny)})",
                    tuple(wpis[k] for k in kolumny),
                )
                wpisy.append(wpis)
            conn.commit()
            return wpisy
        except BaseException:
            conn.rollback()
            raise


def _inwentarz() -> tuple[list[dict], dict, list[dict]]:
    stan = _stan_zdalny()
    if stan["gra"]:
        raise Odmowa("Gra działa na .253. Czekam; nic nie pobieram.")
    uzyte = _czytaj(UZYTE, {})
    if not isinstance(uzyte, dict):
        raise Odmowa("Rejestr użytych klipów nie jest słownikiem.")
    kolejka = _kolejka()
    wykluczone = set(uzyte) | _zrodla_z_kolejki(kolejka)
    return _klipy(stan["pliki"], wykluczone), uzyte, kolejka


def _podglad() -> str:
    klipy, _, kolejka = _inwentarz()
    plan = _plan(kolejka, _teraz())
    ranking = sorted(klipy, key=lambda k: (k["priorytet"], k["czas"]))
    linie = [
        f"PODGLĄD: {len(klipy)} nowych, rozpoznanych klipów po wykluczeniach.",
        "Kandydaci: " + (
            "; ".join(k["nazwa"] for k in ranking[:6]) or "brak"
        ),
        "Ostateczne pary, długości i ranking Likwidacji wymagają pobrania "
        "oraz ffprobe/astats po zatwierdzeniu.",
    ]
    for kanal, dane in plan.items():
        linie.append(
            f"{kanal}: nadchodzących {dane['nadchodzace']}/5; "
            "wolne terminy: " + (", ".join(dane["terminy"]) or "limit zajęty")
            + " (UTC)."
        )
    return "\n".join(linie)


def _start() -> str:
    # Czytamy źródła i limit przed uruchomieniem usługi; bez transferu klipów.
    klipy, _, kolejka = _inwentarz()
    if not klipy:
        return "Brak nowych klipów po wykluczeniu użytych i montażu Dwaj boomerzy."
    if not any(p["terminy"] for p in _plan(kolejka, _teraz()).values()):
        return "Limit 5 nadchodzących shortów zajęty na obu kanałach."
    unit = "shorty-gaming-" + _teraz().strftime("%Y%m%d-%H%M%S-%f")
    _proces([
        "systemd-run", "--user", f"--unit={unit}", "--collect",
        f"--working-directory={BACKEND}", "--property=Nice=10",
        PYTHON, "-m", "app.services.shorty_gaming_2_hand", "przebieg",
    ], timeout=30)
    return (
        f"Uruchomiono w tle {unit}. Najwyżej 3 shorty na kanał. "
        "Raport WA po sprawdzeniu plików, bramek i wpisów kolejki."
    )


async def _pobierz(uruchom: bool) -> str:
    return await asyncio.to_thread(_start if uruchom else _podglad)


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(params, dict) or not isinstance(cmd, str):
        return {"ok": False, "reason": "params musi być słownikiem, cmd tekstem."}
    m = re.fullmatch(WZORZEC, cmd, re.I)
    if m is None:
        return {"ok": False, "reason": "Nie rozpoznaję polecenia shorty_gaming_2."}
    try:
        odpowiedz = await _pobierz(bool(m["zgoda"]))
        if not isinstance(odpowiedz, str) or not odpowiedz.strip():
            raise Odmowa("Źródło nie zwróciło potwierdzonego raportu.")
        return {"ok": True, "reply": odpowiedz}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"Shorty gaming: nie potwierdzam wykonania. "
                      f"Źródło lub narzędzie nie odpowiada albo zwróciło błąd: "
                      f"{type(exc).__name__}: {exc}",
        }


async def start(params: dict | None = None, cmd: str = "") -> dict:
    return await wykonaj({} if params is None else params, cmd)


async def _praca() -> dict:
    teraz = _teraz()
    katalog = EXPORT / teraz.date().isoformat()
    raport = {"wiersze": [], "bledy": [], "pliki": []}
    try:
        klipy, uzyte, kolejka = _inwentarz()
        plan = _plan(kolejka, teraz)
        budzet = {k: len(p["terminy"]) for k, p in plan.items()}
        if not any(budzet.values()):
            return {"ok": True, "reply": "Limit 5 nadchodzących zajęty; bez zmian."}
        if not klipy:
            return {"ok": True, "reply": "Brak nowych klipów; bez zmian."}
        _mkdir(katalog)
        roboczy = katalog / ("przebieg_" + uuid.uuid4().hex)
        _mkdir(roboczy)

        for grupa in _grupy_leniwe(
            klipy, max(budzet.values()), raport["bledy"],
        ):
            aktywne = [k for k, n in budzet.items() if n > 0]
            if not aktywne:
                break
            slug = (
                "fortnite_" + grupa[0]["czas"].strftime("%Y%m%d_%H%M%S")
                + "_" + uuid.uuid4().hex[:10]
            )
            try:
                # Nie rozpoczynamy kolejnego montażu po zajęciu limitu.
                aktualny = _plan(_kolejka(), _teraz())
                aktywne = [k for k in aktywne if aktualny[k]["terminy"]]
                if not aktywne:
                    break
                pion, sek = _montuj(grupa, roboczy, slug)
                baza = roboczy / f"{slug}_baza.mp4"
                wersje = {}
                tytuly = _kolejka()
                # Opis sceny pobieramy raz dla montażu, przed próbami metadanych.
                scena = await _opis_sceny(pion)
                for kanal in aktywne:
                    jezyk = KANALY[kanal]
                    try:
                        meta = await _metadane(
                            grupa, jezyk,
                            {
                                _normalizuj(r.get("title") or "")
                                for r in tytuly if r.get("channel") == kanal
                            },
                            scena=scena,
                        )
                    except Odmowa as exc:
                        if not str(exc).startswith(
                            "Gemma: pięć odrzuconych prób metadanych"
                        ):
                            raise
                        raport["bledy"].append(
                            f"{slug}: {type(exc).__name__}: {exc}"
                        )
                        for klip in grupa:
                            uzyte[klip["nazwa"]] = {
                                "data": _teraz().isoformat(),
                                "slug": slug,
                                "powod": "metadane",
                            }
                        _zapisz(UZYTE, uzyte)
                        break
                    out = katalog / f"{slug}_{jezyk}.mp4"
                    _wersja(pion, baza, out, meta, sek)
                    bramki = _bramki(out)
                    wersje[kanal] = {
                        "plik": out, "meta": meta, "bramki": bramki,
                    }
                if any(
                    wpis.get("powod") == "metadane"
                    for wpis in uzyte.values()
                    if wpis.get("slug") == slug
                ):
                    continue
                wpisy = _wstaw(grupa, slug, wersje, _teraz())
                if not wpisy:
                    break
                # Sprawdzenie trwałego zapisu przed raportem i rejestrem użyć.
                trwale = {r["id"]: r for r in _kolejka()}
                for wpis in wpisy:
                    if trwale.get(wpis["id"]) != wpis:
                        raise Odmowa(
                            f"Nie potwierdzono zapisu wiersza {wpis['id']}."
                        )
                    if not _plik(Path(json.loads(wpis["asset_paths"])[0])):
                        raise Odmowa(f"Zniknął plik wiersza {wpis['id']}.")
                    raport["wiersze"].append(wpis)
                    raport["pliki"].extend(json.loads(wpis["asset_paths"]))
                    budzet[wpis["channel"]] -= 1
                for klip in grupa:
                    uzyte[klip["nazwa"]] = {
                        "data": _teraz().isoformat(), "slug": slug,
                    }
                _zapisz(UZYTE, uzyte)
                for klip in grupa:
                    klip["plik"].unlink(missing_ok=True)
            except Exception as exc:
                raport["bledy"].append(
                    f"{slug}: {type(exc).__name__}: {exc}"
                )
                # Nie kontynuujemy po niepewnym stanie narzędzia lub zapisu.
                break
        if not raport["wiersze"] and not raport["bledy"]:
            raport["bledy"].append(
                "Brak grup spełniających 15–45 s i warunek sąsiedztwa ≤3 min."
            )
    except Exception as exc:
        raport["bledy"].append(f"{type(exc).__name__}: {exc}")

    if raport["wiersze"]:
        try:
            shutil.rmtree(roboczy)
        except Exception as exc:
            raport["bledy"].append(f"Usuwanie katalogu roboczego: {exc}")

    tekst = ["SHORTY GAMING — potwierdzone wyniki:"]
    for r in raport["wiersze"]:
        notes = json.loads(r["notes"])
        tekst.append(
            f"{r['channel']}: {r['title']}; {r['scheduled_at']} UTC; "
            f"id={r['id']}; status={r['status']}\n"
            f"Pliki: {r['asset_paths']}\n"
            f"Źródła: {', '.join(notes['zrodla'])}\n"
            f"Bramki: {json.dumps(notes['bramki'], ensure_ascii=False)}"
        )
    if not raport["wiersze"]:
        tekst.append("Brak potwierdzonych nowych wpisów kolejki.")
    tekst.extend("BŁĄD: " + b for b in raport["bledy"])
    try:
        _zapisz(katalog / ("raport_" + uuid.uuid4().hex + ".json"), raport)
    except Exception as exc:
        raport["bledy"].append(f"Zapis raportu: {exc}")
        tekst.append(f"BŁĄD zapisu raportu: {exc}")
    try:
        await _wa("\n".join(tekst))
    except Exception as exc:
        raport["bledy"].append(f"WhatsApp nie potwierdził wysłania: {exc}")
    return {"ok": not raport["bledy"], **raport}


async def przebieg() -> dict:
    try:
        with _blokada():
            return await _praca()
    except Exception as exc:
        return {"ok": False, "reason": f"Przebieg nie ruszył: {exc}"}


if __name__ == "__main__":
    if sys.argv[1:] != ["przebieg"]:
        print(json.dumps({"ok": False, "reason": "Wymagany argument przebieg."}))
        raise SystemExit(2)
    wynik = asyncio.run(przebieg())
    print(json.dumps(wynik, ensure_ascii=False))
    raise SystemExit(0 if wynik["ok"] else 1)
