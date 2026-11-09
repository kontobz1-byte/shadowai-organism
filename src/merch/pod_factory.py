"""pod_factory — FABRYKA print-on-demand: koncept → plik do druku → listingi per platforma.

Operator 2026-09-20: Merch on Demand zamknięty ostatecznie ([[project-shadowai-amazon-kdp-merch]])
→ „Zrealizuj" listę zamienników: Redbubble, TeePublic, Spreadshirt, Displate, Zazzle,
Printify+Etsy. Do tej pory organizm miał tylko 63 briefy koszulek w inbox/ i jeden goły
prompt ComfyUI w monetization_pipeline — ZERO plików nadających się do druku.

Trzy kroki, wszystkie w organizmie (wzór: kdp_factory):

1. KONCEPT — `SEED_CONCEPTS` (ręcznie pisane, sprawdzone bramką znaków towarowych) albo
   `research_concepts()` (mózg proponuje hasła w niszy; ta sama bramka). Na tych rynkach
   sprzedaje się TYPOGRAFIA + prosty wektor w niszy, nie „ładny obrazek z dyfuzji":
   dyfuzja nie daje przezroczystego tła ani czystych krawędzi i myli litery.
2. PRODUKCJA (`build_design`) — silnik PIL, zero GPU (nie wchodzi w drogę grającym):
   PNG 4500×5400 z PRZEZROCZYSTYM tłem w dwóch kolorystykach (na ciemne i na jasne
   ubrania) + plakat Displate 2900×4060 JPG + podgląd na próbkach tkaniny dla oka.
3. LISTINGI (`platform_listings`) — tytuł/tagi/opis przycięte do limitów KAŻDEJ platformy
   (Etsy: 13 tagów ≤20 znaków, tytuł ≤140; Spreadshirt: nazwa ≤50, ≤25 tagów; itd.).
   Staging: publication_queue channel=pod status **needs_operator** — JEDEN wpis na partię.

Wgranie na platformę = ręka `browser_human` (CDP na .10) po tym, jak operator założy
konta i się zaloguje (hasła, dane podatkowe i wypłat wpisuje operator — nie Shadow).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("shadowai.pod_factory")

_BACKEND = Path(__file__).resolve().parents[2]
EXPORT_ROOT = _BACKEND / "exports" / "pod_fabryka"
FONT_DIR = _BACKEND / "assets" / "fonts"

# Pole nadruku 15×18 cala. TeePublic odblokowuje WSZYSTKIE produkty (w tym wall art)
# dopiero od 5000×5500 px — stąd 6000×7200 zamiast klasycznych 4500×5400 @300 DPI.
# Silnik rysuje wektorowo, więc wyższa rozdzielczość nic nie kosztuje poza sekundami.
W, H = 6000, 7200
MARGIN = 347
DISPLATE_WH = (2900, 4060)  # minimum Displate, pion

FONTS = {
    "anton": "Anton-Regular.ttf",
    "bebas": "BebasNeue-Regular.ttf",
    "slab": "AlfaSlabOne-Regular.ttf",
    "script": "Pacifico-Regular.ttf",
    "sans": "Montserrat-ExtraBold.ttf",
    "sans_md": "Montserrat-SemiBold.ttf",
}

# Kolorystyki: (atrament główny, akcent 1, akcent 2, akcent 3). „dark" = na CIEMNE ubrania.
PALETTES = {
    "retro": {"dark": ("#F4E9D0", "#E8A33D", "#D9552B", "#2A9D8F"),
              "light": ("#1F2430", "#C77B12", "#B8401C", "#1B7268")},
    "forest": {"dark": ("#F1EBDD", "#A7C957", "#F2A541", "#6A994E"),
               "light": ("#1E2A20", "#4F772D", "#BC6C25", "#31572C")},
    "ocean": {"dark": ("#EAF4F4", "#5BC0EB", "#FDE74C", "#9BC53D"),
              "light": ("#0B2545", "#13629A", "#B8860B", "#3E7C17")},
    "neon": {"dark": ("#F8F8F2", "#50FA7B", "#FF79C6", "#8BE9FD"),
             "light": ("#16161D", "#0E8A3B", "#B5127A", "#0B7A99")},
    "mono": {"dark": ("#F5F5F5", "#F5F5F5", "#BDBDBD", "#8A8A8A"),
             "light": ("#141414", "#141414", "#4A4A4A", "#6E6E6E")},
}
DISPLATE_BG = {"retro": "#1F2430", "forest": "#16201A", "ocean": "#0B2545",
               "neon": "#16161D", "mono": "#141414"}

# Bramka znaków towarowych — TYLKO lista oczywistych marek/franczyz. To nie jest badanie
# USPTO/EUIPO; listing niesie pole tm_check="blocklist", żeby nikt nie wziął tego za więcej.
_TM_RE = re.compile(
    r"\b(disney|marvel|pixar|star\s*wars|jedi|pok[eé]mon|nintendo|mario|zelda|minecraft|fortnite|"
    r"roblox|gta|grand theft|rockstar|call of duty|harry potter|hogwarts|nike|adidas|supreme|gucci|"
    r"lego|barbie|netflix|nasa|nfl|nba|fifa|uefa|olympi\w*|coca|pepsi|starbucks|jeep|harley|tesla|"
    r"taylor swift|swiftie|beatles|batman|superman|spider.?man|avengers|simpsons|peppa|bluey|"
    r"hello kitty|dungeons (and|&) dragons|d&d|jack daniel|crossfit|peloton|yeti|stanley|"
    r"life is good|keep calm and|just do it|i'?m lovin)\b", re.I)

# Hasła: linie rozdzielone „|”; „*” na początku linii = akcent (kolor + mocniejszy krój);
# „~” = pismo odręczne (mała linia łącząca). Nisza decyduje o tagach, układ o rysunku.
SEED_CONCEPTS: list[dict] = [
    {"slug": "garden-therapy", "niche": "gardening", "layout": "sunset", "palette": "forest", "motif": "plants",
     "lines": "~cheaper than|*THERAPY|DIRTIER THAN|*MY KITCHEN", "title": "Gardening Is Cheaper Than Therapy Funny Gardener",
     "tags": ["gardening", "gardener gift", "plant lover", "funny garden", "vegetable garden", "plant mom", "plant dad",
              "green thumb", "garden humor", "retro garden", "allotment", "greenhouse", "botanical", "spring", "mothers day"]},
    {"slug": "one-more-plant", "niche": "gardening", "layout": "stack", "palette": "forest",
     "lines": "JUST|*ONE MORE|PLANT|~i promise", "title": "Just One More Plant I Promise Houseplant Lover",
     "tags": ["houseplant", "plant lover", "plant addict", "monstera", "plant lady", "plant dad", "indoor jungle",
              "gardening", "funny plant", "botanical", "succulent", "urban jungle", "plant gift", "green", "nature"]},
    {"slug": "fish-fear-quiet", "niche": "fishing", "layout": "sunset", "palette": "ocean", "motif": "waves",
     "lines": "~shhh|*THE FISH|ARE|*LISTENING", "title": "Shhh The Fish Are Listening Funny Fishing",
     "tags": ["fishing", "angler", "fisherman gift", "funny fishing", "bass fishing", "fly fishing", "carp fishing",
              "lake life", "dad gift", "grandpa gift", "retro fishing", "outdoors", "boat", "fathers day", "river"]},
    {"slug": "camp-hair", "niche": "camping", "layout": "sunset", "palette": "retro", "motif": "mountains",
     "lines": "NO SIGNAL|*NO PROBLEM|~gone camping", "title": "No Signal No Problem Gone Camping Retro Mountains",
     "tags": ["camping", "hiking", "mountains", "outdoors", "retro sunset", "adventure", "camper", "nature lover",
              "digital detox", "campfire", "road trip", "vanlife", "national park", "trail", "wanderlust"]},
    {"slug": "trail-snacks", "niche": "hiking", "layout": "stack", "palette": "forest",
     "lines": "I HIKE|*FOR THE|*SNACKS", "title": "I Hike For The Snacks Funny Hiking",
     "tags": ["hiking", "hiker gift", "funny hiking", "trail", "mountains", "outdoors", "backpacking", "trekking",
              "snacks", "nature", "adventure", "camping", "walking", "national park", "summit"]},
    {"slug": "works-on-my-machine", "niche": "programming", "layout": "stack", "palette": "neon",
     "lines": "IT WORKS|*ON MY|*MACHINE|~ship the machine", "title": "It Works On My Machine Funny Programmer",
     "tags": ["programmer", "developer", "coding", "software engineer", "funny coding", "devops", "debugging",
              "computer science", "geek", "nerd", "tech humor", "it gift", "backend", "hacker", "code"]},
    {"slug": "prod-friday", "niche": "programming", "layout": "banner", "palette": "neon",
     "lines": "~i deploy on|*FRIDAYS|FEAR ME", "title": "I Deploy On Fridays Fear Me DevOps Humor",
     "tags": ["devops", "deploy friday", "programmer", "developer", "sysadmin", "sre", "funny coding", "software",
              "tech humor", "it gift", "engineer", "kubernetes", "on call", "geek", "code"]},
    {"slug": "homelab-budget", "niche": "homelab", "layout": "stack", "palette": "neon",
     "lines": "MY HOMELAB|*IS NOT A|*HOBBY|~it is a lifestyle with a power bill",
     "title": "My Homelab Is Not A Hobby Funny Sysadmin", "tags": [
         "homelab", "sysadmin", "self hosted", "server", "linux", "networking", "it gift", "nerd", "geek",
         "tech humor", "raspberry pi", "gpu", "data hoarder", "devops", "computer"]},
    {"slug": "local-llm", "niche": "ai", "layout": "banner", "palette": "neon",
     "lines": "~my other computer|*IS THINKING|PLEASE WAIT", "title": "My Other Computer Is Thinking Local AI Humor",
     "tags": ["ai", "machine learning", "local llm", "gpu", "data science", "nerd", "geek", "tech humor", "programmer",
              "engineer", "deep learning", "robot", "future", "computer", "it gift"]},
    {"slug": "cat-ceo", "niche": "cats", "layout": "stack", "palette": "retro",
     "lines": "I WORK HARD|~so my|*CAT|CAN HAVE A|*BETTER LIFE", "title": "I Work Hard So My Cat Can Have A Better Life",
     "tags": ["cat lover", "cat mom", "cat dad", "funny cat", "cat gift", "kitten", "crazy cat lady", "pet lover",
              "cat owner", "meow", "retro cat", "animal lover", "cat humor", "work humor", "pets"]},
    {"slug": "dog-walk-therapy", "niche": "dogs", "layout": "sunset", "palette": "retro", "motif": "hills",
     "lines": "~sorry i'm late|*MY DOG|NEEDED|*ONE MORE LAP", "title": "Sorry I'm Late My Dog Needed One More Lap",
     "tags": ["dog lover", "dog walk", "dog owner", "funny dog", "dog gift", "puppy", "pet lover", "rescue dog",
              "dog person", "retro dog", "animal lover", "dog humor", "walkies", "pets", "outdoors"]},
    {"slug": "coffee-then-people", "niche": "coffee", "layout": "banner", "palette": "retro",
     "lines": "~first|*COFFEE|THEN PEOPLE|~maybe", "title": "First Coffee Then People Maybe Funny Introvert",
     "tags": ["coffee", "coffee lover", "introvert", "funny coffee", "caffeine", "espresso", "barista", "morning person",
              "monday", "office humor", "retro coffee", "cafe", "latte", "gift for her", "gift for him"]},
    {"slug": "book-one-more-chapter", "niche": "books", "layout": "stack", "palette": "retro",
     "lines": "ONE MORE|*CHAPTER|~said me at 2 am", "title": "One More Chapter Said Me At 2 AM Book Lover",
     "tags": ["book lover", "reading", "bookworm", "bookish", "librarian", "funny reading", "book club", "reader gift",
              "literature", "library", "booktok", "novel", "teacher gift", "introvert", "cozy"]},
    {"slug": "cyclist-uphill", "niche": "cycling", "layout": "sunset", "palette": "ocean", "motif": "mountains",
     "lines": "IT NEVER GETS|*EASIER|YOU JUST GET|*HUNGRIER", "title": "It Never Gets Easier You Just Get Hungrier Cycling",
     "tags": ["cycling", "cyclist", "road bike", "mtb", "gravel bike", "bike gift", "funny cycling", "bicycle",
              "climbing", "mountains", "sport", "endurance", "bikepacking", "velo", "fahrrad"]},
    {"slug": "nurse-night-shift", "niche": "nurses", "layout": "stack", "palette": "ocean",
     "lines": "NIGHT SHIFT|*NURSE|~powered by coffee|AND SPITE", "title": "Night Shift Nurse Powered By Coffee And Spite",
     "tags": ["nurse", "night shift", "nurse gift", "rn", "nursing student", "healthcare", "funny nurse", "hospital",
              "medical", "icu nurse", "er nurse", "coffee", "nurse week", "scrubs", "caregiver"]},
    {"slug": "teacher-tired", "niche": "teachers", "layout": "banner", "palette": "retro",
     "lines": "~i'm not yelling|*I'M A TEACHER|WE PROJECT", "title": "I'm Not Yelling I'm A Teacher We Project",
     "tags": ["teacher", "teacher gift", "funny teacher", "school", "back to school", "educator", "classroom",
              "elementary", "teaching", "teacher life", "end of year", "professor", "retro teacher", "staff room", "kids"]},
    {"slug": "dad-grill", "niche": "bbq", "layout": "sunset", "palette": "retro", "motif": "flames",
     "lines": "~dad's grill|*LOW|AND|*SLOW", "title": "Dad's Grill Low And Slow Retro BBQ",
     "tags": ["bbq", "grilling", "dad gift", "fathers day", "pitmaster", "smoker", "barbecue", "meat lover", "grill master",
              "retro bbq", "cookout", "summer", "funny dad", "grandpa gift", "chef"]},
    {"slug": "introvert-plans", "niche": "introverts", "layout": "stack", "palette": "mono",
     "lines": "SORRY|*I HAVE PLANS|~with my couch", "title": "Sorry I Have Plans With My Couch Introvert",
     "tags": ["introvert", "homebody", "funny introvert", "antisocial", "couch", "cozy", "sarcasm", "stay home",
              "minimalist", "typography", "mood", "weekend", "relax", "gift for her", "gift for him"]},
    {"slug": "sauna-club", "niche": "sauna", "layout": "sunset", "palette": "retro", "motif": "flames",
     "lines": "SAUNA|*CLUB|~sweat now, think later", "title": "Sauna Club Sweat Now Think Later Retro",
     "tags": ["sauna", "wellness", "spa", "finnish sauna", "aufguss", "cold plunge", "relax", "retro", "nordic",
              "sweat", "steam", "winter", "self care", "sauna gift", "scandinavian"]},
    {"slug": "feierabend", "niche": "de_humor", "layout": "stack", "palette": "retro", "lang": "de",
     "lines": "OFFIZIELL|*FEIERABEND|~bitte nicht ansprechen", "title": "Offiziell Feierabend Bitte Nicht Ansprechen",
     "tags": ["feierabend", "lustig", "spruch", "arbeit", "kollege", "geschenk", "büro", "humor", "wochenende",
              "sarkasmus", "retro", "geschenkidee", "chef", "job", "montag"]},
    {"slug": "gartenzwerg", "niche": "de_garden", "layout": "sunset", "palette": "forest", "motif": "plants", "lang": "de",
     "lines": "~ich muss gar nichts|*ICH MUSS|NUR IN DEN|*GARTEN", "title": "Ich Muss Nur In Den Garten Gärtner Spruch",
     "tags": ["garten", "gärtner", "lustig", "spruch", "schrebergarten", "pflanzen", "hobbygärtner", "geschenk",
              "rentner", "gemüse", "natur", "balkon", "gartenarbeit", "geschenkidee", "opa"]},
    {"slug": "angeln-ruhe", "niche": "de_fishing", "layout": "sunset", "palette": "ocean", "motif": "waves", "lang": "de",
     "lines": "~ich bin nicht weg|*ICH BIN|*ANGELN", "title": "Ich Bin Nicht Weg Ich Bin Angeln Angler Spruch",
     "tags": ["angeln", "angler", "lustig", "spruch", "fischen", "karpfen", "geschenk", "papa", "opa", "see",
              "natur", "hobby", "angler geschenk", "vatertag", "hecht"]},
]


# Koncepty niszy trades_gamer (fachowiec + gracz) mieszkaja w osobnym module, bo zostaly
# ODTWORZONE z plikow partii po nadpisaniu kodu 2026-09-21 - trzymamy je oddzielnie,
# zeby bylo widac, ktora czesc jest rekonstrukcja.
try:
    from app.services.pod_concepts_trades import TRADES_CONCEPTS as _TRADES
    SEED_CONCEPTS += [c for c in _TRADES if c["slug"] not in {x["slug"] for x in SEED_CONCEPTS}]
except Exception as _e:  # brak modulu nie moze zatrzymac fabryki
    logger.warning("[pod_factory] koncepty trades_gamer niedostepne: %s", _e)

# Linia WŁASNEJ marki OKO MASZYNY / MACHINE EYE (2026-09-25) — hasła z najlepiej oglądanych shortów.
try:
    from app.services.pod_concepts_marka import MARKA_CONCEPTS as _MARKA
    SEED_CONCEPTS += [c for c in _MARKA if c["slug"] not in {x["slug"] for x in SEED_CONCEPTS}]
except Exception as _e:
    logger.warning("[pod_factory] koncepty marki OKO MASZYNY niedostepne: %s", _e)

try:
    from app.services.pod_concepts_de import DE_CONCEPTS as _DE
    SEED_CONCEPTS += [c for c in _DE if c["slug"] not in {x["slug"] for x in SEED_CONCEPTS}]
except Exception as _e:
    logger.warning("[pod_factory] koncepty DE niedostepne: %s", _e)

_NICHE_BLURB = {
    "de": "Hochwertiger Druck, klares Retro-Design mit Typografie. Ideal als Geschenk – zum Geburtstag, "
          "zu Weihnachten oder einfach so.",
    "en": "Clean retro typography design, bold and readable from across the room. A great gift for birthdays, "
          "holidays, or just because.",
    # 2026-09-25: hasła PL (trades_gamer, marka OKO MASZYNY) dostawały opis po angielsku.
    "pl": "Wyraźny nadruk w stylu retro, czytelny z drugiego końca pokoju. Dobry prezent na urodziny, "
          "Dzień Ojca albo po prostu dla kogoś, kto gra.",
}


# ─────────────────────────── silnik graficzny (PIL, zero GPU) ───────────────────────────

def _font(key: str, size: int):
    from PIL import ImageFont
    p = FONT_DIR / FONTS.get(key, FONTS["sans"])
    if not p.exists():  # brak pobranego kroju nie może zatrzymać fabryki
        p = FONT_DIR / FONTS["sans"]
    return ImageFont.truetype(str(p), size)


def _fit(draw, text: str, key: str, width: int, max_h: int):
    """Największy krój, przy którym linia mieści się w width i max_h (bisekcja)."""
    lo, hi = 40, 2400
    while lo < hi - 4:
        mid = (lo + hi) // 2
        l, t, r, b = draw.textbbox((0, 0), text, font=_font(key, mid))
        if (r - l) <= width and (b - t) <= max_h:
            lo = mid
        else:
            hi = mid
    f = _font(key, lo)
    return f, draw.textbbox((0, 0), text, font=f)


def _parse_lines(spec: str) -> list[dict]:
    out = []
    for raw in spec.split("|"):
        raw = raw.strip()
        if not raw:
            continue
        kind = "accent" if raw[0] == "*" else "script" if raw[0] == "~" else "plain"
        txt = raw.lstrip("*~").strip()
        if kind == "script" and txt.isupper():
            txt = txt.lower()  # mózg lubi WERSALIKI, a pismo odręczne czyta się wtedy źle
        out.append({"text": txt, "kind": kind})
    return out


def _draw_stack(draw, lines: list[dict], pal: tuple, top: int, bottom: int, display: str) -> int:
    """Hasło w słupku: każda linia dociągnięta do pełnej szerokości. Zwraca y końca."""
    width = W - 2 * MARGIN
    budget = bottom - top
    n_big = sum(1 for ln in lines if ln["kind"] != "script") or 1
    n_small = len(lines) - n_big if len(lines) > n_big else 0
    # 29.09 (operator: „rozmiary liter”): dopisek miał sztywne 360 px przy 1250 px hasła —
    # na kubku/naklejce/czapce to kilka milimetrów i nie da się go przeczytać. Dopisek dostaje
    # ¾ wysokości hasła (min. 650 px) i własne miejsce w budżecie.
    rezerwa = 900
    big_h = int((budget - n_small * rezerwa) / n_big * 0.86)
    big_h = max(420, min(big_h, 1250))
    small_h = max(650, int(big_h * 0.75))   # krój odręczny: wysoki bbox, niskie litery
    y = top
    acc_i = 0
    for ln in lines:
        if ln["kind"] == "script":
            f, bb = _fit(draw, ln["text"], "script", int(width * 0.92), small_h)
            color = pal[1 + (acc_i + 1) % 3]
        elif ln["kind"] == "accent":
            f, bb = _fit(draw, ln["text"], display, width, big_h)
            color = pal[1 + acc_i % 3]
            acc_i += 1
        else:
            f, bb = _fit(draw, ln["text"], display, width, int(big_h * 0.78))
            color = pal[0]
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        draw.text(((W - tw) // 2 - bb[0], y - bb[1]), ln["text"], font=f, fill=color)
        y += th + int(th * 0.20) + 40
    return y


def _lens(d, x0, y0, length, ang, col, fat: float = 0.27):
    """Soczewka (dwa łuki sinusa) obrócona o ang — sylwetka liścia albo płomienia."""
    import math
    pts = []
    for side in (1, -1):
        for i in (range(0, 21) if side == 1 else range(20, -1, -1)):
            t = i / 20
            lx, ly = t * length, side * math.sin(t * math.pi) * length * fat
            pts.append((x0 + lx * math.cos(ang) - ly * math.sin(ang),
                        y0 + lx * math.sin(ang) + ly * math.cos(ang)))
    d.polygon(pts, fill=col)


def _draw_sunset(img, pal: tuple, motif: str, cy: int, r: int):
    """Retro słońce w pasy + prosty wektorowy motyw niszy na pierwszym planie."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx = W // 2
    bands = [pal[1], pal[1], pal[2], pal[2], pal[3]]
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    bh = (2 * r) // len(bands)
    for i, c in enumerate(bands):
        d.rectangle((cx - r, cy - r + i * bh, cx + r, cy - r + (i + 1) * bh), fill=c)
    # przerwy w dolnej połowie — znak rozpoznawczy „retro sunset"
    for k in range(5):
        gy = cy + int(r * (0.10 + k * 0.18))
        d.rectangle((cx - r, gy, cx + r, gy + 22 + k * 12), fill=(0, 0, 0, 0))
    layer.putalpha(Image.composite(layer.getchannel("A"), Image.new("L", (W, H), 0), mask))
    img.alpha_composite(layer)

    d = ImageDraw.Draw(img)
    base = cy + r
    ink = pal[0]
    if motif == "mountains":
        d.polygon([(cx - r - 120, base), (cx - r * 0.35, cy - r * 0.15), (cx + r * 0.1, base)], fill=ink)
        d.polygon([(cx - r * 0.3, base), (cx + r * 0.45, cy - r * 0.45), (cx + r + 120, base)], fill=ink)
        for px in (-0.95, -0.72, 0.78, 1.0):
            x = cx + int(r * px)
            for j in range(3):
                d.polygon([(x - 150 + j * 25, base - j * 150), (x, base - 330 - j * 150),
                           (x + 150 - j * 25, base - j * 150)], fill=pal[3])
    elif motif == "hills":
        hills = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        hd = ImageDraw.Draw(hills)
        hd.ellipse((cx - r - 200, base - 420, cx + 250, base + 700), fill=pal[3])
        hd.ellipse((cx - 350, base - 560, cx + r + 200, base + 800), fill=ink)
        hd.rectangle((0, base, W, H), fill=(0, 0, 0, 0))  # nic poniżej horyzontu — tam stoi hasło
        img.alpha_composite(hills)
        d = ImageDraw.Draw(img)
    elif motif == "waves":
        for row in range(3):
            yy = base - 330 + row * 150
            col = (ink, pal[3], ink)[row]
            for k in range(-1, 9):
                x0 = cx - r - 150 + k * 380 + (190 if row % 2 else 0)
                d.pieslice((x0, yy, x0 + 380, yy + 380), 180, 360, fill=col)
        d.rectangle((cx - r - 150, base - 30, cx + r + 150, base + 20), fill=ink)
    elif motif == "plants":
        import math
        leaf = lambda *a, **kw: _lens(d, *a, **kw)
        for k, (px, hgt) in enumerate(((-0.62, 760), (0.0, 1150), (0.62, 880))):
            x = cx + int(r * px)
            d.rectangle((x - 22, base - hgt, x + 22, base), fill=ink)
            leaf(x, base - hgt + 10, 430, -math.pi / 2, pal[3])
            for j in range(hgt // 330):
                yy = base - 170 - j * 300
                leaf(x, yy, 520, math.radians(-150), ink if (j + k) % 2 else pal[3])
                leaf(x, yy - 120, 520, math.radians(-30), pal[3] if (j + k) % 2 else ink)
    elif motif == "flames":
        import math
        up = -math.pi / 2
        # Ognisko: trzy płomienie sięgające w słońce, każdy w trzech warstwach (chłodniejsza na zewnątrz).
        for px, hgt in ((-0.46, 1500), (0.46, 1500), (0.0, 2250)):
            x = cx + int(r * px)
            _lens(d, x, base, hgt, up, pal[2], fat=0.30)
            _lens(d, x, base, hgt * 0.66, up, pal[1], fat=0.30)
            _lens(d, x, base, hgt * 0.30, up, ink, fat=0.30)
    d.rectangle((cx - r - 200, base - 6, cx + r + 200, base + 34), fill=ink)


def _draw_banner_rules(draw, pal: tuple, y: int):
    """Linia–gwiazdki–linia: rama układu „banner"."""
    cx = W // 2
    draw.rectangle((MARGIN + 200, y, cx - 420, y + 26), fill=pal[0])
    draw.rectangle((cx + 420, y, W - MARGIN - 200, y + 26), fill=pal[0])
    for k in (-1, 0, 1):
        sx, sy, s = cx + k * 250, y + 13, 95 if k == 0 else 65
        pts = []
        import math
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            rad = s if i % 2 == 0 else s * 0.42
            pts.append((sx + rad * math.cos(ang), sy + rad * math.sin(ang)))
        draw.polygon(pts, fill=pal[1 + (k + 1) % 3])


def _distress(img, seed: int, amount: float = 0.07):
    """Wytarcie „vintage": szum niskiej rozdzielczości wygryza alfę (deterministyczny)."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(seed)
    small = (rng.random((H // 12, W // 12)) > amount).astype("uint8") * 255
    m = Image.fromarray(small, "L").resize((W, H), Image.BICUBIC).point(lambda v: 255 if v > 110 else 0)
    a = np.minimum(np.asarray(img.getchannel("A")), np.asarray(m))
    img.putalpha(Image.fromarray(a, "L"))


def _ilustracja_w_polu(sciezka: str, max_h: int):
    """Grafika (RGBA bez tła) przycięta do treści i wpisana w pole szerokość płótna × max_h."""
    from PIL import Image
    il = Image.open(sciezka).convert("RGBA")
    bbox = il.getchannel("A").getbbox()
    if bbox:
        il = il.crop(bbox)
    s = min((W - 2 * MARGIN) / il.width, max_h / il.height)
    return il.resize((max(1, round(il.width * s)), max(1, round(il.height * s))), Image.LANCZOS)


def _render(concept: dict, mode: str):
    from PIL import Image, ImageDraw
    pal = PALETTES[concept.get("palette", "retro")][mode]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    lines = _parse_lines(concept["lines"])
    layout = concept.get("layout", "stack")
    display = {"stack": "anton", "sunset": "bebas", "banner": "slab"}.get(layout, "anton")
    if layout == "ilustracja" and concept.get("ilustracja"):
        # 2026-10-01 (pod_gemini): ilustracja z Gemini nad hasłem. Hasło rysujemy osobno i tylko JE
        # postarzamy — _distress na całym płótnie wyżerałby dziury w grafice.
        # przy 3–4 liniach hasło dostaje więcej miejsca — inaczej drobnieje względem grafiki
        il = _ilustracja_w_polu(concept["ilustracja"], int(H * {2: 0.58, 3: 0.52}.get(len(lines), 0.47)))
        _draw_stack(draw, lines, pal, MARGIN + il.height + 170, H - MARGIN, display)
        if concept.get("palette") != "neon":
            _distress(img, seed=sum(map(ord, concept["slug"])))
        img.alpha_composite(il, ((W - il.width) // 2, MARGIN))
        return img
    if layout == "sunset":
        r = 1250
        _draw_sunset(img, pal, concept.get("motif", ""), cy=MARGIN + r, r=r)
        draw = ImageDraw.Draw(img)
        _draw_stack(draw, lines, pal, MARGIN + 2 * r + 170, H - MARGIN, display)
    elif layout == "banner":
        _draw_banner_rules(draw, pal, MARGIN + 60)
        end = _draw_stack(draw, lines, pal, MARGIN + 330, H - MARGIN - 300, display)
        _draw_banner_rules(draw, pal, min(end + 80, H - MARGIN - 120))
    else:
        _draw_stack(draw, lines, pal, MARGIN, H - MARGIN, display)
    if concept.get("palette") != "neon":  # czysty „tech" zostaje ostry
        _distress(img, seed=sum(map(ord, concept["slug"])))  # hash() jest solony per proces
    return img


def _crop_to_art(img):
    """Przycina płótno do samej grafiki (+2% oddechu).

    Wcześniej plik szedł jako pełne 4500×5400 z grafiką przy górnej krawędzi — platforma
    skaluje CAŁY plik do pola nadruku, więc hasło z dwóch linii lądowało jako mały pasek
    pod szyją, z pustką na pół koszulki. Po przycięciu grafika wypełnia pole nadruku.
    """
    from PIL import Image
    bbox = img.getbbox()
    if not bbox:
        return img
    pad = int(W * 0.02)
    box = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
           min(W, bbox[2] + pad), min(H, bbox[3] + pad))
    crop = img.crop(box)
    # Wpisujemy grafikę w pole 15×18 cala: jeden bok dotyka krawędzi, drugi zostaje mniejszy.
    # (Samo dosunięcie do pełnej szerokości dawało pliki 4500×7300, które platforma i tak
    # zmniejsza po wysokości — nadruk wychodził węższy, niż wynika z projektu.)
    scale = min(W / crop.width, H / crop.height)
    if abs(scale - 1.0) > 0.001:
        crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
                           Image.LANCZOS)
    return crop


# ── Akcesoria ──────────────────────────────────────────────────────────────────
# Odtworzone 2026-09-21 ze specyfikacji zapisanej w partii `partia_spocony_20260920`
# (wymiary i opisy 1:1 z jej `listing.json`), po tym jak wgranie nowej wersji fabryki
# nadpisało kod z 20.09 wieczorem. Wymiary to standard 300 DPI, NIE odczyt ze
# specyfikacji platform — przy zakładaniu kont potwierdzić u źródła.
ACCESSORIES: dict[str, dict] = {
    "naklejka": {"px": (2000, 2000), "wersja": "light", "tlo": None, "margines": 0.06,
                 "opis": "Naklejka die-cut na skrzynkę narzędziową / kufer / bus"},
    "kubek": {"px": (2475, 1155), "wersja": "light", "tlo": None, "margines": 0.20,
              "opis": "Nadruk na kubek 330 ml (biały) — pas owijający"},
    "podkladka": {"px": (3600, 1800), "wersja": "dark", "tlo": "ciemne", "margines": 0.16,
                  "opis": "Podkładka pod mysz XXL — pełne tło, bez przezroczystości"},
    "butelka": {"px": (1500, 2400), "wersja": "light", "tlo": None, "margines": 0.12,
                "opis": "Butelka / termos — pionowy pas"},
    "czapka": {"px": (1800, 700), "wersja": "dark", "tlo": None, "cap": True,
               "opis": "Czapka z daszkiem — JEDNA linia z pola `cap_line`, nic więcej się nie mieści"},
}
PODGLAD_ETYKIETY = {"naklejka": "NAKLEJKA", "kubek": "KUBEK", "podkladka": "PODKŁADKA",
                    "butelka": "BUTELKA / TERMOS", "czapka": "CZAPKA"}


def _wpisz(art, w: int, h: int, margines: float, tlo: str | None):
    """Grafika wyśrodkowana w płótnie przedmiotu (contain). tlo=None → przezroczyste."""
    from PIL import Image
    pole_w, pole_h = int(w * (1 - 2 * margines)), int(h * (1 - 2 * margines))
    skala = min(pole_w / art.width, pole_h / art.height)
    mini = art.resize((max(1, round(art.width * skala)), max(1, round(art.height * skala))),
                      Image.LANCZOS)
    if tlo is None:
        plotno = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        plotno.alpha_composite(mini, ((w - mini.width) // 2, (h - mini.height) // 2))
        return plotno
    plotno = Image.new("RGB", (w, h), tlo)
    plotno.paste(mini, ((w - mini.width) // 2, (h - mini.height) // 2), mini)
    return plotno


def _czapka_png(concept: dict, w: int, h: int):
    """Czapka bierze JEDNĄ linię: pole `cap_line`, inaczej ostatnią linię akcentową."""
    from PIL import Image, ImageDraw
    pal = PALETTES[concept.get("palette", "retro")]["dark"]
    linie = _parse_lines(concept["lines"])
    tekst = (concept.get("cap_line")
             or next((ln["text"] for ln in reversed(linie) if ln["kind"] == "accent"), "")
             or linie[-1]["text"])
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    display = {"stack": "anton", "sunset": "bebas", "banner": "slab"}.get(
        concept.get("layout", "stack"), "anton")
    # _fit mierzy na płótnie fabryki (W×H), więc rysujemy w osobnym module miar.
    lo, hi = 20, h
    f = _font(display, lo)
    while lo < hi - 2:
        mid = (lo + hi) // 2
        l, t, r, b = d.textbbox((0, 0), tekst, font=_font(display, mid))
        if (r - l) <= w * 0.86 and (b - t) <= h * 0.62:
            lo = mid
        else:
            hi = mid
    f = _font(display, lo)
    bb = d.textbbox((0, 0), tekst, font=f)
    d.text(((w - (bb[2] - bb[0])) // 2 - bb[0], (h - (bb[3] - bb[1])) // 2 - bb[1]),
           tekst, font=f, fill=pal[0])
    return img


def _preview_akcesoria(pliki: list[tuple[str, object]], paleta: str):
    """Pasek podglądów z podpisem pod każdym przedmiotem — do oceny jednym spojrzeniem."""
    from PIL import Image, ImageDraw
    kafel, wys, pasek = 620, 620, 76
    tla = {"naklejka": "#B9BDC2", "kubek": "#F4F1EA", "podkladka": "#3A3F45",
           "butelka": "#DCDDE1", "czapka": DISPLATE_BG.get(paleta, "#141414")}
    out = Image.new("RGB", (kafel * len(pliki) + 8 * (len(pliki) + 1), wys + pasek + 16), "#6E6E6E")
    d = ImageDraw.Draw(out)
    font = _font("sans_md", 34)
    for i, (rodzaj, im) in enumerate(pliki):
        x = 8 + i * (kafel + 8)
        tlo = Image.new("RGB", (kafel, wys), tla.get(rodzaj, "#CCCCCC"))
        mini = im.convert("RGBA")
        mini.thumbnail((int(kafel * 0.86), int(wys * 0.86)), Image.LANCZOS)
        tlo.paste(mini, ((kafel - mini.width) // 2, (wys - mini.height) // 2), mini)
        out.paste(tlo, (x, 8))
        d.text((x + 6, wys + 22), PODGLAD_ETYKIETY.get(rodzaj, rodzaj.upper()), font=font, fill="#FFFFFF")
    return out


def _zbuduj_akcesoria(concept: dict, dark, light, katalog: Path) -> dict:
    """Komplet przedmiotów wg `formats` konceptu (domyślnie wszystkie z ACCESSORIES)."""
    rodzaje = concept.get("formats") or list(ACCESSORIES)
    paleta = concept.get("palette", "retro")
    produkty, podglady = {}, []
    for rodzaj in rodzaje:
        spec = ACCESSORIES.get(rodzaj)
        if not spec:
            continue
        w, h = spec["px"]
        if spec.get("cap"):
            obraz = _czapka_png(concept, w, h)
        else:
            zrodlo = dark if spec["wersja"] == "dark" else light
            tlo = DISPLATE_BG.get(paleta, "#141414") if spec.get("tlo") else None
            obraz = _wpisz(zrodlo, w, h, spec.get("margines", 0.12), tlo)
        rozsz = "jpg" if obraz.mode == "RGB" else "png"
        nazwa = f"{concept['slug']}_{rodzaj}.{rozsz}"
        if rozsz == "jpg":
            obraz.save(katalog / nazwa, quality=94, dpi=(300, 300))
        else:
            obraz.save(katalog / nazwa, dpi=(300, 300), optimize=True)
        produkty[rodzaj] = {"plik": nazwa, "opis": spec["opis"], "px": [w, h]}
        podglady.append((rodzaj, obraz))
    if podglady:
        _preview_akcesoria(podglady, paleta).save(katalog / "preview_akcesoria.jpg", quality=88)
    return produkty


def _displate(design, palette: str):
    from PIL import Image
    bg = Image.new("RGB", DISPLATE_WH, DISPLATE_BG.get(palette, "#141414"))
    bbox = design.getbbox()
    art = design.crop(bbox)
    scale = min((DISPLATE_WH[0] * 0.80) / art.width, (DISPLATE_WH[1] * 0.80) / art.height)
    art = art.resize((int(art.width * scale), int(art.height * scale)), Image.LANCZOS)
    bg.paste(art, ((DISPLATE_WH[0] - art.width) // 2, (DISPLATE_WH[1] - art.height) // 2), art)
    return bg


def _preview(dark, light):
    """Podgląd dla oka/operatora: projekt na grafitowej i na białej próbce."""
    from PIL import Image
    sw, sh = 900, 1080
    out = Image.new("RGB", (sw * 2 + 60, sh + 40), "#7A7A7A")
    for i, (im, bg) in enumerate(((dark, "#23262B"), (light, "#F2F0EA"))):
        sw_img = Image.new("RGB", (sw, sh), bg)
        tw = sw - 120
        t = im.resize((tw, max(1, int(tw * im.height / im.width))), Image.LANCZOS)
        if t.height > sh - 80:
            t = t.resize((max(1, int(t.width * (sh - 80) / t.height)), sh - 80), Image.LANCZOS)
        sw_img.paste(t, ((sw - t.width) // 2, (sh - t.height) // 2), t)
        out.paste(sw_img, (20 + i * (sw + 20), 20))
    return out


# ─────────────────────────── bramki i listingi ───────────────────────────

def tm_gate(concept: dict) -> str | None:
    """Zwraca powód odrzutu albo None. Sprawdza hasło, tytuł i tagi."""
    blob = " ".join([concept.get("lines", ""), concept.get("title", ""), " ".join(concept.get("tags", []))])
    m = _TM_RE.search(blob)
    return f"znak towarowy/marka w treści: „{m.group(0)}”" if m else None


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0]


def platform_listings(concept: dict) -> dict:
    """Ten sam projekt, pięć kompletów metadanych — każdy w limitach swojej platformy."""
    lang = concept.get("lang", "en")
    title = concept["title"]
    tags = list(dict.fromkeys(t.strip().lower() for t in concept["tags"] if t.strip()))
    slogan = " ".join(ln["text"] for ln in _parse_lines(concept["lines"]))
    # pod_gemini pisze opis pod konkretną ilustrację; bez niego zostaje opis typografii
    desc = f"“{slogan}” — {concept.get('blurb') or _NICHE_BLURB[lang if lang in _NICHE_BLURB else 'en']}"
    shirt = "T-Shirt" if lang == "en" else "T-Shirt Geschenk"
    # ── Etsy/Printify: wzbogacony opis + tytuł z wyrażeniem prezentowym (Etsy 22.10 / 28.10) ──
    _GIFT = {"en": "gift for", "de": "Geschenk für", "pl": "prezent dla"}
    _etsy_title = _clip(f"{title} — {_GIFT.get(lang, 'gift for')} {tags[1].title()}", 140)
    _details = (
        "Details: Premium Bella+Canvas 3001 or Gildan 5000 blank. "
        "100% cotton (or cotton blend per variant). Sizes S\u20133XL. "
        "Available in multiple colors (dark/light variants). "
        "Care: machine wash inside-out at 30 \u00b0C, no tumble dry."
    )
    _ai_credit = (
        "Graphic design created by ShadowAI Design with AI assistance; "
        "print and fulfilment handled by Printify."
    )
    _etsy_desc = f"{desc}\n\n{_details}\n\n{_ai_credit}"
    return {
        "redbubble": {"title": _clip(title, 100), "tags": tags[:15], "description": desc,
                      "note": "Włącz: koszulki, bluzy, naklejki, kubki, torby; wyłącz produkty, gdzie grafika się rozjeżdża."},
        "teepublic": {"title": _clip(title, 100), "main_tag": tags[0], "supporting_tags": tags[1:16],
                      "description": desc, "note": "Plik *_dark.png na ciemne kolory; domyślny kolor: black/navy."},
        "spreadshirt": {"name": _clip(title, 50), "tags": tags[:25], "description": _clip(desc, 200),
                        "note": "Rynek EU/DE — hasła niemieckie idą najpierw tutaj."},
        "zazzle": {"title": _clip(title, 50), "tags": tags[:10], "description": desc,
                   "note": "Zazzle = personalizacja; ten projekt jest stały, więc niższy priorytet."},
        "etsy_printify": {"title": _etsy_title,
                          "tags": [t for t in (_clip(x, 20) for x in tags) if t][:13],
                          "description": _etsy_desc,
                          "wymaga_zdjecia_produktu": True,
                          "note": "Printify: Bella+Canvas 3001 / Gildan 5000, druk z *_dark.png; cena = koszt×2.2. "
                                  "Etsy: dodaj własne zdjęcie fizycznej koszulki (próbka) zanim oferta pójdzie na cyberokazje 23.11\u20131.12."},
        "displate": {"title": _clip(title, 60), "tags": tags[:20], "description": desc,
                     "note": "Plik *_displate.jpg 2900×4060. Displate wymaga akceptacji portfolio."},
    }


def build_design(concept: dict, out_root: Path) -> dict:
    """Koncept → katalog z plikami do druku, podglądem i listingami."""
    why = tm_gate(concept)
    if why:
        return {"ok": False, "slug": concept.get("slug"), "error": why}
    d = out_root / concept["slug"]
    d.mkdir(parents=True, exist_ok=True)
    dark = _crop_to_art(_render(concept, "dark"))
    light = _crop_to_art(_render(concept, "light"))
    dark.save(d / f"{concept['slug']}_dark.png", dpi=(300, 300), optimize=True)
    light.save(d / f"{concept['slug']}_light.png", dpi=(300, 300), optimize=True)
    _displate(dark, concept.get("palette", "retro")).save(
        d / f"{concept['slug']}_displate.jpg", quality=95, dpi=(300, 300))
    _preview(dark, light).save(d / "preview.jpg", quality=88)
    produkty = _zbuduj_akcesoria(concept, dark, light, d)
    listing = {"slug": concept["slug"], "niche": concept["niche"], "lang": concept.get("lang", "en"),
               "slogan": " ".join(ln["text"] for ln in _parse_lines(concept["lines"])),
               "tm_check": "blocklist", "platforms": platform_listings(concept),
               "produkty": produkty,
               "built_at": datetime.now(timezone.utc).isoformat()}
    for k in ("grupa", "grafika"):          # para językowa i źródło ilustracji (pod_gemini)
        if concept.get(k):
            listing[k] = concept[k]
    (d / "listing.json").write_text(json.dumps(listing, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "slug": concept["slug"], "dir": str(d), "files": sorted(p.name for p in d.iterdir())}


def _write_batch_index(batch_dir: Path, results: list[dict]) -> None:
    rows = ["# Partia PoD — " + batch_dir.name, "",
            "Pliki: `*_dark.png` (na ciemne ubrania), `*_light.png` (na jasne), `*_displate.jpg`, `listing.json`.",
            "Akcesoria (jeśli koncept ma `formats`): `*_naklejka.png`, `*_kubek.png`, `*_podkladka.jpg`, "
            "`*_butelka.png`, `*_czapka.png` + `preview_akcesoria.jpg`.",
            "Wymiary akcesoriów to standard 300 DPI, NIE odczyt ze specyfikacji platform — potwierdź przy zakładaniu kont.",
            "Bramka znaków towarowych = tylko lista oczywistych marek (tm_check=blocklist), NIE badanie USPTO/EUIPO.", "",
            "| projekt | hasło | nisza | przedmioty |", "|---|---|---|---|"]
    for r in results:
        if r.get("ok"):
            lst = json.loads((Path(r["dir"]) / "listing.json").read_text(encoding="utf-8"))
            przedmioty = ", ".join(["koszulka", "displate"] + list((lst.get("produkty") or {}).keys()))
            rows.append(f"| {r['slug']} | {lst['slogan']} | {lst['niche']} | {przedmioty} |")
        else:
            rows.append(f"| {r.get('slug')} | ODRZUCONY: {r.get('error')} | | |")
    (batch_dir / "PARTIA.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _contact_sheet(batch_dir: Path, results: list[dict]) -> Path | None:
    from PIL import Image
    prev = [Path(r["dir"]) / "preview.jpg" for r in results if r.get("ok")]
    if not prev:
        return None
    cols, tw, th = 4, 465, 280
    rows_n = (len(prev) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows_n * th), "#555555")
    for i, p in enumerate(prev):
        im = Image.open(p)
        im.thumbnail((tw - 10, th - 10))
        sheet.paste(im, ((i % cols) * tw + 5, (i // cols) * th + 5))
    out = batch_dir / "STYKOWKA.jpg"
    sheet.save(out, quality=85)
    return out


def build_batch(concepts: list[dict] | None = None, name: str | None = None) -> dict:
    """Synchroniczna partia (woła ją też scripts/pod_fabryka.py — bez backendu, bez GPU)."""
    concepts = concepts or SEED_CONCEPTS
    batch_dir = EXPORT_ROOT / (name or datetime.now().strftime("partia_%Y%m%d_%H%M"))
    batch_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for c in concepts:
        try:
            results.append(build_design(c, batch_dir))
        except Exception as e:  # jeden zły koncept nie kładzie partii
            logger.exception("[pod_factory] build fail %s", c.get("slug"))
            results.append({"ok": False, "slug": c.get("slug"), "error": f"{type(e).__name__}: {e}"})
    _write_batch_index(batch_dir, results)
    sheet = _contact_sheet(batch_dir, results)
    return {"ok": any(r.get("ok") for r in results), "dir": str(batch_dir),
            "built": sum(1 for r in results if r.get("ok")),
            "rejected": [r for r in results if not r.get("ok")],
            "contact_sheet": str(sheet) if sheet else "", "results": results}


# ─────────────────────────── mózg + kolejka (w procesie backendu) ───────────────────────────

async def research_concepts(niche: str, n: int = 6) -> list[dict]:
    """Mózg proponuje hasła w niszy. Wszystko przechodzi tm_gate i walidację kształtu."""
    from app.services.ollama_router import ollama_router
    from app.services.viral_research import wybierz_model_syntezy
    msgs = [{"role": "system", "content":
             "You write ORIGINAL funny t-shirt slogans for print-on-demand. No brands, no franchises, "
             "no song lyrics, no famous quotes, no existing catchphrases. Reply JSON only."},
            {"role": "user", "content":
             f"Niche: {niche}. Give {n} slogans as JSON: {{\"concepts\":[{{\"slug\":\"kebab-case\","
             "\"lines\":\"LINE ONE|*ACCENT LINE|~small script line\",\"title\":\"SEO title max 60 chars\","
             "\"tags\":[15 lowercase search tags]}]}. Rules for lines: 2-4 lines separated by |, "
             "each line max 14 characters, * marks the 1-2 key lines, ~ marks at most one small line."}]
    _model, _limit = await wybierz_model_syntezy("pod_factory")
    reply, _, _ = await ollama_router.chat_messages(
        msgs, model=_model, priority="background", timeout=_limit, format="json")
    try:
        raw = json.loads(reply).get("concepts", [])
    except Exception:
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict) or not c.get("lines") or not c.get("title") or len(c.get("tags") or []) < 8:
            continue
        lines = _parse_lines(str(c["lines"]))
        if not (2 <= len(lines) <= 5) or any(len(ln["text"]) > 22 for ln in lines):
            continue
        c = {"slug": re.sub(r"[^a-z0-9]+", "-", str(c.get("slug") or c["title"]).lower()).strip("-")[:40],
             "niche": niche, "layout": "stack", "palette": "retro", "lines": str(c["lines"]),
             "title": str(c["title"]), "tags": [str(t) for t in c["tags"]]}
        if not tm_gate(c):
            out.append(c)
    return out


async def stage_batch(batch: dict) -> dict:
    """JEDEN wpis na partię w publication_queue, needs_operator (wgranie = po zalogowaniu kont)."""
    import aiosqlite
    from app.config import settings
    pid = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.db_path, timeout=20.0) as db:
        await db.execute(
            """INSERT INTO publication_queue
               (id, opportunity_id, capability, kind, title, asset_paths, seo, first_step,
                channel, status, notes, staged_at)
               VALUES (?,?, 'ai_images_pod', 'staged_asset', ?, ?, ?, ?, 'pod', 'needs_operator', ?, ?)""",
            (pid, Path(batch["dir"]).name, f"Partia PoD: {batch['built']} projektów"[:120],
             json.dumps([batch["dir"]]), "listing.json w katalogu każdego projektu",
             "Operator: załóż/zaloguj konta (Redbubble, TeePublic, Spreadshirt, Printify+Etsy) w profilu CDP "
             "na .10 (noVNC). Potem ręka browser_human wgrywa *_dark.png + listing per platforma.",
             f"FABRYKA PoD {now[:10]} | {batch['built']} projektów | NIE auto-publikować — bramka operatora", now))
        await db.commit()
    return {"ok": True, "queue_id": pid}


async def factory_tick(niche: str | None = None) -> dict:
    """Partia: bez niszy = hasła wzorcowe; z niszą = mózg → bramka → druk. Potem staging."""
    import asyncio
    concepts = await research_concepts(niche) if niche else None
    if niche and not concepts:
        return {"ok": False, "error": f"mózg nie dał poprawnych haseł dla niszy „{niche}”"}
    batch = await asyncio.to_thread(build_batch, concepts, None)
    if batch["ok"]:
        batch["queue"] = await stage_batch(batch)
    return batch
