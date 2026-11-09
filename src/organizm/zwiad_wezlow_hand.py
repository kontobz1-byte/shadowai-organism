"""Ręka zwiad_wezlow (08.11.2026: kuźnia 4× nieudana — recenzent; wersja ucznia Claude z próby 3 zwiad_wezlow_4, przejrzana i wpięta ręcznie przez sesję Claude, decyzja operatora „1” dla rąk zwiadu): gdzie na węzłach LAN leży plik albo katalog o danej nazwie.

Tylko odczyt, bez LLM. Z polecenia bierze numer węzła (172, 253, 130, 237, 197; brak = wszystkie) i słowo
dłuższe niż 3 znaki (bez gdzie, jest, znajdź, zwiad, węzeł, węzle, maszynie, na i podobnych). Przed wstawieniem
do polecenia słowo jest cięte re.split(r"[^A-Za-z0-9_.-]", słowo) (tylko ASCII), a do powłoki trafia wyłącznie
pierwszy kawałek (z „comfy;rm -rf” tylko „comfy”). Nic innego z polecenia operatora nie trafia do powłoki.
Wszystkie węzły równolegle (asyncio.gather), limit 25 s na węzeł, do 15 trafień.

Kanały (istniejące funkcje):
  .172 — ssh user@example.com: find ... -maxdepth 6 -iname '*słowo*' | head -15
  .253 — ssh user@example.com: powershell Get-ChildItem ... -Filter '*słowo*' | Select-Object -First 15
  .130 — app.services.guest_hand.guest_exec(<to samo polecenie PowerShell>)
  .237, .197 — app.services.lan_actuator.run_winrm(<ip>, <to samo polecenie PowerShell>)

Kod ssh różny od 0 bez ścieżek (255 = ssh się nie połączył), pusty stdout z niepustym stderr, guest_exec
z ok=False i wyjątek kanału = „nie odpowiada: <powód>”, nigdy „nic pod tą nazwą”.
Ręka niczego na węzłach nie zmienia i nie zgaduje ścieżek — pokazuje dosłowne wyniki find / Get-ChildItem.
Agent może wołać rękę także krótkim „znajdź <nazwa>” (bez numeru = wszystkie węzły).
"""
from __future__ import annotations

import asyncio
import inspect
import re
import unicodedata

try:
    from app.services import guest_hand
    _BLAD_GUEST = ""
except Exception as _e:  # noqa: BLE001 — bez backendu kanał .130 zgłosi błąd zamiast zgadywać
    guest_hand = None
    _BLAD_GUEST = f"{type(_e).__name__}: {_e}"

try:
    from app.services import lan_actuator
    _BLAD_WINRM = ""
except Exception as _e:  # noqa: BLE001
    lan_actuator = None
    _BLAD_WINRM = f"{type(_e).__name__}: {_e}"

WZORZEC = (r"\bzwiad\w*\b|"
           r"\bznajd[źz]\w*\s+na\s+(?:wszystkich\s+|ka[żz]d\w*\s+)?"
           r"(?:maszyn\w*|w[ęe]z[łl]\w*|\.?(?:172|253|130|237|197)\b)|"
           r"\bgdzie\s+na\s+(?:\.?(?:172|253|130|237|197)\b|maszyn\w*|w[ęe]z[łl]\w*|wszystkich\b)")
GOTOWA = True

_WEJSCIE = re.compile(r"^\s*(?:znajd[źz]\w*|zwiad\w*|gdzie|poszukaj)\b", re.I)
TIMEOUT_WEZLA = 25.0
MAX_TRAFIEN = 15
WEZLY = {
    "172": {"ip": "10.0.0.172", "kanal": "ssh find", "login": "operator"},
    "253": {"ip": "10.0.0.253", "kanal": "ssh PowerShell", "login": "operator"},
    "130": {"ip": "10.0.0.130", "kanal": "guest_exec PowerShell"},
    "237": {"ip": "10.0.0.237", "kanal": "WinRM PowerShell"},
    "197": {"ip": "10.0.0.197", "kanal": "WinRM PowerShell"},
}
KATALOGI_172 = "/home/operator /srv/data /etc/systemd/system"
KATALOGI_WIN = {"253": r"C:\shadowAI,C:\ComfyUI,D:\ShadowAICont", "130": r"C:\shadowAI,C:\ComfyUI",
                "237": r"C:\shadowAI,C:\Users", "197": r"C:\shadowAI,C:\Users"}
_STOP = frozenset({
    "gdzie", "jest", "znajdz", "zwiad", "wezel", "wezle", "maszynie", "na",
    "wszystkich", "wszystkie", "wszedzie", "maszynach", "maszyny", "maszyn", "wezlach", "wezly", "wezlow",
    "wezla", "wezlami", "szukaj", "poszukaj", "pokaz", "sprawdz", "prosze", "zwiadu", "plik", "pliku",
    "katalog", "katalogu", "folder", "folderu", "jakis", "lezy",
})
_KRAWEDZ = " \t,.!?:„”\"'()[]{}<>«»"
_NR = re.compile(r"(?<![A-Za-z0-9_])(172|253|130|237|197)(?![A-Za-z0-9_])")
_SCIEZKA = re.compile(r"^(?:/|[A-Za-z]:\\|\\\\)")
_SZUM_STDERR = ("#< CLIXML", "<Objs")
_WYLACZONY = re.compile(r"no route to host|timed out|connection refused|host is down|network is unreachable", re.I)


# ---------------------------------------------------------------- drobne

def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _blad(e) -> str:
    m = " ".join(str(e).split())[:300]
    return f"{type(e).__name__}: {m}" if m else type(e).__name__


def _tekst(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def _odm(n, jeden, kilka, wiele):
    return jeden if n == 1 else (kilka if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else wiele)


# ---------------------------------------------------------------- polecenie operatora

def _wezly_z(cmd: str, params: dict) -> list:
    raw = params.get("wezel") or params.get("wezly")
    if raw:
        lista = [str(x).strip().lstrip(".") for x in raw] if isinstance(raw, (list, tuple)) \
            else re.findall(r"\d{3}", str(raw))
        zle = [x for x in lista if x not in WEZLY]
        if zle or not lista:
            raise ValueError(f"Nie znam węzła {', '.join(zle) or raw} — znam {', '.join(WEZLY)}")
    else:
        lista = _NR.findall(cmd)
    return [k for k in WEZLY if k in lista] or list(WEZLY)


def _slowa(cmd: str) -> list:
    out = []
    for tok in cmd.split():
        t = tok.strip(_KRAWEDZ)
        if len(t) <= 3 or _norm(t) in _STOP or re.fullmatch(r"[\d.]+", t):
            continue
        out.append(t)
    return out


def _ciecie(slowo: str) -> str:
    """Do powłoki trafia tylko pierwszy kawałek ASCII [A-Za-z0-9_.-]."""
    kaw = re.split(r"[^A-Za-z0-9_.-]", slowo)[0]
    return kaw if len(kaw) >= 3 and re.search(r"[A-Za-z0-9]", kaw) else ""


def _slowo_z(cmd: str, params: dict):
    kandydaci = [str(params["slowo"]).strip()] if params.get("slowo") else _slowa(cmd)
    if not kandydaci or not kandydaci[0]:
        raise ValueError("Nie widzę słowa do szukania (dłuższego niż 3 znaki) — np. „zwiad na 253 win_agent”")
    for oryg in kandydaci:
        kaw = _ciecie(oryg)
        if kaw:
            return oryg, kaw
    raise ValueError(f"Słowo „{kandydaci[0]}” nie zaczyna się od znaków ASCII (A-Z, 0-9, _ . -) — do powłoki "
                     f"nie wstawię nic innego; podaj nazwę w ASCII")


# ---------------------------------------------------------------- polecenia zdalne (tylko odczyt)

def _polecenie_find(slowo: str) -> str:
    return (f"find {KATALOGI_172} -maxdepth 6 -iname '*{slowo}*' 2>/dev/null | head -{MAX_TRAFIEN}")


def _polecenie_ps(klucz: str, slowo: str) -> str:
    return (f"Get-ChildItem -Path {KATALOGI_WIN[klucz]} -Recurse -Klient 5 -ErrorAction SilentlyContinue "
            f"-Filter '*{slowo}*' | Select-Object -First {MAX_TRAFIEN} -ExpandProperty FullName")


def _argv_ssh(klucz: str, slowo: str) -> list:
    w = WEZLY[klucz]
    if klucz == "172":
        zdalne = _polecenie_find(slowo)
    else:
        ps = _polecenie_ps(klucz, slowo)
        zdalne = f'powershell -NoProfile -NonInteractive -Command "{ps}"'
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"{w['login']}@{w['ip']}", zdalne]


# ---------------------------------------------------------------- kanały (granice I/O)

async def _wolaj(fn, *args):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    r = await asyncio.to_thread(fn, *args)
    return await r if inspect.isawaitable(r) else r


def _z_wyniku(r):
    """Wynik guest_exec / run_winrm → (kod, stdout, stderr)."""
    if r is None:
        return 1, "", "kanał nie zwrócił wyniku"
    if isinstance(r, (str, bytes, bytearray)):
        return 0, _tekst(r), ""
    if isinstance(r, dict):
        out = next((r[k] for k in ("stdout", "std_out", "output", "out") if r.get(k) is not None), "")
        err = next((r[k] for k in ("stderr", "std_err", "error", "reason", "message") if r.get(k)), "")
        kod = next((r[k] for k in ("returncode", "code", "exit_code", "status_code") if r.get(k) is not None), 0)
        kod = kod if isinstance(kod, int) and not isinstance(kod, bool) else 1
        if r.get("ok") is False:
            return kod or 1, _tekst(out), _tekst(err) or "kanał zwrócił ok=False bez opisu"
        return kod, _tekst(out), _tekst(err)
    if isinstance(r, (tuple, list)):
        teksty = [x for x in r if isinstance(x, (str, bytes, bytearray))]
        kody = [x for x in r if isinstance(x, int) and not isinstance(x, bool)]
        if any(x is False for x in r):
            kody = kody or [1]
        return (kody[0] if kody else 0), (_tekst(teksty[0]) if teksty else ""), \
            (_tekst(teksty[1]) if len(teksty) > 1 else "")
    return 1, "", f"nieczytelny wynik kanału ({type(r).__name__})"


async def _ssh(klucz: str, slowo: str):
    argv = _argv_ssh(klucz, slowo)
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except (ProcessLookupError, AttributeError):
            pass
        raise
    return proc.returncode, _tekst(out), _tekst(err)


async def _guest(klucz: str, slowo: str):
    if guest_hand is None:
        raise RuntimeError(f"brak app.services.guest_hand ({_BLAD_GUEST})")
    return _z_wyniku(await _wolaj(guest_hand.guest_exec, _polecenie_ps(klucz, slowo)))


async def _winrm(klucz: str, slowo: str):
    if lan_actuator is None:
        raise RuntimeError(f"brak app.services.lan_actuator ({_BLAD_WINRM})")
    return _z_wyniku(await _wolaj(lan_actuator.run_winrm, WEZLY[klucz]["ip"], _polecenie_ps(klucz, slowo)))


async def _kanal(klucz: str, slowo: str):
    if klucz in ("172", "253"):
        return await _ssh(klucz, slowo)
    if klucz == "130":
        return await _guest(klucz, slowo)
    return await _winrm(klucz, slowo)


# ---------------------------------------------------------------- ocena odpowiedzi węzła

def _z_podpowiedzia(powod: str) -> str:
    if _WYLACZONY.search(powod):
        return f"{powod} (węzeł wyłączony albo poza siecią)"
    if "permission denied" in powod.lower():
        return f"{powod} (klucz ssh nie wpuszcza)"
    return powod


def _ocen(kod, out: str, err: str) -> dict:
    kod = 0 if kod is None else kod
    linie = [x.strip() for x in _tekst(out).splitlines() if x.strip()]
    sciezki = [x for x in linie if _SCIEZKA.match(x)]
    bledy = [x.strip() for x in _tekst(err).splitlines() if x.strip() and not x.lstrip().startswith(_SZUM_STDERR)]
    pierwsza = bledy[0][:300] if bledy else ""
    if kod == 255 or (kod != 0 and not sciezki):
        return {"powod": _z_podpowiedzia(pierwsza or f"kod wyjścia {kod} bez komunikatu"), "sciezki": []}
    if not linie and pierwsza:
        return {"powod": _z_podpowiedzia(pierwsza), "sciezki": []}
    if linie and not sciezki:
        return {"powod": f"odpowiedź bez ścieżek: {linie[0][:200]}", "sciezki": []}
    return {"powod": "", "sciezki": sciezki[:MAX_TRAFIEN], "kod": kod}


async def _zwiad_wezla(klucz: str, slowo: str) -> dict:
    try:
        kod, out, err = await asyncio.wait_for(_kanal(klucz, slowo), TIMEOUT_WEZLA)
    except TimeoutError:
        return {"powod": f"limit czasu ({TIMEOUT_WEZLA:g} s) — węzeł nie odpowiedział", "sciezki": []}
    except Exception as e:  # noqa: BLE001
        return {"powod": f"błąd kanału ({_blad(e)})", "sciezki": []}
    return _ocen(kod, out, err)


# ---------------------------------------------------------------- odpowiedź

def _sekcja(klucz: str, w) -> list:
    info = WEZLY[klucz]
    li = [f"== .{klucz} ({info['ip']}, {info['kanal']}) =="]
    if isinstance(w, BaseException):
        li.append(f"  nie odpowiada: błąd kanału ({_blad(w)})")
    elif w["powod"]:
        li.append(f"  nie odpowiada: {w['powod']}")
    elif not w["sciezki"]:
        li.append("  nic pod tą nazwą")
    else:
        n = len(w["sciezki"])
        li.append(f"  {n} {_odm(n, 'trafienie', 'trafienia', 'trafień')}"
                  + (f" (pokazuję pierwsze {MAX_TRAFIEN} — może być więcej)" if n >= MAX_TRAFIEN else "") + ":")
        li += [f"  {p}" for p in w["sciezki"]]
        if w.get("kod"):
            li.append(f"  (kod wyjścia {w['kod']} — część katalogów mogła być niedostępna)")
    return li


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not (re.search(WZORZEC, cmd, re.I) or _WEJSCIE.search(cmd)):
        return {"ok": False, "reason": "To nie jest zwiad węzłów (np. „zwiad na 253 win_agent”, "
                                       "„gdzie na 172 jest bielik”)."}
    params = params or {}
    try:
        wezly = _wezly_z(cmd, params)
        oryg, slowo = _slowo_z(cmd, params)
    except ValueError as e:
        return {"ok": False, "reason": f"{e}."}
    wyniki = await asyncio.gather(*(_zwiad_wezla(k, slowo) for k in wezly), return_exceptions=True)
    opis = "wszystkie węzły" if len(wezly) == len(WEZLY) else ", ".join(f".{k}" for k in wezly)
    li = [f"Zwiad węzłów ({opis}) — szukam „{slowo}” w nazwach plików i katalogów; tylko odczyt, "
          f"do {MAX_TRAFIEN} trafień, limit {TIMEOUT_WEZLA:g} s na węzeł."]
    if slowo != oryg:
        li.append(f"Do poleceń trafia tylko „{slowo}” (z „{oryg}” odciąłem resztę).")
    for k, w in zip(wezly, wyniki):
        li += _sekcja(k, w)
    li.append("Na węzłach niczego nie zmieniłem; ścieżki to dosłowne wyniki find / Get-ChildItem.")
    tekst = "\n".join(li)
    odpowiedzialy = [w for w in wyniki if isinstance(w, dict) and not w["powod"]]
    if not odpowiedzialy:
        return {"ok": False, "reason": "Żaden węzeł nie odpowiedział — nie zgaduję ścieżek.\n" + tekst}
    return {"ok": True, "reply": tekst}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka zwiad_wezlow padła: {_blad(e)}"}


async def handler_zwiad_wezlow(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}
