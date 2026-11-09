# plik: teledysk_ruchu_hand.py
"""Ręka teledysk_ruchu: WYSYŁKA BRIEFU DO RUCHU WAN.

Brief to katalog BRIEFY/<NN_slug>/ z plikami ręki teledysk_rezyseria_2:
  koncepcja.json (pola prompty, wzorce, osoby, bohater), kadry_opis.json, sceny_szkic.json,
  kadry_qwen_v1/ (próby kadrów), obrazy_indeks.json ({id: nazwa pliku wybranej próby}),
  kontrola_kadrow.json (wynik kontroli kadrów: ok / odrzucony / do przeglądu).

Bez „wykonaj” (albo „zatwierdź”) = PODGLĄD: ile kadrów jest, ile brakuje, ile odrzuconych, czy warunek
wysyłki jest spełniony i co zrobiłoby „wykonaj”. Podgląd nie dotyka .253 i niczego nie uruchamia
(tylko `systemctl --user is-active`). Słowo „wyślij” jest tu czasownikiem samego polecenia,
więc nie uruchamia zmian — operator wymaga jawnego „wykonaj”.

Z „wykonaj”:
  1. Gdy kadrów brakuje albo są odrzucone — liczenie w tle przez
     `systemd-run --user --unit=teledysk-kadry-<slug>` (restart backendu go nie zabija), log <katalog>/kadry.log.
     Unit uruchamia ten moduł w trybie `--tlo <katalog>`: generator kadrów (TELEDYSK_KADRY_GEN, dostaje
     katalog briefu i sam pomija gotowe kadry z kontrolą), potem ponowne sprawdzenie kontroli
     i dalsze kroki jak w punkcie 2.
  2. Gdy kadry są komplet i kontrola_kadrow.json nie ma kadrów „do przeglądu” (albo w poleceniu jest jawna
     zgoda, np. „mimo przeglądu”):
       - POWIĘKSZENIE: /opt/shadow/ComfyUI/venv/bin/python .../tools/sr_kadry.py
         <katalog>/obrazy_indeks.json <katalog>/kadry_qwen_v1 <katalog>/kadry_2x, kontrola PNG 2688×1536,
       - arkusz podglądu <katalog>/arkusz_kadrow.jpg (ffmpeg tile, 7 kolumn po 384 px),
       - KOLEJKA RUCHU na .253: scp C:/shadowAI/kolejka.json → dopisanie wpisu (jeśli go nie ma) →
         scp z powrotem → ponowne pobranie i sprawdzenie wpisu. Kopia kolejki sprzed zmiany zostaje
         w <katalog>/out/kolejka_253_przed_<data>.json.
     Kolejka sama pobiera brief z .10, liczy Wan, post3 i montaż, a podstawę odsyła do <katalog>/out/podstawa.mp4.

Granice I/O (podmieniane w testach): _czytaj_brief (pliki briefu), _uruchom (każdy proces: sr_kadry,
ffmpeg, systemd-run, systemctl, generator), _pobierz (scp z .253), _wyslij_scp (scp na .253),
_wymiary_png, _czytaj_plik, _zapisz_plik, _brakujace_pliki.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shlex
import struct
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

WZORZEC = r"\b(?:wy[śs]lij|wysy[łl]\w*|przeka[żz]\w*|pu[śs][ćc]\w*)\s+(?:do\s+ruchu\s+(?:wan\s+)?(?:teledysk\w*|klip\w*|brief\w*)|(?:brief\w*\s+)?(?:teledysk\w*|klip\w*|brief\w*)\b.{0,80}?\bdo\s+ruchu\b)"
GOTOWA = True

BRIEFY = Path(os.environ.get("TELEDYSK_BRIEFY", "/opt/shadow/teledysk_java/briefy_1017"))
PY_SR = os.environ.get("TELEDYSK_SR_PYTHON", "/opt/shadow/ComfyUI/venv/bin/python")
SR_SKRYPT = os.environ.get("TELEDYSK_SR_SKRYPT", "/opt/shadow/teledysk_java/plyta_pl_v3/tools/sr_kadry.py")
GEN_KADRY = tuple(shlex.split(os.environ.get(
    "TELEDYSK_KADRY_GEN",
    "/opt/shadow/shadow/backend/.venv/bin/python "
    "/opt/shadow/shadow/backend/app/services/teledysk_kadry_qwen_hand.py --brief")))
KLUCZ_SSH = "/opt/shadow/.ssh/shadowai_gpu"
KOLEJKA_253 = "user@example.com:C:/shadowAI/kolejka.json"
KOLEJKA_OPIS = r"C:\shadowAI\kolejka.json"
LOCAL_253 = r"D:\ShadowAICont\_persona\creative\Podmiana_kanalow_muzycznych_2026-10\Briefy"
REMOTE_10 = "teledysk_java/briefy_1017"
SCP_OPCJE = ("-q", "-i", KLUCZ_SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10")

SR_W, SR_H = 2688, 1536
ARKUSZ_KOL, ARKUSZ_PX = 7, 384
TIMEOUT_SR = 3600.0
TIMEOUT_FFMPEG = 300.0
TIMEOUT_SCP = 90.0
TIMEOUT_KROTKI = 30.0

K_KONC, K_KADRY, K_SCENY = "koncepcja.json", "kadry_opis.json", "sceny_szkic.json"
K_INDEKS, K_KONTROLA = "obrazy_indeks.json", "kontrola_kadrow.json"
PLIKI_JSON = (K_KONC, K_KADRY, K_SCENY, K_INDEKS, K_KONTROLA)
D_QWEN, D_2X = "kadry_qwen_v1", "kadry_2x"
ARKUSZ, LOG = "arkusz_kadrow.jpg", "kadry.log"
POLA_KONC = ("prompty", "wzorce", "osoby", "bohater")
DZIALA = ("active", "activating", "reloading", "deactivating", "refreshing")

UTWORY = (
    {"tytul": "Heavy Burdens", "wzor": r"heavy\s+burdens?|\b(?:ang|eng)\w*\s+wag\w*\s+wa[żz]n",
     "katalogi": ("33_heavy_burdens_en",)},
    {"tytul": "Echo in the Crowd", "wzor": r"echo\s+in\s+the\s+crowd|\b(?:ang|eng)\w*\s+przesileni",
     "katalogi": ("35_echo_in_the_crowd_en",)},
    {"tytul": "Przesilenie", "wzor": r"przesileni\w*\s+flow", "katalogi": ("34_przesilenie_flow",)},
    {"tytul": "Przesilenie", "wzor": r"przesileni\w*\s+g[łl]os", "katalogi": ("30_przesilenie_glos",)},
    {"tytul": "Przesilenie", "wzor": r"przesileni", "katalogi": ("34_przesilenie_flow", "30_przesilenie_glos")},
    {"tytul": "Waga ważna", "wzor": r"\bwag\w*\s+wa[żz]n\w*\s+flow", "katalogi": ("32_waga_wazna_flow",)},
    {"tytul": "Waga ważna", "wzor": r"\bwag\w*\s+wa[żz]n", "katalogi": ("32_waga_wazna_flow", "31_waga_wazna")},
)

# „wyślij” to czasownik samego polecenia — zmiany tylko z „wykonaj” / „zatwierdź”.
_T_WYKONAJ = re.compile(r"\b(?:wykonaj|zatwierd[źz])\b", re.I)
_T_ZGODA = re.compile(r"\b(?:mimo\s+(?:przegl\w*|kontrol\w*|uwag\w*)|bez\s+przegl\w*|pomi[ńn]\w*\s+przegl\w*|"
                      r"zgoda|zgadzam\s+si[ęe]|akceptuj[ęe]\s+kadry)\b", re.I)

LISTA_KADRY = ("kadry", "sceny", "opisy", "lista", "frames", "ujecia", "ujęcia")
LISTA_KONTROLA = ("kadry", "wyniki", "lista", "kontrola", "frames", "oceny")
ID_KOL = ("id", "kadr", "klucz", "key", "nr", "numer", "scena", "idx", "index")
STATUS_KOL = ("status", "stan", "wynik", "werdykt", "decyzja", "ocena", "verdict", "result")
GRUPY = {"do_przegladu": "przeglad", "przeglad": "przeglad", "odrzucone": "odrzucony", "odrzucony": "odrzucony",
         "ok": "ok", "zaakceptowane": "ok", "gotowe": "ok"}
RANGA = {"ok": 0, "odrzucony": 1, "przeglad": 2}


class BladProcesu(RuntimeError):
    pass


class BladScp(RuntimeError):
    pass


class BladKolejki(RuntimeError):
    pass


def _k(e, n=300):
    return " ".join(str(e).split())[:n] or type(e).__name__


def _blad(e) -> str:
    msg = " ".join(str(e).split())[:300]
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def _odm(n, jeden, kilka, wiele):
    if n == 1:
        return jeden
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return kilka
    return wiele


def _data() -> str:
    return time.strftime("%Y-%m-%d_%H%M%S")


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s).lower().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _klucz(v) -> str:
    s = str(v).strip().lower()
    m = re.fullmatch(r"\D{0,12}?0*(\d+)", s)
    return str(int(m.group(1))) if m else s


def _lista_txt(xs, n=15) -> str:
    xs = [str(x) for x in xs]
    return ", ".join(xs[:n]) + (f" (+{len(xs) - n} więcej)" if len(xs) > n else "")


def _unit(slug: str) -> str:
    return f"teledysk-kadry-{slug}"


def _wpis(slug: str) -> dict:
    return {"id": slug, "local": LOCAL_253 + "\\" + slug, "remote": f"{REMOTE_10}/{slug}",
            "out": "out/podstawa.mp4", "esrgan": "foto", "pobierz": True, "wymaga_kadry": True}


# ---------------------------------------------------------------- rozpoznanie teledysku

def _cel(cmd: str, params: dict):
    kat = str(params.get("katalog") or "").strip()
    if kat:
        p = Path(kat)
        return {"tytul": p.name, "katalogi": [p]}
    slug = str(params.get("slug") or "").strip()
    if not slug:
        m = re.search(r"\b(\d{2}_[a-z0-9_]+)", cmd, re.I)
        slug = m.group(1).lower() if m else ""
    if slug:
        tytul = next((u["tytul"] for u in UTWORY if slug in u["katalogi"]), slug)
        return {"tytul": tytul, "katalogi": [BRIEFY / slug]}
    for u in UTWORY:
        if re.search(u["wzor"], cmd, re.I):
            return {"tytul": u["tytul"], "katalogi": [BRIEFY / k for k in u["katalogi"]]}
    return None


# ---------------------------------------------------------------- stan kadrów (czyste funkcje)

def _lista_z(dane, klucze):
    if isinstance(dane, list):
        return dane
    if isinstance(dane, dict):
        for k in klucze:
            if isinstance(dane.get(k), list):
                return dane[k]
    return None


def _id_z(v: dict):
    for k in ID_KOL:
        x = v.get(k)
        if x is None or isinstance(x, bool):
            continue
        if isinstance(x, (int, str)) and str(x).strip():
            return str(x).strip()
    return None


def _kadry_ids(dane) -> list:
    lista = _lista_z(dane, LISTA_KADRY)
    surowe = []
    if lista is not None:
        for i, v in enumerate(lista):
            surowe.append((_id_z(v) if isinstance(v, dict) else None) or f"s{i:02d}")
    elif isinstance(dane, dict):
        surowe = [str(k) for k in dane if re.search(r"\d", str(k))]
    widziane, out = set(), []
    for x in surowe:
        k = _klucz(x)
        if k not in widziane:
            widziane.add(k)
            out.append(x)
    return out


def _kat_statusu(s: str) -> str:
    n = _norm(s).strip()
    if re.search(r"przegl|review|recznie|sprawdz|niepewn|watpliw", n):
        return "przeglad"
    if re.search(r"odrzu|reject|fail|\bzl[ya]?\b|blad|popraw|przelicz|ponown", n):
        return "odrzucony"
    if n.startswith("nie"):
        return "przeglad"
    if re.search(r"\bok\b|akcept|gotow|pass|dobr|zalicz|zatwierdz|approv|accept", n):
        return "ok"
    return "przeglad"


def _status(v):
    if isinstance(v, bool):
        return ("ok", "ok") if v else ("odrzucony", "odrzucony")
    if isinstance(v, str):
        return _kat_statusu(v), v.strip()
    if isinstance(v, dict):
        for k in STATUS_KOL:
            x = v.get(k)
            if isinstance(x, (str, bool)):
                return _status(x)
        if v.get("do_przegladu") is True:
            return "przeglad", "do przeglądu"
        if v.get("odrzucony") is True or v.get("odrzucone") is True:
            return "odrzucony", "odrzucony"
        if isinstance(v.get("ok"), bool):
            return _status(v["ok"])
    return "przeglad", "bez statusu"


def _kontrola(dane) -> dict:
    out: dict = {}

    def ustaw(kid, kat, raw):
        k = _klucz(kid)
        stary = out.get(k)
        if stary is None or RANGA[kat] > RANGA[stary["kat"]]:
            out[k] = {"id": str(kid).strip(), "kat": kat, "status": raw}

    lista = _lista_z(dane, LISTA_KONTROLA)
    if lista is not None:
        for v in lista:
            kid = _id_z(v) if isinstance(v, dict) else None
            if kid is not None:
                ustaw(kid, *_status(v))
    if isinstance(dane, dict):
        for k, v in dane.items():
            n = _norm(k).replace(" ", "_")
            if n in GRUPY and isinstance(v, list):
                for x in v:
                    if isinstance(x, (str, int)) and not isinstance(x, bool):
                        ustaw(x, GRUPY[n], str(k))
            elif lista is None and re.search(r"\d", str(k)) and len(str(k)) <= 24:
                ustaw(k, *_status(v))
    return out


def _indeks_z_kontroli(dane, pliki: set) -> dict:
    """Indeks {id: nazwa pliku} z kontrola_kadrow.json (kadry.<id>.plik), gdy brak obrazy_indeks.json.

    Tylko kadry, których plik jest w kadry_qwen_v1; nazwa bez katalogu."""
    kadry = dane.get("kadry") if isinstance(dane, dict) else None
    out: dict = {}
    if not isinstance(kadry, dict):
        return out
    for kid, v in kadry.items():
        plik = v.get("plik") if isinstance(v, dict) else None
        if isinstance(plik, str) and plik.strip():
            nazwa = Path(plik.strip()).name
            if nazwa in pliki:
                out[str(kid).strip()] = nazwa
    return out


def _stan(st: dict) -> dict:
    d = st["dane"]
    ids = _kadry_ids(d.get(K_KADRY))
    surowy = d.get(K_INDEKS) if isinstance(d.get(K_INDEKS), dict) else {}
    zbudowany = None
    if d.get(K_INDEKS) is None and K_INDEKS not in (st.get("bledy") or {}):
        zbudowany = _indeks_z_kontroli(d.get(K_KONTROLA), set(st.get("qwen") or []))
        surowy = zbudowany
    ind = {_klucz(k): Path(v.strip()).name for k, v in surowy.items() if isinstance(v, str) and v.strip()}
    pliki = set(st.get("qwen") or [])
    ma_kontrole = d.get(K_KONTROLA) is not None
    kontr = _kontrola(d.get(K_KONTROLA))
    jest, brak, odrz, gotowe, bez = [], [], [], [], []
    for lab in ids:
        k = _klucz(lab)
        ma = k in ind and ind[k] in pliki
        e = kontr.get(k)
        (jest if ma else brak).append(lab)
        if e and e["kat"] == "odrzucony":
            odrz.append(lab)
        if ma and e and e["kat"] == "ok":
            gotowe.append(lab)
        if ma and e is None and ma_kontrole:
            bez.append(lab)
    przeglad = [e["id"] for e in kontr.values() if e["kat"] == "przeglad"]
    do_liczenia = [lab for lab in ids if lab in brak or lab in odrz]
    blokady = []
    if K_KONTROLA in st["bledy"]:
        blokady.append(f"{K_KONTROLA} nieczytelny ({st['bledy'][K_KONTROLA]})")
    elif not ma_kontrole:
        blokady.append(f"brak {K_KONTROLA} (nie wiem, czy coś jest do przeglądu)")
    if przeglad:
        blokady.append("do przeglądu: " + _lista_txt(przeglad))
    if bez:
        blokady.append("bez kontroli: " + _lista_txt(bez))
    return {"ids": ids, "jest": jest, "brak": brak, "odrz": odrz, "gotowe": gotowe, "bez": bez,
            "przeglad": przeglad, "do_liczenia": do_liczenia, "blokady": blokady, "n_indeks": len(ind),
            "indeks_zbudowany": zbudowany}


def _twarde_braki(st: dict):
    b, d, kat = st["bledy"], st["dane"], st["katalog"]
    if K_KADRY in b:
        return f"{K_KADRY} w {kat} nieczytelny ({b[K_KADRY]})."
    if d.get(K_KADRY) is None:
        return f"Brak {K_KADRY} w {kat} — brief niegotowy (najpierw teledysk_rezyseria_2)."
    if K_INDEKS in b:
        return f"{K_INDEKS} w {kat} nieczytelny ({b[K_INDEKS]}) — nie policzę kadrów na ślepo."
    if d.get(K_INDEKS) is not None and not isinstance(d[K_INDEKS], dict):
        return f"{K_INDEKS} w {kat} nie jest słownikiem {{id: nazwa pliku}}."
    return None


def _braki_briefu(st: dict) -> list:
    b, d, out = st["bledy"], st["dane"], []
    if K_KONC in b:
        out.append(f"{K_KONC} nieczytelny ({b[K_KONC]})")
    elif d.get(K_KONC) is None:
        out.append(f"brak {K_KONC}")
    elif not isinstance(d[K_KONC], dict):
        out.append(f"{K_KONC} nie jest obiektem")
    else:
        brak = [p for p in POLA_KONC if p not in d[K_KONC]]
        if brak:
            out.append(f"{K_KONC} bez pól: {', '.join(brak)}")
    if K_SCENY in b:
        out.append(f"{K_SCENY} nieczytelny ({b[K_SCENY]})")
    elif d.get(K_SCENY) is None:
        out.append(f"brak {K_SCENY}")
    return out


# ---------------------------------------------------------------- argumenty procesów

def _argv_sr(kat: Path) -> list:
    return [PY_SR, SR_SKRYPT, str(kat / K_INDEKS), str(kat / D_QWEN), str(kat / D_2X)]


def _argv_arkusz(kat: Path, n: int) -> list:
    rzedy = max(1, math.ceil(n / ARKUSZ_KOL))
    return ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-pattern_type", "glob",
            "-i", str(kat / D_2X / "*.png"), "-vf", f"scale={ARKUSZ_PX}:-2,tile={ARKUSZ_KOL}x{rzedy}",
            "-frames:v", "1", "-q:v", "3", str(kat / ARKUSZ)]


def _argv_tla(kat: Path, slug: str, zgoda: bool) -> list:
    log = kat / LOG
    argv = ["systemd-run", "--user", f"--unit={_unit(slug)}", "--collect", "--quiet",
            f"--description=Shadow: kadry teledysku {slug} (ruch WAN)",
            f"--property=WorkingDirectory={kat}",
            f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
            "--setenv=PYTHONUNBUFFERED=1"]
    argv += [f"--setenv={k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("TELEDYSK_")]
    argv += ["--", sys.executable, str(Path(__file__).resolve()), "--tlo", str(kat)]
    if zgoda:
        argv.append("--zgoda")
    return argv


# ---------------------------------------------------------------- granice I/O

def _env() -> dict:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


async def _uruchom(argv, timeout, przekaz: bool = False) -> dict:
    """Jedyne miejsce, które startuje procesy (sr_kadry, ffmpeg, scp, systemd-run, systemctl, generator)."""
    argv = [str(a) for a in argv]
    rura = None if przekaz else asyncio.subprocess.PIPE
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=rura,
                                                    stderr=rura, env=_env())
    except OSError as e:
        return {"kod": 127, "stdout": "", "stderr": f"nie uruchomię {argv[0]}: {_blad(e)}"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BladProcesu(f"{Path(argv[0]).name} nie skończył w {timeout:.0f} s") from None
    return {"kod": proc.returncode, "stdout": (out or b"").decode("utf-8", "replace"),
            "stderr": (err or b"").decode("utf-8", "replace")}


async def _pobierz(zdalny: str, lokalny: Path) -> None:
    """scp z .253 na dysk lokalny (klucz shadowai_gpu)."""
    r = await _uruchom(["scp", *SCP_OPCJE, zdalny, str(lokalny)], TIMEOUT_SCP)
    if r["kod"] != 0:
        raise BladScp(f"scp {zdalny} kod {r['kod']}: {_k(r['stderr'] or r['stdout'] or 'pusto', 200)}")


async def _wyslij_scp(lokalny: Path, zdalny: str) -> None:
    """scp z dysku lokalnego na .253."""
    r = await _uruchom(["scp", *SCP_OPCJE, str(lokalny), zdalny], TIMEOUT_SCP)
    if r["kod"] != 0:
        raise BladScp(f"scp → {zdalny} kod {r['kod']}: {_k(r['stderr'] or r['stdout'] or 'pusto', 200)}")


async def _czytaj_plik(p: Path) -> bytes:
    return await asyncio.to_thread(Path(p).read_bytes)


async def _zapisz_plik(p: Path, dane: bytes) -> None:
    def run():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".shadow_tmp")
        tmp.write_bytes(dane)
        os.replace(tmp, p)
    await asyncio.to_thread(run)


async def _brakujace_pliki(sciezki) -> list:
    return await asyncio.to_thread(lambda: [str(p) for p in sciezki if not Path(p).is_file()])


async def _wymiary_png(katalog: Path) -> list:
    """(nazwa, szerokość, wysokość) każdego PNG z nagłówka IHDR."""
    def run():
        out = []
        if not katalog.is_dir():
            return out
        for p in sorted(katalog.iterdir()):
            if not p.is_file() or p.suffix.lower() != ".png":
                continue
            with p.open("rb") as f:
                h = f.read(24)
            if len(h) >= 24 and h[:8] == b"\x89PNG\r\n\x1a\n" and h[12:16] == b"IHDR":
                w, hh = struct.unpack(">II", h[16:24])
            else:
                w = hh = None
            out.append((p.name, w, hh))
        return out
    return await asyncio.to_thread(run)


def _wczytaj_sync(katalogi: list) -> dict:
    istn = [Path(k) for k in katalogi if Path(k).is_dir()]
    if len(istn) > 1:
        istn = [k for k in istn if (k / K_KADRY).is_file()] or istn
    st = {"szukane": [str(k) for k in katalogi], "istniejace": [str(k) for k in istn], "katalog": None}
    if len(istn) != 1:
        return st
    kat = istn[0]
    st.update(katalog=kat, dane={}, bledy={})
    for nazwa in PLIKI_JSON:
        p = kat / nazwa
        st["dane"][nazwa] = None
        if not p.is_file():
            continue
        try:
            st["dane"][nazwa] = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            st["bledy"][nazwa] = _blad(e)
    q, x2 = kat / D_QWEN, kat / D_2X
    st["qwen"] = sorted(p.name for p in q.iterdir() if p.is_file()) if q.is_dir() else []
    st["png2x"] = sorted(p.name for p in x2.iterdir() if p.is_file() and p.suffix.lower() == ".png") \
        if x2.is_dir() else []
    st["arkusz"] = (kat / ARKUSZ).is_file()
    st["log"] = ""
    log = kat / LOG
    if log.is_file():
        try:
            with log.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                linie = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
            st["log"] = linie[-1].strip()[:200] if linie else ""
        except OSError:
            pass
    return st


async def _czytaj_brief(katalogi: list) -> dict:
    """Pliki briefu, listy kadrów (kadry_qwen_v1, kadry_2x), arkusz i ostatnia linia kadry.log."""
    return await asyncio.to_thread(_wczytaj_sync, list(katalogi))


async def _stan_unitu(unit: str) -> str:
    try:
        r = await _uruchom(["systemctl", "--user", "is-active", unit], TIMEOUT_KROTKI)
    except Exception:  # noqa: BLE001
        return "nieznany"
    if r["kod"] == 127:
        return "nieznany"
    linie = r["stdout"].strip().splitlines()
    return linie[-1].strip() if linie else "inactive"


# ---------------------------------------------------------------- kolejka .253

def _ma_wpis(wpisy: list, slug: str) -> bool:
    return any(isinstance(x, dict) and str(x.get("id")) == slug for x in wpisy)


async def _kolejka_teraz():
    with tempfile.TemporaryDirectory(prefix="shadow_kolejka_") as d:
        p = Path(d) / "kolejka.json"
        await _pobierz(KOLEJKA_253, p)
        surowe = await _czytaj_plik(p)
    try:
        dane = json.loads(surowe.decode("utf-8-sig"))
    except ValueError as e:
        raise BladKolejki(f"kolejka.json na .253 nieczytelna ({_blad(e)})") from None
    if not isinstance(dane, list):
        raise BladKolejki("kolejka.json na .253 nie jest listą")
    return surowe, dane


async def _kolejka_dopisz(slug: str, kat: Path, data: str) -> dict:
    surowe, wpisy = await _kolejka_teraz()
    if _ma_wpis(wpisy, slug):
        return {"stan": "juz", "n": len(wpisy)}
    kopia = kat / "out" / f"kolejka_253_przed_{data}.json"
    await _zapisz_plik(kopia, surowe)
    wpis = _wpis(slug)
    tresc = (json.dumps(wpisy + [wpis], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="shadow_kolejka_") as d:
        p = Path(d) / "kolejka.json"
        await _zapisz_plik(p, tresc)
        await _wyslij_scp(p, KOLEJKA_253)
    _, po = await _kolejka_teraz()
    moje = [x for x in po if isinstance(x, dict) and str(x.get("id")) == slug]
    if not moje:
        raise BladKolejki("po odesłaniu ponowne pobranie nie pokazuje wpisu")
    if moje[0].get("remote") != wpis["remote"] or moje[0].get("local") != wpis["local"]:
        raise BladKolejki(f"wpis po odesłaniu różni się od wysłanego: {_k(json.dumps(moje[0]), 200)}")
    return {"stan": "dopisany", "n": len(po), "kopia": str(kopia)}


async def _potok(kat: Path, slug: str, n_kadrow: int, data: str, indeks: dict | None = None) -> dict:
    try:
        _, wpisy = await _kolejka_teraz()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f".253 nie odpowiada albo kolejka nieczytelna ({_blad(e)}) — niczego nie "
                                       "powiększałem i do kolejki nie wysłałem."}
    if _ma_wpis(wpisy, slug):
        return {"ok": True, "juz": True, "linie": [
            f"Kolejka .253: wpis „{slug}” już jest w {KOLEJKA_OPIS} — nie dopisuję drugi raz i nie powtarzam "
            "powiększenia."]}
    if indeks and await _brakujace_pliki([kat / K_INDEKS]):
        # indeks zbudowany z kontrola_kadrow.json — sr_kadry potrzebuje pliku; istniejącego nie nadpisuję
        await _zapisz_plik(kat / K_INDEKS,
                           (json.dumps(indeks, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    r = await _uruchom(_argv_sr(kat), TIMEOUT_SR)
    if r["kod"] != 0:
        return {"ok": False, "reason": f"Powiększenie padło: sr_kadry.py kod {r['kod']} "
                                       f"({_k(r['stderr'] or r['stdout'] or 'bez komunikatu', 300)}) — "
                                       "do kolejki nie wysłałem."}
    wym = await _wymiary_png(kat / D_2X)
    if len(wym) < n_kadrow:
        return {"ok": False, "reason": f"Po powiększeniu w {kat / D_2X} jest {len(wym)} PNG, a {K_INDEKS} ma "
                                       f"{n_kadrow} — do kolejki nie wysłałem."}
    zle = [f"{n} ({w}×{h})" if w is not None else f"{n} (to nie PNG)"
           for n, w, h in wym if (w, h) != (SR_W, SR_H)]
    if zle:
        return {"ok": False, "reason": f"Powiększenie nie trzyma rozmiaru {SR_W}×{SR_H}: {_lista_txt(zle)} — "
                                       "do kolejki nie wysłałem."}
    linie = [f"Powiększenie: sr_kadry.py → {kat / D_2X}: {len(wym)} PNG {SR_W}×{SR_H}."]
    try:
        ra = await _uruchom(_argv_arkusz(kat, len(wym)), TIMEOUT_FFMPEG)
        if ra["kod"] == 0 and not await _brakujace_pliki([kat / ARKUSZ]):
            linie.append(f"Arkusz podglądu: {kat / ARKUSZ} ({ARKUSZ_KOL} kolumn, {ARKUSZ_PX} px).")
        else:
            linie.append(f"Arkusz podglądu nie powstał (ffmpeg kod {ra['kod']}: "
                         f"{_k(ra['stderr'] or 'bez komunikatu', 200)}) — raport bez arkusza.")
    except Exception as e:  # noqa: BLE001
        linie.append(f"Arkusz podglądu nie powstał ({_blad(e)}) — raport bez arkusza.")
    try:
        k = await _kolejka_dopisz(slug, kat, data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"Kadry powiększone ({len(wym)} PNG), ale wpis do kolejki .253 nie przeszedł "
                                       f"({_blad(e)}) — sprawdź {KOLEJKA_OPIS} ręcznie (kopia sprzed zmiany w "
                                       f"{kat / 'out'}). Niczego nie zgaduję."}
    if k["stan"] == "juz":
        linie.append(f"Kolejka .253: wpis „{slug}” pojawił się w {KOLEJKA_OPIS} w międzyczasie — nie dopisuję "
                     "drugi raz.")
    else:
        linie.append(f"Kolejka .253: dopisany wpis „{slug}” do {KOLEJKA_OPIS} (teraz {k['n']} "
                     f"{_odm(k['n'], 'wpis', 'wpisy', 'wpisów')}), sprawdzony ponownym pobraniem. "
                     f"Kopia kolejki sprzed zmiany: {k['kopia']}.")
    linie.append(f"Kolejka sama pobierze brief z .10, policzy Wan, post3 i montaż, a podstawę odeśle do "
                 f"{kat / 'out' / 'podstawa.mp4'}.")
    return {"ok": True, "linie": linie}


# ---------------------------------------------------------------- odpowiedzi

def _naglowek_kadrow(stan: dict) -> str:
    return (f"jest {len(stan['jest'])}, brakuje {len(stan['brak'])}, odrzuconych {len(stan['odrz'])}")


async def _start_tla(kat: Path, slug: str, tytul: str, stan: dict, zgoda: bool, su: str) -> dict:
    unit = _unit(slug)
    n = len(stan["do_liczenia"])
    if not GEN_KADRY:
        brak = ["(pusty TELEDYSK_KADRY_GEN)"]
    else:
        brak = await _brakujace_pliki([x for x in GEN_KADRY if os.path.isabs(x)])
    if brak:
        return {"ok": False, "reason": f"Do policzenia {n} {_odm(n, 'kadr', 'kadry', 'kadrów')} "
                                       f"({_lista_txt(stan['do_liczenia'])}), ale nie ma generatora kadrów: "
                                       f"{', '.join(brak)} (ustaw TELEDYSK_KADRY_GEN). Niczego nie uruchomiłem."}
    if su == "failed":
        await _uruchom(["systemctl", "--user", "reset-failed", unit], TIMEOUT_KROTKI)
    r = await _uruchom(_argv_tla(kat, slug, zgoda), TIMEOUT_KROTKI)
    if r["kod"] != 0:
        return {"ok": False, "reason": f"systemd-run nie uruchomił {unit} (kod {r['kod']}: "
                                       f"{_k(r['stderr'] or r['stdout'] or 'bez komunikatu', 200)}) — kadry się "
                                       "nie liczą, do kolejki niczego nie wysłałem."}
    po = await _stan_unitu(unit)
    linie = [f"KADRY LICZĄ SIĘ W TLE — „{tytul}” (brief {slug}): {_naglowek_kadrow(stan)}; do policzenia "
             f"{n} {_odm(n, 'kadr', 'kadry', 'kadrów')}: {_lista_txt(stan['do_liczenia'])}.",
             f"Unit {unit} (systemd-run --user, restart backendu go nie zabija) — stan: {po}. Log: {kat / LOG}.",
             "Gotowe kadry z kontrolą generator pomija (wznawianie od brakujących). Po kadrach unit sam powiększy "
             f"kadry ({D_2X}), zrobi arkusz i dopisze brief do kolejki .253"
             + (" — jawna zgoda z polecenia przechodzi na unit." if zgoda
                else f" — o ile {K_KONTROLA} nie zostawi kadrów do przeglądu."),
             "Do kolejki jeszcze niczego nie wysłałem."]
    return {"ok": True, "reply": "\n".join(linie)}


def _podglad(tytul, kat, slug, st, stan, zgoda, su, braki) -> list:
    unit = _unit(slug)
    linie = [f"PODGLĄD (bez „wykonaj” niczego nie uruchamiam i nie ruszam kolejki .253) — wysyłka do ruchu WAN "
             f"teledysku „{tytul}” (brief {slug}).",
             f"Kadry ({len(stan['ids'])} w {K_KADRY}): {_naglowek_kadrow(stan)}; gotowych z kontrolą "
             f"{len(stan['gotowe'])}."]
    if stan["brak"]:
        linie.append(f"Brakuje: {_lista_txt(stan['brak'])}.")
    if stan["odrz"]:
        linie.append(f"Odrzucone: {_lista_txt(stan['odrz'])}.")
    if stan["przeglad"]:
        linie.append(f"Do przeglądu ({K_KONTROLA}): {_lista_txt(stan['przeglad'])}.")
    if stan["bez"]:
        linie.append(f"Bez wpisu w kontroli: {_lista_txt(stan['bez'])}.")
    if not stan["blokady"]:
        linie.append(f"Warunek wysyłki: spełniony — {K_KONTROLA} bez kadrów do przeglądu.")
    elif zgoda:
        linie.append(f"Warunek wysyłki: {'; '.join(stan['blokady'])} — ale w poleceniu jest jawna zgoda.")
    else:
        linie.append(f"Warunek wysyłki: niespełniony — {'; '.join(stan['blokady'])}.")
    linie.append(f"Powiększone ({D_2X}): {len(st['png2x'])} PNG; arkusz {ARKUSZ}: "
                 f"{'jest' if st['arkusz'] else 'jeszcze nie ma'}.")
    if su in DZIALA:
        linie.append(f"Liczenie kadrów w tle: {unit} działa ({su}) — log {kat / LOG}"
                     + (f", ostatnio: „{st['log']}”" if st.get("log") else "") + ".")
    elif su == "failed":
        linie.append(f"Ostatnie liczenie kadrów w tle ({unit}) skończyło się błędem — zajrzyj do {kat / LOG}.")
    if braki:
        linie.append(f"Brief niekompletny: {'; '.join(braki)}.")
    n = len(stan["do_liczenia"])
    if su in DZIALA:
        linie.append("Z „wykonaj” teraz odmówię — kadry liczą się w tle.")
    elif braki:
        linie.append("Z „wykonaj” odmówię, dopóki brief jest niekompletny (najpierw teledysk_rezyseria_2).")
    elif n:
        linie.append(f"Z „wykonaj”: uruchomię w tle liczenie {n} {_odm(n, 'kadru', 'kadrów', 'kadrów')} "
                     f"({_lista_txt(stan['do_liczenia'])}) — systemd-run --user --unit={unit}, log {kat / LOG}; "
                     "gotowe kadry z kontrolą generator pomija. Potem powiększenie, arkusz i wpis do kolejki .253.")
    elif stan["blokady"] and not zgoda:
        linie.append("Z „wykonaj” odmówię — przejrzyj kadry albo dopisz w poleceniu „mimo przeglądu”.")
    else:
        linie.append(f"Z „wykonaj”: powiększę kadry (sr_kadry.py → {D_2X}, {SR_W}×{SR_H}), zrobię {ARKUSZ} "
                     f"({ARKUSZ_KOL} kolumn, {ARKUSZ_PX} px) i dopiszę do {KOLEJKA_OPIS} na .253 (jeśli go nie ma) "
                     f"wpis: {json.dumps(_wpis(slug), ensure_ascii=False)}.")
    linie.append("Żeby wysłać, dopisz „wykonaj”.")
    return linie


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not cmd.strip() or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie wysyłki briefu do ruchu WAN "
                                       "(wyślij do ruchu teledysk …)."}
    params = params or {}
    cel = _cel(cmd, params)
    if cel is None:
        return {"ok": False, "reason": "Nie rozpoznałem teledysku — znam: Waga ważna (flow), Heavy Burdens, "
                                       "Przesilenie (flow/głos), Echo in the Crowd (albo podaj slug, np. "
                                       "32_waga_wazna_flow). Niczego nie zgaduję."}
    try:
        st = await _czytaj_brief(cel["katalogi"])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"Nie odczytam briefu „{cel['tytul']}” ({_blad(e)}). Niczego nie zgaduję."}
    if st.get("katalog") is None:
        if len(st["istniejace"]) > 1:
            return {"ok": False, "reason": f"Pasuje kilka briefów „{cel['tytul']}”: {', '.join(st['istniejace'])} — "
                                           "dopisz, który (np. „Waga ważna flow” albo slug). Niczego nie ruszyłem."}
        return {"ok": False, "reason": f"Nie ma katalogu briefu „{cel['tytul']}” ({', '.join(st['szukane'])}). "
                                       "Niczego nie zgaduję."}
    kat = Path(st["katalog"])
    slug, tytul = kat.name, cel["tytul"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", slug):
        return {"ok": False, "reason": f"Nazwa briefu „{slug}” nie nadaje się na nazwę unitu i wpis kolejki."}
    twarde = _twarde_braki(st)
    if twarde:
        return {"ok": False, "reason": f"{twarde} Niczego nie ruszyłem."}
    stan = _stan(st)
    if not stan["ids"]:
        return {"ok": False, "reason": f"{K_KADRY} w {kat} nie ma ani jednego kadru. Niczego nie ruszyłem."}
    braki = _braki_briefu(st)
    zgoda = bool(_T_ZGODA.search(cmd)) or params.get("zgoda") is True
    unit = _unit(slug)
    su = await _stan_unitu(unit)

    if not _T_WYKONAJ.search(cmd):
        return {"ok": True, "reply": "\n".join(_podglad(tytul, kat, slug, st, stan, zgoda, su, braki))}

    if su in DZIALA:
        return {"ok": False, "reason": f"Kadry teledysku „{tytul}” liczą się w tle ({unit}: {su}) — poczekaj na "
                                       f"koniec (log {kat / LOG}). Niczego nie dokładam."}
    if braki:
        return {"ok": False, "reason": f"Brief „{tytul}” niekompletny: {'; '.join(braki)} — najpierw "
                                       "teledysk_rezyseria_2. Niczego nie ruszyłem."}
    if stan["do_liczenia"]:
        try:
            return await _start_tla(kat, slug, tytul, stan, zgoda, su)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"Nie uruchomiłem liczenia w tle ({_blad(e)}). Do kolejki niczego "
                                           "nie wysłałem."}
    if stan["blokady"] and not zgoda:
        return {"ok": False, "reason": f"Nie wysyłam do ruchu: {'; '.join(stan['blokady'])} — przejrzyj kadry albo "
                                       "dopisz w poleceniu „mimo przeglądu” (jawna zgoda). Niczego nie ruszyłem."}
    try:
        w = await _potok(kat, slug, stan["n_indeks"], _data(), stan.get("indeks_zbudowany"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"Wysyłka przerwana ({_blad(e)}) — sprawdź {kat / D_2X} i {KOLEJKA_OPIS} "
                                       "na .253 ręcznie. Niczego nie zgaduję."}
    if not w["ok"]:
        return {"ok": False, "reason": w["reason"]}
    n = len(stan["ids"])
    if w.get("juz"):
        nag = f"JUŻ W KOLEJCE RUCHU — „{tytul}” (brief {slug})."
    else:
        nag = f"WYSŁANE DO RUCHU WAN — „{tytul}” (brief {slug}): {n} {_odm(n, 'kadr', 'kadry', 'kadrów')} z kontrolą."
        if stan["blokady"]:
            nag += f" Jawna zgoda w poleceniu mimo: {'; '.join(stan['blokady'])}."
    return {"ok": True, "reply": "\n".join([nag] + w["linie"])}


# ---------------------------------------------------------------- tryb tła (unit systemd)

def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


async def _tlo(kat: Path, zgoda: bool = False) -> int:
    kat = Path(kat)
    slug = kat.name
    _log(f"{_unit(slug)}: generator kadrów {' '.join(GEN_KADRY)} {kat} (gotowe kadry z kontrolą pomija)")
    try:
        r = await _uruchom([*GEN_KADRY, str(kat)], None, przekaz=True)
    except Exception as e:  # noqa: BLE001
        _log(f"generator nie wystartował: {_blad(e)} — do ruchu nie wysyłam")
        return 1
    if r["kod"] != 0:
        _log(f"generator kadrów zakończył się kodem {r['kod']} {_k(r['stderr'], 200) if r['stderr'] else ''}"
             " — do ruchu nie wysyłam")
        return 1
    st = await _czytaj_brief([kat])
    if st.get("katalog") is None:
        _log(f"brief {kat} zniknął — do ruchu nie wysyłam")
        return 1
    problem = _twarde_braki(st) or "; ".join(_braki_briefu(st))
    if problem:
        _log(f"brief niekompletny: {problem} — do ruchu nie wysyłam")
        return 1
    stan = _stan(st)
    if not stan["ids"] or stan["do_liczenia"]:
        _log(f"po generatorze nadal do policzenia: {_lista_txt(stan['do_liczenia']) or 'brak kadrów'} — "
             "do ruchu nie wysyłam")
        return 1
    if stan["blokady"] and not zgoda:
        _log(f"kontrola: {'; '.join(stan['blokady'])} — do ruchu nie wysyłam; przejrzyj kadry i powtórz "
             "polecenie z „wykonaj”")
        return 2
    try:
        w = await _potok(kat, slug, stan["n_indeks"], _data(), stan.get("indeks_zbudowany"))
    except Exception as e:  # noqa: BLE001
        _log(f"wysyłka przerwana: {_blad(e)}")
        return 1
    _log("\n".join(w["linie"]) if w["ok"] else w["reason"])
    return 0 if w["ok"] else 1


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ręka teledysk_ruchu — tryb tła (unit teledysk-kadry-<slug>)")
    ap.add_argument("--tlo", required=True, type=Path)
    ap.add_argument("--zgoda", action="store_true")
    a = ap.parse_args(argv)
    return asyncio.run(_tlo(a.tlo, a.zgoda))


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka teledysk_ruchu padła: {_k(e)}"}


async def handler_teledysk_ruchu(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}


if __name__ == "__main__":
    sys.exit(_main())
