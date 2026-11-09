# plik: teledysk_kadry_qwen_hand.py
"""Ręka teledysk_kadry_qwen: KADRY Z KONTROLĄ teledysku.

Brief: BRIEFY/<NN_slug>/ (utwór po tytule z utwory.tsv; stare 30_*/31_* nietknięte) z plikami ręki
teledysk_rezyseria_2: koncepcja.json (prompty, wzorce, osoby, bohater, styl_en, postac_en), kadry_opis.json,
sceny_szkic.json (kolejność scen).
Bez „wykonaj” = PODGLĄD (plan, stan kontroli, unit; bez sieci i bez GPU).
Z „wykonaj”: kolejka ComfyUI .10 pusta i VRAM ≥ 9000 MiB (inaczej „ComfyUI .10 zajęte”); węzeł kontroli =
TRÓJKĄT (NODES z qwen2.5vl:7b w /api/tags → relocate: decision, potem scored po rosnącym koszcie, bez
wykluczonych); potem systemd-run --user --unit=teledysk-kadry-<slug> (tryb --tlo, log kadry.log).
Tło: każdy brakujący kadr = kadr() z qwen_edit/qwen_kadr.py (seed 31000 + nr·7, potem +100, +200), zapis
kadry_qwen_v1/<id>.png (próby <id>_s100.png, <id>_s200.png), KONTROLA qwen2.5vl przed powiększeniem;
po 3 nieudanych próbach zostaje najlepsza próba i raport „do przeglądu”; wynik w kontrola_kadrow.json;
na koniec record_outcome(<węzeł>, ok|fail). Ręka niczego nie publikuje.
Granice I/O: _pobierz (HTTP), _uruchom (procesy), _wezly (NODES), _record_outcome, _kadr (ComfyUI), _zmniejsz.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import inspect
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import httpx

WZORZEC = r"\b(?:zr[óo]b\s+)?kadry\s+teledysku\b"
GOTOWA = True

ROOT = Path(os.environ.get("TELEDYSK_ROOT", "/opt/shadow/teledysk_java"))
BRIEFY = ROOT / "briefy_1017"
UTWORY_TSV = BRIEFY / "utwory.tsv"
QWEN_KADR = ROOT / "qwen_edit" / "qwen_kadr.py"
STARE = ("30_", "31_")
COMFY = "http://127.0.0.1:8188"
MODEL_VL = "qwen2.5vl:7b"
PORT_OLLAMA = 11434
RELOCATE = "http://127.0.0.1:8001/api/network/relocate"
WZ_TWARZ = ("wz30_bez_okularow.png", "wz30_bok.png")  # wz30_sylwetka.png NIE: przenosi czapkę, okulary i bluzę
SEED0, SEED_KROK, PROBY = 31000, 7, (0, 100, 200)
MIN_VRAM, BOK_VL, SZER, WYS = 9000, 896, 1344, 768
TIMEOUT_WEZLA, TIMEOUT_VL = 10.0, 300.0
KAT_KADRY, K_KONC, K_KADRY, K_SZKIC = "kadry_qwen_v1", "koncepcja.json", "kadry_opis.json", "sceny_szkic.json"
K_KONTROLA, LOG = "kontrola_kadrow.json", "kadry.log"
DZIALA = ("active", "activating", "reloading", "deactivating")
PYTANIA = ('Inspect this film frame. Answer ONLY with a JSON object: {"osoby": <how many people are visible, '
           'integer>, "dlonie": <how many hands/arms are visible, integer>, "tekst": <true if any readable text or '
           'letters are visible>, "drzwi": <true if an open door or a passage is visible in the background>, '
           '"ruch": <true if the people are in motion, doing a body action>, "opis": "<short description>"}. '
           'Count carefully.')
_T_WYKONAJ = re.compile(r"\b(?:wykonaj|wy[śs]lij|zatwierd[źz])\b", re.I)
_AKCJA = re.compile(r"\b(?:walk\w*|run(?:s|ning)?|ran|carr(?:y|ies|ied|ying)|lift\w*|push\w*|pull\w*|climb\w*|"
                    r"fall(?:s|ing|en)?|fell|jump\w*|danc\w*|turn\w*|reach\w*|grab\w*|hold(?:s|ing)?|throw\w*|"
                    r"kneel\w*|crawl\w*|drag\w*|rais(?:e|es|ed|ing)|step\w*|stumbl\w*|sprint\w*|spin\w*|hug\w*|"
                    r"embrac\w*|lean\w*|stretch\w*|swing\w*|shout\w*|scream\w*|wav(?:e|es|ing)|fight\w*|chas\w*|"
                    r"bend\w*|pick\w*)\b", re.I)


class BladWezla(RuntimeError):
    pass


class BladModelu(RuntimeError):
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


def _int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return int(v.strip()) if isinstance(v, str) and v.strip().isdigit() else None


def _bool(v):
    if isinstance(v, bool):
        return v
    n = _norm(v)
    return True if n in ("tak", "yes", "true") else (False if n in ("nie", "no", "false") else None)


def _pole(o, *nazwy):
    for n in nazwy:
        v = o.get(n) if isinstance(o, dict) else getattr(o, n, None)
        if v is not None:
            return v
    return None


def _odm(n, jeden, kilka, wiele):
    return jeden if n == 1 else (kilka if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else wiele)


def _unit(slug: str) -> str:
    return f"teledysk-kadry-{slug}"


# ---------------------------------------------------------------- utwór i plan briefu

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


def _plan(kat: Path) -> dict:
    plan = {"bledy": [], "sceny": [], "styl": "", "postac": ""}
    b, d = plan["bledy"], {}
    for nazwa in (K_KONC, K_KADRY, K_SZKIC):
        p = kat / nazwa
        try:
            d[nazwa] = json.loads(p.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            b.append(f"brak {p}")
        except (OSError, ValueError) as e:
            b.append(f"{nazwa} nieczytelny ({_blad(e)})")
    if b:
        return plan
    konc, opis, szkic = (x if isinstance(x, dict) else {} for x in (d[K_KONC], d[K_KADRY], d[K_SZKIC]))
    pola = {k: konc[k] if isinstance(konc.get(k), dict) else None for k in ("prompty", "wzorce", "osoby", "bohater")}
    b += [f"{K_KONC} bez pola {k}" for k, v in pola.items() if v is None]
    plan["styl"] = next((konc[k].strip() for k in ("styl_en", "styl") if _txt(konc.get(k))), "")
    plan["postac"] = next((konc[k].strip() for k in ("postac_en", "postac", "bohater_opis") if _txt(konc.get(k))), "")
    if not plan["styl"]:
        b.append(f"{K_KONC} bez styl_en (dopisek stylu do promptu)")
    ids = [str(x[1]) for x in (szkic.get("sceny") or []) if isinstance(x, (list, tuple)) and len(x) >= 2]
    if not ids:
        b.append(f"{K_SZKIC} bez scen")
    if b:
        return plan
    for i, sid in enumerate(ids):
        op = opis.get(sid) if isinstance(opis.get(sid), dict) else {}
        pr, o, boh = pola["prompty"].get(sid) or op.get("obraz"), pola["osoby"].get(sid), pola["bohater"].get(sid)
        o = "tlum" if _norm(o) == "tlum" else _int(o)
        if not _txt(pr):
            b.append(f"{sid}: brak promptu")
            continue
        if "{POSTAC}" in pr and not plan["postac"]:
            b.append(f"{sid}: prompt ma {{POSTAC}}, a {K_KONC} nie ma opisu bohatera (postac_en)")
        if o is None or type(boh) is not bool:
            b.append(f"{sid}: osoby ma być liczbą albo tlum, bohater true|false")
        m = re.search(r"\d+", sid)
        plan["sceny"].append({"id": sid, "nr": int(m.group()) if m else i + 1, "prompt": pr.strip(), "osoby": o,
                              "bohater": boh is True,
                              "twarz": boh is True and _norm(pola["wzorce"].get(sid)) == "twarz"})
    return plan


def _kontrole(kat: Path) -> dict:
    try:
        d = json.loads((kat / K_KONTROLA).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    if not isinstance(d.get("kadry"), dict):
        d["kadry"] = {}
    return d


def _brakujace(kat: Path, plan: dict, kon: dict) -> list:
    def jest(s):
        v = kon["kadry"].get(s["id"])
        return isinstance(v, dict) and v.get("status") in ("ok", "do_przegladu") \
            and (kat / KAT_KADRY / f"{s['id']}.png").is_file()
    return [s for s in plan["sceny"] if not jest(s)]


# ---------------------------------------------------------------- prompt kadru i ocena kontroli (czyste)

def _prompt_kadru(s: dict, plan: dict) -> str:
    txt, o = s["prompt"].replace("{POSTAC}", plan["postac"]).strip().rstrip("."), s["osoby"]
    if o == "tlum":
        dop = "A crowd of people, all of them in motion."
    elif o == 1:
        dop = "Exactly one person in the image."
    elif o:
        dop = f"Exactly {o} people in the image."
    else:
        dop = "No people in the image."
    return f"Create a completely new scene: {txt}. {dop} {plan['styl']}"


def _wzorce(s: dict) -> list:
    return list(WZ_TWARZ) if s["twarz"] else []


def _ocen(d, s: dict) -> tuple:
    if not isinstance(d, dict):
        return {}, ["odpowiedź kontroli nieczytelna"]
    os_, dl = _int(d.get("osoby")), _int(d.get("dlonie"))
    w = {"osoby": os_, "dlonie": dl, "tekst": _bool(d.get("tekst")), "drzwi": _bool(d.get("drzwi")),
         "ruch": _bool(d.get("ruch")), "opis": _k(d.get("opis") or "", 200)}
    b, plan = [], s["osoby"]
    if os_ is None:
        b.append("kontrola nie podała liczby osób")
    elif os_ == 0:
        b.append("0 osób — nie obrazy, tylko ludzie")
    elif plan == "tlum" and os_ < 3:
        b.append(f"tłum w planie, widać {os_} os.")
    elif plan != "tlum" and os_ != plan:
        b.append(f"osób {os_}, plan {plan}")
    if os_ and dl is not None and dl > 2 * os_:
        b.append(f"dłoni {dl} > 2·{os_}")
    if w["tekst"] is True:
        b.append("czytelny tekst/litery w kadrze")
    if _AKCJA.search(s["prompt"]) and w["ruch"] is False:
        b.append("postać nieruchoma, a prompt ma akcję ciała")
    return w, b


# ---------------------------------------------------------------- granice I/O

def _env() -> dict:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    return env


async def _pobierz(url: str, payload=None, timeout: float = TIMEOUT_WEZLA):
    """HTTP: GET bez payload, POST JSON z payload (Ollama, relocate trójkąta, kolejka ComfyUI)."""
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


async def _wezly() -> dict:
    from app.services import node_hardware_truth as nht  # backend Shadowa
    out = {}
    raw = nht.NODES
    for k, v in (raw.items() if isinstance(raw, dict) else ((None, x) for x in (raw or []))):
        if _pole(v, "inference") is not True:
            continue
        klucz = str(k if k is not None else (_pole(v, "key", "name", "id") or "")).strip()
        ip = re.sub(r"^https?://", "", str(_pole(v, "ip", "host", "addr", "address") or "")).split("/")[0].split(":")[0]
        if klucz and ip:
            out[klucz] = ip
    return out


async def _record_outcome(klucz: str, wynik: str, opis: str) -> None:
    from app.services import node_hardware_truth as nht
    r = nht.record_outcome(klucz, wynik, opis)
    if inspect.isawaitable(r):
        await r


_MOD_KADR = None
_LOADERY = {"UnetLoaderGGUF": "unet_name", "CLIPLoader": "clip_name", "VAELoader": "vae_name",
            "LoraLoaderModelOnly": "lora_name"}


def _mod_kadr():
    """Moduł qwen_edit/qwen_kadr.py (ładowany raz)."""
    global _MOD_KADR
    if _MOD_KADR is None:
        spec = importlib.util.spec_from_file_location("qwen_kadr", QWEN_KADR)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"nie załaduję {QWEN_KADR}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD_KADR = mod
    return _MOD_KADR


async def _pliki_modeli() -> list:
    """Nazwy plików modeli z wf("") qwen_kadr (UnetLoaderGGUF, CLIPLoader, VAELoader, LoraLoaderModelOnly)."""
    wf = _mod_kadr().wf
    try:
        d = wf("")
    except TypeError:
        d = wf("", [], SEED0, "teledysk_kadry_qwen", w=SZER, h=WYS)
    if inspect.isawaitable(d):
        d = await d
    d = d if isinstance(d, dict) else {}
    if isinstance(d.get("prompt"), dict):
        d = d["prompt"]
    out = []
    for n in d.values():
        if not isinstance(n, dict):
            continue
        pole = _LOADERY.get(n.get("class_type"))
        v = (n.get("inputs") or {}).get(pole) if pole else None
        if _txt(v):
            out.append(v.strip())
    return list(dict.fromkeys(out))


async def _kadr(prompt: str, wzorce: list, seed: int, prefix: str, comfy: str = COMFY) -> bytes:
    """kadr(prompt, wzorce, seed, prefix, w=1344, h=768) z qwen_kadr.py → PNG z ComfyUI `comfy` (qwen_kadr.COMFY)."""
    mod = _mod_kadr()
    mod.COMFY = comfy
    fn = mod.kadr
    r = await fn(prompt, wzorce, seed, prefix, w=SZER, h=WYS) if inspect.iscoroutinefunction(fn) \
        else await asyncio.to_thread(fn, prompt, wzorce, seed, prefix, w=SZER, h=WYS)
    if not isinstance(r, (bytes, bytearray)) or bytes(r[:8]) != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"kadr() nie zwrócił PNG (seed {seed})")
    return bytes(r)


def _zmniejsz(png: bytes) -> bytes:
    """Obraz do kontroli: dłuższy bok ≤ 896 px (PIL, a bez PIL ffmpeg)."""
    try:
        from PIL import Image
    except ImportError:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vf",
                            f"scale=w={BOK_VL}:h={BOK_VL}:force_original_aspect_ratio=decrease",
                            "-f", "image2pipe", "-vcodec", "png", "pipe:1"], input=png, capture_output=True, timeout=60)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(f"ffmpeg nie zmniejszył kadru: {_k(r.stderr.decode('utf-8', 'replace'), 200)}")
        return r.stdout
    im = Image.open(io.BytesIO(png))
    im.thumbnail((BOK_VL, BOK_VL))
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG")
    return out.getvalue()


async def _kontrola(url: str, png: bytes):
    maly = await asyncio.to_thread(_zmniejsz, png)
    d = await _pobierz(url + "/api/chat", {"model": MODEL_VL, "stream": False, "format": "json",
                                           "messages": [{"role": "user", "content": PYTANIA,
                                                         "images": [base64.b64encode(maly).decode("ascii")]}],
                                           "options": {"temperature": 0}, "keep_alive": "10m"}, TIMEOUT_VL)
    tresc = str(((d if isinstance(d, dict) else {}).get("message") or {}).get("content") or "")
    try:
        return json.loads(tresc)
    except ValueError:
        return None


async def _comfy_wezel():
    """(powód odmowy albo None, klucz węzła, url). Węzeł wybiera router (trójkąt), potem jego /queue pusta."""
    try:
        pliki = await _pliki_modeli()
    except Exception as e:  # noqa: BLE001
        return f"ComfyUI: nie czytam modeli z workflow {QWEN_KADR} ({_blad(e)})", None, None
    if not pliki:
        return f"ComfyUI: workflow {QWEN_KADR} nie podaje plików modeli", None, None
    try:
        from app.services import comfyui_router
        w = await comfyui_router.wybierz_wezel_modeli(pliki, MIN_VRAM / 1024, "teledysk_kadry_qwen")
    except Exception as e:  # noqa: BLE001
        return f"ComfyUI: router węzłów nie odpowiedział ({_blad(e)})", None, None
    w = w if isinstance(w, dict) else {}
    key = str(w.get("key") or w.get("node_id") or "?")
    url = str(w.get("url") or "").rstrip("/")
    if not w.get("ok") or not url:
        return f"ComfyUI: router nie dał węzła ({_k(w.get('powod') or 'bez powodu', 200)})", None, None
    try:
        q = await _pobierz(url + "/queue", None, TIMEOUT_WEZLA)
    except Exception as e:  # noqa: BLE001
        return f"ComfyUI {key} nie odpowiada na /queue ({_blad(e)})", key, url
    if not isinstance(q, dict):
        return f"ComfyUI {key}: nieczytelna odpowiedź /queue", key, url
    n = sum(len(q.get(k) or []) for k in ("queue_running", "queue_pending"))
    if n:
        return f"ComfyUI {key} zajęte: kolejka nie jest pusta ({n} w running/pending)", key, url
    return None, key, url


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
    return linie[-1][:400] if linie else ""


async def _stan_unitu(unit: str) -> str:
    try:
        r = await _uruchom(["systemctl", "--user", "is-active", unit], 30)
    except Exception:  # noqa: BLE001
        return "nieznany"
    s = r["stdout"].split()
    return s[-1] if s else "inactive"


# ---------------------------------------------------------------- TRÓJKĄT

def _url(ip: str) -> str:
    return f"http://{ip}:{PORT_OLLAMA}"


async def _tags(ip: str) -> float:
    d = await _pobierz(_url(ip) + "/api/tags", None, TIMEOUT_WEZLA)
    for m in (d.get("models") or []) if isinstance(d, dict) else []:
        if isinstance(m, dict) and MODEL_VL in (m.get("name"), m.get("model")):
            return _f(m.get("size")) or 0.0
    raise RuntimeError(f"brak modelu {MODEL_VL}")


def _dopasuj(k, wezly: dict):
    if k is None or _norm(k) in ("", "none", "break"):
        return None
    s = str(k).strip()
    return s if s in wezly else next((x for x, ip in wezly.items() if ip == s or ip.endswith("." + s)), s)


def _scored(sc) -> list:
    """Zapas po rosnącym koszcie; bez liczbowego skończonego kosztu albo z transport_cost None = wykluczony."""
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
            if v.get("excluded") or ("transport_cost" in v and v["transport_cost"] is None):
                continue
            v = v.get("cost") if "cost" in v else v.get("koszt")
        c = _f(v)
        if k is not None and c is not None and math.isfinite(c):
            pary.append((str(k), c))
    return sorted(pary, key=lambda x: x[1])


async def _wezel():
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
        raise BladWezla(f"żaden węzeł nie ma {MODEL_VL} — " + "; ".join(powody))
    if max(kand.values()) <= 0:
        raise BladWezla(f"/api/tags nie podaje rozmiaru {MODEL_VL} — need_gb nie zgaduję")
    need = math.ceil(max(kand.values()) / 1024 ** 3 * 10) / 10
    try:
        d = await _pobierz(f"{RELOCATE}?{urlencode({'need_gb': f'{need:g}', 'model': MODEL_VL})}", None, TIMEOUT_WEZLA)
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
    raise BladWezla(f"trójkąt nie wskazał węzła z {MODEL_VL} ({odrz}): {pow_t}"
                    + (f"; bez modelu: {'; '.join(powody)}" if powody else ""))


# ---------------------------------------------------------------- tryb tła

def _argv_tla(kat: Path, url: str, wezel: str, tytul: str, comfy: str = COMFY) -> list:
    log = kat / LOG
    pp = os.pathsep.join(dict.fromkeys([os.getcwd()] + [p for p in sys.path if p]))
    return ["systemd-run", "--user", f"--unit={_unit(kat.name)}", "--collect", "--quiet",
            f"--description=Shadow: kadry teledysku {kat.name}", f"--property=WorkingDirectory={kat}",
            f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
            "--setenv=PYTHONUNBUFFERED=1", f"--setenv=PYTHONPATH={pp}", "--", sys.executable,
            str(Path(__file__).resolve()), "--tlo", str(kat), "--url", url, "--wezel", wezel, "--tytul", tytul,
            "--comfy", comfy]


def _wybierz(kd: Path, sid: str, proby: list, naj: dict) -> None:
    if naj is proby[0]:
        return
    glowny = kd / f"{sid}.png"
    if glowny.is_file():
        os.replace(glowny, kd / f"{sid}_s0.png")
        proby[0]["plik"] = f"{KAT_KADRY}/{sid}_s0.png"
    _atomowo(glowny, (kd.parent / naj["plik"]).read_bytes())


async def _odnotuj(kat: Path, wezel: str, wynik: str) -> None:
    try:
        await _record_outcome(wezel, wynik, f"teledysk_kadry_qwen {kat.name}")
    except Exception as e:  # noqa: BLE001
        _log(kat, f"record_outcome({wezel}, {wynik}) nie przeszło: {_blad(e)}")
    else:
        _log(kat, f"trójkąt: record_outcome({wezel}, {wynik})")


async def _tlo(kat: Path, url: str, wezel: str, tytul: str, comfy: str = COMFY) -> int:
    kat = Path(kat)
    plan = _plan(kat)
    if plan["bledy"]:
        _log(kat, "plan niekompletny: " + "; ".join(plan["bledy"][:8]) + " — kadrów nie liczę")
        return 1
    kon = _kontrole(kat)
    kon.update(utwor=tytul, model=MODEL_VL, wezel=wezel)
    brak, kd = _brakujace(kat, plan, kon), kat / KAT_KADRY
    kd.mkdir(exist_ok=True)
    _log(kat, f"start: {len(brak)} z {len(plan['sceny'])} kadrów do zrobienia; kontrola {MODEL_VL} na węźle "
              f"{wezel} ({url})")
    wolano, wynik = False, "ok"
    try:
        for s in brak:
            proby = []
            for off in PROBY:
                seed = SEED0 + s["nr"] * SEED_KROK + off
                png = await _kadr(_prompt_kadru(s, plan), _wzorce(s), seed, f"{kat.name}_{s['id']}_s{off}", comfy)
                nazwa = f"{s['id']}.png" if off == 0 else f"{s['id']}_s{off}.png"
                _atomowo(kd / nazwa, png)
                wolano = True
                try:
                    odp = await _kontrola(url, png)
                except Exception as e:  # noqa: BLE001
                    raise BladModelu(_blad(e)) from None
                w, bl = _ocen(odp, s)
                proby.append({"seed": seed, "plik": f"{KAT_KADRY}/{nazwa}", "kontrola": w, "bledy": bl})
                _log(kat, f"{s['id']} seed {seed}: " + ("OK" if not bl else "odrzucony — " + "; ".join(bl)))
                if not bl:
                    break
            naj = min(proby, key=lambda p: len(p["bledy"]))
            _wybierz(kd, s["id"], proby, naj)
            kon["kadry"][s["id"]] = {"status": "do_przegladu" if naj["bledy"] else "ok", "seed": naj["seed"],
                                     "plik": f"{KAT_KADRY}/{s['id']}.png", "proby": proby}
            kon["do_przegladu"] = sorted(k for k, v in kon["kadry"].items()
                                         if isinstance(v, dict) and v.get("status") == "do_przegladu")
            _atomowo(kat / K_KONTROLA, (json.dumps(kon, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    except BladModelu as e:
        wynik = "fail"
        _log(kat, f"BŁĄD: {MODEL_VL} na węźle {wezel} nie odpowiedział ({e}) — kadr bez kontroli nie trafia do "
                  f"{K_KONTROLA}; wznowię od brakujących")
        return 1
    except Exception as e:  # noqa: BLE001
        _log(kat, f"BŁĄD kadru (ComfyUI {comfy} / qwen_kadr): {_blad(e)} — wznowię od brakujących")
        return 1
    finally:
        if wolano:
            await _odnotuj(kat, wezel, wynik)
    dp = kon.get("do_przegladu") or []
    _log(kat, f"GOTOWE: {len(plan['sceny'])} kadrów w {kd}; do przeglądu: {', '.join(dp) or 'brak'}")
    return 0


# ---------------------------------------------------------------- odpowiedzi

def _podglad(slug: str, tytul: str, kat: Path, plan: dict, kon: dict, brak: list, su: str) -> str:
    sc = plan["sceny"]
    dp = sorted(k for k, v in kon["kadry"].items() if isinstance(v, dict) and v.get("status") == "do_przegladu")
    li = [f"PODGLĄD (bez „wykonaj” niczego nie liczę) — kadry teledysku „{tytul}” (brief {slug})."]
    if plan["bledy"]:
        li.append("Plan niekompletny: " + "; ".join(plan["bledy"][:8]) + ".")
    else:
        li.append(f"Plan: {len(sc)} {_odm(len(sc), 'scena', 'sceny', 'scen')}, wzorce twarzy operatora w "
                  f"{sum(s['twarz'] for s in sc)} (tylko bohater=true); seed 31000+nr·7, do 3 prób.")
        li.append(f"Kontrola ({K_KONTROLA}): gotowe {len(sc) - len(brak)}/{len(sc)}, brakuje {len(brak)}"
                  + (f"; do przeglądu: {', '.join(dp)}" if dp else "") + ".")
    ost = _ostatni_log(kat)
    li.append(f"Unit {_unit(slug)}: {su}." + (f" Ostatnio w {LOG}: {ost}" if ost else ""))
    if su in DZIALA:
        li.append("Z „wykonaj” teraz odmówię — kadry już liczą się w tle.")
    elif plan["bledy"]:
        li.append("Z „wykonaj” odmówię, dopóki plan jest niekompletny (ręka teledysk_rezyseria_2).")
    elif not brak:
        li.append("Wszystkie kadry są — nic do liczenia.")
    else:
        li.append(f"Z „wykonaj”: węzeł ComfyUI (≥ {MIN_VRAM} MiB VRAM) wybierze router i sprawdzę jego kolejkę, "
                  f"węzeł kontroli {MODEL_VL} wybierze trójkąt, kadry policzę w tle (systemd-run --user, "
                  f"log {kat / LOG}).")
    return "\n".join(li)


async def wykonaj(params: dict, cmd: str) -> dict:
    if not isinstance(cmd, str) or not re.search(WZORZEC, cmd, re.I):
        return {"ok": False, "reason": "To nie jest polecenie kadrów teledysku (zrób kadry teledysku … wykonaj)."}
    try:
        slug, tytul = _cel(cmd, params or {})
    except LookupError as e:
        return {"ok": False, "reason": f"{e}. Niczego nie zgaduję."}
    kat = BRIEFY / slug
    if not kat.is_dir():
        return {"ok": False, "reason": f"Nie ma katalogu briefu „{tytul}” ({kat}). Niczego nie zgaduję."}
    plan, kon = _plan(kat), _kontrole(kat)
    brak = [] if plan["bledy"] else _brakujace(kat, plan, kon)
    unit = _unit(slug)
    su = await _stan_unitu(unit)
    if not _T_WYKONAJ.search(cmd):
        return {"ok": True, "reply": _podglad(slug, tytul, kat, plan, kon, brak, su)}
    if su in DZIALA:
        return {"ok": False, "reason": f"Kadry „{tytul}” już liczą się w tle ({unit}: {su}, log {kat / LOG})."}
    if plan["bledy"]:
        return {"ok": False, "reason": f"Brief {slug} niekompletny: " + "; ".join(plan["bledy"][:8])
                + " — najpierw ręka teledysk_rezyseria_2. Niczego nie uruchomiłem."}
    n = len(plan["sceny"])
    if not brak:
        dp = kon.get("do_przegladu") or []
        return {"ok": True, "reply": f"Wszystkie {n} kadrów „{tytul}” już są i przeszły kontrolę ({kat / K_KONTROLA})"
                                     + (f"; do przeglądu: {', '.join(dp)}." if dp else ".")}
    zaj, ckey, curl = await _comfy_wezel()
    if zaj:
        return {"ok": False, "reason": f"{zaj}. Niczego nie uruchomiłem."}
    try:
        wezel, url, powod = await _wezel()
    except BladWezla as e:
        return {"ok": False, "reason": f"Model kontroli {MODEL_VL} nie ma gdzie liczyć — {e}. Niczego nie uruchomiłem."}
    if su == "failed":
        await _uruchom(["systemctl", "--user", "reset-failed", unit], 30)
    try:
        r = await _uruchom(_argv_tla(kat, url, wezel, tytul, curl), 30)
    except Exception as e:  # noqa: BLE001
        r = {"kod": 127, "stdout": "", "stderr": _blad(e)}
    if r["kod"] != 0:
        return {"ok": False, "reason": f"systemd-run nie uruchomił {unit} (kod {r['kod']}: "
                                       f"{_k(r['stderr'] or r['stdout'] or 'bez komunikatu', 200)})."}
    return {"ok": True, "reply": f"KADRY LICZĄ SIĘ W TLE — „{tytul}” (brief {slug}): {len(brak)} z {n} do zrobienia"
                                 + (" (wznawiam od brakujących)" if len(brak) < n else "") + ".\n"
                                 f"Kontrola {MODEL_VL} na węźle {wezel} ({url}) — {powod}.\nComfyUI {ckey} ({curl}): "
                                 f"kolejka pusta; seed 31000+nr·7 (+100, +200), max 3 próby, zapis "
                                 f"{kat / KAT_KADRY}, wynik kontroli w {K_KONTROLA}.\nUnit {unit} (systemd-run --user, "
                                 f"restart backendu go nie zabija), log {kat / LOG}. Niczego nie publikuję. "
                                 f"Stan: „kadry teledysku {tytul}”."}


async def start(params: dict | None = None, cmd: str = "") -> dict:
    try:
        return await wykonaj(params or {}, cmd or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"ręka teledysk_kadry_qwen padła: {_blad(e)}"}


async def handler_teledysk_kadry_qwen(params: dict, cmd: str, section: str = "") -> dict:
    w = await start(params, cmd)
    return {"reply": w.get("reply") or w.get("reason"), "data": w}


async def _brief(kat: Path) -> int:
    """Tryb --brief: wołający (teledysk_ruchu) działa już we własnym unicie — _tlo na miejscu, bez systemd-run."""
    kat = Path(kat)
    zaj, _ckey, curl = await _comfy_wezel()
    if zaj:
        sys.stderr.write(f"{zaj}. Niczego nie uruchomiłem.\n")
        sys.stderr.flush()
        return 3
    try:
        wezel, url, _powod = await _wezel()
    except BladWezla as e:
        sys.stderr.write(f"Model kontroli {MODEL_VL} nie ma gdzie liczyć — {e}. Niczego nie uruchomiłem.\n")
        sys.stderr.flush()
        return 4
    try:
        _slug, tytul = _cel("", {"slug": kat.name})
    except LookupError as e:
        sys.stderr.write(f"{e}. Niczego nie zgaduję.\n")
        sys.stderr.flush()
        return 2
    return await _tlo(kat, url, wezel, tytul, curl)


def _main(argv=None) -> int:
    backend = str(Path(__file__).resolve().parents[2])  # unit bez PYTHONPATH: import app.services.* musi działać
    if backend not in sys.path:
        sys.path.insert(0, backend)
    ap = argparse.ArgumentParser(description="teledysk_kadry_qwen — tryb tła")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tlo", type=Path)
    g.add_argument("--brief", type=Path)
    ap.add_argument("--url")
    ap.add_argument("--wezel")
    ap.add_argument("--tytul")
    ap.add_argument("--comfy", default=COMFY)
    a = ap.parse_args(argv)
    if a.brief is not None:
        return asyncio.run(_brief(a.brief))
    if not (a.url and a.wezel and a.tytul):
        ap.error("--tlo wymaga --url, --wezel i --tytul")
    return asyncio.run(_tlo(a.tlo, a.url, a.wezel, a.tytul, a.comfy))


if __name__ == "__main__":
    sys.exit(_main())
