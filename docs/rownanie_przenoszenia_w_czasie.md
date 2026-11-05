# Równanie przenoszenia w czasie — organ .130 (SHADOWGUEST) w trójkącie decyzyjnym (2026-08-31)

Operator: *„mamy kolejny organ, dodaj go do trójkątów decyzyjnych nieeuklidesowych z równaniem
przenoszenia w czasie — nie tylko transport cost do przodu: do tyłu i teraźniejszość"*
+ *„50.130 nowy adres"*.

## 1. Przeadresowanie organu (odkryte przy okazji, krytyczne)

**SHADOWGUEST przeprowadził się `192.168.2.8` → `10.0.0.130`.** Ta sama maszyna —
`node: shadowguest`, agent 1.1.0, RTX 5070, 33.4 GB RAM, **ciągły uptime 152673 s**. Stary adres
**martwy** (brak odpowiedzi). Cała konfiguracja z 30.08 celowała w nieistniejący adres.

Poprawione: `config.py` (agent+hand URL), `network.py` (`_DEVICES`, `_GROUND_TRUTH`, `_LAN_HOSTS`,
docstringi), `guest_hand.py` (`GUEST_IP`), `node_hardware_truth.py` (`NODES["2.8"].ip`),
`runtime_topology.py`, `Network.tsx` (`GUEST_IP`).

Klucz węzła: początkowo zostawiłem `2.8`, ale na życzenie operatora („chcę widzieć .130 jak
inne .253/.10") zmieniony na **`130`** — spójnie z resztą floty (oktet IP). Dziennik trajektorii
zmigrowany (`node:"2.8"` → `"130"`, backup `.bak_key28`), żeby nauka się nie osierociła.
Organ wszedł do `10.0.0.0/24`, więc wyjątek zakresu w `lan_actuator` nie jest już potrzebny
(zostaje jako nieszkodliwy fallback).

## 2. Równanie — trzy człony czasu zamiast jednego

Dotąd `transport_cost` liczył wyłącznie **przyszłość** (capacity × warm × thermal). Doktryna
trójkąta (`docs/personas-and-decision-triangle-2026-05-23.md`) mówi o pełnym przenoszeniu:
*„co decyzja zamyka, co otwiera w t+Δ"* + *„synthesizer musi widzieć krzywiznę, nie styczną"*.

```
transport_cost = TERAŹNIEJSZOŚĆ × PRZYSZŁOŚĆ × PRZESZŁOŚĆ
```

| Człon | Składniki | Sens |
|---|---|---|
| **TERAŹNIEJSZOŚĆ** | `capacity` (hiperboliczna kara przy free≈need) × `thermal` (Rényi-Tsallis proxy) × `stale` (×1.5 gdy telemetria przeterminowana) | Gdzie system JEST. Stale = decyzja na nieaktualnej prawdzie = ryzyko teraz. |
| **PRZYSZŁOŚĆ** | `warm` (model rezydentny 0.15 / cold 1.0) × `lock_in` (shadow 1.0, operator 1.6, family 1.9) | Co decyzja OTWIERA i ZAMYKA. Wejście na maszynę człowieka zamyka ją na jego powrót — realny koszt re-entry, nie metafora. |
| **PRZESZŁOŚĆ** | `retro` z trajektorii: sukcesy → premia (min 0.85), porażki z rzędu → kara, `≥3` → **CTL** `2^n` | Krzywizna trajektorii. Blokuje rotację wokół zepsutego węzła. |

### Trajektoria jest REALNA, nie zmyślona
Człon przeszłości potrzebuje danych, więc doszedł trwały dziennik
`logs/relocate_trajectory.jsonl` (JSONL, okno 8 zdarzeń/węzeł):
- `pick_node()` zapisuje każdą decyzję (`outcome: "picked"`),
- `record_outcome(node, "ok"|"fail", note)` — **publiczne API dla strony wykonawczej**, żeby
  zamknąć pętlę uczenia. Bez wołania tego z executora/ręki człon przeszłości zostanie neutralny
  (1.0) — uczciwie, ale bez nauki. **To jest otwarty koniec do dopięcia.**

### CTL → BREAK (wierzchołek trójkąta)
`pick_node` zwraca teraz `vertex`:
- **COMMIT** — normalna relokacja do najtańszego węzła;
- **BREAK** — gdy **wszystkie** zdolne węzły siedzą w CTL: `decision=None`, komunikat
  *„nie relokuję, napraw przyczynę zamiast iterować"*. To jest dokładnie break-out z doktryny
  (time-travel synthesizer wykrywa closed timelike loop → BREAK, nie kolejna synteza).

Zwracane jest też pole `czas: {przeszlosc, terazniejszosc, przyszlosc, trajektoria}` — decyzja
jest czytelna, nie jest jedną liczbą bez rozbioru.

## 3. Weryfikacja

**Test jednostkowy** (izolowany dziennik, posprzątany po sobie):
1. render/encode → wygrywa **.130** (`cost 1.25`, owner=shadow → `lock_in 1.0`), .10 `2.00`;
   .253 `inf` (bramka gaming-mode operatora — człowiek > optymalizacja).
2. .130 zawodzi 3× → `past 1.0 → 8.0`, `cost 1.25 → 10.0`, `ctl=True`, organizm przechodzi na .10.
3. wszystkie węzły w CTL → `decision=None`, `vertex=BREAK`.

**Live na .10** (po restarcie):
- `hardware-truth`: `130 10.0.0.130 ok=True free=10.92 temp=42 host=shadowguest` — organ żyje
  pod nowym adresem, 6 węzłów w prawdzie sprzętowej.
- `relocate?need_gb=6&kind=encode`: decyzja `172`, `vertex=COMMIT`,
  `teraz×0.208 · przyszłość×1.6 · przeszłość×1.0`; ranking 172 `0.334` < .130 `1.22` < 253 `3.57`
  < 10 `6.70`. Widać działanie `lock_in`: .172 (operator) płaci ×1.6, .130 (shadow) ×1.0.

Frontend przebudowany i wdrożony na .253 i .10 (`GUEST_IP` = nowy adres).

## 4. Domknięcie pętli uczenia (zrobione 2026-08-31, ta sama sesja)

Człon przeszłości bez skutków byłby ozdobą. Wpięte **dwie realne drogi wykonania**:

- **`lan_actuator.apply_lan_action`** — tuż po ustaleniu `exit_code`, przed zapisem audytu:
  `node_key_for_ip(target_ip)` → `record_outcome(ok|fail, "<akcja> rc=<n>")`. Best-effort:
  nauka nie może wywrócić egzekucji ani audytu.
- **`guest_hand`** (ręka .130, druga droga na ten sam węzeł — inaczej organizm uczyłby się
  z połowy swoich działań):
  - `guest_exec` → `ok|fail` po `rc`; **nieosiągalna ręka też jest porażką WĘZŁA**;
  - `guest_job_start` → `picked` (neutralne!) — start renderu **nie jest** sukcesem, zadanie
    leci odczepione godzinami; zapisanie „ok" byłoby kłamstwem;
  - `guest_job_status` → dopiero przy statusie terminalnym `done|failed` dopisuje prawdziwy
    skutek, **dokładnie raz** (`skutek_juz_zapisany(node, "job=<id>")`). Status bywa odpytywany
    w pętli, a liczba zapytań nie jest liczbą faktów — bez tej bramki jeden render zapisałby
    dziesiątki „ok" i sztucznie wyzerował człon przeszłości.

Pomocnicze w `node_hardware_truth`: `node_key_for_ip()` (strona wykonawcza operuje ADRESAMI,
trajektoria per WĘZEŁ) i `skutek_juz_zapisany()` (dedup).

**Weryfikacja pętli:**
- mapowanie: `.130→130`, `.10→10`, `.253→253`, obcy IP→`None`;
- dedup: 5 odpytań o ten sam job = **1 fakt**;
- wpływ na koszt: 1 sukces `1.163` → 3 porażki `9.796` (`ctl=True`) → naprawa `1.102`, CTL gaśnie;
- **live na .10:** dziennik `logs/relocate_trajectory.jsonl` zawiera realny wpis `picked` z
  decyzji `relocate` (node 172, cost 0.334), a wywołanie ręki .130 (niezainstalowanej) zapisało
  `fail :: exec nieosiągalny` — węzeł uczy się z prawdziwej porażki.

## 5. Otwarte / świadomie niezrobione

1. ~~Nazwa „cyber"~~ — operator: „cyber olej". Temat zamknięty, organ zostaje SHADOWGUEST.
2. ~~Paczka ręki ze starym adresem~~ — **ZROBIONE 2026-08-31**: pakiet przemianowany
   `hands/shadowguest_28/` → `hands/shadowguest_130/` (pliki `shadow_hand_130.py`,
   `install_shadow_hand_130.ps1`), adresy i firewall poprawione (tylko `10.0.0.0/24` —
   podsieć `.2.x` już nieaktualna), zip `reka_shadowguest_130.zip` na .10, USB odświeżone
   (`D:\ShadowAI_Reka_130`, stary katalog usunięty). Smoke-test przemianowanej ręki: health/401/exec OK.
   Endpoint `/hand/package` i strona `/hand/get` zweryfikowane live (200, CRC OK, 7 plików).
