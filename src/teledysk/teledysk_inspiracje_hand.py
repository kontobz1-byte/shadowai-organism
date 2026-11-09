# plik: teledysk_inspiracje_hand.py
"""Ręka teledysk_inspiracje: INSPIRACJA TELEDYSKU (krok 0 artistic flow).

Brief: BRIEFY/<NN_slug>/ (utwór po tytule z utwory.tsv: slug, tytuł, -, audio, tekst; stare 30_*/31_* nietknięte).
„wyszukaj inspiracje do teledysku …” (most 14T / WA):
- gdy inspiracje.json już jest: 5 najlepszych pomysłów ze źródłami z pliku, bez modelu i sieci;
- gdy go nie ma (albo jest „od nowa”): wybór węzła = TRÓJKĄT (kandydaci z node_hardware_truth.NODES z
  inference=True i MODEL w /api/tags, decyzja z GET /api/network/relocate, potem scored po koszcie), katalog briefu
  (tworzony, gdy go nie ma), potem systemd-run --user unit teledysk-inspiracje-<slug> (tryb --tlo).
  W tle wynik każdego kroku modelu idzie do node_hardware_truth.record_outcome (best-effort).
Tryb tła: model układa 4–6 zapytań PL+EN, potem web_search(zapytanie, 8) z app.services.web_research
(przeglądarka Shadowa, bez API; przy 0 wynikach run_research), potem fetch_url po kolei, najwyżej 6 stron.
Na koniec model wyciąga pomysły ze źródłami, bramka sprawdza wynik i inspiracje.json oraz inspiracje.md
zapisuję atomowo. Stan kroków trzymam w inspiracje_stan.json, żeby wznowić od brakującego kroku.
Ręka tylko czyta sieć i zapisuje w katalogu briefu, więc działa bez „wykonaj”. Niczego nie publikuje ani nie
dopisuje do kolejki publikacji (Waga PL/EN nie jest do publikacji).
Granice I/O: _pobierz (HTTP Ollama), _ssh_procesy (ssh .253), _uruchom (procesy), _szukaj/_czytaj/_research (web).
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

import httpx

WZORZEC = r"\binspiracj\w*\s+(?:do\s+|dla\s+|na\s+)?(?:teledysk\w*|klip\w*)"
GOTOWA = True

ROOT = Path(os.environ.get("TELEDYSK_ROOT", "/opt/shadow/teledysk_java"))
BRIEFY = ROOT / "briefy_1017"
UTWORY_TSV = BRIEFY / "utwory.tsv"
MUZYKA = Path("/opt/shadow/shadow/creative/music")
TEKSTY = {"32_waga_wazna_flow": MUZYKA / "Waga_wazna_polski" / "tekst.txt",
          "33_heavy_burdens_en": MUZYKA / "Waga_wazna_english" / "tekst.txt",
          "34_przesilenie_flow": MUZYKA / "Przesilenie_glos_2_polski" / "tekst.txt",
          "35_echo_in_the_crowd_en": MUZYKA / "Przesilenie_glos_2_english" / "tekst.txt"}
STARE = ("30_", "31_")
MODEL = "qwen3.8:27b"
RELOCATE = "http://127.0.0.1:8001/api/network/relocate"
HOST_253, KLUCZ_SSH = "user@example.com", "/opt/shadow/.ssh/shadowai_gpu"
_TLO: dict = {}  # tryb tła: klucz węzła i katalog briefu dla record_outcome
PS_FORTNITE = "Get-Process -Name FortniteClient* -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName }\n"
TIMEOUT_WEZLA, TIMEOUT_CHAT = 10.0, 900.0
NA_ZAPYTANIE, MAX_STRON, ZNAKI_STRONY, NAJLEPSZE = 8, 6, 4000, 5
K_JSON, K_MD, K_STAN, LOG = "inspiracje.json", "inspiracje.md", "inspiracje_stan.json", "inspiracje.log"
TYPY = ("ujecie", "akcja", "set_piece")
DZIALA = ("active", "activating", "reloading", "deactivating")
_T_NOWO = re.compile(r"\b(?:od\s+nowa|od[śs]wie[żz]\w*|ponownie|jeszcze\s+raz)\b", re.I)
_PL = re.compile(r"teledysk|ujec|pomysl|rapow|klipy")
_EN = re.compile(r"\b(?:music videos?|videos?|shots?|scenes?|clips?)\b")


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


def _ocena(v) -> float:
    if isinstance(v, bool):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _uniq(z) -> list:
    return list(dict.fromkeys(x.strip() for x in z if _txt(x)))


def _unit(slug: str) -> str:
    return f"teledysk-inspiracje-{slug}"


def _sekcje(tekst: str) -> list:
    return list(dict.fromkeys(m.strip() for m in re.findall(r"^\s*\[([^\]\n]+)\]", tekst, re.M)))


# ---------------------------------------------------------------- utwór

def _utwory() -> list:
    out = []
    for linia in UTWORY_TSV.read_text(encoding="utf-8-sig").splitlines():
        kol = [x.strip() for x in linia.split("\t")]
        if len(kol) >= 2 and re.fullmatch(r"\d{2}_[a-z0-9_]+", kol[0]) and not kol[0].startswith(STARE):
            t = kol[4] if len(kol) > 4 and kol[4] not in ("", "-") else ""
            p = Path(t) if t else TEKSTY.get(kol[0])
            if p is not None and not p.is_absolute():
                p = UTWORY_TSV.parent / p
            out.append((kol[0], kol[1], p))
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
        raise LookupError("Pasuje kilka briefów: " + ", ".join(u[0] for u in traf) + " — podaj slug")
    raise LookupError("Nie rozpoznałem utworu — znam: " + ", ".join(f"{u[1]} ({u[0]})" for u in utw))


# ---------------------------------------------------------------- bramki i normalizacja (czyste funkcje)

def _bramka_zapytan(d) -> list:
    z = d.get("zapytania") if isinstance(d, dict) else None
    if not isinstance(z, list):
        return ["brak listy zapytania"]
    u = _uniq(z)
    b = [] if 4 <= len(u) <= 6 else [f"zapytania: ma być 4–6, jest {len(u)}"]
    if len(u) != len(z):
        b.append("zapytania: puste albo powtórzone wpisy")
    if not any(_PL.search(_norm(x)) for x in u):
        b.append("brak zapytania po polsku (np. teledyski rapowe o …)")
    if not any(_EN.search(_norm(x)) for x in u):
        b.append("brak zapytania po angielsku (np. music video ideas about …)")
    return b


def _pomysly(d, urls) -> tuple:
    lst = d.get("pomysly") if isinstance(d, dict) else None
    if not isinstance(lst, list):
        return [], ["brak listy pomysly"]
    mapa = {u.rstrip("/"): u for u in urls}
    dobre, b = [], []
    for i, x in enumerate(lst, 1):
        if not isinstance(x, dict) or not _txt(x.get("pomysl")):
            b.append(f"pomysł nr {i}: puste pole pomysl")
            continue
        typ, zr = _norm(x.get("typ")).replace(" ", "_"), str(x.get("zrodlo") or "").strip().rstrip("/")
        if typ not in TYPY:
            b.append(f"pomysł nr {i}: typ „{x.get('typ')}” spoza {'|'.join(TYPY)}")
        elif zr not in mapa:
            b.append(f"pomysł nr {i}: źródło spoza przeczytanych stron ({zr or 'brak'})")
        else:
            dobre.append({"pomysl": x["pomysl"].strip(), "typ": typ, "zrodlo": mapa[zr],
                          "jak_u_nas": str(x.get("jak_u_nas") or "").strip(), "ocena": _ocena(x.get("ocena"))})
    if len(dobre) < NAJLEPSZE:
        b.append(f"za mało pomysłów ze źródłem z listy: {len(dobre)}, ma być co najmniej {NAJLEPSZE}")
    return dobre, b


def _lista_wynikow(r) -> list:
    if isinstance(r, dict):
        r = next((r[k] for k in ("results", "wyniki", "sources", "zrodla", "items") if isinstance(r.get(k), list)), [])
    out = []
    for x in r if isinstance(r, list) else []:
        x = {"url": x} if isinstance(x, str) else x
        if not isinstance(x, dict):
            continue
        u = next((str(x[k]).strip() for k in ("url", "link", "href") if _txt(x.get(k))), "")
        if u.startswith(("http://", "https://")):
            out.append({"url": u, "tytul": _k(x.get("title") or x.get("tytul") or "", 200),
                        "opis": _k(x.get("snippet") or x.get("body") or x.get("opis") or "", 300)})
    return out


def _tekst_strony(r) -> str:
    if isinstance(r, dict):
        if r.get("ok") is False or r.get("error"):
            raise RuntimeError(_k(r.get("error") or "ok=false", 200))
        r = next((r[k] for k in ("text", "content", "tekst", "markdown", "body") if _txt(r.get(k))), "")
    return " ".join(str(r or "").split())


def _przeplot(listy) -> list:
    out, widz = [], set()
    for i in range(max((len(x) for x in listy), default=0)):
        for lst in listy:
            if i < len(lst) and lst[i]["url"] not in widz:
                widz.add(lst[i]["url"])
                out.append(lst[i])
    return out


def _krok(st) -> str:
    if not st.get("zapytania"):
        return "od początku (zapytania)"
    if not st.get("wyniki"):
        return "wznawiam od wyszukiwania"
    pr, n = len(st.get("proby") or []), min(MAX_STRON, len(st["wyniki"]))
    return f"wznawiam od czytania stron ({pr} z {n})" if pr < n else "wznawiam od wyciągania pomysłów"


# ---------------------------------------------------------------- prompty i inspiracje.md

def _prompt_zapytan(tytul: str, tekst: str) -> list:
    sys_ = ("Jesteś researcherem teledysku — krok 0 artistic flow: INSPIRACJA. Z tekstu piosenki wyprowadź temat i ułóż "
            "4–6 zapytań do wyszukiwarki, po polsku I po angielsku (np. „teledyski rapowe o <temat>”, „pomysły na "
            "ujęcia do teledysku o <motyw>”, „music video ideas about <theme>”, „famous music videos with <motif>”). "
            'Odpowiedz WYŁĄCZNIE obiektem JSON {"temat": "<temat utworu jednym zdaniem>", "zapytania": ["…"]}.')
    usr = f"Utwór: {tytul}\nSekcje: {', '.join(_sekcje(tekst)) or '—'}\n\nTekst:\n{tekst}"
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


def _prompt_pomyslow(tytul: str, tekst: str, st: dict) -> list:
    zr = "\n\n".join(f"[{i}] {x['url']} — {x['tytul']}\n{x['tekst']}" for i, x in enumerate(st["strony"], 1))
    sys_ = ("Jesteś researcherem teledysku — krok 0 artistic flow: INSPIRACJA. Z przeczytanych stron wyciągnij KONKRETNE "
            "pomysły na ujęcia (typ ujecie), akcje postaci (typ akcja) i set pieces (typ set_piece) pasujące do utworu. "
            "To materiał do kroku POMYSŁ: inspiracja, nie kopia cudzego teledysku 1:1 — w jak_u_nas napisz, jak zrobić "
            "z niego własny pomysł dla tego utworu. zrodlo = dokładny adres strony z listy, z której pochodzi pomysł; "
            "nie podawaj źródeł spoza listy. ocena 1–10 = jak bardzo pasuje do tekstu. Odpowiedz WYŁĄCZNIE obiektem "
            'JSON {"pomysly": [{"pomysl": "…", "typ": "ujecie|akcja|set_piece", "zrodlo": "<url>", "jak_u_nas": "…", '
            f'"ocena": 8}}]}} — od {NAJLEPSZE} do 12 pomysłów.')
    usr = (f"Utwór: {tytul}\nTemat: {st.get('temat') or '—'}\n\nTekst:\n{tekst}\n\nPrzeczytane strony:\n{zr}")
    return [{"role": "system", "content": sys_}, {"role": "user", "content": usr}]


def _md(rek: dict) -> str:
    def c(s):
        return " ".join(str(s).replace("|", "/").split())
    out = [f"# Inspiracje do teledysku „{c(rek['utwor'])}” ({rek['slug']})", "",
           f"Model {MODEL} na węźle {rek['wezel']}; droga wyszukiwania: {rek['droga']}; {rek['data']}.",
           f"Temat: {c(rek['temat'] or '—')}", "", rek["uwaga"].capitalize() + ".", "", "## Zapytania"]
    out += [f"- {c(q)}" for q in rek["zapytania"]]
    out += ["", "## Pomysły", "", "| # | Typ | Pomysł | Jak u nas | Ocena | Źródło |", "|---|---|---|---|---|---|"]
    out += [f"| {i} | {x['typ']} | {c(x['pomysl'])} | {c(x['jak_u_nas'] or '—')} | {x['ocena']:g} | {x['zrodlo']} |"
            for i, x in enumerate(rek["pomysly"], 1)]
    out += ["", "## Przeczytane strony"] + [f"- {x['url']} — {c(x['tytul'] or '—')}" for x in rek["zrodla"]]
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


async def _wolaj(nazwa: str, *a):
    from app.services import web_research as wr  # backend Shadowa: przeglądarka jak człowiek
    fn = getattr(wr, nazwa)
    r = fn(*a) if inspect.iscoroutinefunction(fn) else await asyncio.to_thread(fn, *a)
    return await r if inspect.isawaitable(r) else r


async def _szukaj(zapytanie: str, ile: int = NA_ZAPYTANIE) -> list:
    """web_search(zapytanie, 8): xvfb Chromium → DuckDuckGo, bez API."""
    return _lista_wynikow(await _wolaj("web_search", zapytanie, ile))


async def _czytaj(url: str) -> str:
    """fetch_url(url): jedna strona naraz, bez logowania."""
    return _tekst_strony(await _wolaj("fetch_url", url))


async def _research(zapytanie: str) -> list:
    """Inna droga z web_research, gdy przeglądarka da 0 wyników."""
    return _lista_wynikow(await _wolaj("run_research", zapytanie))


def _atomowo(p: Path, dane: bytes) -> None:
    tmp = p.with_name(p.name + ".shadow_tmp")
    tmp.write_bytes(dane)
    os.replace(tmp, p)


def _stan(kat: Path) -> dict:
    try:
        d = json.loads((kat / K_STAN).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _zapisz_stan(kat: Path, st: dict) -> None:
    _atomowo(kat / K_STAN, (json.dumps(st, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


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


def _nht():
    """app.services.node_hardware_truth; w trybie tła dopisuję katalog backend do sys.path."""
    try:
        from app.services import node_hardware_truth as n
    except ImportError:
        b = str(Path(__file__).resolve().parents[2])
        if b not in sys.path:
            sys.path.insert(0, b)
        from app.services import node_hardware_truth as n
    return n


def _wezly() -> dict:
    """Węzły z NODES z inference=True: klucz → URL Ollamy (adresy wyłącznie z NODES)."""
    nodes = getattr(_nht(), "NODES", None) or {}
    if isinstance(nodes, dict):
        pary = list(nodes.items())
    else:
        pary = [((n.get("key") if isinstance(n, dict) else getattr(n, "key", None)), n) for n in nodes]
    out = {}
    for k, n in pary:
        pole = (lambda a, n=n: n.get(a)) if isinstance(n, dict) else (lambda a, n=n: getattr(n, a, None))
        ip = pole("ip") or pole("host")
        if k is not None and pole("inference") is True and _txt(ip):
            out[str(k)] = f"http://{ip.strip()}:11434"
    return out


def _scored(d) -> list:
    """Klucze z pola scored po rosnącym koszcie; bez skończonego kosztu albo transport_cost=None — wykluczone."""
    sc = d.get("scored") if isinstance(d, dict) else None
    if isinstance(sc, dict):
        sc = [dict(v, node=k) if isinstance(v, dict) else {"node": k, "cost": v} for k, v in sc.items()]
    out = []
    for e in sc if isinstance(sc, list) else []:
        if not isinstance(e, dict):
            continue
        k = next((e[x] for x in ("node", "key", "name", "wezel") if e.get(x) is not None), None)
        c = e.get("cost")
        if k is None or isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c):
            continue
        if "transport_cost" in e and e["transport_cost"] is None:
            continue
        out.append((float(c), str(k)))
    return [k for _, k in sorted(out, key=lambda x: x[0])]


async def _wynik(stan: str) -> None:
    """record_outcome(<klucz węzła>, ok|fail, „teledysk_inspiracje <katalog>”) — best-effort, błąd do logu."""
    w, kat = _TLO.get("wezel"), _TLO.get("kat")
    if not w or kat is None:
        return
    try:
        r = _nht().record_outcome(w, stan, f"teledysk_inspiracje {Path(kat).name}")
        if inspect.isawaitable(r):
            await r
    except Exception as e:  # noqa: BLE001
        try:
            _log(Path(kat), f"record_outcome {w} {stan} nie zapisany (best-effort): {_blad(e)}")
        except OSError:
            pass


async def _model_na(url: str) -> float:
    """Sprawdza MODEL w /api/tags; zwraca jego rozmiar w GB."""
    d = await _pobierz(url + "/api/tags", None, TIMEOUT_WEZLA)
    ms = [m for m in (d.get("models") or []) if isinstance(m, dict)]
    nazwy = [str(m.get("name")) for m in ms]
    if MODEL not in nazwy:
        raise RuntimeError(f"brak modelu {MODEL} (są: {', '.join(nazwy[:5]) or 'żadne'})")
    s = next((m.get("size") for m in ms if str(m.get("name")) == MODEL), 0)
    return round(float(s) / 1e9, 2) if isinstance(s, (int, float)) and not isinstance(s, bool) else 0.0


async def _wezel():
    """TRÓJKĄT: kandydaci z NODES z modelem, decyzja z relocate, potem scored. Zwraca (klucz, url, powód)."""
    from urllib.parse import urlencode
    wezly = _wezly()
    if not wezly:
        raise BladWezla("brak węzłów z inference=True w node_hardware_truth.NODES")
    kand, powody, need = {}, [], 0.0
    for k, url in wezly.items():
        try:
            gb = await _model_na(url)
        except Exception as e:  # noqa: BLE001
            powody.append(f"{k}: {_blad(e)}")
            continue
        kand[k], need = url, max(need, gb)
    if not kand:
        raise BladWezla("; ".join(powody) or f"żaden węzeł nie ma {MODEL}")
    try:
        d = await _pobierz(RELOCATE + "?" + urlencode({"need_gb": need, "model": MODEL}), None, TIMEOUT_WEZLA)
    except Exception as e:  # noqa: BLE001
        raise BladWezla(f"trójkąt (relocate) nie odpowiada: {_blad(e)}") from None
    d = d if isinstance(d, dict) else {}
    dec, powod = d.get("decision"), _k(d.get("reason") or "", 300)
    dk = None if dec is None else str(dec).strip()
    if dk and dk.upper() != "BREAK" and dk in kand:
        return dk, kand[dk], powod or f"decyzja trójkąta: {dk}"
    for k in _scored(d):
        if k in kand:
            return k, kand[k], (f"decyzja {dk or 'brak'} bez {MODEL} — następny w scored po koszcie: {k}"
                                + (f" ({powod})" if powod else ""))
    raise BladWezla(f"trójkąt nie wskazał węzła: {powod or 'bez powodu'}"
                    + (f"; {'; '.join(powody)}" if powody else ""))


async def _chat(url: str, msgs: list) -> str:
    d = await _pobierz(url + "/api/chat", {"model": MODEL, "messages": msgs, "stream": False, "format": "json",
                                           "options": {"num_ctx": 16384, "temperature": 0.8},
                                           "keep_alive": "10m"}, TIMEOUT_CHAT)
    return str((d.get("message") or {}).get("content") or "")


async def _z_poprawka(url: str, msgs: list, bramka):
    """_z_poprawka_model + wynik węzła do node_hardware_truth (w trybie tła): odpowiedział → ok, wyjątek → fail."""
    try:
        w = await _z_poprawka_model(url, msgs, bramka)
    except Exception:
        await _wynik("fail")
        raise
    await _wynik("ok")
    return w


async def _z_poprawka_model(url: str, msgs: list, bramka):
    """Odpowiedź modelu + 1 poprawka z listą błędów bramki. Zwraca (obiekt, błędy ostatniej próby)."""
    for proba in (1, 2):
        tresc = await _chat(url, msgs)
        try:
            d = json.loads(tresc)
        except ValueError as e:
            d, bl = None, [f"odpowiedź nie jest JSON-em ({_blad(e)})"]
        else:
            bl = bramka(d)
        if not bl or proba == 2:
            return d, bl
        msgs = msgs + [{"role": "assistant", "content": tresc},
                       {"role": "user", "content": "Popraw odpowiedź. Błędy bramki:\n- " + "\n- ".join(bl)
                                                   + "\nZwróć cały poprawiony obiekt JSON."}]


def _argv_tla(kat: Path, url: str, wezel: str, tytul: str, tekst_p: Path) -> list:
    log = kat / LOG
    pp = os.pathsep.join(dict.fromkeys([os.getcwd()] + [p for p in sys.path if p]))
    return ["systemd-run", "--user", f"--unit={_unit(kat.name)}", "--collect", "--quiet",
            f"--description=Shadow: inspiracje teledysku {kat.name}", f"--property=WorkingDirectory={kat}",
            f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
            "--setenv=PYTHONUNBUFFERED=1", f"--setenv=PYTHONPATH={pp}", "--", sys.executable,
            str(Path(__file__).resolve()), "--tlo", str(kat), "--url", url, "--wezel", wezel, "--tytul", tytul,
            "--tekst", str(tekst_p)]


# ---------------------------------------------------------------- tryb tła

async def _tlo(kat: Path, url: str, wezel: str, tytul: str, tekst_p: Path) -> int:
    kat, tekst_p = Path(kat), Path(tekst_p)
    kat.mkdir(parents=True, exist_ok=True)
    try:
        tekst = tekst_p.read_text(encoding="utf-8-sig")
    except OSError as e:
        _log(kat, f"BŁĄD: nie czytam tekstu utworu {tekst_p} ({_blad(e)}) — {K_JSON} bez zmian")
        return 1
    st = _stan(kat)
    _TLO.update(wezel=str(wezel), kat=kat)
    _log(kat, f"start: {MODEL} na węźle {wezel} ({url}); {_krok(st)}")
    try:
        if not st.get("zapytania"):
            d, bl = await _z_poprawka(url, _prompt_zapytan(tytul, tekst), _bramka_zapytan)
            if bl:
                _log(kat, "ODRZUCONE: zapytania po poprawce — " + "; ".join(bl) + f" — {K_JSON} bez zmian")
                return 2
            st = {"temat": str(d.get("temat") or "").strip(), "zapytania": _uniq(d["zapytania"])}
            _zapisz_stan(kat, st)
            _log(kat, "zapytania: " + " | ".join(st["zapytania"]))
        if not st.get("wyniki"):
            listy, bledy, droga = [], [], "przegladarka"
            for q in st["zapytania"]:
                try:
                    listy.append(await _szukaj(q, NA_ZAPYTANIE))
                except Exception as e:  # noqa: BLE001
                    bledy.append(f"web_search „{q}”: {_blad(e)}")
            if not any(listy):
                droga = "run_research"
                _log(kat, "przeglądarka: 0 wyników — próbuję run_research")
                for q in st["zapytania"]:
                    try:
                        listy.append(await _research(q))
                    except Exception as e:  # noqa: BLE001
                        bledy.append(f"run_research „{q}”: {_blad(e)}")
            st.update(wyniki=_przeplot(listy), droga=droga, strony=[], proby=[])
            if not st["wyniki"]:
                _log(kat, "BŁĄD: źródło nie odpowiada — przeglądarka i run_research dały 0 wyników"
                     + (f" ({_k('; '.join(bledy))})" if bledy else "") + f" — {K_JSON} bez zmian")
                return 1
            _zapisz_stan(kat, st)
            _log(kat, f"wyszukiwanie ({droga}): {len(st['wyniki'])} adresów")
        st.setdefault("strony", [])
        st.setdefault("proby", [])
        for w in st["wyniki"]:
            if len(st["proby"]) >= MAX_STRON:
                break
            if w["url"] in st["proby"]:
                continue
            st["proby"].append(w["url"])
            try:
                t = await _czytaj(w["url"])
            except Exception as e:  # noqa: BLE001
                t = ""
                _log(kat, f"fetch_url {w['url']}: {_blad(e)}")
            if t.strip():
                st["strony"].append({"url": w["url"], "tytul": w.get("tytul") or "", "tekst": t[:ZNAKI_STRONY]})
            _zapisz_stan(kat, st)
        if not st["strony"]:
            n, st["proby"] = len(st["proby"]), []
            _zapisz_stan(kat, st)
            _log(kat, f"BŁĄD: źródło nie odpowiada — żadna z {n} stron nie dała tekstu — {K_JSON} bez zmian")
            return 1
        urls = [x["url"] for x in st["strony"]]
        d, _ = await _z_poprawka(url, _prompt_pomyslow(tytul, tekst, st), lambda x: _pomysly(x, urls)[1])
        dobre, bl = _pomysly(d, urls)
    except Exception as e:  # noqa: BLE001
        _log(kat, f"BŁĄD: {_blad(e)} — {K_JSON} bez zmian, stan w {K_STAN} (wznowię od brakującego kroku)")
        return 1
    if len(dobre) < NAJLEPSZE:
        _log(kat, "ODRZUCONE: bramka pomysłów nie przeszła po poprawce — " + "; ".join(bl) + f" — {K_JSON} bez zmian")
        return 2
    if bl:
        _log(kat, "pominięte po bramce: " + "; ".join(bl))
    dobre.sort(key=lambda x: -x["ocena"])
    rek = {"utwor": tytul, "slug": kat.name, "temat": st.get("temat") or "", "model": MODEL, "wezel": wezel,
           "data": time.strftime("%Y-%m-%d %H:%M"), "droga": st.get("droga"), "zapytania": st["zapytania"],
           "zrodla": [{"url": x["url"], "tytul": x["tytul"]} for x in st["strony"]], "pomysly": dobre,
           "uwaga": "materiał dla kroku POMYSŁ — inspiracja, nie kopia cudzych teledysków 1:1"}
    _atomowo(kat / K_JSON, (json.dumps(rek, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _atomowo(kat / K_MD, _md(rek).encode("utf-8"))
    _log(kat, f"GOTOWE na węźle {wezel}: {kat / K_JSON} ({len(dobre)} pomysłów z {len(urls)} stron)")
    return 0


# ---------------------------------------------------------------- odpowiedzi

def _pokaz(kat: Path, tytul: str) -> dict:
    p = kat / K_JSON
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {"ok": False, "reason": f"{p} nieczytelny ({_blad(e)}). Niczego nie zgaduję."}
    pom = [x for x in (d.get("pomysly") or []) if isinstance(x, dict) and _txt(x.get("pomysl"))] \
        if isinstance(d, dict) else []
    if not pom:
        return {"ok": False, "reason": f"{p} nie ma pomysłów. Od nowa: „wyszukaj od nowa inspiracje do teledysku "
                                       f"{tytul}”. Niczego nie zgaduję."}
    naj = sorted(pom, key=lambda x: -_ocena(x.get("ocena")))[:NAJLEPSZE]
    linie = [f"INSPIRACJE — „{tytul}” (brief {kat.name}): {len(naj)} najlepszych z {len(pom)} "
             f"(model na węźle {d.get('wezel') or '?'})."]
    linie += [f"{i}. [{x.get('typ')}, {_ocena(x.get('ocena')):g}/10] {x['pomysl'].strip()}"
              + (f" → u nas: {x['jak_u_nas'].strip()}" if _txt(x.get("jak_u_nas")) else "")
              + f" (źródło: {x.get('zrodlo')})" for i, x in enumerate(naj, 1)]
    linie.append(f"To materiał dla kroku POMYSŁ — inspiracja, nie kopia 1:1. Plik {p}; od nowa: „wyszukaj od nowa "
                 f"inspiracje do teledysku {tytul}”.")
    return {"ok": True, "reply": "\n".join(linie)}


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie inspiracji (wyszukaj inspiracje do teledysku …)."}
    try:
        slug, tytul, tekst_p = _cel(cmd, params or {})
    except LookupError as e:
        return {"ok": False, "reason": f"{e}. Niczego nie zgaduję."}
    kat, unit, nowo = BRIEFY / slug, _unit(slug), bool(_T_NOWO.search(cmd))
    su = await _stan_unitu(unit)
    if su in DZIALA:
        return {"ok": True, "reply": f"INSPIRACJE dla „{tytul}” wciąż szukają się w tle ({unit}: {su}). Ostatnio w "
                                     f"{LOG}: {_ostatni_log(kat) or '—'}. Wynik: powtórz komendę, gdy skończy."}
    if (kat / K_JSON).is_file() and not nowo:
        return _pokaz(kat, tytul)
    try:
        if tekst_p is None or not tekst_p.read_text(encoding="utf-8-sig").strip():
            return {"ok": False, "reason": f"Pusty albo nieznany tekst utworu „{tytul}” ({tekst_p}). "
                                           "Niczego nie zgaduję."}
    except OSError as e:
        return {"ok": False, "reason": f"Nie czytam tekstu utworu „{tytul}” {tekst_p} ({_blad(e)}). "
                                       "Niczego nie zgaduję."}
    try:
        wezel, url, powod_wezla = await _wezel()
    except BladWezla as e:
        return {"ok": False, "reason": f"Model {MODEL} nie ma gdzie liczyć — {e}. Niczego nie uruchomiłem."}
    try:
        kat.mkdir(parents=True, exist_ok=True)
        if nowo:
            (kat / K_STAN).unlink(missing_ok=True)
    except OSError as e:
        return {"ok": False, "reason": f"Nie przygotuję katalogu briefu {kat} ({_blad(e)})."}
    krok = _krok(_stan(kat))
    if su == "failed":
        await _uruchom(["systemctl", "--user", "reset-failed", unit], 30)
    try:
        r = await _uruchom(_argv_tla(kat, url, wezel, tytul, tekst_p), 30)
    except Exception as e:  # noqa: BLE001
        r = {"kod": 127, "stdout": "", "stderr": _blad(e)}
    if r["kod"] != 0:
        return {"ok": False, "reason": f"systemd-run nie uruchomił {unit} (kod {r['kod']}: "
                                       f"{_k(r['stderr'] or r['stdout'] or 'bez komunikatu', 200)})."}
    return {"ok": True, "wezel": wezel, "powod_wezla": powod_wezla,
            "reply": f"INSPIRACJE SZUKAJĄ SIĘ W TLE — „{tytul}” (brief {slug}), {krok}.\n"
                                 f"Wybór węzła (trójkąt): {wezel} — {powod_wezla}.\n"
                                 f"Model {MODEL} liczy na węźle {wezel} ({url}): zapytania PL i EN, potem przeglądarka "
                                 f"Shadowa (web_search → DuckDuckGo, po kolei najwyżej {MAX_STRON} stron fetch_url, "
                                 f"bez logowania), na koniec pomysły ze źródłami.\nUnit {unit} (systemd-run --user, "
                                 f"restart backendu go nie zabija), log {kat / LOG}.\n{K_JSON} i {K_MD} zapiszę "
                                 f"atomowo dopiero po bramce. Niczego nie publikuję ani nie dopisuję do kolejki "
                                 f"publikacji. Wynik ({NAJLEPSZE} najlepszych pomysłów): powtórz „wyszukaj inspiracje "
                                 f"do teledysku {tytul}”."}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka teledysk_inspiracje padła: {_blad(e)}"}


async def handler_teledysk_inspiracje(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="teledysk_inspiracje — tryb tła")
    ap.add_argument("--tlo", required=True, type=Path)
    ap.add_argument("--url", required=True)
    ap.add_argument("--wezel", required=True)
    ap.add_argument("--tytul", required=True)
    ap.add_argument("--tekst", required=True, type=Path)
    a = ap.parse_args(argv)
    return asyncio.run(_tlo(a.tlo, a.url, a.wezel, a.tytul, a.tekst))


if __name__ == "__main__":
    sys.exit(_main())
