# plik: teledysk_plan_rezyserski_hand.py
"""Ręka teledysk_plan_rezyserski: PLAN REŻYSERSKI TELEDYSKU (kroki 3 MONTAŻ i 4 KRYTYK artistic flow).

Brief: BRIEFY/<NN_slug>/ (utwór po tytule z utwory.tsv: slug, tytuł, -, audio, tekst; stare 30_*/31_* pomijane).
Wejście: koncepcja.json ze scenami (ręka teledysk_sceny), czasy_szkic.json, audio/slowa.json [{w,t0,t1}]
+ cały ZASADY_ARTISTIC_FLOW.md w prompcie autora i krytyka.
Bez „wykonaj” = PODGLĄD (model nie jest wołany). „pokaż reżyserię teledysku …” = pomysł, motyw, puenta i 12
wierszy tabeli z REZYSERIA.md. „oceń pomysł teledysku …” = bramki koncepcji, scen i planu na istniejących
plikach, bez modelu i bez zapisu. Z „wykonaj”: węzeł z TRÓJKĄTA (kandydaci = NODES inference z modelem w
/api/tags, decyzja = /api/network/relocate, fallback = scored po koszcie; wynik → record_outcome), systemd-run --user
unit teledysk-plan-<slug> (tryb --tlo) → autor → krytyk (max 2 rundy) → bramka → atomowy zapis
plan_rezyserski.json i REZYSERIA.md. Ręka niczego nie publikuje.
Granice I/O: _pobierz (HTTP), _ssh_procesy (ssh .253), _uruchom (procesy).
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

WZORZEC = r"\b(?:plan\w*\s+re[żz]yser\w*|poka[żz]\w*\s+re[żz]yseri\w*)\s+(?:do\s+|dla\s+)?(?:teledysk\w*|klip\w*)|\boce[ńn]\w*\s+(?:pomys[łl]\w*|koncepcj\w*|plan\w*(?:\s+re[żz]yser\w*)?)\s+(?:do\s+|dla\s+|na\s+)?(?:teledysk\w*|klip\w*)"
GOTOWA = True

ROOT = Path(os.environ.get("TELEDYSK_ROOT", "/opt/shadow/teledysk_java"))
BRIEFY = ROOT / "briefy_1017"
UTWORY_TSV = BRIEFY / "utwory.tsv"
ZASADY = ROOT / "flow" / "ZASADY_ARTISTIC_FLOW.md"
STARE = ("30_", "31_")
MODEL = "qwen3.8:27b"
URL_RELOCATE = "http://127.0.0.1:8001/api/network/relocate"
REKA = "teledysk_plan_rezyserski"
_SLUG = ""
HOST_253, KLUCZ_SSH = "user@example.com", "/opt/shadow/.ssh/shadowai_gpu"
PS_FORTNITE = "Get-Process -Name FortniteClient* -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName }\n"
TIMEOUT_WEZLA, TIMEOUT_CHAT, MAX_RUND, TOL = 10.0, 3600.0, 2, 0.3
K_KONC, K_CZASY, K_SLOWA = "koncepcja.json", "czasy_szkic.json", "audio/slowa.json"
K_PLAN, K_MD, LOG = "plan_rezyserski.json", "REZYSERIA.md", "plan_rezyserski.log"
POLA = ("pomysl", "motyw", "sekcje", "stuki", "hasla", "callbacki", "wstawki", "rymy", "spowolnienia", "spiew")
PRZEJSCIA = ("glitch", "blysk", "wypalenie", "ciemnosc", "brak")
DZIALA = ("active", "activating", "reloading", "deactivating")
PRZYKLAD = {"pomysl": "<jedno zdanie>", "motyw": "<przedmiot, gest albo kolor>",
            "sekcje": [{"nazwa": "refren", "t0": 31.2, "t1": 52.0, "energia": 3, "ciecie_co_bitow": 1,
                        "przejscie_na_start": "blysk"}],
            "stuki": [{"t": 33.41, "slowo": "<słowo z slowa.json>"}],
            "hasla": [{"t0": 33.0, "t1": 35.5, "tekst": "<dosłowny fragment tekstu>"}],
            "callbacki": {"refren": ["<id sceny>"]},
            "wstawki": [{"t0": 40.0, "t1": 40.6, "kadr": "<id sceny>", "haslo": "<fragment tekstu>"}],
            "rymy": [{"t": 47.8, "slowa": ["<a>", "<b>"], "ujecia": ["<id>", "<id>"], "puenta": "<co spina rym>"}],
            "spowolnienia": [{"t0": 150.0, "t1": 154.0}], "spiew": [3, 7]}
_T_WYKONAJ = re.compile(r"\b(?:wykonaj|wy[śs]lij|zatwierd[źz])\b", re.I)
_T_POKAZ = re.compile(r"\bpoka[żz]\w*", re.I)
_T_OCEN = re.compile(r"\boce[ńn]\w*", re.I)


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


def _sid(x) -> str:
    v = x.get("id") if isinstance(x, dict) else None
    return "" if v is None or isinstance(v, bool) else str(v).strip()


def _opis(s) -> str:
    return next((s[k].strip() for k in ("obraz", "opis", "prompt") if _txt(s.get(k))), "")


def _unit(slug: str) -> str:
    return f"teledysk-plan-{slug}"


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


def _sekcje(w) -> list:
    out = []
    for x in w["linie"]:
        s = str(x.get("sekcja") or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _wejscie(kat: Path) -> dict:
    w = {"bledy": {}}
    b = w["bledy"]
    for klucz, p, js in (("konc", kat / K_KONC, True), ("czasy", kat / K_CZASY, True),
                         ("slowa", kat / K_SLOWA, True), ("zasady", ZASADY, False)):
        w[klucz] = None
        if not p.is_file():
            b[klucz] = f"brak {p}"
            continue
        try:
            t = p.read_text(encoding="utf-8-sig")
            w[klucz] = json.loads(t) if js else t
        except (OSError, ValueError) as e:
            b[klucz] = f"{p.name} nieczytelny ({_blad(e)})"
    w["linie"] = [x for x in _lst(w["czasy"], "linie") if _txt(x.get("tekst"))]
    if "czasy" not in b and not w["linie"]:
        b["czasy"] = f"{K_CZASY} bez linii tekstu"
    w["sceny"] = _lst(w["konc"], "sceny")
    w["ids"] = [_sid(x) for x in w["sceny"] if _sid(x)]
    if "konc" not in b and not w["ids"]:
        b["sceny"] = f"{K_KONC} bez scen (najpierw ręka teledysk_sceny)"
    sl = w["slowa"].get("slowa") if isinstance(w["slowa"], dict) else w["slowa"]
    w["slowa"] = [x for x in sl if isinstance(x, dict) and _txt(x.get("w"))] if isinstance(sl, list) else []
    if "slowa" not in b and not w["slowa"]:
        b["slowa"] = f"{K_SLOWA} bez słów [{{w,t0,t1}}]"
    if w["zasady"] is not None and not w["zasady"].strip():
        b["zasady"] = f"pusty plik zasad {ZASADY}"
    w["tekst"] = _norm(" ".join(x["tekst"] for x in w["linie"]))
    return w


# ---------------------------------------------------------------- bramki (czyste funkcje)

def _bramka(p, w) -> list:
    if not isinstance(p, dict):
        return ["odpowiedź nie jest obiektem JSON"]
    b = [f"puste pole {k}" for k in ("pomysl", "motyw") if not _txt(p.get(k))]
    ids = set(w["ids"])
    if not _lst(p, "sekcje"):
        b.append("brak sekcji")
    for s in _lst(p, "sekcje"):
        e = s.get("energia")
        if type(e) is not int or not 0 <= e <= 3 or s.get("ciecie_co_bitow") not in (1, 2, 4) \
                or _norm(s.get("przejscie_na_start")) not in PRZEJSCIA:
            b.append(f"sekcja {s.get('nazwa')}: energia 0-3, ciecie_co_bitow 1|2|4, przejście z listy")
    for h in _lst(p, "hasla"):
        t = _norm(h.get("tekst"))
        if not t or t not in w["tekst"]:
            b.append(f"hasło spoza tekstu utworu: „{h.get('tekst')}”")
    sl = [(_norm(x["w"]), _f(x.get("t0")), _f(x.get("t1"))) for x in w["slowa"]]
    stuki = [(t, _norm(s.get("slowo")), s.get("slowo")) for s in _lst(p, "stuki") if (t := _f(s.get("t"))) is not None]
    if len(stuki) != len(_lst(p, "stuki")):
        b.append("stuk bez czasu t")
    for t, n, raw in stuki:
        if not any(a == n and t0 is not None and t1 is not None and t0 - TOL <= t <= t1 + TOL for a, t0, t1 in sl):
            b.append(f"stuk „{raw}” @{t:g} s nie pasuje do {K_SLOWA}")
    for i, li in enumerate(w["linie"], 1):
        if re.search(r"refren|chorus", _norm(li.get("sekcja"))):
            t0, t1 = _f(li.get("t0")), _f(li.get("t1"))
            if t0 is None or t1 is None or not any(t0 <= t <= t1 for t, _, _ in stuki):
                b.append(f"linia refrenu nr {i} bez stuku")
    cb = p.get("callbacki")
    for k, v in (cb.items() if isinstance(cb, dict) else []):
        for x in (v if isinstance(v, list) else [v]):
            if str(x).strip() not in ids:
                b.append(f"callback {k}: nieznane id sceny {x}")
    for x in _lst(p, "wstawki"):
        if str(x.get("kadr")).strip() not in ids:
            b.append(f"wstawka: nieznane id sceny {x.get('kadr')}")
    for r in _lst(p, "rymy"):
        sw = r.get("slowa")
        if not isinstance(sw, list) or len(sw) != 2:
            b.append("rym bez pary słów")
        elif not any(n == _norm(sw[1]) for _, n, _ in stuki):
            b.append(f"rym {sw[0]}/{sw[1]}: brak stuku na „{sw[1]}”")
    for x in p.get("spiew") if isinstance(p.get("spiew"), list) else []:
        if type(x) is not int or not 1 <= x <= len(w["linie"]):
            b.append(f"spiew: nie ma linii nr {x}")
    return b


def _bramka_konc(k, sekcje) -> list:
    if not isinstance(k, dict):
        return [f"{K_KONC} nie jest obiektem"]
    b = [f"puste pole {x}" for x in ("pomysl", "motyw", "puenta") if not _txt(k.get(x))]
    odr = k.get("odrzucone")
    if not isinstance(odr, list) or len(odr) != 3:
        b.append(f"odrzucone: ma być 3, jest {len(odr) if isinstance(odr, list) else 0}")
    maja = {_norm(x.get("sekcja")) for x in _lst(k, "luk") if _txt(x.get("obraz"))}
    brak = [s for s in sekcje if _norm(s) not in maja]
    if brak:
        b.append("luk bez sekcji: " + ", ".join(brak))
    return b


def _bramka_scen(w) -> list:
    if not w["sceny"]:
        return [f"{K_KONC} bez scen (najpierw ręka teledysk_sceny)"]
    b, widz = [], set()
    for i, s in enumerate(w["sceny"], 1):
        sid = _sid(s)
        if not sid:
            b.append(f"scena nr {i} bez id")
            continue
        if sid in widz:
            b.append(f"powtórzone id sceny {sid}")
        widz.add(sid)
        if not _opis(s):
            b.append(f"scena {sid} bez obrazu/opisu")
        t0, t1 = _f(s.get("t0")), _f(s.get("t1"))
        if t0 is not None and t1 is not None and t1 <= t0:
            b.append(f"scena {sid}: t1 ≤ t0")
    return b


def _parsuj(tresc: str, w):
    try:
        d = json.loads(tresc)
    except ValueError as e:
        return None, [f"odpowiedź nie jest JSON-em ({_blad(e)})"]
    return d, _bramka(d, w)


# ---------------------------------------------------------------- prompty i REZYSERIA.md

def _prompt(w) -> list:
    k = w["konc"]
    sys_ = (f"Jesteś MONTAŻYSTĄ-REŻYSEREM teledysku — krok 3 artistic flow: MONTAŻ. Pracujesz według zasad poniżej "
            f"(cały plik {ZASADY.name}).\n\n{w['zasady']}\n\nOdpowiedz WYŁĄCZNIE obiektem JSON planu reżyserskiego. "
            f"Reguły twarde: hasla.tekst to dosłowny fragment tekstu utworu; czas stuku = czas słowa z {K_SLOWA}; "
            "co najmniej 1 stuk w każdej linii refrenu; callbacki, kadr wstawek i ujecia rymów tylko z id scen: "
            f"{', '.join(w['ids'])}; w każdym rymie stuk na drugim słowie; energia 0-3; ciecie_co_bitow 1, 2 albo 4; "
            f"przejscie_na_start: {'|'.join(PRZEJSCIA)}; spiew = numery linii od 1. Sekcje: {', '.join(_sekcje(w))}.\n"
            "Przykład FORMATU (treść napisz sam z tekstu i scen, nie kopiuj):\n" + json.dumps(PRZYKLAD, ensure_ascii=False))
    usr = (f"{K_KONC}:\n" + json.dumps({x: k.get(x) for x in ("pomysl", "motyw", "puenta", "sceny")},
                                       ensure_ascii=False, indent=1)
           + f"\n\n{K_CZASY}:\n" + json.dumps(w["czasy"], ensure_ascii=False, indent=1)
           + f"\n\n{K_SLOWA}:\n" + json.dumps(w["slowa"], ensure_ascii=False, separators=(",", ":")))
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


def _prompt_krytyk(w, plan_txt: str) -> list:
    sys_ = (f"Jesteś KRYTYKIEM teledysku — krok 4 artistic flow: KRYTYK. Oceń plan reżyserski listą kontrolną z zasad "
            f"poniżej (cały plik {ZASADY.name}).\n\n{w['zasady']}\n\nOdpowiedz WYŁĄCZNIE obiektem JSON, np. "
            '{"ocena": "<krótko>", "poprawki": ["<punkt zasad, czas, co zmienić>"]}. Pusta lista poprawki = plan '
            "przechodzi listę kontrolną.")
    usr = (f"Linie tekstu z czasami:\n{json.dumps(w['linie'], ensure_ascii=False)}\n\nId scen: {', '.join(w['ids'])}"
           f"\n\nPlan do oceny:\n{plan_txt}")
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


# ---------------------------------------------------------------- partie sekcji

K_PARTIE = "plan_rezyserski_partie.json"


def _sekcja_zakres(w, sekcja: str) -> tuple[float, float]:
    """Min t0 / max t1 linii należących do sekcji."""
    t0s = [_f(x.get("t0")) for x in w["linie"] if _norm(x.get("sekcja")) == _norm(sekcja)]
    t1s = [_f(x.get("t1")) for x in w["linie"] if _norm(x.get("sekcja")) == _norm(sekcja)]
    t0s = [t for t in t0s if t is not None]
    t1s = [t for t in t1s if t is not None]
    return (min(t0s) if t0s else 0.0, max(t1s) if t1s else 0.0)


def _sceny_sekcji(w, sekcja: str) -> list:
    t0, t1 = _sekcja_zakres(w, sekcja)
    out = []
    for s in w["sceny"]:
        st0, st1 = _f(s.get("t0")), _f(s.get("t1"))
        if st0 is not None and st1 is not None and st0 < t1 and st1 > t0:
            out.append(s)
    return out


def _slowa_sekcji(w, sekcja: str) -> list:
    t0, t1 = _sekcja_zakres(w, sekcja)
    out = []
    for sl in w["slowa"]:
        st0, st1 = _f(sl.get("t0")), _f(sl.get("t1"))
        if st0 is not None and st0 >= t0 - TOL and st0 <= t1 + TOL:
            out.append(sl)
    return out


def _prompt_partii(w, sekcja: str) -> list:
    k = w["konc"]
    sceny = _sceny_sekcji(w, sekcja)
    sl = _slowa_sekcji(w, sekcja)
    t0, t1 = _sekcja_zakres(w, sekcja)
    sys_ = (f"Jesteś MONTAŻYSTĄ-REŻYSEREM teledysku — krok 3 artistic flow: MONTAŻ. "
            f"Pracujesz TYLKO nad sekcją „{sekcja}” (czas {t0:.1f}–{t1:.1f}s). "
            f"Zasady (cały plik {ZASADY.name}):\n\n{w['zasady']}\n\n"
            f"Wspólny pomysł: {k.get('pomysl', '')}\nWspólny motyw: {k.get('motyw', '')}\n\n"
            "Odpowiedz WYŁĄCZNIE obiektem JSON z polami: sekcje, stuki, hasla, callbacki, "
            "wstawki, rymy, spowolnienia, spiew — TYLKO dla tej sekcji. "
            f"Reguły twarde: hasla.tekst to dosłowny fragment tekstu; czas stuku = czas słowa z {K_SLOWA}; "
            "co najmniej 1 stuk w każdej linii refrenu (jeśli sekcja to refren); "
            f"callbacki, kadr wstawek i ujecia rymów tylko z id scen: {', '.join(_sid(s) for s in sceny)}; "
            f"energia 0-3; ciecie_co_bitow 1, 2 albo 4; przejscie_na_start: {'|'.join(PRZEJSCIA)}; "
            "spiew = numery linii od 1 (tylko w tej sekcji).\n"
            "Przykład FORMATU (treść napisz sam):\n" + json.dumps(PRZYKLAD, ensure_ascii=False))
    usr = (f"SEKCJA: {sekcja} (t0={t0:.1f}, t1={t1:.1f})\n\n"
           f"Sceny tej sekcji:\n{json.dumps(sceny, ensure_ascii=False, indent=1)}\n\n"
           f"Linie tekstu tej sekcji:\n{json.dumps([x for x in w['linie'] if _norm(x.get('sekcja')) == _norm(sekcja)], ensure_ascii=False, indent=1)}\n\n"
           f"Słowa z czasami (w zakresie sekcji):\n{json.dumps(sl, ensure_ascii=False, separators=(',', ':'))}")
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


def _bramka_partii(d: dict, w, sekcja: str) -> list:
    """Sprawdzenie stuków partii: słowo i czas zgodne z wystąpieniem w slowa.json."""
    b = []
    t0, t1 = _sekcja_zakres(w, sekcja)
    sl_words = {_norm(x["w"]) for x in w["slowa"]}
    for i, st in enumerate(_lst(d, "stuki"), 1):
        sw = _norm(st.get("slowo"))
        if sw not in sl_words:
            b.append(f"stuk #{i}: słowo „{st.get('slowo')}” nie jest w {K_SLOWA}")
            continue
        st_t = _f(st.get("t"))
        if st_t is None:
            continue
        if not any(a == sw and s0 is not None and s1 is not None and s0 - TOL <= st_t <= s1 + TOL
                   for a, s0, s1 in ((_norm(x["w"]), _f(x.get("t0")), _f(x.get("t1"))) for x in w["slowa"])):
            nearest = None
            best = float("inf")
            for x in w["slowa"]:
                if _norm(x["w"]) != sw:
                    continue
                s0 = _f(x.get("t0"))
                if s0 is not None and t0 - TOL <= s0 <= t1 + TOL:
                    d_ = abs(st_t - s0)
                    if d_ < best:
                        best = d_
                        nearest = s0
            hint = f": najbliższe wystąpienie @{nearest:g} s" if nearest is not None else ""
            b.append(f"stuk #{i} „{st.get('slowo')}” @{st_t:g} s nie pasuje do {K_SLOWA}{hint}")
    if re.search(r"refren|chorus", _norm(sekcja)):
        for i, li in enumerate(w["linie"], 1):
            if _norm(li.get("sekcja")) != _norm(sekcja):
                continue
            a0, a1 = _f(li.get("t0")), _f(li.get("t1"))
            ma_stuk = (a0 is not None and a1 is not None and any(
                (st_t := _f(st.get("t"))) is not None
                and a0 - TOL <= st_t <= a1 + TOL
                for st in _lst(d, "stuki")
            ))
            if not ma_stuk:
                zakres = f"{a0:g}–{a1:g} s" if a0 is not None and a1 is not None else "brak czasu"
                b.append(f"linia refrenu nr {i} bez stuku ({zakres}, {li.get('tekst')})")
    return b


def _czytaj_stan_partii(kat: Path) -> dict:
    p = kat / K_PARTIE
    if not p.is_file():
        return {"done": [], "results": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict) and isinstance(d.get("done"), list) and isinstance(d.get("results"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"done": [], "results": {}}


def _zapisz_stan_partii(kat: Path, done: list, results: dict) -> None:
    p = kat / K_PARTIE
    _atomowo(p, (json.dumps({"done": done, "results": results}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _scal_partie(w, sekcje: list, partie: dict) -> dict:
    """Scali wyniki partii w jeden plan reżyserski."""
    k = w["konc"]
    out = {
        "pomysl": k.get("pomysl", ""),
        "motyw": k.get("motyw", ""),
        "sekcje": [],
        "stuki": [],
        "hasla": [],
        "callbacki": {},
        "wstawki": [],
        "rymy": [],
        "spowolnienia": [],
        "spiew": [],
    }
    for sek in sekcje:
        part = partie.get(sek, {})
        out["sekcje"].extend(_lst(part, "sekcje"))
        out["stuki"].extend(_lst(part, "stuki"))
        out["hasla"].extend(_lst(part, "hasla"))
        cb = part.get("callbacki")
        if isinstance(cb, dict):
            for kk, vv in cb.items():
                out["callbacki"][kk] = vv
        out["wstawki"].extend(_lst(part, "wstawki"))
        out["rymy"].extend(_lst(part, "rymy"))
        out["spowolnienia"].extend(_lst(part, "spowolnienia"))
        sp = part.get("spiew")
        if isinstance(sp, list):
            out["spiew"].extend(x for x in sp if type(x) is int)
    out["stuki"].sort(key=lambda x: _f(x.get("t")) or 0.0)
    out["hasla"].sort(key=lambda x: _f(x.get("t0")) or 0.0)
    out["wstawki"].sort(key=lambda x: _f(x.get("t0")) or 0.0)
    out["rymy"].sort(key=lambda x: _f(x.get("t")) or 0.0)
    out["spowolnienia"].sort(key=lambda x: _f(x.get("t0")) or 0.0)
    out["spiew"] = sorted(set(out["spiew"]))
    return out


def _sekcje_bledow(w, sekcje: list, plan: dict, bledy: list) -> list:
    """Sekcje, których dotyczą błędy końcowej bramki (czas stuku, hasła albo wstawki w _sekcja_zakres).
    [] gdy któregoś błędu nie da się przypisać do sekcji — wtedy trzeba liczyć wszystko od nowa."""
    zakresy = [(s, *_sekcja_zakres(w, s)) for s in sekcje]
    out: list = []
    for b in bledy:
        txt, czasy = str(b), []
        if txt.startswith("stuk"):
            m = re.search(r"(?:@|czas\s*)(-?\d+(?:[.,]\d+)?)\s*s\b", txt)
            if m:
                czasy.append(float(m.group(1).replace(",", ".")))
        elif txt.startswith("hasło"):
            m = re.search(r"„(.*)”", txt)
            if m:
                czasy += [_f(h.get("t0")) for h in _lst(plan, "hasla") if str(h.get("tekst")) == m.group(1)]
        elif txt.startswith("wstawka"):
            m = re.search(r"nieznane id sceny (.*)$", txt)
            if m:
                czasy += [_f(x.get("t0")) for x in _lst(plan, "wstawki")
                          if str(x.get("kadr")).strip() == m.group(1).strip()]
        trafione = [s for t in czasy if t is not None for s, a, z in zakresy if a - TOL <= t <= z + TOL]
        if not trafione:
            return []
        out += [s for s in trafione if s not in out]
    return [s for s in sekcje if s in out]


def _nachodzi(x, t0, t1) -> bool:
    a, z = _f(x.get("t0")), _f(x.get("t1"))
    return a is not None and z is not None and a < t1 and z > t0


def _tabela(p, w) -> list:
    sek = {_norm(s.get("nazwa")): s for s in _lst(p, "sekcje")}
    cb = p.get("callbacki") if isinstance(p.get("callbacki"), dict) else {}
    spiew = set(x for x in p.get("spiew") or [] if type(x) is int) if isinstance(p.get("spiew"), list) else set()
    out = []
    for i, li in enumerate(w["linie"], 1):
        t0, t1 = _f(li.get("t0")) or 0.0, _f(li.get("t1")) or 0.0
        ob = [f"{_sid(s)}: {_opis(s)}" for s in w["sceny"] if _nachodzi(s, t0, t1)]
        ob += [f"wstawka {x.get('kadr')}" + (f" „{x['haslo']}”" if _txt(x.get("haslo")) else "")
               for x in _lst(p, "wstawki") if _nachodzi(x, t0, t1)]
        ef = []
        s = sek.get(_norm(li.get("sekcja")))
        if s:
            ef.append(f"{s.get('nazwa')}: energia {s.get('energia')}, cięcie co {s.get('ciecie_co_bitow')} bit.")
            ps = _f(s.get("t0"))
            if _norm(s.get("przejscie_na_start")) not in ("", "brak") and ps is not None and t0 - TOL <= ps <= t1:
                ef.append(f"przejście {s.get('przejscie_na_start')}")
        ef += [f"stuk „{x.get('slowo')}” {x.get('t')} s" for x in _lst(p, "stuki")
               if (t := _f(x.get("t"))) is not None and t0 <= t <= t1]
        ef += [f"hasło „{x.get('tekst')}”" for x in _lst(p, "hasla") if _nachodzi(x, t0, t1)]
        ef += [f"rym {'/'.join(map(str, x.get('slowa') or []))}" for x in _lst(p, "rymy")
               if (t := _f(x.get("t"))) is not None and t0 <= t <= t1]
        ef += ["spowolnienie" for x in _lst(p, "spowolnienia") if _nachodzi(x, t0, t1)][:1]
        if i in spiew:
            ef.append("śpiew")
        for k, v in cb.items():
            if _norm(k) and _norm(k) == _norm(li.get("sekcja")):
                ef.append("callback " + ", ".join(map(str, v if isinstance(v, list) else [v])))
        out.append((li["tekst"], "; ".join(ob) or "—", "; ".join(ef) or "—"))
    return out


def _md(p, w, slug, wezel, rundy) -> str:
    def c(s):
        return " ".join(str(s).replace("|", "/").split())
    tytul = (w["czasy"] or {}).get("utwor") or slug
    out = [f"# Reżyseria teledysku „{c(tytul)}” ({slug})", "",
           f"Model {MODEL} na węźle {wezel}; rundy krytyka: {rundy}.", "",
           f"Pomysł: {c(p['pomysl'])}", f"Motyw: {c(p['motyw'])}", f"Puenta: {c(w['konc'].get('puenta') or '—')}", "",
           "| Fragment tekstu | Obraz | Efekt |", "|---|---|---|"]
    out += [f"| {c(a)} | {c(b)} | {c(e)} |" for a, b, e in _tabela(p, w)]
    return "\n".join(out) + "\n"


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
    """Procesy FortniteClient* na .253 (PowerShell Get-Process przez ssh, skrypt na stdin)."""
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
    return linie[-1][:300] if linie else ""


async def _stan_unitu(unit: str) -> str:
    try:
        r = await _uruchom(["systemctl", "--user", "is-active", unit], 30)
    except Exception:  # noqa: BLE001
        return "nieznany"
    s = r["stdout"].split()
    return s[-1] if s else "inactive"


async def _model_na(url: str) -> float:
    """Sprawdza model na węźle; zwraca jego rozmiar w GB (z /api/tags, 0.0 gdy brak pola size)."""
    d = await _pobierz(url + "/api/tags", None, TIMEOUT_WEZLA)
    modele = [m for m in (d.get("models") or []) if isinstance(m, dict)]
    nazwy = [str(m.get("name")) for m in modele]
    if MODEL not in nazwy:
        raise RuntimeError(f"brak modelu {MODEL} (są: {', '.join(nazwy[:5]) or 'żadne'})")
    rozm = next((m.get("size") for m in modele if str(m.get("name")) == MODEL), 0)
    try:
        return round(float(rozm or 0) / 1e9, 2)
    except (TypeError, ValueError):
        return 0.0


def _nht():
    try:
        from app.services import node_hardware_truth as nht
    except ImportError:  # tryb --tlo uruchomiony jako skrypt
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from app.services import node_hardware_truth as nht
    return nht


def _pole(n, *nazwy):
    for x in nazwy:
        v = n.get(x) if isinstance(n, dict) else getattr(n, x, None)
        if v is not None:
            return v
    return None


def _wezly() -> list:
    """[(klucz, url_ollamy)] — węzły z NODES z inference=True; adresy wyłącznie z NODES."""
    nodes = _nht().NODES
    pary = nodes.items() if isinstance(nodes, dict) else [(_pole(n, "key", "name", "id"), n) for n in nodes or []]
    out = []
    for k, n in pary:
        if k is None or not _pole(n, "inference"):
            continue
        url = _pole(n, "ollama_url", "url")
        if not url:
            ip = _pole(n, "ip", "host", "addr")
            if not ip:
                continue
            url = f"http://{ip}:11434"
        out.append((str(k), str(url).rstrip("/")))
    return out


def _klucz_url(url: str):
    u = str(url).rstrip("/")
    try:
        for k, x in _wezly():
            if x == u:
                return k
    except Exception:  # noqa: BLE001
        return None
    return None


def _zapisz_wynik(url: str, wynik: str) -> None:
    """Trójkąt uczy się z wyniku: record_outcome(<klucz>, ok|fail, '<ręka> <slug>')."""
    try:
        k = _klucz_url(url)
        if k is not None:
            _nht().record_outcome(k, wynik, f"{REKA} {_SLUG}".strip())
    except Exception:  # noqa: BLE001
        pass


def _scored(d) -> list:
    s = d.get("scored")
    if isinstance(s, dict):
        s = list(s.items())
    out = []
    for x in s if isinstance(s, list) else []:
        tc_brak = False
        if isinstance(x, dict):
            k, c = _pole(x, "node", "key", "name", "id"), _pole(x, "cost", "koszt", "score")
            tc_brak = "transport_cost" in x and x["transport_cost"] is None
        elif isinstance(x, (list, tuple)) and x:
            k, c = x[0], (x[1] if len(x) > 1 else None)
        else:
            continue
        if isinstance(c, dict):
            tc_brak = tc_brak or ("transport_cost" in c and c["transport_cost"] is None)
            c = _pole(c, "cost", "koszt", "score")
        # wpis wykluczony przez trójkąt (gaming-mode, gra gościa, free<need) nie ma kosztu — pomijam
        if tc_brak or c is None or isinstance(c, bool):
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c != c or c in (float("inf"), float("-inf")):
            continue
        if k is not None:
            out.append((c, str(k)))
    return sorted(out, key=lambda t: t[0])


async def _wezel():
    powody, kand, need = [], {}, 0.0
    for klucz, url in _wezly():
        try:
            rozm = await _model_na(url)
        except Exception as e:  # noqa: BLE001
            powody.append(f"{klucz}: {_blad(e)}")
            continue
        kand[klucz] = url
        need = max(need, rozm or 0.0)
    if not kand:
        raise BladWezla("; ".join(powody) or f"brak węzła inference z modelem {MODEL} w NODES")
    q = urlencode({"need_gb": f"{need:.2f}", "model": MODEL})
    try:
        d = await _pobierz(f"{URL_RELOCATE}?{q}", None, TIMEOUT_WEZLA)
    except Exception as e:  # noqa: BLE001
        raise BladWezla("; ".join(powody + [f"trójkąt (relocate): {_blad(e)}"])) from None
    d = d if isinstance(d, dict) else {}
    dec = d.get("decision")
    if isinstance(dec, dict):
        dec = _pole(dec, "node", "key", "name", "id")
    dec = None if dec is None else str(dec)
    powod = _k(d.get("reason") or "")
    dop = f"; {powod}" if powod else ""
    if dec in kand:
        return f"{dec} (trójkąt: decision {dec}{dop})", kand[dec]
    why = f"decision {dec or 'brak'}" + (f" bez modelu {MODEL}" if dec and dec.upper() != "BREAK" else "")
    for koszt, k in _scored(d):
        if k in kand:
            return f"{k} (trójkąt: {why} → następny ze scored, koszt {koszt:g}{dop})", kand[k]
    raise BladWezla("; ".join(powody + [f"trójkąt: {why}; nikt ze scored nie ma modelu; reason: {powod or 'brak'}"]))


async def _chat(url: str, msgs: list) -> str:
    try:
        d = await _pobierz(url + "/api/chat", {"model": MODEL, "messages": msgs, "stream": False, "format": "json",
                                               "options": {"num_ctx": 16384, "temperature": 0.3},
                                               "keep_alive": "10m"}, TIMEOUT_CHAT)
    except Exception:
        _zapisz_wynik(url, "fail")
        raise
    tresc = str((d.get("message") or {}).get("content") or "")
    _zapisz_wynik(url, "ok" if tresc.strip() else "fail")
    return tresc


async def _krytyk(url: str, plan_txt: str, w) -> list:
    try:
        d = json.loads(await _chat(url, _prompt_krytyk(w, plan_txt)))
    except ValueError:
        return []
    pop = d.get("poprawki") if isinstance(d, dict) and isinstance(d.get("poprawki"), list) else []
    return [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in pop if x]


def _argv_tla(kat: Path, url: str, wezel: str) -> list:
    log = kat / LOG
    return ["systemd-run", "--user", f"--unit={_unit(kat.name)}", "--collect", "--quiet",
            f"--description=Shadow: plan reżyserski {kat.name}", f"--property=WorkingDirectory={kat}",
            f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
            "--setenv=PYTHONUNBUFFERED=1", "--", sys.executable, str(Path(__file__).resolve()),
            "--tlo", str(kat), "--url", url, "--wezel", wezel, "--zasady", str(ZASADY)]


# ---------------------------------------------------------------- tryb tła

async def _tlo(kat: Path, url: str, wezel: str) -> int:
    global _SLUG
    kat = Path(kat)
    _SLUG = kat.name
    w = _wejscie(kat)
    if w["bledy"]:
        _log(kat, "wejście niekompletne: " + "; ".join(w["bledy"].values()) + f" — {K_PLAN} bez zmian")
        return 1
    sekcje = _sekcje(w)
    if not sekcje:
        _log(kat, f"brak sekcji w {K_CZASY} — {K_PLAN} bez zmian")
        return 1
    stan = _czytaj_stan_partii(kat)
    done: list[str] = list(stan["done"])
    partie: dict = dict(stan["results"])
    _log(kat, f"start: {MODEL} na węźle {wezel} ({url}) — autor MONTAŻ (partie: {', '.join(sekcje)}; "
              f"wznowienie od: {done[0] if done else 'brak'})")
    try:
        for sek in sekcje:
            if sek in done:
                _log(kat, f"partia „{sek}”: już w stanie — pomijam")
                continue
            msgs = _prompt_partii(w, sek)
            tresc = await _chat(url, msgs)
            d, bledy_partii = _parsuj(tresc, w)
            if d is not None:
                bledy_partii = _bramka_partii(d, w, sek)
            else:
                bledy_partii = [f"odpowiedź sekcji „{sek}” nie jest JSON-em"]
            if bledy_partii:
                _log(kat, f"partia „{sek}”: błędy — {bledy_partii}; jedna poprawka")
                pop = (f"Popraw plan reżyserski TYLKO dla sekcji „{sek}”. "
                       "Błędy:\n- " + "\n- ".join(bledy_partii)
                       + "\nZwróć poprawiony obiekt JSON (tylko pola tej sekcji).")
                msgs = msgs + [{"role": "assistant", "content": tresc},
                               {"role": "user", "content": pop}]
                tresc = await _chat(url, msgs)
                d, bledy_partii = _parsuj(tresc, w)
                if d is not None:
                    bledy_partii = _bramka_partii(d, w, sek)
                else:
                    bledy_partii = [f"odpowiedź sekcji „{sek}” po poprawce nadal nie jest JSON-em"]
            if bledy_partii:
                _log(kat, f"ODRZUCONE: partia „{sek}” nie przeszła po poprawce — {bledy_partii} — {K_PLAN} bez zmian")
                return 2
            partie[sek] = d
            done.append(sek)
            _zapisz_stan_partii(kat, done, partie)
            _log(kat, f"partia „{sek}”: OK (stuków: {len(_lst(d, 'stuki'))})")
    except Exception as e:  # noqa: BLE001
        _log(kat, f"BŁĄD: model na {wezel} nie odpowiedział ({_blad(e)}) — {K_PLAN} bez zmian")
        return 1
    plan = _scal_partie(w, sekcje, partie)
    bledy = _bramka(plan, w)
    if bledy:
        od_nowa = _sekcje_bledow(w, sekcje, plan, bledy)
        if od_nowa:
            done = [s for s in done if s not in od_nowa]
            partie = {k: v for k, v in partie.items() if k not in od_nowa}
            _zapisz_stan_partii(kat, done, partie)
            info = "od nowa pójdą sekcje: " + ", ".join(od_nowa)
        else:
            _zapisz_stan_partii(kat, [], {})
            info = "nie wyznaczę sekcji z błędów — stan partii wyczyszczony, od nowa pójdą wszystkie: " + ", ".join(sekcje)
        _log(kat, "ODRZUCONE: bramka nie przeszła — " + "; ".join(bledy) + f" — {info} — {K_PLAN} bez zmian")
        return 2
    runda = 0
    try:
        uwagi = await _krytyk(url, json.dumps(plan, ensure_ascii=False), w)
        runda = 1
        _log(kat, "krytyk (runda 1): " + ("; ".join(uwagi) if uwagi else "bez poprawek") + " — partii nie przeliczam")
    except Exception as e:  # noqa: BLE001
        _log(kat, f"krytyk na {wezel} nie odpowiedział ({_blad(e)}) — plan bez uwag krytyka")
    rek = {k: plan.get(k) for k in POLA}
    _atomowo(kat / K_PLAN, (json.dumps(rek, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _atomowo(kat / K_MD, _md(rek, w, kat.name, wezel, runda).encode("utf-8"))
    _atomowo(kat / K_PARTIE, b"{}\n")
    _log(kat, f"GOTOWE na węźle {wezel}: {kat / K_PLAN}, {kat / K_MD} (partie: {len(sekcje)})")
    return 0


# ---------------------------------------------------------------- odpowiedzi

def _pokaz(kat: Path, tytul: str) -> dict:
    p = kat / K_MD
    if not p.is_file():
        return {"ok": False, "reason": f"Brief {kat.name} nie ma jeszcze {K_MD} — najpierw „ułóż plan reżyserski "
                                       f"teledysku {tytul} wykonaj”."}
    try:
        linie = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        return {"ok": False, "reason": f"Nie czytam {p} ({_blad(e)}). Niczego nie zgaduję."}
    pole = {n: next((x.split(":", 1)[1].strip() for x in linie if x.startswith(n + ":")), "")
            for n in ("Pomysł", "Motyw", "Puenta")}
    wiersze = [x for x in linie if x.startswith("|") and not x.startswith(("| Fragment tekstu", "|---"))]
    if not pole["Pomysł"] or not wiersze:
        return {"ok": False, "reason": f"{p} nie ma pomysłu albo tabeli. Niczego nie zgaduję."}
    return {"ok": True, "reply": f"REŻYSERIA — „{tytul}” (brief {kat.name})\nPomysł: {pole['Pomysł']}\n"
                                 f"Motyw: {pole['Motyw']}\nPuenta: {pole['Puenta']}\n| Fragment tekstu | Obraz | Efekt |\n"
                                 + "\n".join(wiersze[:12]) + f"\n({min(12, len(wiersze))} z {len(wiersze)} wierszy; "
                                 f"plik {p})"}


def _wynik(nazwa: str, bl: list, ok_txt: str) -> str:
    if not bl:
        return f"{nazwa}: OK ({ok_txt})."
    return f"{nazwa}: {len(bl)} {_odm(len(bl), 'błąd', 'błędy', 'błędów')} — " + "; ".join(bl) + "."


def _ocen(kat: Path, slug: str, tytul: str, w) -> dict:
    b, out = w["bledy"], [f"OCENA (bez modelu i bez zapisu) — „{tytul}” (brief {slug})."]
    if "konc" in b:
        out += [f"Koncepcja: {b['konc']}.", f"Sceny: {b['konc']}."]
    else:
        out.append(_wynik("Koncepcja", _bramka_konc(w["konc"], _sekcje(w)), "pomysł, motyw, puenta, 3 odrzucone, łuk"))
        n = len(w["ids"])
        out.append(_wynik("Sceny", _bramka_scen(w), f"{n} {_odm(n, 'scena', 'sceny', 'scen')}"))
    p = kat / K_PLAN
    braki = [b[k] for k in ("czasy", "slowa") if k in b]
    if not p.is_file():
        out.append(f"Plan: brak {K_PLAN}.")
    elif braki:
        out.append(f"Plan: nie sprawdzę — {'; '.join(braki)}.")
    else:
        try:
            d = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            out.append(f"Plan: {K_PLAN} nieczytelny ({_blad(e)}).")
        else:
            out.append(_wynik("Plan", _bramka(d, w), "hasła, stuki, callbacki, wstawki, rymy"))
    return {"ok": True, "reply": "\n".join(out)}


def _podglad(slug: str, tytul: str, kat: Path, w, su: str) -> str:
    b, n, nl = w["bledy"], len(w["ids"]), len(w["linie"])
    linie = [f"PODGLĄD (bez „wykonaj” modelu nie wołam i niczego nie zapisuję) — plan reżyserski teledysku "
             f"„{tytul}” (brief {slug}).",
             f"{K_KONC}: " + (b.get("konc") or b.get("sceny") or
                              f"{n} {_odm(n, 'scena', 'sceny', 'scen')} ({', '.join(w['ids'])})") + ".",
             f"{K_CZASY}: " + (b.get("czasy") or f"{nl} {_odm(nl, 'linia', 'linie', 'linii')}, sekcje: "
                                                 f"{', '.join(_sekcje(w))}") + ".",
             f"{K_SLOWA}: " + (b.get("slowa") or f"{len(w['slowa'])} słów z czasami") + ".",
             f"Zasady {ZASADY}: " + (b.get("zasady") or f"{len(w['zasady'])} znaków, idą w całości do autora i "
                                                         "krytyka") + ".",
             f"{K_PLAN}: {'jest' if (kat / K_PLAN).is_file() else 'jeszcze nie ma'}; "
             f"{K_MD}: {'jest' if (kat / K_MD).is_file() else 'jeszcze nie ma'}."]
    ost = _ostatni_log(kat)
    linie.append(f"Unit {_unit(slug)}: {su}." + (f" Ostatnio w {LOG}: {ost}" if ost else ""))
    if su in DZIALA:
        linie.append("Z „wykonaj” teraz odmówię — model już liczy w tle.")
    elif b:
        linie.append("Z „wykonaj” odmówię — brakuje: " + "; ".join(b.values()) + ".")
    else:
        linie.append(f"Z „wykonaj”: {MODEL} (najpierw .172, potem .253 bez FortniteClient*) ułoży plan, krytyk oceni "
                     f"go (do {MAX_RUND} rund) w tle (systemd-run --user, log {kat / LOG}); {K_PLAN} i {K_MD} "
                     "zapiszę dopiero po bramce.")
    return "\n".join(linie)


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie planu reżyserskiego (ułóż plan reżyserski / pokaż "
                                       "reżyserię / oceń pomysł teledysku …)."}
    params = params or {}
    try:
        slug, tytul = _cel(cmd, params)
    except LookupError as e:
        return {"ok": False, "reason": f"{e}. Niczego nie zgaduję."}
    kat = BRIEFY / slug
    if not kat.is_dir():
        return {"ok": False, "reason": f"Nie ma katalogu briefu „{tytul}” ({kat}). Niczego nie zgaduję."}
    wyk = bool(_T_WYKONAJ.search(cmd))
    if _T_OCEN.search(cmd):
        return _ocen(kat, slug, tytul, _wejscie(kat))
    if _T_POKAZ.search(cmd) and not wyk:
        return _pokaz(kat, tytul)
    w, unit = _wejscie(kat), _unit(slug)
    su = await _stan_unitu(unit)
    if not wyk:
        return {"ok": True, "reply": _podglad(slug, tytul, kat, w, su)}
    if su in DZIALA:
        return {"ok": False, "reason": f"Plan „{tytul}” już liczy się w tle ({unit}: {su}, log {kat / LOG})."}
    if w["bledy"]:
        return {"ok": False, "reason": "Brief niekompletny: " + "; ".join(w["bledy"].values())
                                       + ". Modelu nie wołałem."}
    try:
        wezel, url = await _wezel()
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
    return {"ok": True, "reply": f"PLAN REŻYSERSKI LICZY SIĘ W TLE — „{tytul}” (brief {slug}).\n"
                                 f"Model {MODEL} liczy na węźle {wezel} ({url}) — autor (MONTAŻ) i krytyk "
                                 f"(do {MAX_RUND} rund).\nUnit {unit} (systemd-run --user, restart backendu go nie "
                                 f"zabija), log {kat / LOG}.\n{K_PLAN} i {K_MD} zapiszę atomowo dopiero po bramce. "
                                 f"Niczego nie publikuję. Wynik: „pokaż reżyserię teledysku {tytul}”."}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka teledysk_plan_rezyserski padła: {_blad(e)}"}


async def handler_teledysk_plan_rezyserski(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}


def _main(argv=None) -> int:
    global ZASADY
    ap = argparse.ArgumentParser(description="teledysk_plan_rezyserski — tryb tła")
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
