"""Bramki organizmu na odruchu JEV — jeden słownik decyzji dla wszystkich organów.

Claude-10 2026-09-20. To jest ta część, która „zespaja organizm". Sam silnik
(`jev_service`) jest tylko maszynką do prawdopodobieństw. Spoiwem jest TO:
jedno miejsce, w którym zapisane są pytania, jakie Shadow zadaje sobie
w każdym organie — z tymi samymi progami, tą samą kalibracją i tym samym logiem.

Dotąd każda bramka miała własny prompt, własny model i własny „chyba tak":
router pisał zdanie o wyborze węzła, mentor ocenia rękę 60–120 s, dedup IG pisał
esej o dwóch klipach, `rag_resolve` zgadywał czy pytanie to zadanie twórcze.
Cztery organy, cztery prawdy, zero pomiaru. Teraz jedna klasa pytań, jeden log
(`jev_judgments`), jedna tabela kalibracji — i można w końcu POWIEDZIEĆ, ile
warta jest intuicja Shadowa, bo jest policzona.

Kaskada (za układem z opisu Jeva i za dual-process w psychologii):
    kod deterministyczny  →  JEV (System 1)  →  próg/pas niepewności
                                  ↓ abstynencja
                          duży model (System 2)  →  operator
Kod sprawdza to, co da się sprawdzić bez modelu. JEV rozstrzyga semantykę w ~0.5 s.
Gdy sąd wpada w pas niepewności, płacimy za myślenie — ale dopiero wtedy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import aiosqlite

from app.services.jev_service import Question, ask, warm as jev_warm
from app.services.jev_service import _DB

logger = logging.getLogger("shadowai.jev.gates")


# ── słownik pytań organizmu ──────────────────────────────────────────────────
# Klucz pytania = klucz kalibracji. NIE zmieniaj kluczy bez powodu: zmiana klucza
# kasuje historię, na której stoi kalibracja (i sąd wraca do `calibrated=False`).
GATES: dict[str, Question] = {
    # 06.10 (operator: „Tak” na bramkę ręki + „Pamiętaj o JEV”): wykonawca agentowy dopisywał na żywo puste ręce
    # (`pass`, samo zarejestrowanie siebie). Decyduje kod (bramka_reki.sprawdz); ten sąd jest DORADCZY, a werdykt
    # kodu idzie mu jako wynik → po ≥30 parach kalibracja. Fałszywe TAK (przepuść pustą) kosztuje 3×.
    "reka_robi_cos": Question(
        kind="bool",
        instructions=("Czy ten nowy kod ręki Shadowa (funkcji obsługującej polecenie operatora) naprawdę coś robi — "
                      "czyta dane, woła usługę albo inną funkcję, liczy i zwraca konkretną odpowiedź? Pusta zaślepka "
                      "(samo pass, sam return bez treści, samo wpisanie siebie do AUTO_HANDLERS) to NIE."),
        cost_fp=3.0, cost_fn=1.0, margin=0.10),

    # 08.10 (operator: „Shadow ma tylko żeby dobrze w nie trafiał” + „JEV?”): kuźnia dobiera lekcje z bazy do punktów
    # zadania (trafianie_lekcji.wybierz). Decyduje System 2 (wybór z listy kandydatów z embeddingu); ten sąd jest
    # DORADCZY, a wybór Systemu 2 idzie mu jako wynik → po ≥30 parach kalibracja. Pomiar 08.10 bez kalibracji: e2b
    # mówił „tak” p=1.0 o lekcji „'wow' to wykrzyknienie” przy każdym punkcie wa_rodzina — głos dopiero po kalibracji.
    # Fałszywe NIE (uczeń nie dostaje lekcji, która by pomogła) kosztuje 2×, fałszywe TAK to tylko szum w prompcie.
    "lekcja_dotyczy_punktu": Question(
        kind="bool",
        instructions=("Czy ta LEKCJA z bazy pomaga zrobić dokładnie to, co opisuje PUNKT ZADANIA — czy jej problem jest "
                      "tym samym mechanizmem błędu albo jej rozwiązanie da się wprost zastosować? Sama wspólna dziedzina "
                      "(WhatsApp, polszczyzna, Shadow, handler) bez tego samego mechanizmu to NIE."),
        cost_fp=1.0, cost_fn=2.0, margin=0.10),

    # bramka potwierdzeń — koszt przepuszczenia akcji nieodwracalnej > koszt pytania
    "akcja_wymaga_zgody": Question(
        kind="bool",
        instructions=("Czy ta akcja zmienia świat poza komputerem albo jest nieodwracalna "
                      "(wysyłka, publikacja, kasowanie, płatność, sterowanie urządzeniem), "
                      "czyli wymaga zgody operatora? Odczyt, podgląd, notatka i generowanie "
                      "pliku lokalnie NIE wymagają zgody."),
        cost_fp=1.0, cost_fn=5.0, margin=0.08),

    # 29.09 (operator: „naucz shadow rozumieć w ten sposób”): rozdzielacz na .237 na „sprawdź, czy
    # płyty wyszły w sklepach” wybrał `distrokid_wydaj`. To pytanie odróżnia ZAMIAR z samego
    # POLECENIA (nie z nazwy akcji, jak `akcja_wymaga_zgody`). Fałszywe TAK = Shadow bierze
    # sprawdzenie za rozkaz działania → koszt 5×; fałszywe NIE = tylko dopyta.
    "polecenie_chce_dzialania": Question(
        kind="bool",
        instructions=("Czy operator w tym poleceniu chce, żeby Shadow ZROBIŁ coś ze skutkiem poza "
                      "rozmową — wysłał, opublikował, wydał, uruchomił, przełączył, zmienił ustawienie "
                      "maszyny, otworzył program? Pytanie, prośba o sprawdzenie, pokazanie, przeczytanie, stan, "
                      "rozmowa albo stworzenie tekstu, obrazu czy pliku lokalnie to NIE."),
        cost_fp=5.0, cost_fn=1.0, margin=0.10),

    # routing: czy to w ogóle zadanie dla dużego modelu
    "zadanie_wymaga_myslenia": Question(
        kind="bool",
        instructions=("Czy odpowiedź na to wymaga wieloetapowego rozumowania, planu albo "
                      "syntezy z wielu źródeł? Pytanie o fakt, stan systemu albo prosta "
                      "komenda NIE wymagają."),
        cost_fp=1.0, cost_fn=1.0, margin=0.10),

    "zadanie_tworcze": Question(
        kind="bool",
        instructions=("Czy to jest zadanie twórcze (napisz, wymyśl, zaproponuj, skomponuj) "
                      "albo pytanie o samego Shadowa? Takie zadanie NIE wymaga szukania "
                      "w bazie wiedzy."),
        cost_fp=1.0, cost_fn=1.0, margin=0.10),

    "wymaga_researchu": Question(
        kind="bool",
        # Claude-10 2026-09-21: sformułowanie JEST parametrem modelu, nie stylu. Zmierzone na
        # 200 prawdziwych poleceniach z `agent_memory` (etykieta: czy poleciał `web_search`):
        #   „fakty spoza tej rozmowy…”        → AUC 0.452 (PONIŻEJ losowania!)
        #   „czy musi sięgnąć do INTERNETU…”  → AUC 0.630
        #   „prośba o WYSZUKANIE informacji…” → AUC 0.946  ← zostaje ta
        # Kalibracja jest kluczowana także ODCISKIEM TEJ TREŚCI (jev_service.pytanie_hash),
        # więc każda zmiana tego zdania unieważnia współczynniki zamiast je po cichu dziedziczyć.
        instructions=("Czy to polecenie to prośba o WYSZUKANIE informacji (w sieci, "
                      "w dokumentach), a nie o wykonanie czegoś ani o wytworzenie treści?"),
        cost_fp=2.0, cost_fn=1.0, margin=0.10),

    "organ": Question(
        kind="choice",
        instructions="Który organ ShadowAI powinien wykonać to zadanie?",
        options=["backend .10 (logika, baza, maile)", "gaming .253 (ekran, gra, Windows)",
                 "render .130 (montaż, wideo, GPU)", "operator (człowiek decyduje)"],
        margin=0.20),

    # 31.10 (operator: „nic hardcoded, trójkąty, areny, JEV”): montaz_rodzinny wołał montage_jasio na sztywno
    # z --styl nooby_rodzice, a tego wieczoru Dziecko grał obok taty i mówił do mikrofonu. Pierwsze słowo opcji =
    # nazwa stylu montage_jasio (skrypt bierze z niej styl i listę dozwolonych --styl). Klucz stały (kalibracja).
    # 31.10 03:46Z (pomiar Claude): gemma4 przy choice otwierała kanał myślenia → abstain, dist {}. bez_myslenia=True
    # przywraca odruch; margin 0.5 posyła niejasne stany (pewność 0.33–0.46) do Systemu 2 przez decide(.
    "styl_montazu_rodzinnego": Question(
        kind="choice",
        instructions=("Który styl lektora pasuje do tego montażu rodzinnego? Wybierz styl lektora z transkryptów "
                      "mikrofonów i z tego, które komputery nagrywały. Imiona i zwroty w transkrypcie („tato”, "
                      "„mamo”, „Dziecko”, głos dziecka) są ważniejsze niż numer komputera; .130 to komputer gościnny "
                      "(grają na nim różne osoby). Głos dziecka albo zwroty do dziecka (Dziecko, synku) = gra Dziecko; "
                      "dorośli mówiący do siebie (kochanie, skarbie, mamo/tato jako zwrot do partnera) bez głosu "
                      "dziecka = rodzice razem; brak mowy na wszystkich mikrofonach = za mało danych."),
        options=["sigma_jas — gra sam Dziecko (9 lat), lektor-kumpel woła go Sigma Dziecko",
                 "roast_tata — tata gra z Dziecko albo przy nim, Dziecko gada, lektor żartuje z taty",
                 "nooby_rodzice — grają mama i tata razem, oboje początkujący"],
        bez_myslenia=True,
        margin=0.5),

    # publikacja — lekcje z OKO MASZYNY i z niemych nagrań .130
    "material_gotowy_do_publikacji": Question(
        kind="bool",
        instructions=("Czy ten materiał nadaje się do publikacji: ma opis, ma treść z gry "
                      "lub nagrania (a nie pulpit, e-mail czy okno kodu), i nie jest pusty? "
                      "Brak etykiety, cisza albo klatka pulpitu = NIE."),
        cost_fp=3.0, cost_fn=1.0, margin=0.12),

    "ten_sam_material": Question(
        kind="bool",
        instructions=("Czy te dwa wpisy opisują TEN SAM materiał (ten sam odcinek, ten sam "
                      "klip)? Ten sam cykl przy różnych numerach to RÓŻNE materiały."),
        cost_fp=4.0, cost_fn=1.0, margin=0.12),

    "reka_juz_istnieje": Question(
        kind="bool",
        instructions=("Czy proponowana nowa zdolność już istnieje na liście zdolności "
                      "Shadowa, tylko pod inną nazwą?"),
        cost_fp=1.0, cost_fn=2.0, margin=0.10),

    # poczta / zgłoszenia
    "pilnosc": Question(
        kind="score",
        instructions="Jak pilna jest ta sprawa dla operatora?",
        levels=["błaha", "niska", "średnia", "wysoka", "alarm"], margin=0.15),

    "dotyczy_pieniedzy": Question(
        kind="bool",
        instructions=("Czy ta wiadomość dotyczy pieniędzy operatora: faktury, płatności, "
                      "zwrotu, zadłużenia, wypłaty albo dotacji?"),
        cost_fp=1.0, cost_fn=3.0, margin=0.10),
}

# ── prior klasy TAK w prawdziwym życiu (poprawka case-control) ───────────────
# Operator/Claude-10 2026-09-20: żniwa biorą po równo „tak" i „nie", żeby model zobaczył
# obie klasy — ale życie nie jest zbalansowane. Kalibracja policzona na zbalansowanej próbce
# dziedziczy jej prior i bramka zaczyna pytać o wszystko (§3 handoffu). Ten słownik mieszkał
# tylko w `scripts/jev.py`, więc `POST /api/jev/calibrate/{k}` liczył kalibrację BEZ poprawki
# i po cichu nadpisywał dobrą — ta sama pułapka co [[feedback_zmiana_jednej_strony_pary]].
# Teraz jedno miejsce dla CLI i dla API.
BASE_RATE_SQL: dict[str, str] = {
    # ile akcji w historii naprawdę wymagało potwierdzenia (605 / 5997 ≈ 0.10)
    # polecenia z akcją ze skutkiem vs tylko-odczyt w agent_memory (51 / 956 ≈ 0.05)
    "polecenie_chce_dzialania": (
        "SELECT SUM(action IN ('gaming_toggle','start_phone','gaming_optimize','wa_reply','code_edit',"
        "'start_comfy','generate_document','stop_comfy','gaming_restore','wa_send','gmail_send')), "
        "SUM(action IN ('check_machines','check_services','full_diagnostics','gaming_perf','gaming_status',"
        "'web_search','read_file','my_failures','list_wa_manuals','search_service_manual')) "
        "FROM agent_memory WHERE length(command) > 12"),
    "akcja_wymaga_zgody": (
        "SELECT (SELECT COUNT(*) FROM action_audit WHERE status='blocked_confirmation'), "
        "(SELECT COUNT(*) FROM action_audit WHERE status='allowed')"),
    # ile poleceń w historii było NAPRAWDĘ zadaniem twórczym (186 / 1187 ≈ 0.157; na podzbiorze,
    # gdzie słowa kluczowe milczą — czyli tam, gdzie bramka jest pytana — wychodzi 0.133)
    "zadanie_tworcze": (
        "SELECT SUM(action IN ('generate_coloring','generate_music','generate_book',"
        "'generate_image','generate_campaign')), "
        "SUM(action IN ('check_machines','check_services','full_diagnostics','gaming_perf',"
        "'list_wa_manuals','web_search','search_service_manual','read_file','desktop_launch',"
        "'my_failures','analyze_code','scan_content')) FROM agent_memory WHERE length(command)>15"),
    # ile poleceń naprawdę wymagało faktów spoza rozmowy — poleciał po nie `web_search`
    # (115 / 714 ≈ 0.161)
    "wymaga_researchu": (
        "SELECT SUM(action='web_search'), "
        "SUM(action IN ('generate_coloring','generate_music','generate_book','generate_image',"
        "'generate_campaign','check_machines','check_services','gaming_perf','desktop_launch',"
        "'full_diagnostics')) FROM agent_memory WHERE length(command)>15"),
    # ile par, które tani pre-filtr (wspólny prefiks tytułu) poda dedupowi, to naprawdę
    # ten sam plik (528 / 1984 ≈ 0.27)
    "ten_sam_material": (
        "SELECT SUM(a.asset_paths=b.asset_paths), SUM(a.asset_paths<>b.asset_paths) "
        "FROM publication_queue a JOIN publication_queue b ON a.id<b.id "
        "WHERE a.asset_paths NOT IN ('','[]') AND b.asset_paths NOT IN ('','[]') "
        "AND substr(a.title,1,10)=substr(b.title,1,10)"),
    # ile par (punkt zadania × kandydat z embeddingu) System 2 uznał za pasujące. Kuźnia sądzi WSZYSTKICH
    # 15 kandydatów i każdy dostaje wynik, więc żniwa = populacja i poprawka wychodzi ~0 (08.10: 4 / 60)
    "lekcja_dotyczy_punktu": (
        "SELECT SUM(lower(actual) IN ('true','tak','1','yes')), SUM(lower(actual) NOT IN ('true','tak','1','yes')) "
        "FROM jev_outcomes WHERE qkey='lekcja_dotyczy_punktu'"),
}

# Ile wyników KAŻDEJ klasy musi być, zanim klucz dostanie kalibrację. `min_n` liczy pary, a przy rzadkiej
# klasie (lekcje: System 2 „tak” 4 na 60) Platt dopasowany do kilku trafień chodziłby jako „skalibrowany”
# i w trafianiu lekcji decydowałby sam, gdy Systemu 2 nie ma (Operator 09.10: „add”).
MIN_KLASY: dict[str, int] = {"lekcja_dotyczy_punktu": 20}


async def base_rate(qkey: str) -> float | None:
    """Udział klasy TAK w populacji, którą bramka widzi na żywo. None = nie wiem (i mówię to)."""
    sql = BASE_RATE_SQL.get(qkey)
    if not sql:
        return None
    try:
        async with aiosqlite.connect(_DB) as db:
            npos, nneg = await (await db.execute(sql)).fetchone()
    except Exception as exc:
        logger.warning("[jev] prior dla %s nie policzony (%s) — kalibracja bez poprawki", qkey, exc)
        return None
    if not (npos and nneg):
        return None
    return npos / (npos + nneg)



async def judge(state: str, keys: list[str], *, source: str = "",
                extra: dict[str, Question | dict] | None = None,
                model: str | None = None) -> dict[str, dict]:
    """Zadaj bramkom organizmu wybrane pytania o jeden stan (równolegle)."""
    unknown = [k for k in keys if k not in GATES]
    if unknown:
        raise KeyError(f"nieznane bramki: {unknown}. Dopisz je do GATES, nie twórz promptu w locie.")
    qs: dict[str, Question | dict] = {k: GATES[k] for k in keys}
    qs.update(extra or {})
    return await ask(state, qs, source=source or "gates", model=model)


_S2_LOCK = asyncio.Lock()


async def _system_two_cel(model: str | None) -> tuple[str | None, str | None]:
    """(model, węzeł) Systemu 2. Pokrętła: brain_modes.jev_system_two_model (domyślnie model
    mentora — jedyny 3/3 w benchmarku 19.09) i jev_system_two_node (domyślnie `z13`;
    pusty napis = stare zachowanie, router wybiera sam)."""
    m, n = model, "z13"
    try:
        async with aiosqlite.connect(_DB) as db:
            rows = dict(await (await db.execute(
                "SELECT key, value FROM brain_modes WHERE key IN "
                "('jev_system_two_model','jev_system_two_node','mentor_eval_model')")).fetchall())
        m = m or rows.get("jev_system_two_model") or rows.get("mentor_eval_model") or "qwen3.8:27b"
        if "jev_system_two_node" in rows:
            n = (rows["jev_system_two_node"] or "").strip() or None
    except Exception as exc:
        logger.debug("[jev] pokrętła Systemu 2 nieczytelne (%s) — domyślne", exc)
        m = m or "qwen3.8:27b"
    return m, n


async def decide(state: str, key: str, *, source: str = "",
                 escalate: bool = True, system_two_model: str | None = None) -> dict:
    """Pełna kaskada dla JEDNEJ decyzji: odruch → (gdy niepewny) myślenie.

    Zwraca sąd JEV-a wzbogacony o `escalated`, `system_two` i `final` — z zachowaniem
    obu śladów. Gdy System 2 też nie rozstrzyga, `final` jest None i to znaczy
    „pytaj operatora", a nie „rób domyślnie".
    """
    res = await judge(state, [key], source=source)
    j = res[key]
    j["escalated"] = False
    j["final"] = None if j["abstain"] else j["value"]

    if not j["abstain"] or not escalate:
        return j

    q = GATES[key]
    prompt = (f"STAN:\n{state}\n\nPYTANIE: {q.instructions}\n\n"
              "Odpowiedz wyłącznie JSON-em: "
              + ('{"odpowiedz": true|false, "pewnosc": 0.0-1.0, "dlaczego": "jedno zdanie"}'
                 if q.kind == "bool" else
                 '{"odpowiedz": "<jedna z opcji>", "pewnosc": 0.0-1.0, "dlaczego": "jedno zdanie"}'))
    if q.kind == "choice":
        prompt += "\nOpcje: " + " | ".join(q.options)
    elif q.kind == "score":
        prompt += "\nPoziomy: " + " | ".join(q.levels)

    # 2026-09-25 (operator: „ewolucji, ale działaj"): System 2 myśli na Z13 (.172, 64 GB UMA),
    # nie na .10. Na .10 Ollama trzyma JEDEN model — eskalacja wciągała 18.6 GB i wypychała
    # odruch, więc eskalację wyłączono w bramce. Teraz odruch zostaje na .10, namysł idzie
    # tam, gdzie jest pamięć. Z13 poza domem / bez modelu = NIE spadamy na .10 (to wróciłoby
    # do eksmisji) — mówimy „System 2 niedostępny" i decyzja zostaje przy operatorze.
    # Szybkość nie jest kryterium (operator: liczy się poprawny wynik) — budżet 300 s.
    s2_model, s2_node = await _system_two_cel(system_two_model)
    if s2_node:
        try:
            from app.services.ollama_router import ollama_router
            _n = getattr(ollama_router, s2_node, None)
            if not (_n is not None and _n.available and _n.enabled and _n.owns_exact(s2_model)):
                j["system_two"] = {"skipped": f"System 2 ({s2_model} na {s2_node}) niedostępny — "
                                              f"nie spadam na .10, decyzja zostaje przy operatorze"}
                j["reason"] = (j["reason"] + " | System 2 niedostępny").strip(" |")
                return j
        except Exception as exc:
            j["system_two"] = {"skipped": f"nie sprawdziłem węzła Systemu 2: {exc}"}
            return j
    try:
        from app.services.ollama_router import ollama_router
        async with _S2_LOCK:     # jeden namysł naraz — Z13 dzieli pamięć z mentorem
            text, used_model, node = await ollama_router.chat_messages(
                [{"role": "user", "content": prompt}], model=s2_model, preferred_node=s2_node,
                priority="background", think=False, format="json", timeout=300.0)
        if s2_node and node != s2_node:
            logger.warning("[jev] System 2 miał iść na %s, router oddał %s (%s)", s2_node, node, used_model)
        parsed = json.loads(text[text.find("{"): text.rfind("}") + 1])
        j["escalated"] = True
        j["system_two"] = {"answer": parsed.get("odpowiedz"), "confidence": parsed.get("pewnosc"),
                           "why": str(parsed.get("dlaczego", ""))[:300],
                           "model_used": used_model, "node": node}
        j["final"] = parsed.get("odpowiedz")
        j["abstain"] = j["final"] is None
        logger.info("[jev] %s: System 2 (%s@%s) → %r (pewność %s)", key, used_model, node,
                    j["final"], parsed.get("pewnosc"))
        # System 2 na .10 potrafi wypchnąć odruch z VRAM-u (zmierzone 2026-09-20) — budzimy
        # go z powrotem tylko wtedy, gdy namysł faktycznie poszedł na ten sam węzeł.
        if node in ("gpu", "10"):
            asyncio.create_task(jev_warm())
    except Exception as exc:  # System 2 padł — mówimy to, nie podstawiamy odruchu
        j["system_two"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        j["reason"] = (j["reason"] + " | eskalacja do Systemu 2 nie powiodła się").strip(" |")
        logger.warning("[jev] eskalacja %s padła: %s", key, exc)
    return j


# ── gotowe wejścia dla organów (tu wpina się resztę kodu) ────────────────────
async def ocen_akcje(action: str, params: dict | str, section: str = "",
                     respect_lists: bool = True, escalate: bool = True) -> dict:
    """Bramka potwierdzeń — DORADCZA. Nie podmienia `action_system.gate_action`.

    Kaskada w tej kolejności, bo tak jest taniej i uczciwiej:
      1. listy polityki (`READ_ONLY_ACTIONS`, `SIDE_EFFECT_ACTIONS`) — kod, 0 ms, 100% pewności;
      2. JEV — tylko dla akcji, których NIE MA na żadnej liście;
      3. eskalacja do Systemu 2, gdy sąd wpada w pas niepewności.

    Po co krok 2: w `action_audit` jest 225 różnych akcji, a obie listy znają 55.
    Pozostałe 189 (5 353 wywołania) nie przeszły przez ŻADNĄ politykę — po prostu
    poleciały (zmierzone 2026-09-20). To nie jest dziura w liście, to dziura
    w sposobie robienia listy: nowa ręka rodzi się szybciej, niż ktokolwiek ją
    dopisze. Odruch obejmuje nowe akcje od pierwszego wywołania i mówi, jak bardzo
    jest ich pewien.

    WPIĘTE 2026-09-20 (operator: „jev ma działać") w `action_system.gate_action`, ale
    **wyłącznie dla akcji spoza obu list** i **domyślnie DORADCZO**: sąd trafia do
    `jev_judgments` i do logu, werdykt bramki się nie zmienia. Pokrętło:
    `brain_modes.jev_gate_mode` = `off` | `advisory` (domyślnie) | `enforce`.
    Prawo ZATRZYMANIA akcji zostaje przy operatorze — `enforce` włącza tylko on
    (dług `gaming_toggle`: 21× blocked, 0× allowed — decyzja operatora, nie obchodzić).
    Do 2026-09-24 tryb doradczy wołał z `escalate=False`: eskalacja ciągnęła 18.6 GB modelu
    Systemu 2 na .10 i wypychała odruch z VRAM-u (§3 handoffu). Od 2026-09-25 System 2
    myśli na Z13 (`_system_two_cel`), więc eskalacja jest włączona także doradczo.
    """
    p = params if isinstance(params, str) else json.dumps(params, ensure_ascii=False)
    if respect_lists:
        try:
            from app.services.action_system import READ_ONLY_ACTIONS, SIDE_EFFECT_ACTIONS
            if action in READ_ONLY_ACTIONS:
                return {"key": "akcja_wymaga_zgody", "value": False, "label": "nie",
                        "confidence": 1.0, "abstain": False, "method": "polityka",
                        "reason": "akcja na liście READ_ONLY_ACTIONS", "final": False,
                        "escalated": False, "calibrated": True}
            if action in SIDE_EFFECT_ACTIONS:
                return {"key": "akcja_wymaga_zgody", "value": True, "label": "tak",
                        "confidence": 1.0, "abstain": False, "method": "polityka",
                        "reason": "akcja na liście SIDE_EFFECT_ACTIONS", "final": True,
                        "escalated": False, "calibrated": True}
        except Exception as exc:  # brak list to nie werdykt — schodzimy na odruch i mówimy to
            logger.warning("[jev] nie doczytałem list polityki (%s) — sądzę samym odruchem", exc)
    state = (f"Shadow (agent AI gospodarstwa) chce wykonać akcję.\n"
             f"akcja: {action}\nsekcja: {section}\nparametry: {p[:600]}")
    return await decide(state, "akcja_wymaga_zgody", source=f"gate:{action}", escalate=escalate)


# 28.10: osądy gate:publikacja i gate:dedup miały 0 wyników — kalibracja nie ruszała. Znacznik w notatce pozycji
# kolejki wiąże osąd z pozycją; `domknij_wyniki_jev` zapisuje skutek, gdy pozycja dojdzie do stanu końcowego.
_ZNACZNIK_RE = re.compile(r"JEV:([^:\s|]+):(material_gotowy_do_publikacji|ten_sam_material)")
_DOMKNIETE_O: float | None = None


def znacznik_jev(sad: dict | None, key: str) -> str:
    """Znacznik do notatki pozycji: ` | JEV:<judgment_id>:<pytanie>`; pusty, gdy sąd bez identyfikatora."""
    jid = (sad or {}).get("judgment_id")
    return f" | JEV:{jid}:{key}" if jid else ""


async def dopisz_znacznik(pozycja_id: str, sad: dict | None, key: str, db_path: str | None = None) -> str:
    """Dopisuje znacznik osądu do notatki pozycji kolejki (gdy podano pozycję). Zwraca znacznik."""
    zn = znacznik_jev(sad, key)
    if zn and pozycja_id:
        try:
            from app.config import settings
            async with aiosqlite.connect(str(db_path or settings.db_path), timeout=30) as db:
                await db.execute("UPDATE publication_queue SET notes=COALESCE(notes,'')||? WHERE id=?",
                                 (zn, pozycja_id))
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[jev] znacznik %s dla %s nie zapisany: %r", key, pozycja_id, exc)
    return zn


async def _ma_wynik(db, jid: str) -> bool:
    try:
        return bool(await (await db.execute(
            "SELECT 1 FROM jev_outcomes WHERE judgment_id=? LIMIT 1", (jid,))).fetchone())
    except Exception:  # noqa: BLE001 — brak tabeli/kolumny = nie wiem o wyniku, chroni znacznik JEV_WYNIK
        return False


async def domknij_wyniki_jev(db_path: str | None = None, min_odstep: float = 0.0) -> int:
    """Skutek osądów publikacji: pozycje ze znacznikiem JEV i statusem published/rejected/abandoned → record_outcome.

    material_gotowy_do_publikacji: actual = published. ten_sam_material: actual = odrzucona albo porzucona jako
    powtórka. Zapisany wynik zostawia w notatce `JEV_WYNIK:<id>:<pytanie>`, więc drugi przebieg go nie powtórzy.
    `min_odstep` (s) — najwyżej raz na tyle. Zwraca liczbę zapisów; wyjątek = jedna linia w logu."""
    global _DOMKNIETE_O
    teraz = time.monotonic()
    if min_odstep and _DOMKNIETE_O is not None and teraz - _DOMKNIETE_O < min_odstep:
        return 0
    _DOMKNIETE_O = teraz
    zapisane = 0
    try:
        from app.config import settings
        from app.services.jev_service import record_outcome
        async with aiosqlite.connect(str(db_path or settings.db_path), timeout=30) as db:
            rows = await (await db.execute(
                "SELECT id, status, COALESCE(notes,'') FROM publication_queue "
                "WHERE status IN ('published','rejected','abandoned') AND notes LIKE '%JEV:%'")).fetchall()
            for pid, status, notes in rows:
                for jid, key in dict.fromkeys(_ZNACZNIK_RE.findall(notes)):
                    wpis = f"JEV_WYNIK:{jid}:{key}"
                    if wpis in notes or await _ma_wynik(db, jid):
                        continue
                    if key == "material_gotowy_do_publikacji":
                        actual = status == "published"
                    else:
                        actual = status == "rejected" or (status == "abandoned" and "DUPLIKAT" in notes.upper())
                    await record_outcome(jid, actual,
                                         note=f"domknij_wyniki_jev: {pid} {status}")
                    notes += f" | {wpis}"
                    await db.execute("UPDATE publication_queue SET notes=COALESCE(notes,'')||? WHERE id=?",
                                     (f" | {wpis}", pid))
                    await db.commit()
                    zapisane += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jev] domknij_wyniki_jev przerwane: %r", exc)
    return zapisane


async def ocen_material(opis: str, sciezka: str = "", audio: str = "", klatki: str = "",
                        pozycja_id: str = "") -> dict:
    state = (f"Materiał do publikacji.\nopis/etykieta: {opis or '(BRAK)'}\nplik: {sciezka}\n"
             f"ścieżka audio: {audio or '(BRAK)'}\nco widać na klatkach: {klatki or '(nieznane)'}")
    j = await decide(state, "material_gotowy_do_publikacji", source="gate:publikacja")
    j["znacznik"] = await dopisz_znacznik(pozycja_id, j, "material_gotowy_do_publikacji")
    return j


async def ocen_duplikat(a: str, b: str) -> dict:
    j = await decide(f"WPIS A:\n{a}\n\nWPIS B:\n{b}", "ten_sam_material", source="gate:dedup")
    j["znacznik"] = znacznik_jev(j, "ten_sam_material")
    return j


async def ocen_wiadomosc(nadawca: str, temat: str, tresc: str) -> dict[str, dict]:
    """Triage poczty jednym strzałem: pilność + pieniądze + czy wymaga researchu."""
    state = f"Wiadomość.\nod: {nadawca}\ntemat: {temat}\ntreść:\n{tresc[:1500]}"
    return await judge(state, ["pilnosc", "dotyczy_pieniedzy", "wymaga_researchu"],
                       source="gate:poczta")


async def ocen_zadanie(tekst: str) -> dict[str, dict]:
    """Routing jednym strzałem: myślenie? twórcze? research? który organ?"""
    state = f"Zadanie od operatora:\n{tekst[:1500]}"
    return await judge(state, ["zadanie_wymaga_myslenia", "zadanie_tworcze",
                               "wymaga_researchu", "organ"], source="gate:routing")
