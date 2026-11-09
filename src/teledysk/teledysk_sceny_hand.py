# plik: teledysk_sceny_hand.py
"""Ręka teledysk_sceny: SCENY TELEDYSKU (krok 2 artistic flow).

Brief: BRIEFY/<NN_slug>/ (utwór po tytule z utwory.tsv: slug, tytuł, -, audio, tekst; stare 30_*/31_* nietknięte).
Wejście: koncepcja.json (ręka teledysk_koncepcja; brak = „najpierw pomysł”), czasy_szkic.json, tekst_z_czasami.txt
oraz cały ZASADY_ARTISTIC_FLOW.md w prompcie modelu (zasad nie przepisuję do kodu).
Bez „wykonaj” = PODGLĄD (bez modelu, bez sieci). „pokaż sceny teledysku …” = liczba scen i 12 pierwszych.
Z „wykonaj”: WĘZEŁ = TRÓJKĄT: kandydaci = NODES (inference=True) z modelem w /api/tags, decyzja z
/api/network/relocate?need_gb=&model= (decision, potem scored po rosnącym koszcie), unit systemd-run --user
teledysk-sceny-<slug> (tryb --tlo): model rozpisuje sceny PARTIAMI po sekcjach (każda partia dostaje koncepcję
i poprzednie sceny), bramka po każdej partii, max 2 poprawki; potem record_outcome(<węzeł>, ok|fail) i atomowy
zapis sceny_szkic.json, kadry_opis.json oraz kluczy scen w koncepcja.json. Ręka niczego nie publikuje.
Granice I/O: _pobierz (HTTP), _uruchom (procesy), _wezly (NODES), _record_outcome (trójkąt uczy się z wyniku).
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import httpx

WZORZEC = r"\bscen(?:y|a|e|ę|ami|ach|om)?\s+(?:do\s+|dla\s+)?(?:teledysk\w*|klip\w*)"
GOTOWA = True

ROOT = Path(os.environ.get("TELEDYSK_ROOT", "/opt/shadow/teledysk_java"))
BRIEFY = ROOT / "briefy_1017"
UTWORY_TSV = BRIEFY / "utwory.tsv"
ZASADY = ROOT / "flow" / "ZASADY_ARTISTIC_FLOW.md"
STARE = ("30_", "31_")
MODEL = "qwen3.8:27b"
PORT_OLLAMA = 11434
RELOCATE = "http://127.0.0.1:8001/api/network/relocate"
TIMEOUT_WEZLA, TIMEOUT_CHAT, MAX_POPRAWEK, TOL = 10.0, 900.0, 2, 0.05
CEL, MIN_SCEN, MAX_SCEN = 46, 36, 56
K_KONC, K_CZASY, K_TZC = "koncepcja.json", "czasy_szkic.json", "tekst_z_czasami.txt"
K_SZKIC, K_KADRY, K_STAN, LOG = "sceny_szkic.json", "kadry_opis.json", "sceny_stan.json", "sceny.log"
KLUCZE_SCEN = ("sceny", "prompty", "ruch", "wzorce", "osoby", "bohater", "wersy_kadrow")
POLA = ("grade", "ruch", "prompt", "opis_ruchu", "osoby", "bohater", "wzorzec", "wers")
GRADE = ("desat", "noc", "cieply", "swit", "neutral")
RUCH = ("in", "out", "pl", "pr", "up", "down", "stoj")
DZIALA = ("active", "activating", "reloading", "deactivating")
PRZYKLAD = {"motyw_en": "<motyw z koncepcji po angielsku, zawsze tymi samymi słowami>", "sceny": [
    {"start": 31.2, "id": "P09", "grade": "noc", "ruch": "in", "prompt": "close-up, a young man in a grey hoodie "
     "carries a heavy suitcase up a dark staircase, red thread around his wrist, 35mm film", "opis_ruchu":
     "slow push in on his face as he lifts the suitcase", "osoby": 1, "bohater": True, "wzorzec": "twarz", "wers": "L7"},
    {"start": 34.0, "id": "P10", "grade": "noc", "ruch": "stoj", "prompt": "wide shot, a crowd walks past an empty "
     "bus stop in the rain", "opis_ruchu": "people stream past a static camera", "osoby": "tlum", "bohater": False,
     "wzorzec": "brak", "wers": "L8"}]}
_T_WYKONAJ = re.compile(r"\b(?:wykonaj|wy[śs]lij|zatwierd[źz])\b", re.I)
_T_POKAZ = re.compile(r"\bpoka[żz]\w*", re.I)
_T_NOWO = re.compile(r"\bod\s+nowa\b", re.I)
_KOMP = (("close-up", re.compile(r"\b(?:extreme\s+)?close[\s-]?ups?\b|\bcloseups?\b", re.I)),
         ("medium", re.compile(r"\bmedium\b|\bmid[\s-]shot\b", re.I)),
         ("wide", re.compile(r"\bwide\b|\blong\s+shot\b|\bestablishing\b", re.I)))
_AKCJA = re.compile(r"\b(?:" + "|".join((
    r"walk\w*", r"run(?:s|ning)?", "ran", r"carr(?:y|ies|ied|ying)", r"lift\w*", r"push\w*", r"pull\w*",
    r"climb\w*", r"fall(?:s|ing|en)?", "fell", r"jump\w*", r"danc\w*", r"turn\w*", r"reach\w*", r"grab\w*",
    r"hold(?:s|ing)?", "held", r"throw(?:s|ing|n)?", "threw", r"kneel\w*", "knelt", r"crawl\w*", r"drag\w*",
    r"rais(?:e|es|ed|ing)", r"drop\w*", r"step\w*", r"stumbl\w*", r"sprint\w*", r"spin(?:s|ning)?", r"punch\w*",
    r"kick\w*", r"hug\w*", r"embrac\w*", r"ris(?:e|es|ing)", "rose", r"lean\w*", r"stretch\w*", r"swing\w*",
    r"clench\w*", r"shout\w*", r"scream\w*", r"sit(?:s|ting)?", r"stand(?:s|ing)?", r"wav(?:e|es|ed|ing)",
    r"tear(?:s|ing)?", "tore", r"breath\w*", r"cr(?:y|ies|ied|ying)", r"fight\w*", r"chas\w*", r"bend\w*",
    r"pick\w*", r"lay(?:s|ing)?", r"lie(?:s)?", "lying",
    r"mov\w*", r"grip\w*", r"grasp\w*", r"squeez\w*", r"touch\w*", r"press\w*", r"rap\w*", r"sing\w*",
    r"perform\w*", r"gestur\w*", r"point\w*", r"paint\w*", r"spray\w*", r"writ\w*", r"scribbl\w*",
    r"nod\w*", r"clap\w*", r"bounc\w*", r"shak\w*", r"crouch\w*", r"emerg\w*", r"smash\w*", r"break\w*",
    r"rip\w*", r"open\w*", r"slam\w*", r"march\w*", r"stomp\w*", r"sway\w*", r"whirl\w*", r"tilt\w*"
)) + r")\b", re.I)


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


def _f(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _odm(n, jeden, kilka, wiele):
    return jeden if n == 1 else (kilka if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else wiele)


def _lst(d, k) -> list:
    v = d.get(k) if isinstance(d, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _pole(o, *nazwy):
    for n in nazwy:
        v = o.get(n) if isinstance(o, dict) else getattr(o, n, None)
        if v is not None:
            return v
    return None


def _unit(slug: str) -> str:
    return f"teledysk-sceny-{slug}"


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


def _partie(linie: list, dl: float) -> list:
    P = []
    for i, li in enumerate(linie, 1):
        s = str(li.get("sekcja") or "").strip() or "—"
        if P and P[-1]["sekcja"] == s:
            P[-1]["linie"].append(i)
        else:
            P.append({"sekcja": s, "t0": _f(li.get("t0")), "linie": [i]})
    P[0]["t0"] = 0.0
    for a, z in zip(P, P[1:]):
        a["t1"] = z["t0"]
    P[-1]["t1"] = dl
    raw = [CEL * max(p["t1"] - p["t0"], 0.0) / dl for p in P]
    n = [max(1, int(r)) for r in raw]
    while sum(n) < CEL:
        i = max(range(len(P)), key=lambda j: raw[j] - n[j])
        n[i] += 1
    while sum(n) > CEL and any(x > 1 for x in n):
        i = max((j for j in range(len(P)) if n[j] > 1), key=lambda j: n[j] - raw[j])
        n[i] -= 1
    tol = 1 if len(P) <= MAX_SCEN - CEL else 0
    for p, x in zip(P, n):
        p.update(n=x, lo=max(1, x - tol), hi=x + tol, motyw=bool(re.search(r"refren|chorus|hook|outro",
                                                                           _norm(p["sekcja"]))))
    return P


def _klucz(P) -> list:
    return [[p["sekcja"], p["t0"], p["t1"], p["n"]] for p in P]


def _wejscie(kat: Path) -> dict:
    w = {"bledy": {}, "partie": [], "linie": [], "dl": None, "motyw": "", "motyw_en": ""}
    b = w["bledy"]
    for klucz, p, js in (("konc", kat / K_KONC, True), ("czasy", kat / K_CZASY, True),
                         ("tzc", kat / K_TZC, False), ("zasady", ZASADY, False)):
        w[klucz] = None
        if not p.is_file():
            b[klucz] = f"brak {p}"
            continue
        try:
            t = p.read_text(encoding="utf-8-sig")
            w[klucz] = json.loads(t) if js else t
        except (OSError, ValueError) as e:
            b[klucz] = f"{p.name} nieczytelny ({_blad(e)})"
            continue
        if not js and not t.strip():
            b[klucz] = f"pusty {p}"
    if "konc" in b:
        b["konc"] += " — najpierw pomysł"
    elif not isinstance(w["konc"], dict) or not _txt(w["konc"].get("motyw")):
        b["konc"] = f"{K_KONC} bez motywu — najpierw pomysł"
    else:
        w["motyw"] = w["konc"]["motyw"].strip()
        w["motyw_en"] = str(w["konc"].get("motyw_en") or "").strip()
    if "czasy" not in b:
        w["linie"] = [x for x in _lst(w["czasy"], "linie") if _txt(x.get("tekst"))]
        t0s = [_f(x.get("t0")) for x in w["linie"]]
        dl = _f(w["czasy"].get("dlugosc")) if isinstance(w["czasy"], dict) else None
        dl = dl or max((_f(x.get("t1")) or 0.0 for x in w["linie"]), default=0.0)
        if not w["linie"] or None in t0s:
            b["czasy"] = f"{K_CZASY} bez linii tekstu z t0"
        elif any(z < a for a, z in zip(t0s, t0s[1:])) or dl <= t0s[-1]:
            b["czasy"] = f"{K_CZASY}: czasy linii nie rosną albo dlugosc kończy się przed ostatnią linią"
        else:
            w["dl"], w["partie"] = dl, _partie(w["linie"], dl)
            if any(p["t1"] <= p["t0"] for p in w["partie"]):
                b["czasy"], w["partie"] = f"{K_CZASY}: sekcja o zerowej długości", []
    return w


# ---------------------------------------------------------------- bramka (czyste funkcje)

def _kompozycja(prompt: str):
    traf = [(m.start(), n) for n, r in _KOMP if (m := r.search(prompt or ""))]
    return min(traf)[1] if traf else None


def _serie(sceny, fn, limit: int, nazwa: str) -> list:
    b, run, prev = [], 0, None
    for i, s in enumerate(sceny):
        v = fn(s)
        run = run + 1 if v and v == prev else (1 if v else 0)
        prev = v
        if run == limit + 1:
            et = nazwa if v is True else f"{nazwa} {v}"
            b.append(f"{et} w {limit + 1} scenach pod rząd od {sceny[i - limit]['id']} — max {limit}")
    return b


def _odp(d, k: int, sekcja: str) -> tuple:
    if not isinstance(d, dict) or not isinstance(d.get("sceny"), list):
        return [], ["brak listy sceny"]
    out, b = [], []
    for i, x in enumerate(d["sceny"], 1):
        start = _f(x.get("start")) if isinstance(x, dict) else None
        if start is None:
            b.append(f"scena nr {i} bez start (sekundy)")
            continue
        o, wers = x.get("osoby"), x.get("wers")
        o = int(o) if isinstance(o, str) and o.strip().isdigit() else ("tlum" if _norm(o) == "tlum" else o)
        wers = f"L{wers}" if type(wers) is int else str(wers or "").strip().upper()
        out.append({"start": start, "id": str(x.get("id") or "").strip().upper(), "grade": _norm(x.get("grade")),
                    "ruch": _norm(x.get("ruch")), "prompt": str(x.get("prompt") or "").strip(),
                    "opis_ruchu": str(x.get("opis_ruchu") or x.get("ruch_opis") or "").strip(), "osoby": o,
                    "bohater": x.get("bohater"), "wzorzec": _norm(x.get("wzorzec")), "wers": wers,
                    "sekcja": sekcja, "partia": k})
    return out, b


def _bramka(sceny, w, motyw_en: str, k: int) -> list:
    P, b, ids, nl = w["partie"], [], set(), len(w["linie"])
    for s in sceny:
        i = s["id"] or "?"
        if not re.fullmatch(r"P\d{2,3}", s["id"]):
            b.append(f"scena {i}: id ma być P01, P02…")
        elif s["id"] in ids:
            b.append(f"powtórzone id {i}")
        ids.add(s["id"])
        if s["grade"] not in GRADE or s["ruch"] not in RUCH:
            b.append(f"scena {i}: grade {'|'.join(GRADE)}, ruch {'|'.join(RUCH)}")
        if not s["prompt"] or not s["opis_ruchu"]:
            b.append(f"scena {i}: pusty prompt albo opis_ruchu")
        if s["osoby"] != "tlum" and not (type(s["osoby"]) is int and s["osoby"] >= 1):
            b.append(f"scena {i}: osoby ma być liczbą ≥1 albo tlum")
        if type(s["bohater"]) is not bool or s["wzorzec"] not in ("twarz", "brak"):
            b.append(f"scena {i}: bohater true|false, wzorzec twarz|brak")
        m = re.fullmatch(r"L(\d+)", s["wers"])
        if not m or not 1 <= int(m.group(1)) <= nl:
            b.append(f"scena {i}: wers ma być L1…L{nl}")
    for a, z in zip(sceny, sceny[1:]):
        if z["start"] <= a["start"]:
            b.append(f"starty nie rosną: {z['id']} @{z['start']:g} s po {a['id']} @{a['start']:g} s")
    motywy = [m for m in (_norm(motyw_en), _norm(w["motyw"])) if m]
    for j in range(k + 1):
        p, ps = P[j], [s for s in sceny if s["partia"] == j]
        op = f"sekcja {p['sekcja']} ({p['t0']:g}–{p['t1']:g} s)"
        if not p["lo"] <= len(ps) <= p["hi"]:
            b.append(f"{op}: {len(ps)} scen, ma być {p['lo']}–{p['hi']}")
        if ps and abs(ps[0]["start"] - p["t0"]) > TOL:
            b.append(f"{op}: pierwsza scena {ps[0]['id']} ma startować w {p['t0']:g} s (pokrycie bez dziur)")
        poza = [s["id"] for s in ps if not p["t0"] - TOL <= s["start"] < p["t1"]]
        if poza:
            b.append(f"{op}: starty poza sekcją: {', '.join(poza)}")
        if p["motyw"] and ps and not any(f" {m} " in f" {_norm(s['prompt'])} " for s in ps for m in motywy):
            b.append(f"{op}: motyw „{motyw_en or w['motyw']}” nie ma go w prompcie żadnej sceny — ma być w ≥1")
    n = len(sceny)
    if n and (n >= 10 or k == len(P) - 1):
        h = sum(s["bohater"] is True for s in sceny)
        if not 0.2 <= h / n <= 0.4:
            b.append(f"bohater w {h} z {n} scen ({100 * h / n:.0f} %, licząc z poprzednimi) — ma być 20–40 %")
    if k == len(P) - 1 and not MIN_SCEN <= n <= MAX_SCEN:
        b.append(f"cały utwór: {n} scen, ma być {MIN_SCEN}–{MAX_SCEN}")
    b += _serie(sceny, lambda s: s["bohater"] is True, 3, "bohater")
    b += _serie(sceny, lambda s: _kompozycja(s["prompt"]), 2, "kompozycja")
    a = sum(bool(_AKCJA.search(s["prompt"])) for s in sceny)
    if n and a < 0.8 * n:
        b.append(f"czasownik akcji ciała w {a} z {n} promptów — ma być ≥80 % (walks, carries, falls…)")
    return b


# ---------------------------------------------------------------- prompt

def _prompt(w, k: int, st: dict) -> list:
    P, p = w["partie"], w["partie"][k]
    sys_ = (f"Jesteś REŻYSEREM teledysku — krok 2 artistic flow: SCENY. Pracujesz według zasad poniżej (cały plik "
            f"{ZASADY.name}).\n\n{w['zasady']}\n\nRozpisujesz sceny PARTIAMI po sekcjach utworu — teraz jedna sekcja. "
            'Odpowiedz WYŁĄCZNIE obiektem JSON {"motyw_en": "…", "sceny": […]}. Pola sceny: start (sekundy, rosnąco), '
            f"id P01… (ciągła numeracja), grade {'|'.join(GRADE)}, ruch kamery {'|'.join(RUCH)}, prompt obrazu EN "
            "(zacznij od kompozycji close-up / medium / wide — max 2 sceny pod rząd tej samej; w ≥80 % promptów "
            "czasownik akcji ciała: walks, carries, falls…), opis_ruchu EN, osoby liczba ≥1 albo \"tlum\", bohater "
            "true|false (20–40 % scen, max 3 pod rząd), wzorzec twarz|brak, wers L<nr linii>. W refrenach i outro "
            "motyw z koncepcji w prompcie co najmniej jednej sceny.\nPrzykład FORMATU (treść napisz sam z koncepcji "
            "i tekstu, nie kopiuj):\n" + json.dumps(PRZYKLAD, ensure_ascii=False))
    konc = {x: v for x, v in w["konc"].items() if x not in KLUCZE_SCEN}
    li = "\n".join(f"L{i} @{w['linie'][i - 1].get('t0')} s: {w['linie'][i - 1]['tekst']}" for i in p["linie"])
    poprz = json.dumps([[s["start"], s["id"], s["grade"], s["ruch"], s["bohater"], s["prompt"][:110]]
                        for s in st["sceny"]], ensure_ascii=False)
    h, nr0 = sum(s["bohater"] is True for s in st["sceny"]), len(st["sceny"]) + 1
    usr = (f"{K_KONC}:\n{json.dumps(konc, ensure_ascii=False, indent=1)}\n\n{K_TZC}:\n{w['tzc']}\n\n"
           f"PARTIA {k + 1}/{len(P)} — sekcja „{p['sekcja']}” od {p['t0']:g} do {p['t1']:g} s (utwór {w['dl']:g} s):\n"
           f"{li}\nNapisz od {p['lo']} do {p['hi']} scen (najlepiej {p['n']}); pierwsza start = {p['t0']:g}, każda "
           f"start < {p['t1']:g}; id od P{nr0:02d}.\n"
           + ("To refren/outro: motyw w prompcie ≥1 sceny.\n" if p["motyw"] else "")
           + (f"Motyw w promptach zapisuj dokładnie jako: „{st['motyw_en']}” (motyw_en bez zmian).\n"
              if st["motyw_en"] else "W motyw_en podaj motyw z koncepcji po angielsku — w promptach zawsze tymi słowami.\n")
           + f"Poprzednie sceny ({len(st['sceny'])}, bohater w {h}) [start, id, grade, ruch, bohater, prompt]:\n{poprz}")
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


# ---------------------------------------------------------------- granice I/O

def _env() -> dict:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


async def _pobierz(url: str, payload=None, timeout: float = TIMEOUT_WEZLA):
    """HTTP: GET bez payload, POST JSON z payload (Ollama i relocate trójkąta)."""
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


def _nodes(raw) -> dict:
    out = {}
    pary = raw.items() if isinstance(raw, dict) else ((None, x) for x in (raw or []))
    for k, v in pary:
        if _pole(v, "inference") is not True:
            continue
        klucz = str(k if k is not None else (_pole(v, "key", "name", "id") or "")).strip()
        ip = re.sub(r"^https?://", "", str(_pole(v, "ip", "host", "addr", "address") or "")).split("/")[0].split(":")[0]
        if klucz and ip:
            out[klucz] = ip
    return out


async def _wezly() -> dict:
    from app.services import node_hardware_truth as nht  # backend Shadowa
    return _nodes(nht.NODES)


async def _record_outcome(klucz: str, wynik: str, opis: str) -> None:
    from app.services import node_hardware_truth as nht
    r = nht.record_outcome(klucz, wynik, opis)
    if inspect.isawaitable(r):
        await r


def _url(ip: str) -> str:
    return f"http://{ip}:{PORT_OLLAMA}"


async def _tags(ip: str) -> float:
    d = await _pobierz(_url(ip) + "/api/tags", None, TIMEOUT_WEZLA)
    for m in (d.get("models") or []) if isinstance(d, dict) else []:
        if isinstance(m, dict) and MODEL in (m.get("name"), m.get("model")):
            return _f(m.get("size")) or 0.0
    raise RuntimeError(f"brak modelu {MODEL}")


def _dopasuj(k, wezly: dict):
    if k is None or _norm(k) in ("", "none", "break"):
        return None
    s = str(k).strip()
    return s if s in wezly else next((x for x, ip in wezly.items() if ip == s or ip.endswith("." + s)), s)


def _scored(sc) -> list:
    pary = []
    for k, v in (sc.items() if isinstance(sc, dict) else enumerate(sc) if isinstance(sc, list) else []):
        if isinstance(sc, list):
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                k, v = v[0], v[1]
            elif isinstance(v, dict):
                k = _pole(v, "node", "key", "wezel", "name", "id")
            else:
                continue
        if isinstance(v, dict):
            if v.get("excluded") or v.get("wykluczony"):
                continue
            v = _pole(v, "cost", "koszt", "score")
        c = _f(v)
        if k is not None and c is not None and math.isfinite(c):
            pary.append((str(k), c))
    return sorted(pary, key=lambda x: x[1])


async def _wezel():
    """TRÓJKĄT: NODES z modelem → relocate (decision, potem scored po koszcie). Zwraca (klucz, url, powód)."""
    try:
        wezly = await _wezly()
    except Exception as e:  # noqa: BLE001
        raise BladWezla(f"nie czytam NODES z node_hardware_truth ({_blad(e)})") from None
    if not wezly:
        raise BladWezla("NODES nie ma węzłów inference=True")
    wyn = await asyncio.gather(*(_tags(ip) for ip in wezly.values()), return_exceptions=True)
    kand, powody = {}, []
    for (k, ip), r in zip(wezly.items(), wyn):
        if isinstance(r, BaseException):
            powody.append(f"{k} ({ip}): {_blad(r)}")
        else:
            kand[k] = r
    if not kand:
        raise BladWezla(f"żaden węzeł nie ma {MODEL} — " + "; ".join(powody))
    if max(kand.values()) <= 0:
        raise BladWezla(f"/api/tags nie podaje rozmiaru {MODEL} — need_gb nie zgaduję")
    need = math.ceil(max(kand.values()) / 1024 ** 3 * 10) / 10
    try:
        d = await _pobierz(f"{RELOCATE}?{urlencode({'need_gb': f'{need:g}', 'model': MODEL})}", None, TIMEOUT_WEZLA)
    except Exception as e:  # noqa: BLE001
        raise BladWezla(f"trójkąt (relocate) nie odpowiada ({_blad(e)})") from None
    d = d if isinstance(d, dict) else {}
    pow_t = _k(d.get("reason") or "bez powodu", 200)
    dec = _dopasuj(d.get("decision"), wezly)
    if dec in kand:
        return dec, _url(wezly[dec]), f"decyzja trójkąta: {pow_t}"
    odrz = f"decyzja trójkąta {d.get('decision')} " + ("nie ma modelu" if dec else "(brak węzła)")
    for k, koszt in _scored(d.get("scored")):
        k = _dopasuj(k, wezly)
        if k in kand and k != dec:
            return k, _url(wezly[k]), f"{odrz} → następny ze scored: {k} (koszt {koszt:g}); trójkąt: {pow_t}"
    raise BladWezla(f"trójkąt nie wskazał węzła z {MODEL} ({odrz}): {pow_t}"
                    + (f"; bez modelu: {'; '.join(powody)}" if powody else ""))


async def _chat(url: str, msgs: list) -> str:
    d = await _pobierz(url + "/api/chat", {"model": MODEL, "messages": msgs, "stream": False, "format": "json",
                                           "options": {"num_ctx": 16384, "temperature": 0.8},
                                           "keep_alive": "10m"}, TIMEOUT_CHAT)
    return str((d.get("message") or {}).get("content") or "")


async def _z_poprawka(url: str, msgs: list, ocen):
    """Odpowiedź modelu + max MAX_POPRAWEK poprawek z listą błędów bramki. Zwraca (obiekt, błędy)."""
    for proba in range(MAX_POPRAWEK + 1):
        tresc = await _chat(url, msgs)
        try:
            d = json.loads(tresc)
        except ValueError as e:
            d, bl = None, [f"odpowiedź nie jest JSON-em ({_blad(e)})"]
        else:
            bl = ocen(d)
        if not bl or proba == MAX_POPRAWEK:
            return d, bl
        msgs = msgs + [{"role": "assistant", "content": tresc},
                       {"role": "user", "content": "Popraw odpowiedź (ta sama partia). Błędy bramki:\n- "
                                                   + "\n- ".join(bl) + '\nZwróć cały obiekt JSON {"motyw_en", "sceny"}.'}]


def _atomowo(p: Path, dane) -> None:
    tmp = p.with_name(p.name + ".shadow_tmp")
    tmp.write_text(json.dumps(dane, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def _stan(kat: Path) -> dict:
    try:
        d = json.loads((kat / K_STAN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _log(kat: Path, msg: str) -> None:
    with (kat / LOG).open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")


def _ostatni_log(kat: Path) -> str:
    try:
        linie = [x for x in (kat / LOG).read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return linie[-1][:400] if linie else ""


async def _stan_unitu(unit: str) -> str:
    try:
        r = await _uruchom(["systemctl", "--user", "is-active", unit], 30)
    except Exception:  # noqa: BLE001
        return "nieznany"
    s = r["stdout"].split()
    return s[-1] if s else "inactive"


def _argv_tla(kat: Path, url: str, wezel: str, tytul: str) -> list:
    log = kat / LOG
    pp = os.pathsep.join(dict.fromkeys([os.getcwd()] + [p for p in sys.path if p]))
    return ["systemd-run", "--user", f"--unit={_unit(kat.name)}", "--collect", "--quiet",
            f"--description=Shadow: sceny teledysku {kat.name}", f"--property=WorkingDirectory={kat}",
            f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
            "--setenv=PYTHONUNBUFFERED=1", f"--setenv=PYTHONPATH={pp}", "--", sys.executable,
            str(Path(__file__).resolve()), "--tlo", str(kat), "--url", url, "--wezel", wezel, "--tytul", tytul,
            "--zasady", str(ZASADY)]


# ---------------------------------------------------------------- tryb tła

def _zapisz_wynik(kat: Path, w, st: dict, tytul: str) -> None:
    sc, sceny = st["sceny"], []
    for i, s in enumerate(sc):
        t1 = sc[i + 1]["start"] if i + 1 < len(sc) else w["dl"]
        sceny.append({"id": s["id"], "start": s["start"], "t0": s["start"], "t1": t1, "sekcja": s["sekcja"],
                      **{x: s[x] for x in POLA}})
    szkic = {"tytul": tytul, "audio": "audio/utwor.wav", "czasy": K_CZASY, "bity": None, "kadry": "kadry_2x",
             "fps": 30, "puls": 0.006, "sceny": [[s["start"], s["id"], s["grade"], s["ruch"]] for s in sceny]}
    kadry = {s["id"]: {"obraz": s["prompt"], "ruch": s["opis_ruchu"], "plik": f"kadry_qwen_v1/{s['id']}.png"}
             for s in sceny}
    konc = json.loads((kat / K_KONC).read_text(encoding="utf-8-sig"))
    for klucz, pole in (("prompty", "prompt"), ("ruch", "opis_ruchu"), ("wzorce", "wzorzec"), ("osoby", "osoby"),
                        ("bohater", "bohater"), ("wersy_kadrow", "wers")):
        konc[klucz] = {s["id"]: s[pole] for s in sceny}
    konc["sceny"] = sceny
    _atomowo(kat / K_SZKIC, szkic)
    _atomowo(kat / K_KADRY, kadry)
    _atomowo(kat / K_KONC, konc)


async def _odnotuj(kat: Path, wezel: str, wynik: str) -> None:
    try:
        await _record_outcome(wezel, wynik, f"teledysk_sceny {kat.name}")
    except Exception as e:  # noqa: BLE001
        _log(kat, f"record_outcome({wezel}, {wynik}) nie przeszło: {_blad(e)}")
    else:
        _log(kat, f"trójkąt: record_outcome({wezel}, {wynik})")


async def _tlo(kat: Path, url: str, wezel: str, tytul: str) -> int:
    kat = Path(kat)
    w = _wejscie(kat)
    if w["bledy"]:
        _log(kat, "wejście niekompletne: " + "; ".join(w["bledy"].values()) + " — pliki bez zmian")
        return 1
    P = w["partie"]
    st = _stan(kat)
    if st.get("klucz") != _klucz(P):
        st = {"klucz": _klucz(P), "motyw_en": w["motyw_en"], "sceny": [], "gotowe": 0}
    _log(kat, f"start: {MODEL} na węźle {wezel} ({url}); {len(P)} partii, gotowe {st['gotowe']}")
    wolano, wynik = False, "ok"
    try:
        for k in range(st["gotowe"], len(P)):
            def ocen(d, k=k):
                nowe, bl = _odp(d, k, P[k]["sekcja"])
                m = st["motyw_en"] or (str(d.get("motyw_en") or "").strip() if isinstance(d, dict) else "")
                return bl + ([] if m else ["puste motyw_en"]) + _bramka(st["sceny"] + nowe, w, m, k)
            wolano = True
            d, bl = await _z_poprawka(url, _prompt(w, k, st), ocen)
            if bl:
                _log(kat, f"ODRZUCONE: sekcja {P[k]['sekcja']} ({k + 1}/{len(P)}) po {MAX_POPRAWEK} poprawkach — "
                     + "; ".join(bl) + " — pliki bez zmian")
                return 2
            st["motyw_en"] = st["motyw_en"] or str(d.get("motyw_en")).strip()
            st["sceny"] += _odp(d, k, P[k]["sekcja"])[0]
            st["gotowe"] = k + 1
            _atomowo(kat / K_STAN, st)
            _log(kat, f"partia {k + 1}/{len(P)} ({P[k]['sekcja']}): razem {len(st['sceny'])} scen")
    except Exception as e:  # noqa: BLE001
        wynik = "fail"
        _log(kat, f"BŁĄD: model na węźle {wezel} nie odpowiedział ({_blad(e)}) — pliki bez zmian, stan w {K_STAN}")
        return 1
    finally:
        if wolano:
            await _odnotuj(kat, wezel, wynik)
    bl = _bramka(st["sceny"], w, st["motyw_en"], len(P) - 1)
    if bl:
        _log(kat, "ODRZUCONE: bramka całości — " + "; ".join(bl) + " — pliki bez zmian")
        return 2
    _zapisz_wynik(kat, w, st, tytul)
    (kat / K_STAN).unlink(missing_ok=True)
    _log(kat, f"GOTOWE na węźle {wezel}: {len(st['sceny'])} scen → {K_KONC}, {K_SZKIC}, {K_KADRY}")
    return 0


# ---------------------------------------------------------------- odpowiedzi

def _pokaz(kat: Path, tytul: str) -> dict:
    p = kat / K_KONC
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {"ok": False, "reason": f"{p} nieczytelny ({_blad(e)}). Niczego nie zgaduję."}
    sc = [x for x in _lst(d, "sceny") if _txt(str(x.get("id") or ""))]
    if not sc:
        return {"ok": False, "reason": f"Brief {kat.name} nie ma jeszcze scen w {K_KONC} — najpierw „rozpisz sceny "
                                       f"teledysku {tytul} wykonaj”."}
    out = [f"SCENY — „{tytul}” (brief {kat.name}): {len(sc)} {_odm(len(sc), 'scena', 'sceny', 'scen')}; "
           f"pierwsze {min(12, len(sc))}:"]
    for x in sc[:12]:
        t, pr = _f(x.get("start")), " ".join(str(x.get("prompt") or "—").split())
        out.append(f"{x['id']} @{'?' if t is None else f'{t:g}'} s: {pr if len(pr) <= 80 else pr[:79] + '…'}")
    out.append(f"Plik {p}; {K_SZKIC} i {K_KADRY} obok.")
    return {"ok": True, "reply": "\n".join(out)}


def _podglad(slug: str, tytul: str, kat: Path, w, su: str) -> str:
    b, P, n = w["bledy"], w["partie"], len(_lst(w["konc"], "sceny"))
    linie = [f"PODGLĄD (bez „wykonaj” modelu nie wołam i niczego nie zapisuję) — sceny teledysku „{tytul}” "
             f"(brief {slug}).",
             f"{K_KONC}: " + (b.get("konc") or f"motyw „{w['motyw']}”"
                              + (f", ma już {n} scen (z „wykonaj” nadpiszę je po bramce)" if n else "")) + ".",
             f"{K_CZASY}: " + (b.get("czasy") or f"{len(w['linie'])} linii, {w['dl']:g} s") + ".",
             f"{K_TZC}: " + (b.get("tzc") or "jest") + ".",
             f"Zasady {ZASADY}: " + (b.get("zasady") or f"{len(w['zasady'])} znaków, idą w całości do modelu") + "."]
    if P:
        linie.append(f"Partie ({len(P)}, cel {CEL} scen, razem {MIN_SCEN}–{MAX_SCEN}): " + "; ".join(
            f"{p['sekcja']} {p['t0']:g}–{p['t1']:g} s: {p['lo']}–{p['hi']} scen" for p in P) + ".")
    ost = _ostatni_log(kat)
    linie.append(f"Unit {_unit(slug)}: {su}." + (f" Ostatnio w {LOG}: {ost}" if ost else ""))
    if su in DZIALA:
        linie.append("Z „wykonaj” teraz odmówię — model już liczy w tle.")
    elif b:
        linie.append("Z „wykonaj” odmówię — brakuje: " + "; ".join(b.values()) + ".")
    else:
        linie.append(f"Z „wykonaj”: węzeł wybierze trójkąt (NODES z {MODEL} → relocate), model rozpisze sceny "
                     f"partiami w tle (systemd-run --user, log {kat / LOG}); pliki zapiszę dopiero po bramce.")
    return "\n".join(linie)


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie scen teledysku (rozpisz / pokaż sceny teledysku …)."}
    try:
        slug, tytul = _cel(cmd, params or {})
    except LookupError as e:
        return {"ok": False, "reason": f"{e}. Niczego nie zgaduję."}
    kat = BRIEFY / slug
    if not kat.is_dir():
        return {"ok": False, "reason": f"Nie ma katalogu briefu „{tytul}” ({kat}). Niczego nie zgaduję."}
    if not (kat / K_KONC).is_file():
        return {"ok": False, "reason": f"Brak {K_KONC} w briefie {slug} — najpierw pomysł („wymyśl pomysł teledysku "
                                       f"{tytul} wykonaj”, ręka teledysk_koncepcja). Niczego nie zgaduję."}
    wyk = bool(_T_WYKONAJ.search(cmd))
    if _T_POKAZ.search(cmd) and not wyk:
        return _pokaz(kat, tytul)
    w, unit = _wejscie(kat), _unit(slug)
    su = await _stan_unitu(unit)
    if not wyk:
        return {"ok": True, "reply": _podglad(slug, tytul, kat, w, su)}
    if su in DZIALA:
        return {"ok": False, "reason": f"Sceny „{tytul}” już liczą się w tle ({unit}: {su}, log {kat / LOG})."}
    if w["bledy"]:
        return {"ok": False, "reason": "Brief niekompletny: " + "; ".join(w["bledy"].values()) + ". Modelu nie wołałem."}
    try:
        wezel, url, powod = await _wezel()
    except BladWezla as e:
        return {"ok": False, "reason": f"Model {MODEL} nie ma gdzie liczyć — {e}. Niczego nie uruchomiłem."}
    if _T_NOWO.search(cmd):
        (kat / K_STAN).unlink(missing_ok=True)
    st, P = _stan(kat), w["partie"]
    krok = (f"wznawiam od partii {st['gotowe'] + 1}/{len(P)}" if st.get("klucz") == _klucz(P) and st.get("gotowe")
            else f"{len(P)} partii od początku")
    if su == "failed":
        await _uruchom(["systemctl", "--user", "reset-failed", unit], 30)
    try:
        r = await _uruchom(_argv_tla(kat, url, wezel, tytul), 30)
    except Exception as e:  # noqa: BLE001
        r = {"kod": 127, "stdout": "", "stderr": _blad(e)}
    if r["kod"] != 0:
        return {"ok": False, "reason": f"systemd-run nie uruchomił {unit} (kod {r['kod']}: "
                                       f"{_k(r['stderr'] or r['stdout'] or 'bez komunikatu', 200)})."}
    return {"ok": True, "reply": f"SCENY ROZPISUJĄ SIĘ W TLE — „{tytul}” (brief {slug}), {krok}, cel ~{CEL} scen.\n"
                                 f"Model {MODEL} liczy na węźle {wezel} ({url}) — {powod}.\nUnit {unit} (systemd-run "
                                 f"--user, restart backendu go nie zabija), log {kat / LOG}; po wywołaniu "
                                 f"record_outcome({wezel}).\n{K_SZKIC}, {K_KADRY} i sceny w {K_KONC} zapiszę atomowo "
                                 f"dopiero po bramce. Niczego nie publikuję. Wynik: „pokaż sceny teledysku {tytul}”."}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka teledysk_sceny padła: {_blad(e)}"}


async def handler_teledysk_sceny(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}


def _main(argv=None) -> int:
    global ZASADY
    ap = argparse.ArgumentParser(description="teledysk_sceny — tryb tła")
    ap.add_argument("--tlo", required=True, type=Path)
    ap.add_argument("--url", required=True)
    ap.add_argument("--wezel", required=True)
    ap.add_argument("--tytul", required=True)
    ap.add_argument("--zasady", type=Path)
    a = ap.parse_args(argv)
    if a.zasady:
        ZASADY = a.zasady
    return asyncio.run(_tlo(a.tlo, a.url, a.wezel, a.tytul))


if __name__ == "__main__":
    sys.exit(_main())
