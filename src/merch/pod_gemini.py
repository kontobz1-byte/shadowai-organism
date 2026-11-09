"""pod_gemini — seria koszulek z ilustracją z Gemini i hasłem w kilku językach.

Operator 2026-10-01: „shadow ma to robić z promptu Avatar lub WA, wymyślać grafiki, korzystać z gemini”
oraz „inne wersje językowe publikuj”. Fabryka (pod_factory) rysowała tylko typografię i wektorowe
słońca, a wersje językowe powstawały jako osobne koncepty bez śladu, co z czym tworzy parę.

  1. KONCEPTY — Gemini w przeglądarce (scripts/gemini_web_ask.py) wymyśla N pomysłów w temacie:
     motyw ilustracji po angielsku + hasło, tytuł, tagi i opis w KAŻDYM żądanym języku (żart ma
     działać w danym języku, nie być tłumaczeniem). Gdy Gemini nie odpowie — mózg lokalny (JSON).
  2. ILUSTRACJA — Gemini (scripts/gemini_web_image.py) rysuje naszywkę BEZ TEKSTU na białym tle,
     1024×1024 (więcej przeglądarka nie oddaje); rembg zdejmuje tło, Real-ESRGAN anime powiększa 4×
     (scripts/pod_upscale.py w venv ComfyUI — LANCZOS rozmywał kontury).
  3. DRUK — pod_factory, układ „ilustracja”: jedna grafika → tyle wersji, ile języków. Listing niesie
     `grupa` (para językowa) i `grafika: gemini`. Partia + STYKOWKA + wpis needs_operator.
  4. Publikacja to OSOBNE polecenie („opublikuj na teepublic partia_…”) za bramką potwierdzeń.

Uruchamiane jako osobny proces (scripts/pod_seria_gemini.py), żeby restart backendu nie urwał serii.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.services import pod_factory as pf

logger = logging.getLogger("shadowai.pod_gemini")

BACK = Path(__file__).resolve().parents[2]
SKRYPTY = BACK.parent / "scripts"
GEMINI_ASK = SKRYPTY / "gemini_web_ask.py"
GEMINI_IMG = SKRYPTY / "gemini_web_image.py"
UPSCALE = BACK / "scripts" / "pod_upscale.py"
COMFY_PY = Path("/opt/shadow/ComfyUI/venv/bin/python")
PY = BACK / ".venv" / "bin" / "python"

JEZYKI = {"pl": "polski", "en": "angielski (USA)", "de": "niemiecki"}
_KOLORY = {  # kolory ilustracji zgrane z paletą hasła (pod_factory.PALETTES)
    "retro": "orange, mustard yellow, teal, cream and dark red",
    "forest": "olive green, lime, warm orange, cream and dark green",
    "ocean": "sky blue, sunny yellow, lime green, white and navy",
    "neon": "neon green, hot pink, cyan, white and deep purple",
    "mono": "white, light grey, mid grey with black outlines",
}
MAX_KONCEPTOW = 8
SILNIKI = ("gemini", "lokalnie", "chatgpt")


# ───────────────────────────── polecenie operatora → parametry ─────────────────────────────

def silnik_z_tekstu(t: str) -> str:
    t = t.lower()
    if re.search(r"lokaln|na\s+naszych\s+modelach|\boffline\b", t):
        return "lokalnie"
    if re.search(r"chatgpt|chat\s+gpt|\bgpt\b", t):
        return "chatgpt"
    return "gemini"

def jezyki_z_tekstu(t: str) -> list[str]:
    t = t.lower()
    out = [k for k, wz in (("pl", r"polsk|\bpl\b"), ("en", r"angiel|english|\ben\b"), ("de", r"niemieck|german|\bde\b"))
           if re.search(wz, t)]
    return out or ["pl", "en"]


_LICZBY = {"jedn": 1, "dwa": 2, "dwie": 2, "trzy": 3, "cztery": 4, "pięć": 5, "piec": 5, "sześć": 6, "szesc": 6,
           "siedem": 7, "osiem": 8}


def ile_z_tekstu(t: str, domyslnie: int = 4) -> int:
    m = re.search(r"\b(\d{1,2})\s*(koszul|projekt|wzor|wzór|grafik|pomysł|pomysl|t-?shirt|szt)", t.lower())
    if m:
        return max(1, min(MAX_KONCEPTOW, int(m.group(1))))
    for slowo, n in _LICZBY.items():
        if re.search(rf"\b{slowo}\w*\s+(koszul|projekt|wzor|wzór|grafik|pomysł|pomysl)", t.lower()):
            return n
    return domyslnie


def temat_z_tekstu(t: str) -> str:
    """„zaprojektuj 4 koszulki dla wędkarzy po polsku i angielsku” → „dla wędkarzy”."""
    s = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"\b(o|dla|na temat|temat:?|z motywem|w temacie|motyw:?)\s+(.+)", s, re.I)
    temat = m.group(0) if m else s
    temat = re.split(r"\s+(po\s+(polsku|angielsku|niemiecku)|w\s+(językach|jezykach|wersjach)|i\s+opublikuj|na\s+teepublic)\b",
                     temat, flags=re.I)[0]
    # „…, trzy projekty” / „… 4 koszulki” — liczba to parametr, nie temat
    temat = re.sub(r",?\s*(\d{1,2}|jedn\w*|dwa|dwie|trzy|cztery|pi[eę][cć]|sze[sś][cć]|siedem|osiem)\s+"
                   r"(projekt|koszul|wzor|wzór|grafik|pomys)\w*.*$", "", temat, flags=re.I)
    return temat.strip(" .,!?:;")[:120] or s[:120]


def pomysly(n: int) -> str:
    if n == 1:
        return "1 pomysł"
    return f"{n} pomysły" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else f"{n} pomysłów"


def _slug(t: str, n: int = 40) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t.replace("ł", "l").replace("Ł", "L")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:n] or "seria"


# ───────────────────────────── 1. koncepty ─────────────────────────────

def _prompt_konceptow(temat: str, jezyki: list[str], ile: int) -> str:
    wersje = ", ".join(f"„{k}” ({JEZYKI[k]})" for k in jezyki)
    przyklad = ", ".join(f'"{k}": {{"linie": "...", "tytul": "...", "tagi": ["..."], "opis": "..."}}' for k in jezyki)
    return (
        f"Jesteś projektantem koszulek print-on-demand. Temat serii: {temat}.\n"
        f"Wymyśl {ile} RÓŻNYCH, oryginalnych pomysłów na koszulkę: ilustracja + krótkie, zabawne hasło.\n"
        f"Każdy pomysł ma wersje językowe: {wersje}. Hasło w każdym języku ma brzmieć naturalnie dla native "
        "speakera — nie tłumacz dosłownie, zachowaj żart.\n"
        "Zasady:\n"
        "- bez marek, logo, nazw gier, filmów, postaci, piosenek i znanych cytatów; bez wulgaryzmów i polityki;\n"
        "- \"linie\": hasło w 2–4 liniach rozdzielonych |, każda linia najwyżej 16 znaków; * na początku linii = "
        "linia kluczowa (1–2 takie); ~ na początku = mała linia pisana odręcznie (najwyżej jedna);\n"
        "- \"motyw\": opis ilustracji PO ANGIELSKU dla generatora obrazów — konkretna scena, postać albo przedmiot "
        "pasujący do hasła; na ilustracji nie może być żadnego tekstu;\n"
        "- \"paleta\": jedna z retro|forest|ocean|neon|mono;\n"
        "- \"tytul\": tytuł oferty w danym języku, najwyżej 60 znaków, ze słowami, których ludzie szukają;\n"
        "- \"tagi\": 15 tagów wyszukiwania w danym języku, małymi literami;\n"
        "- \"opis\": jedno zdanie opisu produktu w danym języku.\n"
        "Hasło ma być krótkie i celne jak na najlepiej sprzedających się koszulkach; sprawdź pisownię "
        "i gramatykę każdej wersji, zanim odpowiesz.\n"
        "Odpowiedz WYŁĄCZNIE JSON-em, bez komentarza:\n"
        f'{{"koncepty": [{{"grupa": "krotki-slug-po-angielsku", "motyw": "...", "paleta": "retro", '
        f'"wersje": {{{przyklad}}}}}]}}'
    )


def _json_z_odpowiedzi(tekst: str) -> dict:
    i, j = tekst.find("{"), tekst.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("brak JSON w odpowiedzi")
    # Wycięcie obiektu pomija także ogrodzenie Markdown.
    # Zachowaj całe napisy, usuwaj końcowe przecinki tylko poza nimi.
    fragment = re.sub(
        r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])',
        lambda m: m.group(1) or "",
        tekst[i:j + 1],
    )
    return json.loads(fragment)


async def _uruchom(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        return -9, "", f"timeout {timeout} s"
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _koncepty_gemini(prompt: str, zapis: Path | None = None) -> dict:
    for proba in range(1, 4):  # pierwsza próba + najwyżej dwa ponowienia
        rc, out, err = await _uruchom([str(PY), str(GEMINI_ASK), prompt], timeout=480)
        if rc != 0:
            raise RuntimeError(f"Gemini: rc={rc} {(err or out)[-300:]}")
        try:
            return _json_z_odpowiedzi(out)
        except ValueError as e:
            logger.warning("Gemini: błędny JSON, próba %s/3: %s", proba, e)
            if zapis is not None:
                debug = zapis.with_name(f"{zapis.stem}_blad_{proba}.txt")
                try:
                    debug.parent.mkdir(parents=True, exist_ok=True)
                    debug.write_text(out, encoding="utf-8")
                except OSError as blad_zapisu:
                    logger.warning("Nie można zapisać %s: %s; surowa odpowiedź: %s",
                                   debug, blad_zapisu, out)
            else:
                logger.warning("Gemini: surowa odpowiedź, próba %s/3: %s", proba, out)
            if proba == 3:
                raise


async def _koncepty_lokalnie(prompt: str) -> dict:
    from app.services.ollama_router import ollama_router
    from app.services.viral_research import wybierz_model_syntezy
    model, limit = await wybierz_model_syntezy("pod_factory")
    reply, _, _ = await ollama_router.chat_messages(
        [{"role": "system", "content": "Reply with JSON only."}, {"role": "user", "content": prompt}],
        model=model, priority="background", timeout=min(limit, 240), format="json")
    return _json_z_odpowiedzi(reply or "")


_KLUCZE_JEZYKA = {"pl": ("pl", "pl-pl", "polski", "polish"), "en": ("en", "en-us", "en_us", "angielski", "english"),
                  "de": ("de", "de-de", "niemiecki", "german", "deutsch")}


def _wersja(wersje: dict, lang: str) -> dict:
    """Gemini raz pisze „en”, raz „en-US” albo „english” — klucz po znaczeniu, nie po literze."""
    norm = {str(k).strip().lower().replace("_", "-"): v for k, v in wersje.items()}
    for k in _KLUCZE_JEZYKA.get(lang, (lang,)):
        if isinstance(norm.get(k), dict):
            return norm[k]
    return {}


# 01.10: Gemini napisał „JUŽ” (czeskie Ž) w polskim haśle. Pomyłki diakrytyków mapujemy, inne obce
# litery odrzucają wersję — na koszulce literówka zostaje na zawsze.
_POMYLKI = {"pl": {"Ž": "Ż", "ž": "ż", "Č": "Ć", "č": "ć", "Š": "Ś", "š": "ś", "Ň": "Ń", "ň": "ń", "Ě": "Ę", "ě": "ę"}}
_LITERY = {"pl": "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "de": "äöüßÄÖÜẞ", "en": ""}


def _znaki(tekst: str, lang: str) -> tuple[str, str]:
    """(tekst z poprawionymi diakrytykami, obce litery, które zostały)."""
    tekst = "".join(_POMYLKI.get(lang, {}).get(ch, ch) for ch in tekst)
    obce = "".join(dict.fromkeys(ch for ch in tekst if ch.isalpha() and not ch.isascii()
                                 and ch not in _LITERY.get(lang, "")))
    return tekst, obce


def _linie(w: dict) -> str:
    """Hasło jako „A|*B|~c”: lista albo tekst z nowymi liniami też się liczą (Gemini miesza oba)."""
    v = next((w[k] for k in ("linie", "lines", "haslo", "hasło", "slogan") if w.get(k)), "")
    if isinstance(v, list):
        v = "|".join(str(x) for x in v)
    v = re.sub(r"\s*[\r\n]+\s*", "|", str(v))
    return "|".join(x.strip() for x in v.split("|") if x.strip())


_SLOWA_PISOWNI = re.compile(r"\b\w+(?:['’]\w+)*\b")
_PISOWNIA_LOGOWANA = False


def _bledy_pisowni(tekst: str, lang: str) -> list[str]:
    """Hunspell dla PL/EN; niedostępna kontrola pisowni nie blokuje fabryki."""
    global _PISOWNIA_LOGOWANA
    slownik = {"pl": "pl_PL", "en": "en_US"}.get(lang)
    if not slownik:
        return []
    slowa = [
        s for s in _SLOWA_PISOWNI.findall(tekst)
        if not any(ch.isdigit() for ch in s)
        and not (lang == "en" and ("'" in s or "’" in s))
    ]
    if not slowa:
        return []
    try:
        wynik = subprocess.run(
            ["hunspell", "-d", slownik, "-l"],
            input="\n".join(slowa) + "\n",
            capture_output=True, encoding="utf-8", check=False, timeout=5,
        )
        if wynik.returncode != 0:
            raise RuntimeError(wynik.stderr.strip() or f"kod {wynik.returncode}")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        if not _PISOWNIA_LOGOWANA:
            _PISOWNIA_LOGOWANA = True
            logger.warning("Kontrola pisowni niedostępna (non-fatal), %s: %s", slownik, exc)
        return []
    return list(dict.fromkeys(s.strip() for s in wynik.stdout.splitlines() if s.strip()))


def _waliduj(surowe: dict, temat: str, jezyki: list[str]) -> tuple[list[dict], list[str]]:
    """Koncepty → grupy z poprawnymi wersjami. Błędy pisowni hasła lub tytułu odrzucają wersję."""
    grupy, odrzuty, zajete = [], [], set()
    nazwy = {s.casefold() for s in _SLOWA_PISOWNI.findall(temat) if s[0].isupper()}

    def sprawdz(tekst: str, lang: str) -> list[str]:
        return [s for s in _bledy_pisowni(tekst, lang) if s.casefold() not in nazwy]
    for k in (surowe.get("koncepty") or surowe.get("concepts") or [])[:MAX_KONCEPTOW]:
        if not isinstance(k, dict) or not k.get("motyw") or not isinstance(k.get("wersje"), dict):
            continue
        grupa = _slug(str(k.get("grupa") or k["motyw"]), 32)
        while grupa in zajete:
            grupa += "-2"
        paleta = k.get("paleta") if k.get("paleta") in pf.PALETTES else "retro"
        wersje = {}
        for lang in jezyki:
            w = _wersja(k["wersje"], lang)
            linie, obce = _znaki(_linie(w), lang)
            tytul, _ = _znaki(str(w.get("tytul") or ""), lang)
            w = {**w, "tytul": tytul}
            tagi = [str(t).strip().lower() for t in (w.get("tagi") or w.get("tags") or []) if str(t).strip()]
            parsed = pf._parse_lines(linie) if linie else []
            bledy = list(dict.fromkeys(sprawdz(linie, lang) + sprawdz(tytul, lang)))
            bledy_opisu = sprawdz(str(w.get("opis") or ""), lang)
            if bledy_opisu:
                logger.warning("%s/%s: błąd pisowni w opisie: %s",
                               grupa, lang, ", ".join(bledy_opisu))
            powod = (f"obce litery w haśle: {obce}" if obce else
                     "brak hasła" if not parsed else
                     "hasło poza 2–4 liniami" if not (2 <= len(parsed) <= 4) else
                     "linia dłuższa niż 22 znaki" if any(len(x["text"]) > 22 for x in parsed) else
                     f"błąd pisowni: {', '.join(bledy)}" if bledy else
                     "za mało tagów" if len(tagi) < 5 or not w.get("tytul") else None)
            c = {"slug": f"{grupa}-{lang}", "niche": f"gemini:{_slug(temat, 24)}", "layout": "ilustracja",
                 "palette": paleta, "lang": lang, "lines": linie, "title": str(w.get("tytul") or "")[:100],
                 "tags": (tagi + [lang, "gift"])[:16], "blurb": str(w.get("opis") or "")[:300],
                 "grupa": grupa, "grafika": "gemini"}
            powod = powod or pf.tm_gate(c)
            if powod:
                odrzuty.append(f"{grupa}/{lang}: {powod}")
            else:
                wersje[lang] = c
        if wersje:
            zajete.add(grupa)
            grupy.append({"grupa": grupa, "motyw": str(k["motyw"])[:600], "paleta": paleta, "wersje": wersje})
    return grupy, odrzuty


async def wymysl(temat: str, jezyki: list[str], ile: int, log: Callable[[str], None] = print,
                 zapis: Path | None = None, silnik: str = "gemini") -> tuple[list[dict], list[str]]:
    prompt = _prompt_konceptow(temat, jezyki, ile)
    if silnik == "lokalnie":
        surowe = await _koncepty_lokalnie(prompt)
        log(f"koncepty: lokalny model dał {len(surowe.get('koncepty') or [])}")
    else:
        try:
            surowe = await _koncepty_gemini(prompt, zapis=zapis)
            log(f"koncepty: Gemini dał {len(surowe.get('koncepty') or [])}")
        except Exception as e:
            log(f"koncepty: Gemini nie odpowiedział ({type(e).__name__}: {str(e)[:160]}) — mózg lokalny")
            surowe = await _koncepty_lokalnie(prompt)
    if zapis:                                       # surowa odpowiedź zostaje przy partii — do sprawdzenia odrzutów
        zapis.parent.mkdir(parents=True, exist_ok=True)
        zapis.write_text(json.dumps(surowe, ensure_ascii=False, indent=1), encoding="utf-8")
    return _waliduj(surowe, temat, jezyki)


# ───────────────────────────── 2. ilustracja ─────────────────────────────

def _prompt_obrazu(motyw: str, paleta: str, wariant: int) -> str:
    return (
        "Generate an image in square 1:1 format: flat vector t-shirt illustration in a retro badge style. "
        f"Subject: {motyw}. Thick clean outlines, limited palette of 5–6 colors ({_KOLORY.get(paleta, _KOLORY['retro'])}) "
        "that look great on a black t-shirt, bold readable silhouette, one centered emblem or badge with a clear white "
        "margin on all four sides — the artwork must not touch or be cut by the image edges. "
        "Absolutely no text, no letters, no numbers, no logos, no watermark, no signature. "
        "Background: plain solid pure white, no shadow, no frame, no gradient."
        + (" Make a fresh variant, different from any previous image." if wariant else "")
    )


PROB_OBRAZU = 3


async def _obraz_flux(prompt: str, out: Path) -> bool:
    import secrets
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30) as klient:
            baza, checkpoint = None, None
            for adres in ("http://127.0.0.1:8188", "http://10.0.0.130:8188"):
                try:
                    r = await klient.get(f"{adres}/object_info/CheckpointLoaderSimple", timeout=5)
                    r.raise_for_status()
                    nazwy = r.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
                    checkpoint = next((n for n in nazwy if "flux" in n.lower()), None)
                    if checkpoint:
                        baza = adres
                        break
                except Exception as e:
                    logger.warning("Flux: węzeł %s niedostępny: %s", adres, e)
            if baza is None:
                return False

            workflow = {
                "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
                "5": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 1024, "height": 1024, "batch_size": 1}},
                "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
                "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
                "3": {"class_type": "KSampler", "inputs": {
                    "seed": secrets.randbits(63), "steps": 4, "cfg": 1.0,
                    "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                    "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                    "latent_image": ["5", 0]}},
                "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
                "9": {"class_type": "SaveImage", "inputs": {
                    "filename_prefix": "pod_flux", "images": ["8", 0]}},
            }
            r = await klient.post(f"{baza}/prompt", json={"prompt": workflow})
            r.raise_for_status()
            prompt_id = r.json()["prompt_id"]
            petla = asyncio.get_running_loop()
            deadline = petla.time() + 560
            while petla.time() < deadline:
                r = await klient.get(f"{baza}/history/{prompt_id}")
                r.raise_for_status()
                historia = r.json().get(prompt_id, {})
                status = historia.get("status", {})
                if status.get("status_str") == "error":
                    logger.warning("Flux: generacja nie powiodła się: %s", status)
                    return False
                for wynik in historia.get("outputs", {}).values():
                    for obraz in wynik.get("images", []):
                        r = await klient.get(f"{baza}/view", params={
                            "filename": obraz["filename"],
                            "subfolder": obraz.get("subfolder", ""),
                            "type": obraz.get("type", "output"),
                        })
                        r.raise_for_status()
                        if not r.content:
                            return False
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(r.content)
                        return True
                if status.get("completed"):
                    return False
                await asyncio.sleep(2)
            logger.warning("Flux: timeout generacji %s", prompt_id)
    except Exception as e:
        logger.warning("Flux: %s: %s", type(e).__name__, e)
    return False


def _tlo_po_kolorze(img):
    """Białe tło zdjęte od krawędzi kadru: piksele w kolorze tła połączone z ramką → przezroczyste,
    przejście miękkie, kolor krawędzi odkażony z bieli (bez jasnej obwódki na ciemnej koszulce).

    01.10: rembg (u2net) brał jasne elementy grafiki za tło — srebrna korona wyszła półprzezroczysta.
    Gemini rysuje na jednolitym tle, więc kolor + spójność z ramką są pewniejsze niż „istotność” sieci.
    Zwraca None, gdy tło nie jest jednolite (wtedy rembg)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    ramka = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
    bg = np.median(ramka, axis=0)
    if (np.linalg.norm(ramka - bg, axis=1) < 18).mean() < 0.9:
        return None
    dist = np.linalg.norm(arr - bg, axis=2)
    t0, t1 = 10.0, 48.0
    lab, _ = ndimage.label(dist < t1)
    brzeg = np.setdiff1d(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])), [0])
    tlo = np.isin(lab, brzeg)
    alfa = np.ones(dist.shape, np.float32)
    alfa[tlo] = np.clip((dist[tlo] - t0) / (t1 - t0), 0, 1)
    # drobinki w tle (szum, pojedyncze kropki) — nie są częścią grafiki
    wysp, n = ndimage.label(alfa > 0.5)
    if n > 1:
        pola = ndimage.sum(np.ones_like(alfa), wysp, index=np.arange(1, n + 1))
        alfa[np.isin(wysp, np.arange(1, n + 1)[pola < 30])] = 0
    a3 = alfa[..., None]
    rgb = np.where(a3 > 0.02, (arr - (1 - a3) * bg) / np.maximum(a3, 0.02), arr)
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), alfa * 255]).astype(np.uint8), "RGBA")


def _bez_tla(surowy: Path, cel: Path) -> dict:
    """Tło po kolorze (zapas: rembg) → przycięcie do treści. Kontrola: grafika nie może być pustym kółkiem
    ani dotykać krawędzi kadru."""
    from PIL import Image
    zrodlo = Image.open(surowy).convert("RGB")
    rgba = _tlo_po_kolorze(zrodlo)
    if rgba is None:
        from rembg import new_session, remove
        rgba = remove(zrodlo, session=new_session("u2net"))
    a = rgba.getchannel("A")
    bbox = a.point(lambda v: 255 if v > 16 else 0).getbbox()
    if not bbox:
        return {"ok": False, "powod": "rembg nie znalazł grafiki"}
    # 01.10: scena dociągnięta do brzegu kadru dawała na koszulce płasko uciętą krawędź grafiki
    if bbox[0] <= 3 or bbox[1] <= 3 or bbox[2] >= rgba.width - 3 or bbox[3] >= rgba.height - 3:
        return {"ok": False, "powod": "grafika dotyka krawędzi kadru (ucięta)"}
    rgba = rgba.crop(bbox)
    hist = rgba.getchannel("A").point(lambda v: 255 if v > 128 else 0).histogram()
    pokrycie = hist[255] / max(1, rgba.width * rgba.height)
    if min(rgba.size) < 300 or pokrycie < 0.2:
        return {"ok": False, "powod": f"za mała/pusta grafika {rgba.size}, pokrycie {pokrycie:.0%}"}
    rgba.save(cel)
    return {"ok": True, "rozmiar": rgba.size, "pokrycie": round(pokrycie, 2)}


async def ilustracja(motyw: str, paleta: str, katalog: Path, nazwa: str,
                     log: Callable[[str], None] = print, silnik: str = "gemini") -> Path | None:
    katalog.mkdir(parents=True, exist_ok=True)
    for wariant in range(PROB_OBRAZU):
        surowy = katalog / f"{nazwa}_gemini_{wariant}.png"
        prompt = _prompt_obrazu(motyw, paleta, wariant)
        if silnik == "lokalnie":
            rc = 0 if await _obraz_flux(prompt, surowy) else 1
            out, err = "", ""
        else:
            rc, out, err = await _uruchom([str(PY), str(GEMINI_IMG), prompt,
                                           "--out", str(surowy)], timeout=560)
        if rc != 0 or not surowy.exists():
            log(f"  {nazwa}: {'Gemini' if silnik == 'gemini' else silnik} bez obrazu (rc={rc}) {(err or out).strip()[-200:]}")
            continue
        bez = katalog / f"{nazwa}_bez_tla.png"
        kontrola = await asyncio.to_thread(_bez_tla, surowy, bez)
        if not kontrola["ok"]:
            log(f"  {nazwa}: odrzucam obraz — {kontrola['powod']}")
            continue
        duzy = katalog / f"{nazwa}_ilustracja.png"
        rc, out, err = await _uruchom([str(COMFY_PY), str(UPSCALE), str(bez), str(duzy)], timeout=600)
        if rc != 0 or not duzy.exists():
            log(f"  {nazwa}: upscaler padł (rc={rc}) — biorę wersję bez powiększenia")
            return bez
        log(f"  {nazwa}: ilustracja {kontrola['rozmiar']} → {out.strip()[-120:]}")
        return duzy
    return None


# ───────────────────────────── 3. seria ─────────────────────────────

async def seria(temat: str, jezyki: list[str] | None = None, ile: int = 4, nazwa: str | None = None,
                log: Callable[[str], None] = print, silnik: str = "gemini") -> dict:
    if silnik == "chatgpt":
        return {"ok": False, "nazwa": nazwa, "blad": "silnik chatgpt jeszcze niedostępny"}
    jezyki = [j for j in (jezyki or ["pl", "en"]) if j in JEZYKI] or ["pl", "en"]
    ile = max(1, min(MAX_KONCEPTOW, ile))
    nazwa = nazwa or f"partia_{silnik}_{_slug(temat, 20)}_{datetime.now():%Y%m%d_%H%M}"
    log(f"seria „{temat}”: {pomysly(ile)} × {'/'.join(jezyki)} → {nazwa}")
    grafiki = pf.EXPORT_ROOT / nazwa / "_grafiki"
    grupy, odrzuty = await wymysl(temat, jezyki, ile, log, zapis=grafiki / "_koncepty_gemini.json",
                                 silnik=silnik)
    if not grupy:
        return {"ok": False, "nazwa": nazwa, "blad": "brak poprawnych konceptów", "odrzuty": odrzuty}
    log(f"koncepty po kontroli: {len(grupy)} grup" + (f", odrzuty: {'; '.join(odrzuty)}" if odrzuty else ""))
    koncepty, bez_grafiki = [], []
    for g in grupy:
        il = await ilustracja(g["motyw"], g["paleta"], grafiki, g["grupa"], log, silnik=silnik)
        if not il:
            bez_grafiki.append(g["grupa"])
            continue
        for c in g["wersje"].values():
            koncepty.append({**c, "ilustracja": str(il), "grafika": silnik})
    if not koncepty:
        return {"ok": False, "nazwa": nazwa, "blad": "Gemini nie dał żadnej ilustracji", "odrzuty": odrzuty,
                "bez_grafiki": bez_grafiki}
    batch = await asyncio.to_thread(pf.build_batch, koncepty, nazwa)
    try:
        staged = await pf.stage_batch(batch)
    except Exception as e:                         # kolejka to ślad, nie warunek istnienia partii
        staged = {"ok": False, "blad": f"{type(e).__name__}: {e}"}
    hasla = {}
    for c in koncepty:
        hasla.setdefault(c["grupa"], {})[c["lang"]] = " ".join(x["text"] for x in pf._parse_lines(c["lines"]))
    return {"ok": batch.get("ok", False), "nazwa": nazwa, "dir": batch.get("dir"), "zbudowane": batch.get("built", 0),
            "stykowka": batch.get("contact_sheet"), "hasla": hasla, "odrzuty": odrzuty + [
                f"{r.get('slug')}: {r.get('error')}" for r in batch.get("rejected", [])],
            "bez_grafiki": bez_grafiki, "kolejka": staged}


async def przebuduj(nazwa: str, log: Callable[[str], None] = print) -> dict:
    """Partia od nowa z zapisanych surowych obrazów Gemini i konceptów — po poprawce tła, znaków albo
    układu, bez nowych generacji (te same ilustracje, te same hasła)."""
    kat = pf.EXPORT_ROOT / nazwa
    grafiki = kat / "_grafiki"
    surowe = json.loads((grafiki / "_koncepty_gemini.json").read_text(encoding="utf-8"))
    listingi = [json.loads(p.read_text(encoding="utf-8")) for p in kat.glob("*/listing.json")]
    jezyki = sorted({l.get("lang") for l in listingi if l.get("lang") in JEZYKI}) or ["pl", "en"]
    temat = next((l["niche"].split(":", 1)[1] for l in listingi if str(l.get("niche", "")).startswith("gemini:")), nazwa)
    grupy, odrzuty = _waliduj(surowe, temat, jezyki)
    koncepty = []
    for g in grupy:
        wersje_surowe = sorted(grafiki.glob(f"{g['grupa']}_gemini_*.png"))
        if not wersje_surowe:
            log(f"  {g['grupa']}: brak surowego obrazu — pomijam")
            continue
        bez = grafiki / f"{g['grupa']}_bez_tla.png"
        kontrola = await asyncio.to_thread(_bez_tla, wersje_surowe[-1], bez)
        if not kontrola["ok"]:
            log(f"  {g['grupa']}: {kontrola['powod']} — pomijam")
            continue
        duzy = grafiki / f"{g['grupa']}_ilustracja.png"
        rc, _, _ = await _uruchom([str(COMFY_PY), str(UPSCALE), str(bez), str(duzy)], timeout=600)
        il = duzy if rc == 0 and duzy.exists() else bez
        koncepty += [{**c, "ilustracja": str(il)} for c in g["wersje"].values()]
    batch = await asyncio.to_thread(pf.build_batch, koncepty, nazwa)
    log(f"przebudowana {nazwa}: {batch.get('built', 0)} plików" + (f", odrzuty: {'; '.join(odrzuty)}" if odrzuty else ""))
    return {"ok": batch.get("ok", False), "nazwa": nazwa, "zbudowane": batch.get("built", 0),
            "stykowka": batch.get("contact_sheet"), "odrzuty": odrzuty}


def raport(w: dict) -> str:
    """Wiadomość dla operatora (WA/Avatar) — co powstało i jak to wgrać."""
    if not w.get("ok"):
        return (f"Seria {w.get('nazwa')}: nie wyszło — {w.get('blad', 'brak projektów')}."
                + (f" Odrzuty: {'; '.join(w.get('odrzuty', [])[:4])}" if w.get("odrzuty") else ""))
    linie = [f"Seria gotowa: {w['nazwa']} — {w['zbudowane']} plików do druku (ilustracje z Gemini)."]
    for grupa, h in w["hasla"].items():
        linie.append(f"• {grupa}: " + " / ".join(f"{k.upper()}: {v}" for k, v in h.items()))
    if w.get("bez_grafiki"):
        linie.append(f"Bez ilustracji (pominięte): {', '.join(w['bez_grafiki'])}")
    if w.get("odrzuty"):
        linie.append(f"Odrzucone wersje: {'; '.join(w['odrzuty'][:4])}")
    linie.append(f"Żeby wgrać do sklepu: „opublikuj na teepublic {w['nazwa']}” (potwierdzisz „tak”).")
    return "\n".join(linie)
