# plik: teledysk_koncepcja_hand.py
"""Ręka teledysk_koncepcja: KONCEPCJA TELEDYSKU (krok 1 artistic flow: POMYSŁ).

Brief: BRIEFY/<NN_slug>/ (utwór po tytule z utwory.tsv: slug, tytuł, -, audio, tekst; stare 30_*/31_* pomijane).
Wejście: czasy_szkic.json, tekst_z_czasami.txt, opcjonalnie inspiracje.json + cały ZASADY_ARTISTIC_FLOW.md w prompcie.
Bez „wykonaj” = PODGLĄD stanu briefu (model nie jest wołany). „pokaż pomysł teledysku …” = pomysł/motyw/puenta z pliku.
Z „wykonaj”: wybór węzła przez TRÓJKĄT (kandydaci = NODES inference=True z modelem w /api/tags, decyzja =
/api/network/relocate; po wyniku record_outcome), potem systemd-run --user unit
teledysk-koncepcja-<slug> (tryb --tlo) → Ollama /api/chat → bramka (1 poprawka) → atomowy zapis koncepcja.json.
Ręka niczego nie publikuje. Granice I/O: _pobierz (HTTP), _ssh_procesy (ssh .253), _uruchom (procesy).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import httpx

WZORZEC = r"\b(?:(?:wymy[śs]l\w*|poka[żz]\w*|zr[óo]b|napisz|przygotuj|daj)\s+)?(?<!oce[ńn]\s)(?:pomys[łl]\w*|koncepcj\w*)\s+(?:na\s+|do\s+|dla\s+)?(?:teledysk\w*|klip\w*)"
GOTOWA = True

ROOT = Path(os.environ.get("TELEDYSK_ROOT", "/opt/shadow/teledysk_java"))
BRIEFY = ROOT / "briefy_1017"
UTWORY_TSV = BRIEFY / "utwory.tsv"
ZASADY = ROOT / "flow" / "ZASADY_ARTISTIC_FLOW.md"
STARE = ("30_", "31_")
MODEL = "qwen3.8:27b"
NAZWA_REKI = "teledysk_koncepcja"
URL_RELOCATE = "http://127.0.0.1:8001/api/network/relocate"
PORT_OLLAMA = 11434
HOST_253, KLUCZ_SSH = "user@example.com", "/opt/shadow/.ssh/shadowai_gpu"
PS_FORTNITE = "Get-Process -Name FortniteClient* -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName }\n"
TIMEOUT_WEZLA, TIMEOUT_CHAT = 10.0, 900.0
K_CZASY, K_TEKST, K_INSP, K_KONC, LOG = ("czasy_szkic.json", "tekst_z_czasami.txt", "inspiracje.json",
                                         "koncepcja.json", "koncepcja.log")
POLA = ("pomysl", "motyw", "puenta", "odrzucone", "o_czym", "ton", "styl", "unikac", "postac_en", "styl_en", "luk")
DZIALA = ("active", "activating", "reloading", "deactivating")
_T_WYKONAJ = re.compile(r"\b(?:wykonaj|wy[śs]lij|zatwierd[źz])\b", re.I)
_T_POKAZ = re.compile(r"\bpoka[żz]\w*", re.I)


class BladWezla(RuntimeError):
    pass


def _k(e, n=300) -> str:
    return " ".join(str(e).split())[:n]


def _blad(e) -> str:
    m = _k(e)
    return f"{type(e).__name__}: {m}" if m else type(e).__name__


def _txt(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower().replace("ł", "l"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _data() -> str:
    return time.strftime("%Y-%m-%d_%H%M%S")


def _unit(slug: str) -> str:
    return f"teledysk-koncepcja-{slug}"


# ---------------------------------------------------------------- utwór i wejście briefu

def _utwory() -> list:
    out = []
    for linia in UTWORY_TSV.read_text(encoding="utf-8-sig").splitlines():
        kol = [x.strip() for x in linia.split("\t")]
        if len(kol) >= 2 and re.fullmatch(r"\d{2}_[a-z0-9_]+", kol[0]) and not kol[0].startswith(STARE):
            out.append((kol[0], kol[1]))
    return out


def _cel(cmd: str, params: dict):
    try:
        utw = _utwory()
    except OSError as e:
        raise LookupError(f"Nie czytam {UTWORY_TSV} ({_blad(e)})") from None
    slug = str(params.get("slug") or "").strip()
    if not slug:
        m = re.search(r"\b(\d{2}_[a-z0-9_]+)", cmd, re.I)
        slug = m.group(1).lower() if m else ""
    if slug.startswith(STARE):
        raise LookupError(f"Brief {slug} jest stary (30_*/31_*) — zostaje nietknięty")
    if slug:
        traf = [u for u in utw if u[0] == slug]
    else:
        c = f" {_norm(cmd)} "
        traf = [u for u in utw if u[1] and f" {_norm(u[1])} " in c]
        if traf:
            n = max(len(_norm(u[1])) for u in traf)
            traf = [u for u in traf if len(_norm(u[1])) == n]
    if len(traf) == 1:
        return traf[0]
    if traf:
        raise LookupError("Pasuje kilka briefów: " + ", ".join(s for s, _ in traf) + " — podaj slug")
    raise LookupError("Nie rozpoznałem utworu — znam: " + ", ".join(f"{t} ({s})" for s, t in utw))


def _sekcje(czasy) -> list:
    linie = czasy.get("linie") if isinstance(czasy, dict) else None
    out = []
    for x in linie if isinstance(linie, list) else []:
        s = str(x.get("sekcja") or "").strip() if isinstance(x, dict) else ""
        if s and s not in out:
            out.append(s)
    return out


def _wejscie(kat: Path) -> dict:
    w = {"bledy": {}, "sekcje": []}
    for klucz, p, js, wym in (("czasy", kat / K_CZASY, True, True), ("tekst", kat / K_TEKST, False, True),
                              ("insp", kat / K_INSP, True, False), ("zasady", ZASADY, False, True)):
        w[klucz] = None
        if not p.is_file():
            if wym:
                w["bledy"][klucz] = f"brak {p}"
            continue
        try:
            t = p.read_text(encoding="utf-8-sig")
            w[klucz] = json.loads(t) if js else t
        except (OSError, ValueError) as e:
            w["bledy"][klucz] = f"{p.name} nieczytelny ({_blad(e)})"
    if "czasy" not in w["bledy"]:
        w["sekcje"] = _sekcje(w["czasy"])
        if not w["sekcje"]:
            w["bledy"]["czasy"] = f"{K_CZASY} bez linii z polem sekcja"
    if w["zasady"] is not None and not w["zasady"].strip():
        w["bledy"]["zasady"] = f"pusty plik zasad {ZASADY}"
    return w


def _czytaj_konc(kat: Path):
    p = kat / K_KONC
    if not p.is_file():
        return None, None
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return None, f"{K_KONC} nieczytelny ({_blad(e)})"
    return (d, None) if isinstance(d, dict) else (None, f"{K_KONC} nie jest obiektem")


# ---------------------------------------------------------------- prompt i bramka (czyste funkcje)

def _prompt(w: dict) -> list:
    sys_ = (f"Jesteś autorem koncepcji teledysku — krok 1 artistic flow: POMYSŁ. Pracujesz według zasad poniżej "
            f"(cały plik {ZASADY.name}).\n\n{w['zasady']}\n\n"
            "Odpowiedz WYŁĄCZNIE obiektem JSON z kluczami: pomysl (jedno zdanie myśli przewodniej wyprowadzone z "
            "tekstu piosenki), motyw (powracający przedmiot, gest albo kolor), puenta (obraz puenty w outro), "
            "odrzucone (dokładnie 3 obiekty {pomysl, powod}), o_czym, ton, styl, unikac, postac_en (postać po "
            "angielsku), styl_en (styl po angielsku), luk (łuk wizualny: lista obiektów {sekcja, obraz}, po jednym "
            f"dla każdej sekcji: {', '.join(w['sekcje'])}).")
    usr = (f"{K_CZASY}:\n{json.dumps(w['czasy'], ensure_ascii=False, indent=1)}\n\n{K_TEKST}:\n{w['tekst']}")
    if w["insp"] is not None:
        usr += f"\n\n{K_INSP}:\n{json.dumps(w['insp'], ensure_ascii=False, indent=1)}"
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


def _bramka(d, sekcje: list) -> list:
    if not isinstance(d, dict):
        return ["odpowiedź nie jest obiektem JSON"]
    b = [f"puste pole {k}" for k in ("pomysl", "motyw", "puenta") if not _txt(d.get(k))]
    odr = d.get("odrzucone")
    if not isinstance(odr, list) or len(odr) != 3:
        b.append(f"odrzucone: ma być 3, jest {len(odr) if isinstance(odr, list) else 0}")
    elif any(not isinstance(x, dict) or not _txt(x.get("pomysl")) or not _txt(x.get("powod")) for x in odr):
        b.append("odrzucone: każdy wpis musi mieć niepuste pomysl i powod")
    luk = d.get("luk") if isinstance(d.get("luk"), list) else []
    maja = {_norm(x.get("sekcja")) for x in luk if isinstance(x, dict) and _txt(x.get("obraz"))}
    brak = [s for s in sekcje if _norm(s) not in maja]
    if brak:
        b.append("luk bez sekcji: " + ", ".join(brak))
    return b


def _rekord(d: dict) -> dict:
    r = {k: d.get(k, "") for k in POLA}
    r["odrzucone"] = [{"pomysl": x["pomysl"].strip(), "powod": x["powod"].strip()} for x in d["odrzucone"]]
    r["luk"] = [{"sekcja": str(x.get("sekcja")).strip(), "obraz": x["obraz"].strip()}
                for x in d["luk"] if isinstance(x, dict) and _txt(x.get("obraz"))]
    return r


# ---------------------------------------------------------------- granice I/O

def _env() -> dict:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


async def _pobierz(url: str, payload=None, timeout: float = TIMEOUT_WEZLA):
    """HTTP do Ollamy: GET bez payload, POST JSON z payload."""
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await (c.get(url) if payload is None else c.post(url, json=payload))
        r.raise_for_status()
        return r.json()


async def _uruchom(argv, timeout: float, wejscie: bytes | None = None) -> dict:
    pipe = asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(*[str(a) for a in argv], stdout=pipe, stderr=pipe, env=_env(),
                                                stdin=pipe if wejscie is not None else asyncio.subprocess.DEVNULL)
    try:
        out, err = await asyncio.wait_for(proc.communicate(wejscie), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{argv[0]} nie skończył w {timeout:.0f} s") from None
    return {"kod": proc.returncode, "stdout": (out or b"").decode("utf-8", "replace"),
            "stderr": (err or b"").decode("utf-8", "replace")}


async def _ssh_procesy() -> list:
    """Procesy FortniteClient* na .253 (PowerShell przez ssh, skrypt na stdin)."""
    r = await _uruchom(["ssh", "-T", "-i", KLUCZ_SSH, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", HOST_253,
                        "powershell", "-NoProfile", "-Command", "-"], 30, PS_FORTNITE.encode("utf-8"))
    if r["kod"] != 0:
        raise RuntimeError(f"ssh {HOST_253} kod {r['kod']}: {_k(r['stderr'] or 'pusto', 200)}")
    return [x.strip() for x in r["stdout"].splitlines() if x.strip().lower().startswith("fortniteclient")]


def _atomowo(p: Path, dane: bytes) -> None:
    tmp = p.with_name(p.name + ".shadow_tmp")
    tmp.write_bytes(dane)
    os.replace(tmp, p)


def _log(kat: Path, msg: str) -> None:
    with (kat / LOG).open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def _ostatni_log(kat: Path) -> str:
    try:
        linie = [x for x in (kat / LOG).read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return linie[-1][:200] if linie else ""


async def _stan_unitu(unit: str) -> str:
    try:
        r = await _uruchom(["systemctl", "--user", "is-active", unit], 30)
    except Exception:  # noqa: BLE001
        return "nieznany"
    s = r["stdout"].split()
    return s[-1] if s else "inactive"


async def _model_na(url: str) -> float:
    """Sprawdza model na węźle; zwraca jego rozmiar w GB (z pola size /api/tags)."""
    d = await _pobierz(url + "/api/tags", None, TIMEOUT_WEZLA)
    modele = [m for m in (d.get("models") or []) if isinstance(m, dict)]
    nazwy = [str(m.get("name")) for m in modele]
    if MODEL not in nazwy:
        raise RuntimeError(f"brak modelu {MODEL} (są: {', '.join(nazwy[:5]) or 'żadne'})")
    try:
        return round(float(modele[nazwy.index(MODEL)].get("size") or 0) / 1024 ** 3, 2)
    except (TypeError, ValueError):
        return 0.0


def _nht():
    try:
        from app.services import node_hardware_truth as nht
    except ImportError:  # tryb tła: skrypt odpalony ścieżką, backend/ nie na sys.path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from app.services import node_hardware_truth as nht
    return nht


def _pole(n, *klucze):
    for k in klucze:
        v = n.get(k) if isinstance(n, dict) else getattr(n, k, None)
        if v is not None:
            return v
    return None


def _kl(x) -> str:
    return str(x).strip().lstrip(".").lower()


def _wezly_inferencji() -> list:
    nodes = _nht().NODES
    para = nodes.items() if isinstance(nodes, dict) else ((_pole(n, "key", "name", "id"), n) for n in nodes)
    out = []
    for k, n in para:
        if k is None or not _pole(n, "inference"):
            continue
        ip = str(_pole(n, "ip", "host", "addr") or "").strip()
        if ip:
            out.append((str(k), ip.rstrip("/") if ip.startswith("http") else f"http://{ip}:{PORT_OLLAMA}"))
    return out


def _scored(s) -> list:
    """Pole scored trójkąta → [(klucz, koszt)] po rosnącym koszcie; bez kosztu / wykluczone pomijane."""
    if isinstance(s, dict):
        s = [{"node": k, **v} if isinstance(v, dict) else {"node": k, "cost": v} for k, v in s.items()]
    out = []
    for x in s if isinstance(s, list) else []:
        if isinstance(x, dict):
            if x.get("excluded") or x.get("ok") is False:
                continue
            k, c = _pole(x, "node", "key", "name", "id"), _pole(x, "cost", "koszt", "score")
        elif isinstance(x, (list, tuple)) and len(x) >= 2:
            k, c = x[0], x[1]
        else:
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if k is not None and c == c and c != float("inf"):
            out.append((str(k), c))
    return sorted(out, key=lambda t: t[1])


async def _wezel():
    """TRÓJKĄT: kandydaci z NODES z modelem → decyzja relocate → (klucz, url, powód)."""
    try:
        wezly = _wezly_inferencji()
    except Exception as e:  # noqa: BLE001
        raise BladWezla(f"nie czytam NODES ({_blad(e)})") from None
    if not wezly:
        raise BladWezla("NODES nie ma węzłów inference=True")
    wyniki = await asyncio.gather(*(_model_na(u) for _, u in wezly), return_exceptions=True)
    kand, powody, need = {}, [], 0.0
    for (k, u), w in zip(wezly, wyniki):
        if isinstance(w, BaseException):
            powody.append(f"{k}: {_blad(w)}")
        else:
            kand[_kl(k)] = (k, u)
            need = max(need, w)
    if not kand:
        raise BladWezla("żaden węzeł nie ma modelu — " + "; ".join(powody))
    try:
        d = await _pobierz(f"{URL_RELOCATE}?{urlencode({'need_gb': need, 'model': MODEL})}", None, TIMEOUT_WEZLA)
    except Exception as e:  # noqa: BLE001
        raise BladWezla(f"trójkąt (relocate) nie odpowiedział ({_blad(e)})") from None
    d = d if isinstance(d, dict) else {}
    dec, powod = d.get("decision"), _k(d.get("reason") or "", 200)
    if dec is not None and _kl(dec) != "break" and _kl(dec) in kand:
        k, u = kand[_kl(dec)]
        return k, u, f"trójkąt wybrał {k}" + (f": {powod}" if powod else "")
    for ks, koszt in _scored(d.get("scored")):
        if _kl(ks) in kand:
            k, u = kand[_kl(ks)]
            dlaczego = "BREAK/brak decyzji" if dec is None or _kl(dec) == "break" else f"{dec} nie ma modelu"
            return k, u, f"trójkąt: {dlaczego} — następny ze scored: {k} (koszt {koszt:g})"
    raise BladWezla(f"trójkąt nie wskazał węzła z modelem — {powod or 'bez powodu'} (decision {dec}; "
                    f"model mają: {', '.join(k for k, _ in kand.values())})")


async def _wynik(kat: Path, wezel: str, wynik: str) -> None:
    """Trójkąt uczy się z wyniku: record_outcome(<klucz>, ok|fail, '<ręka> <slug>')."""
    try:
        r = _nht().record_outcome(wezel, wynik, f"{NAZWA_REKI} {Path(kat).name}")
        if asyncio.iscoroutine(r):
            await r
    except Exception as e:  # noqa: BLE001
        try:
            _log(kat, f"record_outcome({wezel}, {wynik}) nie zapisał: {_blad(e)}")
        except OSError:
            pass


async def _autor(url: str, msgs: list) -> str:
    d = await _pobierz(url + "/api/chat", {"model": MODEL, "messages": msgs, "stream": False, "format": "json",
                                           "options": {"num_ctx": 16384, "temperature": 0.8},
                                           "keep_alive": "10m"}, TIMEOUT_CHAT)
    return str((d.get("message") or {}).get("content") or "")


def _zapisz(kat: Path, d: dict) -> Path:
    p = kat / K_KONC
    stare = {}
    if p.is_file():
        surowe = p.read_bytes()
        _atomowo(kat / f"koncepcja_przed_{_data()}.json", surowe)
        try:
            stare = json.loads(surowe.decode("utf-8-sig"))
        except ValueError:
            stare = {}
    stare = stare if isinstance(stare, dict) else {}
    _atomowo(p, (json.dumps({**stare, **_rekord(d)}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return p


def _argv_tla(kat: Path, url: str, wezel: str) -> list:
    log = kat / LOG
    return ["systemd-run", "--user", f"--unit={_unit(kat.name)}", "--collect", "--quiet",
            f"--description=Shadow: koncepcja teledysku {kat.name}", f"--property=WorkingDirectory={kat}",
            f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
            "--setenv=PYTHONUNBUFFERED=1", "--", sys.executable, str(Path(__file__).resolve()),
            "--tlo", str(kat), "--url", url, "--wezel", wezel, "--zasady", str(ZASADY)]


# ---------------------------------------------------------------- tryb tła

async def _tlo(kat: Path, url: str, wezel: str) -> int:
    kat = Path(kat)
    w = _wejscie(kat)
    if w["bledy"]:
        _log(kat, "wejście niekompletne: " + "; ".join(w["bledy"].values()) + f" — {K_KONC} bez zmian")
        return 1
    msgs = _prompt(w)
    _log(kat, f"start: {MODEL} na węźle {wezel} ({url})")
    for proba in (1, 2):
        try:
            tresc = await _autor(url, msgs)
        except Exception as e:  # noqa: BLE001
            _log(kat, f"BŁĄD: model na {wezel} nie odpowiedział ({_blad(e)}) — {K_KONC} bez zmian")
            await _wynik(kat, wezel, "fail")
            return 1
        try:
            d = json.loads(tresc)
        except ValueError as e:
            d, bledy = None, [f"odpowiedź nie jest JSON-em ({_blad(e)})"]
        else:
            bledy = _bramka(d, w["sekcje"])
        if not bledy:
            p = _zapisz(kat, d)
            _log(kat, f"GOTOWE na węźle {wezel}: {p} — pomysł: {d['pomysl'].strip()}")
            await _wynik(kat, wezel, "ok")
            return 0
        _log(kat, f"bramka (próba {proba}): " + "; ".join(bledy))
        msgs = msgs + [{"role": "assistant", "content": tresc},
                       {"role": "user", "content": "Popraw odpowiedź. Błędy bramki:\n- " + "\n- ".join(bledy)
                                                   + "\nZwróć cały poprawiony obiekt JSON."}]
    _log(kat, f"ODRZUCONE: bramka nie przeszła po poprawce — {K_KONC} bez zmian")
    await _wynik(kat, wezel, "fail")
    return 2


# ---------------------------------------------------------------- odpowiedzi

def _pokaz(kat: Path, tytul: str) -> dict:
    d, err = _czytaj_konc(kat)
    if err:
        return {"ok": False, "reason": f"{err} w {kat}. Niczego nie zgaduję."}
    if d is None:
        return {"ok": False, "reason": f"Brief {kat.name} nie ma jeszcze {K_KONC} — najpierw „wymyśl pomysł "
                                       f"teledysku {tytul} wykonaj”."}
    if not all(_txt(d.get(k)) for k in ("pomysl", "motyw", "puenta")):
        return {"ok": False, "reason": f"{kat / K_KONC} nie ma pomysłu, motywu albo puenty (krok POMYSŁ "
                                       "nie przeszedł). Niczego nie zgaduję."}
    luk = d.get("luk") if isinstance(d.get("luk"), list) else []
    return {"ok": True, "reply": f"POMYSŁ — „{tytul}” (brief {kat.name}): {d['pomysl'].strip()}\n"
                                 f"Motyw: {d['motyw'].strip()}\nPuenta: {d['puenta'].strip()}\n"
                                 f"Łuk: {len(luk)} sekcji; plik {kat / K_KONC}."}


def _podglad(slug: str, tytul: str, kat: Path, w: dict, su: str) -> str:
    b = w["bledy"]
    linie = [f"PODGLĄD (bez „wykonaj” modelu nie wołam i niczego nie zapisuję) — koncepcja teledysku „{tytul}” "
             f"(brief {slug}).",
             f"{K_CZASY}: " + (b.get("czasy") or f"{len(w['czasy'].get('linie') or [])} linii, sekcje: "
                                                 f"{', '.join(w['sekcje'])}") + ".",
             f"{K_TEKST}: {b.get('tekst') or 'jest'}.",
             f"{K_INSP}: " + (b.get("insp") or ("jest" if w["insp"] is not None else "brak (opcjonalny)")) + ".",
             f"Zasady {ZASADY}: " + (b.get("zasady") or f"{len(w['zasady'])} znaków, idą w całości do promptu") + "."]
    d, err = _czytaj_konc(kat)
    linie.append(f"{K_KONC}: " + (err or (f"jest — pomysł: „{d.get('pomysl')}”" if d and _txt(d.get("pomysl"))
                                          else ("jest, ale bez pomysłu" if d else "jeszcze nie ma")) + "."))
    ost = _ostatni_log(kat)
    linie.append(f"Unit {_unit(slug)}: {su}." + (f" Ostatnio w {LOG}: {ost}" if ost else ""))
    if su in DZIALA:
        linie.append("Z „wykonaj” teraz odmówię — model już liczy w tle.")
    elif b:
        linie.append("Z „wykonaj” odmówię — brakuje: " + "; ".join(b.values()) + ".")
    else:
        linie.append(f"Z „wykonaj”: {MODEL} (węzeł wybiera trójkąt /api/network/relocate) napisze koncepcję w tle "
                     f"(systemd-run --user, log {kat / LOG}); {K_KONC} zapiszę dopiero po bramce.")
    return "\n".join(linie)


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie koncepcji teledysku (wymyśl/pokaż pomysł teledysku …)."}
    params = params or {}
    try:
        slug, tytul = _cel(cmd, params)
    except LookupError as e:
        return {"ok": False, "reason": f"{e}. Niczego nie zgaduję."}
    kat = BRIEFY / slug
    if not kat.is_dir():
        return {"ok": False, "reason": f"Nie ma katalogu briefu „{tytul}” ({kat}). Niczego nie zgaduję."}
    wyk = bool(_T_WYKONAJ.search(cmd))
    if _T_POKAZ.search(cmd) and not wyk:
        return _pokaz(kat, tytul)
    w, unit = _wejscie(kat), _unit(slug)
    su = await _stan_unitu(unit)
    if not wyk:
        return {"ok": True, "reply": _podglad(slug, tytul, kat, w, su)}
    if su in DZIALA:
        return {"ok": False, "reason": f"Koncepcja „{tytul}” już liczy się w tle ({unit}: {su}, log {kat / LOG})."}
    if w["bledy"]:
        return {"ok": False, "reason": "Brief niekompletny: " + "; ".join(w["bledy"].values())
                                       + ". Modelu nie wołałem."}
    try:
        wezel, url, powod = await _wezel()
    except BladWezla as e:
        return {"ok": False, "reason": f"Model {MODEL} nie ma gdzie liczyć — {e}. Niczego nie uruchomiłem."}
    if su == "failed":
        await _uruchom(["systemctl", "--user", "reset-failed", unit], 30)
    try:
        r = await _uruchom(_argv_tla(kat, url, wezel), 30)
    except Exception as e:  # noqa: BLE001
        r = {"kod": 127, "stdout": "", "stderr": _blad(e)}
    if r["kod"] != 0:
        return {"ok": False, "reason": f"systemd-run nie uruchomił {unit} (kod {r['kod']}: "
                                       f"{_k(r['stderr'] or r['stdout'] or 'bez komunikatu', 200)})."}
    return {"ok": True, "reply": f"KONCEPCJA LICZY SIĘ W TLE — „{tytul}” (brief {slug}).\n"
                                 f"Model {MODEL} liczy na węźle {wezel} ({url}). Powód: {powod}.\n"
                                 f"Unit {unit} (systemd-run --user, restart backendu go nie zabija), log {kat / LOG}.\n"
                                 f"{K_KONC} zapiszę atomowo dopiero po bramce (1 poprawka modelu). Niczego nie "
                                 f"publikuję. Wynik: „pokaż pomysł teledysku {tytul}”."}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka teledysk_koncepcja padła: {_blad(e)}"}


async def handler_teledysk_koncepcja(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}


def _main(argv=None) -> int:
    global ZASADY
    ap = argparse.ArgumentParser(description="teledysk_koncepcja — tryb tła")
    ap.add_argument("--tlo", required=True, type=Path)
    ap.add_argument("--url", required=True)
    ap.add_argument("--wezel", required=True)
    ap.add_argument("--zasady", type=Path)
    a = ap.parse_args(argv)
    if a.zasady:
        ZASADY = a.zasady
    return asyncio.run(_tlo(a.tlo, a.url, a.wezel))


if __name__ == "__main__":
    sys.exit(_main())
