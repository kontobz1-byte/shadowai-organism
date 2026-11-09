# plik: sciagala_nowe_nagrania_hand.py
"""Zgrywanie nowych nagrań z gier z domowych maszyn na serwer .10.

Nigdy nie zgrywa z maszyny, na której trwa gra (lagi u Dziecko 08.10).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import ntpath
import os
import re
import secrets
import shlex
import urllib.request
from pathlib import Path

log = logging.getLogger("shadowai.sciagala")

# 2026-11-06: operator mówił „zrzuć nagranie z 253”, „mamy nowe nagrania na 253” — namysł dawał
# zrzut ekranu / gaming_status. Wzorzec łapie też inne czasowniki, powtórki/replaye i nazwę maszyny;
# zdania o zrzucie EKRANU (zrzut ekranu, screenshot) zostają poza ręką.
_WZ_ZGODA = r"(?:wykonaj|wyślij|zatwierdź)"
_WZ_CZASOWNIK = r"(?:ściągnij|sciagnij|zgraj|pobierz|zrzu[ćc]\w*|przenie[śs]\w*|skopiuj\w*)"
_WZ_RZECZOWNIK = (
    r"(?:nowe\s+)?(?:nagrani\w*|nagrań|powt[óo]rk\w*|replay\w*)"
    r"(?:\s+(?:z\s+gier|z\s+gry|gry\s+fortnite|z\s+fortnite))?"
)
_WZ_ZRODLO = (
    r"(?:ze?\s+(?:wszystkich\s+)?(?:domowych\s+)?maszyn"
    r"|(?:z|ze|na)\s+\.?(?:253|130|197)\b"
    r"|z\s+laptopa\s+dziecko)"
)
WZORZEC = (
    r"^(?!.*\b(?:zrzut\w*\s+ekran\w*|screenshot\w*))\s*(?:" + _WZ_ZGODA + r"[\s,:]+)?"
    r"(?:" + _WZ_CZASOWNIK + r"\s+(?:je\s+)?" + _WZ_RZECZOWNIK + r"\s+" + _WZ_ZRODLO
    + r"|(?:mamy\s+)?" + _WZ_RZECZOWNIK + r"\s+" + _WZ_ZRODLO
    + r"[\s,:]+(?:i\s+)?" + _WZ_CZASOWNIK + r"(?:\s+je)?)"
    r"(?:[\s,:]+(?:i\s+)?" + _WZ_ZGODA + r")?\s*[.!]?\s*$"
)
_WZ_MASZYNA = r"\b(?:z|ze|na)\s+\.?(253|130|197)\b|\bz\s+laptopa\s+(dziecko)\b"
GOTOWA = True


def _maszyna_z(cmd: str) -> str | None:
    """Klucz maszyny nazwanej w poleceniu („z 253”, „na .197”, „z laptopa Dziecko”) albo None."""
    m = re.search(_WZ_MASZYNA, cmd or "", re.I)
    if not m:
        return None
    return m.group(1) or "197"

# Prefiks domowego LAN (serwer .10 = 10.0.0.10).
PODSIEC = "192.168.50."  # 2026-10-10: nauczyciel wpisał 192.168.1. — straż .253/.130 zawsze „niedostępna”
IP_253 = PODSIEC + "253"
IP_130 = PODSIEC + "130"

NAZWY = {
    "197": "laptop Dziecko (.197)",
    "253": "PC operatora (.253)",
    "130": "maszyna .130",
}
WOLNA, GRA, NIEDOSTEPNA = "wolna", "gra", "niedostępna"

_ZGODA = r"\b(?:wykonaj|wyślij|zatwierdź)\b"
_RODZAJE_197 = ("nagrania", "powtórki", "highlighty")
_BUDZET_197 = 8 * 1024**3
_DO_ZGRANIA = {
    "197": (
        "do zgrania: nowe nagrania, powtórki, highlighty "
        f"(limit {_BUDZET_197 // 1024**3} GiB)"
    ),
    "253": "do zgrania: highlighty Fortnite",
}
_POLA = 6


async def _straz_197():
    """Straż laptopa Dziecko: tekst powodu, gdy gra trwa, albo None, gdy wolny."""
    from app.services import dziecko_fetch

    return await dziecko_fetch._dz_gra()


async def _pobierz_197(rodzaj: str) -> dict:
    """Jedno zgranie z laptopa Dziecko na serwer .10."""
    from app.services import dziecko_fetch

    if rodzaj == "nagrania":
        return await dziecko_fetch.fetch_new_recordings(reason="kuznia")
    if rodzaj == "powtórki":
        return await dziecko_fetch.fetch_new_replays(reason="kuznia")
    if rodzaj == "highlighty":
        return await dziecko_fetch.fetch_new_highlights(
            reason="kuznia", budget_bytes=_BUDZET_197
        )
    raise ValueError(f"Nieznany rodzaj zgrania: {rodzaj}")


async def _straz_winrm(ip: str) -> tuple:
    """Procesy gier na maszynie Windows przez WinRM; zwraca (out, rc)."""
    from app.services.lan_actuator import run_winrm
    from app.services.nvenc_offload_197 import GAME_PROCS

    procesy = GAME_PROCS if isinstance(GAME_PROCS, str) else ",".join(GAME_PROCS)
    out, rc = await run_winrm(
        ip,
        "$ProgressPreference='SilentlyContinue'; "
        "(Get-Process -Name " + procesy + " -ErrorAction SilentlyContinue).Name",
    )
    return out, rc


async def _straz_130() -> tuple:
    """Procesy gier na .130 przez agenta HTTP (_ps); zwraca (out, 0)."""
    from app.services.nvenc_offload_197 import GAME_PROCS

    procesy = GAME_PROCS if isinstance(GAME_PROCS, str) else ",".join(GAME_PROCS)
    skrypt = (
        "$ProgressPreference='SilentlyContinue'; "
        "(Get-Process -Name " + procesy + " -ErrorAction SilentlyContinue).Name"
    )
    out = await _ps("130", skrypt, timeout=30)
    return out, 0


async def _pobierz_253() -> dict:
    """Zgranie highlightów z PC operatora na serwer .10."""
    from app.services import fortnite_highlights

    return await fortnite_highlights.rescue()


def _stan_197(odpowiedz) -> tuple[str, str]:
    if isinstance(odpowiedz, BaseException):
        return NIEDOSTEPNA, f"straż nie odpowiada: {type(odpowiedz).__name__}"
    if odpowiedz is None:
        return WOLNA, ""
    # Każda inna odpowiedź straży oznacza grę — w razie wątpliwości nie zgrywamy.
    return GRA, str(odpowiedz).strip() or "straż nie podała powodu"


def _stan_winrm(odpowiedz) -> tuple[str, str]:
    if isinstance(odpowiedz, BaseException):
        return NIEDOSTEPNA, f"WinRM: {type(odpowiedz).__name__}"
    try:
        out, rc = odpowiedz
    except (TypeError, ValueError):
        return NIEDOSTEPNA, "nieoczekiwana odpowiedź WinRM"
    if rc != 0:
        return NIEDOSTEPNA, f"WinRM rc={rc}"
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    elif isinstance(out, (list, tuple)):
        out = "\n".join(map(str, out))
    # 2026-10-10: SSH do .253 dokleja „[stderr]” z CLIXML postępu PowerShella — to nie nazwy procesów
    out = re.sub(r"\[stderr\].*|#< CLIXML.*", "", str(out or ""), flags=re.S)
    procesy = ", ".join(
        linia.strip() for linia in str(out or "").splitlines() if linia.strip()
    )
    return (GRA, procesy) if procesy else (WOLNA, "")


def _udane(wynik) -> bool:
    return (
        isinstance(wynik, dict)
        and wynik.get("ok") is not False
        and not wynik.get("error")
    )


def _skrot(wynik) -> str:
    """Krótki opis odpowiedzi zbieracza — tylko to, co faktycznie zwrócił."""
    if not isinstance(wynik, dict):
        return f"nieoczekiwana odpowiedź ({type(wynik).__name__})"
    if not wynik:
        return "pusta odpowiedź"
    pola = []
    for klucz, wartosc in wynik.items():
        if isinstance(wartosc, (list, tuple, set, dict)):
            wartosc = f"{len(wartosc)} poz."
        else:
            wartosc = str(wartosc)
            if len(wartosc) > 60:
                wartosc = wartosc[:59] + "…"
        pola.append(f"{klucz}={wartosc}")
    tekst = ", ".join(pola[:_POLA]) + (", …" if len(pola) > _POLA else "")
    return tekst if _udane(wynik) else f"niepowodzenie ({tekst})"


def _powod(stan: str, opis: str) -> str:
    return f"gra trwa ({opis})" if stan == GRA else f"niedostępna ({opis})"


def _linia_stanu(klucz: str, stan: str, opis: str, podglad: bool) -> str:
    """Opis maszyny bez wyniku zgrywania."""
    if klucz == "130" and stan != NIEDOSTEPNA:
        gra = _powod(stan, opis) if stan == GRA else "wolna"
        dalej = "tylko zgłaszam stan" if podglad else "nic nie zgrano"
        return f"{gra} — brak zbieracza, {dalej}"
    if stan == WOLNA:
        return f"wolna — {_DO_ZGRANIA[klucz]}"
    if podglad:
        return f"{_powod(stan, opis)} — pominę"
    return f"pominięto — {_powod(stan, opis)}"


async def _zgraj_197() -> tuple[str, int, int]:
    """Zgrywa po kolei; przed każdym kolejnym krokiem ponownie pyta straż."""
    czesci, udane, proby = [], 0, 0
    for nr, rodzaj in enumerate(_RODZAJE_197):
        if nr:
            try:
                odpowiedz = await _straz_197()
            except Exception as exc:
                odpowiedz = exc
            stan, opis = _stan_197(odpowiedz)
            if stan != WOLNA:
                reszta = ", ".join(_RODZAJE_197[nr:])
                czesci.append(f"przerwano przed: {reszta} — {_powod(stan, opis)}")
                break
        proby += 1
        try:
            wynik = await _pobierz_197(rodzaj)
        except Exception as exc:
            czesci.append(f"{rodzaj}: błąd ({type(exc).__name__})")
            continue
        udane += int(_udane(wynik))
        czesci.append(f"{rodzaj}: {_skrot(wynik)}")
    return "; ".join(czesci), udane, proby


async def _zgraj_253() -> tuple[str, int, int]:
    try:
        wynik = await _pobierz_253()
    except Exception as exc:
        return f"błąd zgrywania ({type(exc).__name__})", 0, 1
    return _skrot(wynik), int(_udane(wynik)), 1


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {
            "ok": False,
            "reason": "Nie rozpoznaję polecenia ściągania nagrań ze wszystkich maszyn.",
        }
    zgoda = bool(re.search(_ZGODA, cmd, re.I))
    cel = _maszyna_z(cmd)

    # Straże sprawdzamy równolegle; wyjątek jednej nie przerywa pozostałych.
    odp_197, odp_253, odp_130 = await asyncio.gather(
        _straz_197(),
        _straz_winrm(IP_253),
        _straz_winrm(IP_130),
        return_exceptions=True,
    )
    stany = {
        "197": _stan_197(odp_197),
        "253": _stan_winrm(odp_253),
        "130": _stan_winrm(odp_130),
    }

    if all(stan == NIEDOSTEPNA for stan, _ in stany.values()):
        szczegoly = "; ".join(
            f"{NAZWY[k]}: {opis}" for k, (_, opis) in stany.items()
        )
        return {
            "ok": False,
            "reason": f"Żadna maszyna nie odpowiada ({szczegoly}). Nic nie zgrano.",
        }

    if not zgoda:
        linie = "\n".join(
            f"- {NAZWY[k]}: {_linia_stanu(k, stan, opis, podglad=True)}"
            for k, (stan, opis) in stany.items()
        )
        return {
            "ok": True,
            "reply": (
                f"PODGLĄD — nic nie zgrano:\n{linie}\n"
                "Aby zgrać z wolnych maszyn: "
                "ściągnij nagrania ze wszystkich maszyn wykonaj"
            ),
        }

    zgrania = {"197": _zgraj_197, "253": _zgraj_253}
    wolne = [k for k in zgrania
             if stany[k][0] == WOLNA and (cel is None or k == cel)]
    # Wolne maszyny zgrywamy równolegle; każda ma własny zbieracz.
    wyniki = dict(zip(wolne, await asyncio.gather(
        *(zgrania[k]() for k in wolne), return_exceptions=True
    )))

    linie, udane, proby = [], 0, 0
    for k, (stan, opis) in stany.items():
        if cel is not None and k != cel:
            opis_linii = f"pominięta — polecenie dotyczy .{cel}"
        elif k not in wyniki:
            opis_linii = _linia_stanu(k, stan, opis, podglad=False)
        elif isinstance(wyniki[k], BaseException):
            opis_linii = f"błąd zgrywania ({type(wyniki[k]).__name__})"
            proby += 1
        else:
            opis_linii, udane_k, proby_k = wyniki[k]
            udane += udane_k
            proby += proby_k
        linie.append(f"- {NAZWY[k]}: {opis_linii}")

    tresc = "\n".join(linie)
    if not proby:
        return {
            "ok": True,
            "reply": f"Nic nie zgrano — żadna maszyna ze zbieraczem nie jest wolna:\n{tresc}",
        }
    if not udane:
        return {
            "ok": False,
            "reason": f"Nie udało się niczego zgrać na serwer .10:\n{tresc}",
        }
    return {
        "ok": True,
        "reply": f"WYKONANO — zgrywanie nowych nagrań na serwer .10:\n{tresc}",
    }


# ── Przebieg automatyczny: powtórki Fortnite → trzy dyski Toshiba ─────────────
# 2026-11-05: poprzedni wyrób liczył „skopiowane” bez kopiowania. Każda liczba w wyniku
# pochodzi teraz z realnej operacji: pobranie na .10, kopia na .253 i .172, SHA256 każdej
# kopii porównany ze źródłem; oryginał kasowany dopiero, gdy WSZYSTKIE trzy kopie są zgodne.

MASZYNY = ("197", "253", "130")
IP_10 = PODSIEC + "10"
IP_172 = PODSIEC + "172"
IP_197 = PODSIEC + "197"
# Trzy dyski Toshiba (tabela magazyny_nagran).
MAGAZYN_10 = "/mnt/usb_toshiba/Dziecko"
MAGAZYN_253 = r"E:\_shadowtest"
MAGAZYN_172 = "/srv/data/magazyn_powtorki"
DYSKI = ("10", "253", "172")
_OPIS_DYSKU = {"10": "Toshiba .10", "253": "Toshiba .253 (E:)", "172": "Toshiba .172"}

KLUCZ_SSH_253 = "/opt/shadow/.ssh/shadowai_gpu"
CEL_253 = f"operator@{IP_253}"
CEL_172 = f"operator@{IP_172}"
_SSH_OPCJE = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
SSH_253 = ("ssh", "-i", KLUCZ_SSH_253, *_SSH_OPCJE, CEL_253)
SCP_253 = ("scp", "-i", KLUCZ_SSH_253, *_SSH_OPCJE)
SSH_172 = ("ssh", *_SSH_OPCJE, CEL_172)
SCP_172 = ("scp", *_SSH_OPCJE)
AGENT_130 = f"http://{IP_130}:8002/exec"
# 2026-11-05: 8765 zamknięty w zaporze .10 (deny); 18099 = port scripts/odbior_130_http.py (zakres 16384–32768 otwarty)
PORT_ODBIORU = 18099
_ENV = Path(__file__).resolve().parents[2] / ".env"

DEMOS = r"AppData\Local\FortniteGame\Saved\Demos"
_KONTA_POMIJANE = frozenset({"public", "default", "default user", "all users"})
_MIN_WIEK_S = 10 * 60

_SKRYPT_LISTY = (
    "$ProgressPreference='SilentlyContinue'; $t=[DateTime]::UtcNow; "
    "$r=@(Get-ChildItem -LiteralPath 'C:\\Users' -Directory -Force -ErrorAction SilentlyContinue | "
    "ForEach-Object { $k=$_.Name; $d=Join-Path $_.FullName '" + DEMOS + "'; "
    "if (Test-Path -LiteralPath $d) { Get-ChildItem -LiteralPath $d -Filter '*.replay' -File "
    "-ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{konto=$k; "
    "sciezka=$_.FullName; rozmiar=$_.Length; "
    "wiek_s=[int](($t - $_.LastWriteTimeUtc).TotalSeconds)} } } }); "
    "ConvertTo-Json -InputObject $r -Compress"
)


def _nowy_wynik() -> dict:
    return {"skopiowane": 0, "skasowane_oryginaly": 0, "bajty": 0,
            "pominiete": 0, "powody": [], "cele": {}}


def _klucze(maszyny) -> tuple[list[str], list[str]]:
    """None → wszystkie; napis „253” / „10.0.0.253” albo lista takich → klucze."""
    if maszyny is None:
        return list(MASZYNY), []
    if isinstance(maszyny, (str, int)):
        maszyny = [maszyny]
    klucze, nieznane = [], []
    for m in maszyny:
        k = str(m).strip().rsplit(".", 1)[-1]
        if k in MASZYNY:
            if k not in klucze:
                klucze.append(k)
        else:
            nieznane.append(str(m))
    return klucze, nieznane


def _ps_lit(tekst: str) -> str:
    return "'" + str(tekst).replace("'", "''") + "'"


def _sha_z(out) -> str:
    m = re.search(r"\b[0-9a-fA-F]{64}\b", str(out or ""))
    if not m:
        raise ValueError("brak SHA256 w odpowiedzi")
    return m.group(0).lower()


def _sha256_pliku(sciezka: str) -> str:
    h = hashlib.sha256()
    with open(sciezka, "rb") as f:
        for blok in iter(lambda: f.read(1 << 20), b""):
            h.update(blok)
    return h.hexdigest()


def _json_z(out: str):
    tekst = re.sub(r"#< CLIXML.*", "", str(out or ""), flags=re.S).strip()
    if not tekst:
        return []
    starty = [i for i in (tekst.find("["), tekst.find("{")) if i >= 0]
    if not starty:
        raise ValueError("brak JSON w odpowiedzi")
    return json.JSONDecoder().raw_decode(tekst[min(starty):])[0]


def _nazwa_docelowa(konto: str, sciezka: str) -> str:
    return re.sub(r"[^\w.-]+", "_", f"{konto}_{ntpath.basename(sciezka)}", flags=re.ASCII)


async def _uruchom(argv, wejscie: bytes | None = None, timeout: float = 600) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if wejscie is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(wejscie), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"{argv[0]}: przekroczono {timeout} s")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


# ── PowerShell na maszynach ──────────────────────────────────────────────────

async def _ps_253(skrypt: str, timeout: float = 300) -> str:
    rc, out, err = await _uruchom(
        SSH_253 + ("powershell -NoProfile -Command -",),
        (skrypt + "\n\n").encode("utf-8"), timeout,
    )
    if rc != 0:
        raise RuntimeError(f"ssh .253 rc={rc}: {err.strip()[:200]}")
    return out


def _token_130() -> str:
    token = <UKRYTE>("SHADOWGUEST_AGENT_TOKEN", "").strip()
    if token:
        return token
    try:
        for linia in _ENV.read_text(encoding="utf-8").splitlines():
            klucz, _, wartosc = linia.partition("=")
            if klucz.strip() == "SHADOWGUEST_AGENT_TOKEN":
                return wartosc.strip().strip("'\"")
    except OSError as exc:
        log.warning("sciagala: nie czytam %s: %s", _ENV, exc)
    raise RuntimeError("brak SHADOWGUEST_AGENT_TOKEN w backend/.env")


def _post_130(skrypt: str, timeout: float):
    dane = json.dumps({"cmd": skrypt, "shell": "powershell", "timeout": int(min(timeout, 3600))}).encode("utf-8")
    zadanie = urllib.request.Request(
        AGENT_130, data=dane, method="POST",
        headers={"Authorization": f"Bearer {_token_130()}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(zadanie, timeout=timeout + 30) as odp:
        tekst = odp.read().decode("utf-8", "replace")
    return json.loads(tekst) if tekst.strip() else {}


async def _ps_130(skrypt: str, timeout: float = 300) -> str:
    odp = await asyncio.to_thread(_post_130, skrypt, timeout)
    if not isinstance(odp, dict):
        return str(odp)
    rc = next((odp[k] for k in ("rc", "returncode", "exit_code") if k in odp), None)
    if odp.get("ok") is False or rc not in (None, 0):
        raise RuntimeError(f"agent .130 rc={rc}: {str(odp)[:200]}")
    return str(next((odp[k] for k in ("stdout", "out", "output", "result") if k in odp), "") or "")


async def _ps_197(skrypt: str, timeout: float = 300) -> str:
    from app.services.lan_actuator import run_winrm

    out, rc = await asyncio.wait_for(run_winrm(IP_197, skrypt), timeout)
    if rc != 0:
        raise RuntimeError(f"WinRM .197 rc={rc}")
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return str(out or "").split("[stderr]")[0]


async def _ps(k: str, skrypt: str, timeout: float = 300) -> str:
    return await {"197": _ps_197, "253": _ps_253, "130": _ps_130}[k](skrypt, timeout)


async def _lista_powtorek(k: str) -> list[dict]:
    dane = _json_z(await _ps(k, _SKRYPT_LISTY, timeout=120))
    if isinstance(dane, dict):
        dane = [dane]
    return [d for d in dane if isinstance(d, dict)]


async def _hash_zrodla(k: str, sciezka: str) -> str:
    return _sha_z(await _ps(
        k, f"(Get-FileHash -LiteralPath {_ps_lit(sciezka)} -Algorithm SHA256).Hash", timeout=900))


async def _skasuj_zrodlo(k: str, sciezka: str) -> bool:
    p = _ps_lit(sciezka)
    out = await _ps(
        k,
        f"Remove-Item -LiteralPath {p} -Force -ErrorAction SilentlyContinue; "
        f"if (Test-Path -LiteralPath {p}) {{ 'ZOSTAL' }} else {{ 'SKASOWANO' }}",
        timeout=120,
    )
    return "SKASOWANO" in out and "ZOSTAL" not in out


# ── Odbiornik HTTP PUT dla .130 (agent jako SYSTEM nie pisze po SMB) ─────────

class _Odbiornik:
    """PUT tylko z 10.0.0.130: zapis do <cel>.part, po komplecie bajtów rename na <cel>."""

    def __init__(self) -> None:
        self.cele: dict[str, str] = {}
        self.odebrane: set[str] = set()
        self.serwer = None
        self.port = PORT_ODBIORU

    def zarejestruj(self, cel: str) -> str:
        token = <UKRYTE>(16)
        self.cele[token] = cel
        return token

    def wyrejestruj(self, token: str) -> None:
        self.cele.pop(token, None)
        self.odebrane.discard(token)

    def gotowe(self, token: str) -> bool:
        return token in self.odebrane

    @staticmethod
    async def _odpowiedz(writer, status: str) -> None:
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()

    async def obsluz(self, reader, writer) -> None:
        czesc = None
        try:
            peer = (writer.get_extra_info("peername") or ("",))[0]
            if peer != IP_130:
                log.warning("sciagala: odbiornik odrzucił połączenie z %s", peer)
                return
            naglowek = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            linie = naglowek.decode("latin-1").split("\r\n")
            czesci = linie[0].split(" ")
            metoda, sciezka = (czesci + ["", ""])[:2]
            pola = {}
            for linia in linie[1:]:
                if ":" in linia:
                    a, b = linia.split(":", 1)
                    pola[a.strip().lower()] = b.strip()
            token = <UKRYTE>("/")
            cel = self.cele.get(token)
            if metoda != "PUT" or cel is None or "content-length" not in pola:
                await self._odpowiedz(writer, "404 Not Found")
                return
            zostalo = int(pola["content-length"])
            if pola.get("expect", "").lower() == "100-continue":
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            czesc = cel + ".part"
            with open(czesc, "wb") as f:
                while zostalo > 0:
                    blok = await asyncio.wait_for(reader.read(min(zostalo, 1 << 20)), 120)
                    if not blok:
                        break
                    await asyncio.to_thread(f.write, blok)
                    zostalo -= len(blok)
            if zostalo:
                os.remove(czesc)
                await self._odpowiedz(writer, "400 Bad Request")
                return
            os.replace(czesc, cel)
            czesc = None
            self.odebrane.add(token)
            await self._odpowiedz(writer, "201 Created")
        except Exception as exc:  # noqa: BLE001
            log.warning("sciagala: odbiór PUT z .130 padł: %s", exc)
            if czesc:
                with contextlib.suppress(OSError):
                    os.remove(czesc)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


_ODBIORNIK: _Odbiornik | None = None
_BLOKADA = asyncio.Lock()


async def _odbiornik_start() -> _Odbiornik:
    odb = _Odbiornik()
    odb.serwer = await asyncio.start_server(odb.obsluz, host="0.0.0.0", port=odb.port)
    return odb


async def _odbiornik_stop(odb) -> None:
    serwer = getattr(odb, "serwer", None)
    if serwer is not None:
        serwer.close()
        await serwer.wait_closed()


# ── Pobranie źródła na .10 (zawsze .part → rename) ───────────────────────────

async def _sciagnij_197(sciezka: str, cel: str) -> None:
    import shutil
    import tempfile

    from app.services import dziecko_fetch

    dane = dziecko_fetch._smb_dane(IP_197)
    if not dane:
        raise RuntimeError("brak danych logowania .197")
    user, pw = dane
    tmp = await asyncio.to_thread(tempfile.mkdtemp, dir=os.path.dirname(cel))
    try:
        await dziecko_fetch._smb_get(IP_197, sciezka, Path(tmp), user, pw)
        pobrany = os.path.join(tmp, sciezka.rsplit("\\", 1)[-1])
        if not os.path.isfile(pobrany):
            raise RuntimeError(f"_smb_get nie zapisał {ntpath.basename(sciezka)} na .10")
        os.replace(pobrany, cel)
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp, True)


async def _sciagnij_253(sciezka: str, cel: str) -> None:
    czesc = cel + ".part"
    rc, _, err = await _uruchom(
        SCP_253 + (f"{CEL_253}:" + sciezka.replace("\\", "/"), czesc), timeout=1800)
    if rc != 0 or not os.path.isfile(czesc):
        with contextlib.suppress(OSError):
            os.remove(czesc)
        raise RuntimeError(f"scp z .253 rc={rc}: {err.strip()[:200]}")
    os.replace(czesc, cel)


async def _sciagnij_130(sciezka: str, cel: str) -> None:
    odb = _ODBIORNIK
    if odb is None:
        raise RuntimeError("odbiornik HTTP PUT nie działa")
    token = <UKRYTE>(cel)
    try:
        url = f"http://{IP_10}:{odb.port}/{token}"
        out = await _ps_130(
            "$ProgressPreference='SilentlyContinue'; "
            f"& curl.exe -sS -f --max-time 3000 -T {_ps_lit(sciezka)} {_ps_lit(url)}; "
            "'RC=' + $LASTEXITCODE",
            timeout=3600,
        )
        if not odb.gotowe(token):
            raise RuntimeError(f"odbiór z .130 nieudany ({out.strip()[:200]})")
    finally:
        odb.wyrejestruj(token)


async def _sciagnij(k: str, sciezka: str, cel: str) -> None:
    await {"197": _sciagnij_197, "253": _sciagnij_253, "130": _sciagnij_130}[k](sciezka, cel)


# ── Kopie na dyski .253 i .172 oraz ich SHA256 ───────────────────────────────

async def _wyslij_kopie(dysk: str, lokalny: str, k: str, nazwa: str,
                        rodzaj: str = "powtorki") -> str:
    """Kopia pliku z .10 na dysk zdalny; zwraca ścieżkę kopii na tym dysku."""
    if dysk == "172":
        katalog = f"{MAGAZYN_172}/{rodzaj}_{k}"
        rc, _, err = await _uruchom(SSH_172 + (f"mkdir -p {shlex.quote(katalog)}",), timeout=60)
        if rc != 0:
            raise RuntimeError(f"mkdir na .172 rc={rc}: {err.strip()[:200]}")
        cel = f"{katalog}/{nazwa}"
        rc, _, err = await _uruchom(SCP_172 + (lokalny, f"{CEL_172}:{cel}"), timeout=1800)
        if rc != 0:
            raise RuntimeError(f"scp na .172 rc={rc}: {err.strip()[:200]}")
        return cel
    if dysk == "253":
        katalog = f"{MAGAZYN_253}\\{rodzaj.capitalize()}_{k}"
        await _ps_253(f"New-Item -ItemType Directory -Force -Path {_ps_lit(katalog)} | Out-Null",
                      timeout=60)
        cel = f"{katalog}\\{nazwa}"
        rc, _, err = await _uruchom(
            SCP_253 + (lokalny, f"{CEL_253}:" + cel.replace("\\", "/")), timeout=1800)
        if rc != 0:
            raise RuntimeError(f"scp na .253 rc={rc}: {err.strip()[:200]}")
        return cel
    raise ValueError(f"nieznany dysk: {dysk}")


async def _hash_kopii(dysk: str, sciezka: str) -> str:
    if dysk == "10":
        return await asyncio.to_thread(_sha256_pliku, sciezka)
    if dysk == "172":
        rc, out, err = await _uruchom(SSH_172 + (f"sha256sum {shlex.quote(sciezka)}",), timeout=900)
        if rc != 0:
            raise RuntimeError(f"sha256sum na .172 rc={rc}: {err.strip()[:200]}")
        return _sha_z(out)
    if dysk == "253":
        return _sha_z(await _ps_253(
            f"(Get-FileHash -LiteralPath {_ps_lit(sciezka)} -Algorithm SHA256).Hash", timeout=900))
    raise ValueError(f"nieznany dysk: {dysk}")


# ── Przebieg ─────────────────────────────────────────────────────────────────

async def _przenies_plik(k: str, konto: str, sciezka: str, rozmiar: int, wynik: dict) -> None:
    powody = wynik["powody"]
    opis = f"{NAZWY[k]}: {konto}\\{ntpath.basename(sciezka)}"
    try:
        sha_zrodla = (await _hash_zrodla(k, sciezka)).lower()
    except Exception as exc:  # noqa: BLE001
        powody.append(f"{opis}: brak SHA256 źródła ({type(exc).__name__}) — oryginał zostaje")
        return
    nazwa = _nazwa_docelowa(konto, sciezka)
    katalog = os.path.join(MAGAZYN_10, f"powtorki_{k}")
    cel = os.path.join(katalog, nazwa)
    try:
        await asyncio.to_thread(os.makedirs, katalog, exist_ok=True)
        await _sciagnij(k, sciezka, cel)
        pobrano = await asyncio.to_thread(os.path.getsize, cel)
    except Exception as exc:  # noqa: BLE001
        powody.append(f"{opis}: pobranie na .10 nieudane ({type(exc).__name__}: {exc}) — oryginał zostaje")
        return
    if pobrano != rozmiar:
        powody.append(f"{opis}: pobrano {pobrano} B zamiast {rozmiar} B — oryginał zostaje")
        return

    async def _kopia(dysk: str) -> str:
        miejsce = cel if dysk == "10" else await _wyslij_kopie(dysk, cel, k, nazwa)
        return await _hash_kopii(dysk, miejsce)

    hashe = await asyncio.gather(*(_kopia(d) for d in DYSKI), return_exceptions=True)
    zle = []
    for dysk, wartosc in zip(DYSKI, hashe):
        if isinstance(wartosc, BaseException):
            zle.append(f"{_OPIS_DYSKU[dysk]}: {type(wartosc).__name__}")
        elif str(wartosc).strip().lower() != sha_zrodla:
            zle.append(f"{_OPIS_DYSKU[dysk]}: SHA256 niezgodny")
    if zle:
        powody.append(f"{opis}: {', '.join(zle)} — oryginał zostaje")
        return
    wynik["skopiowane"] += 1
    wynik["bajty"] += rozmiar
    try:
        skasowano = await _skasuj_zrodlo(k, sciezka)
    except Exception as exc:  # noqa: BLE001
        log.warning("sciagala: kasowanie %s padło: %s", opis, exc)
        skasowano = False
    if skasowano:
        wynik["skasowane_oryginaly"] += 1
    else:
        powody.append(f"{opis}: trzy kopie zgodne, ale kasowanie oryginału nieudane")


async def _przebieg_maszyny(k: str) -> dict:
    wynik = _nowy_wynik()
    try:
        pliki = await _lista_powtorek(k)
    except Exception as exc:  # noqa: BLE001
        wynik["powody"].append(
            f"{NAZWY[k]}: nie udało się wylistować powtórek ({type(exc).__name__}: {exc})")
        return wynik
    pominiete = 0
    for plik in pliki:
        konto = str(plik.get("konto") or "").strip()
        sciezka = str(plik.get("sciezka") or "")
        try:
            rozmiar = int(plik.get("rozmiar"))
            wiek = float(plik.get("wiek_s"))
        except (TypeError, ValueError):
            pominiete += 1
            continue
        if not konto or konto.lower() in _KONTA_POMIJANE or wiek < _MIN_WIEK_S:
            pominiete += 1
            continue
        # Kasujemy wyłącznie *.replay z Demos tego konta — nic innego nie dotykamy.
        oczekiwany = f"c:\\users\\{konto}\\{DEMOS}\\".lower()
        if not sciezka.lower().startswith(oczekiwany) or not sciezka.lower().endswith(".replay"):
            pominiete += 1
            wynik["powody"].append(f"{NAZWY[k]}: podejrzana ścieżka pominięta ({sciezka})")
            continue
        await _przenies_plik(k, konto, sciezka, rozmiar, wynik)
    if pominiete:
        wynik["powody"].append(
            f"{NAZWY[k]}: pominięto {pominiete} plik(ów) — świeże (<10 min) lub konto systemowe")
    wynik["pominiete"] += pominiete
    return wynik


# ── Etap 2: duże nagrania → JEDEN dysk Toshiba wybrany trójkątem ─────────────
# 2026-11-05: każdy plik idzie na jeden dysk wybrany przez node_hardware_truth.pick_node;
# wolne miejsce mierzone przed każdym plikiem (statvfs / Get-PSDrive E / ssh df -B1) minus 50 GB.
# Weryfikacja: rozmiar + SHA256 całego pliku po obu stronach (kasowanie oryginału jest nieodwracalne).

KATALOGI_NAGRAN = (r"Videos\NVIDIA", r"Videos\Captures", r"AppData\Local\Temp\Highlights")
ROZSZERZENIA_NAGRAN = (".mp4", ".mkv", ".mov")
TOSHIBA_10 = "/mnt/usb_toshiba"
DANE_172 = "/srv/data"
_GB = 1024**3
_ZAPAS_GB = 50
_PROBKA = 16 * 1024**2

_SKRYPT_NAGRAN = (
    "$ProgressPreference='SilentlyContinue'; $t=[DateTime]::UtcNow; "
    "$pom=@('Public','Default','Default User','All Users'); "
    "$kat=@(" + ",".join(_ps_lit(x) for x in KATALOGI_NAGRAN) + "); "
    "$roz=@(" + ",".join(_ps_lit(x) for x in ROZSZERZENIA_NAGRAN) + "); "
    "$r=@(Get-ChildItem -LiteralPath 'C:\\Users' -Directory -Force -ErrorAction SilentlyContinue | "
    "Where-Object { $pom -notcontains $_.Name } | "
    "ForEach-Object { $k=$_.Name; $u=$_.FullName; foreach ($s in $kat) { $d=Join-Path $u $s; "
    "if (Test-Path -LiteralPath $d) { Get-ChildItem -LiteralPath $d -Recurse -File -Force "
    "-ErrorAction SilentlyContinue | Where-Object { $roz -contains $_.Extension.ToLower() } | "
    "ForEach-Object { [pscustomobject]@{konto=$k; sciezka=$_.FullName; rozmiar=$_.Length; "
    "wiek_s=[int](($t - $_.LastWriteTimeUtc).TotalSeconds)} } } } }); "
    "ConvertTo-Json -InputObject $r -Compress"
)


def _liczba_z(out) -> int:
    tekst = re.sub(r"#< CLIXML.*", "", str(out or ""), flags=re.S)
    m = re.search(r"(?m)^\s*(\d+)\s*$", tekst)
    if not m:
        raise ValueError("brak liczby w odpowiedzi")
    return int(m.group(1))


async def _lista_nagran(k: str) -> list[dict]:
    dane = _json_z(await _ps(k, _SKRYPT_NAGRAN, timeout=180))
    if isinstance(dane, dict):
        dane = [dane]
    return [d for d in dane if isinstance(d, dict)]


# 2026-11-09: prawdziwy katalog zapisu NVIDIA mierzy dziecko_fetch.katalogi_nagran_nvidia
# (08.11: .253 → D:\_shadowtest) — pliki spoza C:\Users też trafiają do przebiegu.
_IP_MASZYNY = {"197": IP_197, "253": IP_253, "130": IP_130}
_PODKATALOGI_POMIJANE = frozenset({"_audio", "highlights"})


def _skrypt_nagran_nvidia(katalogi) -> str:
    return (
        "$ProgressPreference='SilentlyContinue'; $t=[DateTime]::UtcNow; "
        "$kat=@(" + ",".join(_ps_lit(x) for x in katalogi) + "); "
        "$roz=@(" + ",".join(_ps_lit(x) for x in ROZSZERZENIA_NAGRAN) + "); "
        "$r=@(foreach ($d in $kat) { if (Test-Path -LiteralPath $d) { "
        "Get-ChildItem -LiteralPath $d -Recurse -File -Force -ErrorAction SilentlyContinue | "
        "Where-Object { $roz -contains $_.Extension.ToLower() } | "
        "ForEach-Object { $k='nvidia'; "
        r"if ($_.FullName -match '^[A-Za-z]:\\Users\\([^\\]+)\\') { $k=$Matches[1] }; "
        "[pscustomobject]@{konto=$k; sciezka=$_.FullName; rozmiar=$_.Length; "
        "wiek_s=[int](($t - $_.LastWriteTimeUtc).TotalSeconds)} } } }); "
        "ConvertTo-Json -InputObject $r -Compress"
    )


async def _katalogi_nvidia(k: str) -> list[str]:
    """Katalogi zapisu NVIDIA zmierzone dla IP maszyny k."""
    from app.services import dziecko_fetch

    ip = _IP_MASZYNY.get(k)
    odp = await dziecko_fetch.katalogi_nagran_nvidia()
    katalogi: list[str] = []
    for w in odp or []:
        if not isinstance(w, dict) or str(w.get("ip") or "").strip() != ip:
            continue
        d = str(w.get("katalog") or "").strip().rstrip("\\")
        if d.endswith(":"):
            d += "\\"
        if d and d not in katalogi:
            katalogi.append(d)
    return katalogi


async def _lista_nagran_nvidia(k: str, katalogi) -> list[dict]:
    dane = _json_z(await _ps(k, _skrypt_nagran_nvidia(katalogi), timeout=180))
    if isinstance(dane, dict):
        dane = [dane]
    return [d for d in dane if isinstance(d, dict)]


def _pod_katalogiem(sciezka: str, katalogi) -> tuple[str, str] | None:
    """(katalog, ścieżka względna), gdy ścieżka leży pod którymś katalogiem; inaczej None."""
    s = sciezka.lower()
    for kat in katalogi:
        pref = kat.lower().rstrip("\\") + "\\"
        if s.startswith(pref):
            return kat, sciezka[len(pref):]
    return None


async def _wolne_10() -> int:
    st = await asyncio.to_thread(os.statvfs, TOSHIBA_10)
    return st.f_bavail * st.f_frsize


async def _wolne_253() -> int:
    return _liczba_z(await _ps_253(
        "$ProgressPreference='SilentlyContinue'; (Get-PSDrive -Name E -PSProvider FileSystem).Free",
        timeout=60))


async def _wolne_172() -> int:
    rc, out, err = await _uruchom(
        SSH_172 + (f"df -B1 --output=avail {shlex.quote(DANE_172)}",), timeout=60)
    if rc != 0:
        raise RuntimeError(f"df na .172 rc={rc}: {err.strip()[:200]}")
    return _liczba_z(out)


async def _wolne_dyski() -> tuple[dict, list[str]]:
    """Wolne bajty każdego dysku Toshiba; dysk z nieudanym pomiarem nie bierze udziału."""
    funkcje = {"10": _wolne_10, "253": _wolne_253, "172": _wolne_172}
    odp = await asyncio.gather(*(f() for f in funkcje.values()), return_exceptions=True)
    wolne, bledy = {}, []
    for dysk, w in zip(funkcje, odp):
        if isinstance(w, BaseException):
            bledy.append(f"{_OPIS_DYSKU[dysk]}: pomiar miejsca nieudany ({type(w).__name__})")
        else:
            wolne[dysk] = int(w)
    return wolne, bledy


def _wezly(wolne: dict) -> list[dict]:
    return [
        {"node": d, "free_gb": round(max(0.0, wolne[d] / _GB - _ZAPAS_GB), 3),
         "gpu": "toshiba", "temp_c": None, "ok": True}
        for d in DYSKI if d in wolne
    ]


async def _wybierz_dysk(nodes: list[dict], need_gb: float) -> tuple[str | None, str]:
    if not nodes:
        return None, "żaden dysk nie podał wolnego miejsca"
    from app.services.node_hardware_truth import pick_node

    odp = pick_node(nodes, need_gb=need_gb, require_inference=False)
    if inspect.isawaitable(odp):
        odp = await odp
    if not isinstance(odp, dict):
        return None, f"nieoczekiwana odpowiedź pick_node ({type(odp).__name__})"
    decyzja = odp.get("decision")
    if isinstance(decyzja, dict):
        decyzja = decyzja.get("node")
    dysk = str(decyzja).strip().rsplit(".", 1)[-1] if decyzja not in (None, "") else ""
    if dysk in DYSKI:
        return dysk, ""
    return None, str(odp.get("reason") or odp.get("why") or f"decision={decyzja!r}")[:200]


async def _rozmiar_kopii(dysk: str, sciezka: str) -> int:
    if dysk == "10":
        return await asyncio.to_thread(os.path.getsize, sciezka)
    if dysk == "172":
        rc, out, err = await _uruchom(SSH_172 + (f"stat -c %s {shlex.quote(sciezka)}",), timeout=60)
        if rc != 0:
            raise RuntimeError(f"stat na .172 rc={rc}: {err.strip()[:200]}")
        return _liczba_z(out)
    if dysk == "253":
        return _liczba_z(await _ps_253(
            f"(Get-Item -LiteralPath {_ps_lit(sciezka)} -Force).Length", timeout=60))
    raise ValueError(f"nieznany dysk: {dysk}")


async def _usun_robocza(sciezka: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        await asyncio.to_thread(os.remove, sciezka)


async def _stan_maszyny(k: str) -> tuple[str, str]:
    try:
        if k == "197":
            odp = await _straz_197()
        elif k == "130":
            odp = await _straz_130()
        else:
            odp = await _straz_winrm(IP_253)
    except Exception as exc:  # noqa: BLE001
        odp = exc
    if k == "197":
        return _stan_197(odp)
    stan, opis = _stan_winrm(odp)
    if k == "130":
        try:
            from app.services import vram_manager
            if not vram_manager.is_guest_available():
                return GRA, "tryb gościa — .130 zajęta grą"
        except Exception:  # noqa: BLE001
            pass
    return stan, opis


async def _kopia_na_dysk(dysk: str, lokalny: str, k: str, nazwa: str, rozmiar: int,
                         wzor) -> str:
    """Kopia na wybrany dysk i weryfikacja; zwraca opis błędu albo "" przy zgodności."""
    try:
        miejsce = lokalny if dysk == "10" else await _wyslij_kopie(
            dysk, lokalny, k, nazwa, rodzaj="nagrania")
        jest = await _rozmiar_kopii(dysk, miejsce)
        if jest != rozmiar:
            return f"kopia ma {jest} B zamiast {rozmiar} B"
        kopia = str(await _hash_kopii(dysk, miejsce)).strip().lower()
        return "" if wzor and kopia == wzor else "SHA256 niezgodny"
    except Exception as exc:  # noqa: BLE001
        return f"kopia nieudana ({type(exc).__name__}: {exc})"


async def _przenies_nagranie(k: str, konto: str, sciezka: str, nazwa: str, rozmiar: int,
                             wynik: dict) -> None:
    powody = wynik["powody"]
    opis = f"{NAZWY[k]}: {konto}\\{ntpath.basename(sciezka)}"
    wolne, bledy = await _wolne_dyski()
    try:
        dysk, uwaga = await _wybierz_dysk(_wezly(wolne), rozmiar / _GB)
    except Exception as exc:  # noqa: BLE001
        dysk, uwaga = None, f"pick_node padł ({type(exc).__name__}: {exc})"
    if dysk is None:
        wynik["pominiete"] += 1
        szczegoly = "; ".join([*bledy, uwaga] if uwaga else bledy) or "brak powodu"
        powody.append(f"{opis}: trójkąt nie wybrał dysku ({szczegoly}) — oryginał zostaje")
        return
    try:
        wzor = (await _hash_zrodla(k, sciezka)).strip().lower()
    except Exception as exc:  # noqa: BLE001
        powody.append(f"{opis}: brak SHA256 źródła ({type(exc).__name__}) — oryginał zostaje")
        return
    katalog = os.path.join(MAGAZYN_10, f"nagrania_{k}")
    lokalny = os.path.join(katalog, nazwa)
    try:
        try:
            await asyncio.to_thread(os.makedirs, katalog, exist_ok=True)
            await _sciagnij(k, sciezka, lokalny)
            pobrano = await _rozmiar_kopii("10", lokalny)
        except Exception as exc:  # noqa: BLE001
            blad = f"pobranie na .10 nieudane ({type(exc).__name__}: {exc})"
        else:
            if pobrano != rozmiar:
                blad = f"pobrano {pobrano} B zamiast {rozmiar} B"
            else:
                blad = await _kopia_na_dysk(dysk, lokalny, k, nazwa, rozmiar, wzor)
    finally:
        if dysk != "10":
            try:
                await _usun_robocza(lokalny)
            except Exception as exc:  # noqa: BLE001
                log.warning("sciagala: usunięcie kopii roboczej %s padło: %s", lokalny, exc)
    if blad:
        powody.append(f"{opis} → {_OPIS_DYSKU[dysk]}: {blad} — oryginał zostaje")
        return
    wynik["skopiowane"] += 1
    wynik["bajty"] += rozmiar
    wynik["cele"][f"{k}:{sciezka}"] = dysk
    try:
        skasowano = await _skasuj_zrodlo(k, sciezka)
    except Exception as exc:  # noqa: BLE001
        log.warning("sciagala: kasowanie %s padło: %s", opis, exc)
        skasowano = False
    if skasowano:
        wynik["skasowane_oryginaly"] += 1
    else:
        powody.append(f"{opis}: kopia na {_OPIS_DYSKU[dysk]} zgodna, ale kasowanie oryginału nieudane")


async def _przebieg_nagran(k: str) -> dict:
    wynik = _nowy_wynik()
    stan, opis = await _stan_maszyny(k)
    if stan != WOLNA:
        wynik["powody"].append(f"{NAZWY[k]}: nagrania pominięte — {_powod(stan, opis)}")
        return wynik
    try:
        pliki = await _lista_nagran(k)
    except Exception as exc:  # noqa: BLE001
        wynik["powody"].append(
            f"{NAZWY[k]}: nie udało się wylistować nagrań ({type(exc).__name__}: {exc})")
        return wynik
    pliki = list(pliki)
    nv_katalogi: list[str] = []
    try:
        nv_katalogi = await _katalogi_nvidia(k)
        if nv_katalogi:
            dodatkowe = await _lista_nagran_nvidia(k, nv_katalogi)
            znane = {str(p.get("sciezka") or "").lower() for p in pliki}
            for p in dodatkowe:
                s = str(p.get("sciezka") or "").lower()
                if s and s not in znane:
                    znane.add(s)
                    pliki.append(p)
    except Exception as exc:  # noqa: BLE001
        nv_katalogi = []
        wynik["powody"].append(
            f"{NAZWY[k]}: katalogi_nagran_nvidia nieudane ({type(exc).__name__}: {exc})"
            " — zostaje lista z C:\\Users")
    # 2026-11-07: ręce shortów czytają świeże klipy z maszyn — nagrania młodsze niż N dni zostają.
    try:
        from app.services import brain_core

        tryby = await brain_core.get_brain_modes()
        dni_zostaw = int(str(tryby.get("nagrania_zostaw_dni", "")).strip())
        if dni_zostaw < 0:
            dni_zostaw = 7
    except Exception:  # noqa: BLE001
        dni_zostaw = 7
    granica_s = max(_MIN_WIEK_S, dni_zostaw * 86400)
    mlode = 0
    pominiete = 0
    for plik in pliki:
        konto = str(plik.get("konto") or "").strip()
        sciezka = str(plik.get("sciezka") or "")
        try:
            rozmiar = int(plik.get("rozmiar"))
            wiek = float(plik.get("wiek_s"))
        except (TypeError, ValueError):
            pominiete += 1
            continue
        if (not konto or konto.lower() in _KONTA_POMIJANE or wiek < _MIN_WIEK_S
                or rozmiar <= 0):
            pominiete += 1
            continue
        trafienie = _pod_katalogiem(sciezka, nv_katalogi)
        if trafienie is not None and wiek >= granica_s:
            kat_nv, wzgledna = trafienie
            czlony = wzgledna.split("\\")
            # _audio i Highlights zostają na maszynie (shorty_gaming_2, fetch_highlights_253).
            if any(c.lower() in _PODKATALOGI_POMIJANE for c in czlony[:-1]):
                pominiete += 1
                continue
            if (not wzgledna or ".." in czlony
                    or not sciezka.lower().endswith(ROZSZERZENIA_NAGRAN)):
                pominiete += 1
                wynik["powody"].append(f"{NAZWY[k]}: podejrzana ścieżka pominięta ({sciezka})")
                continue
            ostatni = ntpath.basename(kat_nv.rstrip("\\")) or kat_nv.rstrip("\\:")
            nazwa = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{konto}_{ostatni}_{wzgledna}", flags=re.A)
            await _przenies_nagranie(k, konto, sciezka, nazwa, rozmiar, wynik)
            continue
        if wiek < granica_s:
            mlode += 1
            continue
        # Kasujemy wyłącznie *.mp4/*.mkv/*.mov z trzech katalogów nagrań tego konta.
        kat = next((kt for kt in KATALOGI_NAGRAN
                    if sciezka.lower().startswith(f"c:\\users\\{konto}\\{kt}\\".lower())), None)
        if (kat is None or ".." in sciezka
                or not sciezka.lower().endswith(ROZSZERZENIA_NAGRAN)):
            pominiete += 1
            wynik["powody"].append(f"{NAZWY[k]}: podejrzana ścieżka pominięta ({sciezka})")
            continue
        wzgledna = sciezka[len(f"c:\\users\\{konto}\\{kat}\\"):]
        nazwa = re.sub(r"[^\w.-]+", "_", f"{konto}_{ntpath.basename(kat)}_{wzgledna}",
                       flags=re.ASCII)
        await _przenies_nagranie(k, konto, sciezka, nazwa, rozmiar, wynik)
    if pominiete:
        wynik["powody"].append(
            f"{NAZWY[k]}: pominięto {pominiete} nagrań — świeże (<10 min), konto systemowe "
            "lub złe dane")
    if mlode:
        wynik["powody"].append(
            f"{NAZWY[k]}: zostawiono {mlode} nagrań młodszych niż {dni_zostaw} dni (dla shortów)")
    wynik["pominiete"] += pominiete + mlode
    return wynik


async def przebieg_auto(maszyny=None) -> dict:
    """Powtórki Fortnite z .197/.253/.130 → trzy dyski Toshiba; oryginał znika po 3 zgodnych kopiach.

    maszyny: None (wszystkie), lista kluczy ["197", "253", "130"] albo pojedynczy klucz/adres IP.
    """
    global _ODBIORNIK
    wynik = _nowy_wynik()
    klucze, nieznane = _klucze(maszyny)
    wynik["powody"].extend(f"nieznana maszyna: {m!r} — pominięta" for m in nieznane)
    wynik["powody"].extend(f"{n}: wymaga operatora (bez integracji)" for n in ("PS5", "Xbox"))
    if not klucze:
        return wynik
    if _BLOKADA.locked():
        wynik["powody"].append("poprzedni przebieg jeszcze trwa — pomijam")
        return wynik
    async with _BLOKADA:
        stany = await asyncio.gather(
            *(_stan_maszyny(k) for k in klucze),
            return_exceptions=True,
        )
        wolne = []
        for k, stan_opis in zip(klucze, stany):
            if isinstance(stan_opis, BaseException):
                stan, opis = NIEDOSTEPNA, f"{type(stan_opis).__name__}"
            else:
                stan, opis = stan_opis
            if stan == WOLNA:
                wolne.append(k)
            else:
                wynik["powody"].append(f"{NAZWY[k]}: pominięto — {_powod(stan, opis)}")
        if "130" in wolne:
            try:
                _ODBIORNIK = await _odbiornik_start()
            except Exception as exc:  # noqa: BLE001
                wolne.remove("130")
                wynik["powody"].append(
                    f"{NAZWY['130']}: odbiornik HTTP PUT nie wstał ({type(exc).__name__}: {exc})")
        try:
            czesci = await asyncio.gather(
                *(_przebieg_maszyny(k) for k in wolne), return_exceptions=True)
            # Etap 2: duże nagrania — maszyny po kolei, bo każdy plik zajmuje miejsce
            # na wybranym dysku (pomiar wolnego miejsca przed każdym plikiem).
            nagrania = []
            for k in wolne:
                try:
                    nagrania.append(await _przebieg_nagran(k))
                except Exception as exc:  # noqa: BLE001
                    nagrania.append(exc)
        finally:
            if _ODBIORNIK is not None:
                odb, _ODBIORNIK = _ODBIORNIK, None
                try:
                    await _odbiornik_stop(odb)
                except Exception as exc:  # noqa: BLE001
                    log.warning("sciagala: zatrzymanie odbiornika padło: %s", exc)
        for k, czesc in [*zip(wolne, czesci), *zip(wolne, nagrania)]:
            if isinstance(czesc, BaseException):
                wynik["powody"].append(f"{NAZWY[k]}: przebieg padł ({type(czesc).__name__}: {czesc})")
                continue
            for pole in ("skopiowane", "skasowane_oryginaly", "bajty", "pominiete"):
                wynik[pole] += czesc[pole]
            wynik["powody"].extend(czesc["powody"])
            wynik["cele"].update(czesc.get("cele") or {})
    return wynik

SLUG = 'sciagala_nowe_nagrania'


async def start(params: dict | None = None, cmd: str = "") -> dict:
    """Wejście dopisane przez kuźnię — handler woła start(), ręka ma wykonaj()."""
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka {SLUG} padła: {exc}"}
