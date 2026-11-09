#!/usr/bin/env python3
"""Buduje publiczny pakiet ShadowAI z repo ~/bbl1888shadow: kopiuje wybrane moduły i dokumenty, czyści dane prywatne, skanuje wycieki → SKAN.md.
Uruchamiaj ponownie po zmianach; NIC nie wypycha (repo bez remote). 05.11.2026, sesja Claude .10, na polecenie operatora („pakiet Shadow, co można pokazać na GitHub”)."""
import re, shutil
from pathlib import Path
SRC = Path("/home/bbl1888/bbl1888shadow"); DST = Path(__file__).resolve().parent
PLIKI = {
  "backend/app/services/reka_kuznia.py": "src/kuznia/reka_kuznia.py",          # kuźnia: Shadow pisze własne ręce (nauczyciel/uczeń, testy, bramka)
  "backend/app/services/node_hardware_truth.py": "src/trojkat/node_hardware_truth.py",  # trójkąt decyzyjny: TERAŹNIEJSZOŚĆ × PRZYSZŁOŚĆ × PRZESZŁOŚĆ
  "backend/app/services/jev_service.py": "src/jev/jev_service.py",              # JEV: System 1 / System 2, kalibracja
  "backend/app/services/jev_gates.py": "src/jev/jev_gates.py",
  "backend/rag/quadrant_config.py": "src/rag/quadrant_config.py",               # multi-RAG w kwadrantach
  "backend/app/services/organism_probes.py": "src/organizm/organism_probes.py", # samoaudyt organizmu
  "docs/rownanie_przenoszenia_w_czasie_2026-08-31.md": "docs/rownanie_przenoszenia_w_czasie.md",
  # 09.11 (operator: „GitHub raczej jest do zaktualizowania, nie uwzględnia wszystkiego”) — moduły dojrzałe po 05.11:
  "backend/app/services/ollama_router.py": "src/router/ollama_router.py",            # routing modeli po węzłach (zapas, budżet, reguły węzłów)
  "backend/app/services/tension_engine.py": "src/organizm/tension_engine.py",        # napięcia: sprzeczności odpowiedzi vs blokada akcji
  "backend/app/services/operator_memory.py": "src/pamiec/operator_memory.py",        # pamięć rozmowy (warstwy, korekty, świeżość)
  "backend/app/services/rag_service.py": "src/rag/rag_service.py",                   # wiedza trwała (lekcje dla heartbeatu)
  "backend/app/services/sciagala_nowe_nagrania_hand.py": "src/magazyn/sciagala_nowe_nagrania_hand.py",  # 3 magazyny, SHA256 przed kasowaniem
  "backend/app/services/teledysk_inspiracje_hand.py": "src/teledysk/teledysk_inspiracje_hand.py",       # artistic flow: inspiracje
  "backend/app/services/teledysk_koncepcja_hand.py": "src/teledysk/teledysk_koncepcja_hand.py",         # pomysł, motyw, puenta
  "backend/app/services/teledysk_sceny_hand.py": "src/teledysk/teledysk_sceny_hand.py",                 # sceny partiami z bramką
  "backend/app/services/teledysk_plan_rezyserski_hand.py": "src/teledysk/teledysk_plan_rezyserski_hand.py",  # autor + krytyk, stuki na słowach
  "backend/app/services/teledysk_kadry_qwen_hand.py": "src/teledysk/teledysk_kadry_qwen_hand.py",       # kadry z kontrolą wizji
  "backend/app/services/teledysk_ruchu_hand.py": "src/teledysk/teledysk_ruchu_hand.py",                 # ruch Wan na węzłach
  "backend/app/services/shorty_gaming_2_hand.py": "src/shorty/shorty_gaming_2_hand.py",                 # shorty PL/EN z klipów, tytuły z klatek
  "backend/app/services/oko_codziennie_hand.py": "src/shorty/oko_codziennie_hand.py",                   # seria OKO MASZYNY (maszyna typuje akcję)
  "backend/app/services/zwiad_organizmu_hand.py": "src/organizm/zwiad_organizmu_hand.py",               # Shadow szuka w sobie, zanim powie „nie ma”
  "backend/app/services/zwiad_wezlow_hand.py": "src/organizm/zwiad_wezlow_hand.py",                     # zwiad po maszynach sieci
  "backend/app/services/odbior_2_hand.py": "src/organizm/odbior_2_hand.py",                             # odbiór produktu na dysk operatora (napisana przez kuźnię)
  "backend/app/services/pod_factory.py": "src/merch/pod_factory.py",                                    # fabryka print-on-demand
  "backend/app/services/pod_gemini.py": "src/merch/pod_gemini.py",                                      # serie z ilustracją i wersjami językowymi
  "/home/bbl1888/teledysk_java/flow/ZASADY_ARTISTIC_FLOW.md": "docs/ZASADY_ARTISTIC_FLOW.md",
}
ZAMIANY = [
  (r"192\.168\.50\.(\d{1,3})", r"10.0.0.\1"), (r"/home/bbl1888", "/opt/shadow"), (r"\bbbl1888shadow\b", "shadow"), (r"\bbbl1888\w*", "shadow"),
  (r"\bonseb\b", "operator"), (r"sebastian-kalinowski", "operator"), (r"(?i)\bsebastian\w*", "Operator"), (r"(?i)\bkalinowsk\w*", "Operator"),
  (r"\bJa[śs]\w*|\bJasi\w*|\bJA[ŚS]\b", "Dziecko"), (r"(?i)jasi[oa]", "dziecko"), (r"\b_jas_", "_dz_"), (r"\bjas_", "dz_"), (r"\"ja[śs]\"", "\"dziecko\""), (r"\bHani\w*", "Dziecko2"), (r"\bLes(?:io|ia|iowi)\b|\bLech\w*", "Brat"), (r"\bCzar\w*|\bWiktor\w*", "Kolega"),
  (r"(?i)\b(gruszkowsk\w*|promogaz\w*|dept\w*|bogacz\w*|bajak\w*|lig[e]?n[d]?z\w*|kukurin\w*|s[łl]obion\w*|ad[aś]\w*)\b", "Klient"),
  (r"(?i)\becotek\w*", "Firma"), (r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com"), (r"\b(?:48)?\d{9}(?:@(?:c\.us|lid))?\b", "000000000"),
  (r"\b\d{12,16}@lid\b", "0@lid"), (r"\b\d{10}\b", "0000000000"),
  (r"(?i)((?:token|password|passwd|haslo|hasło|secret|api_key|bearer)\s*[=:]\s*)[\"']?[A-Za-z0-9_\-\.]{8,}[\"']?", r"\1<UKRYTE>"),
]
WZ_SKAN = {"ip_prywatne": r"\b(?:192\.168|172\.(?:1[6-9]|2\d|3[01])|10\.\d+)\.\d+\.\d+\b", "email": r"[\w.+-]+@[\w-]+\.\w+",
           "telefon": r"\b\d{9,11}\b", "sekret": r"(?i)(token|password|haslo|hasło|secret|api[_-]?key|bearer)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{12,}",
           "sciezki_domowe": r"/home/\w+|C:\\\\Users\\\\\w+", "imiona": r"(?i)\b(sebastian|kalinowsk|jaś|jasio|hania|lesio|onseb|bbl1888)"}
def czysc(t):
    for a, b in ZAMIANY:
        t = re.sub(a, b, t)
    return t
skan = ["# Skan wycieków pakietu publicznego\n", "Wzorce szukane PO czyszczeniu (zostały do ręcznego przejrzenia; 10.0.0.x to zamaskowane adresy LAN):\n",
        "| plik | " + " | ".join(WZ_SKAN) + " |", "|---|" + "---|" * len(WZ_SKAN)]
for src, dst in PLIKI.items():
    t = czysc((Path(src) if src.startswith("/") else SRC / src).read_text(encoding="utf-8", errors="replace"))
    p = DST / dst; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(t, encoding="utf-8")
for p in sorted(x for x in DST.rglob("*") if x.is_file() and x.suffix in (".py", ".md") and x.name not in ("SKAN.md", "zbuduj_pakiet.py")):
    t = p.read_text(encoding="utf-8", errors="replace")
    skan.append(f"| {p.relative_to(DST)} | " + " | ".join(str(len(re.findall(w, t))) for w in WZ_SKAN.values()) + " |")
(DST / "SKAN.md").write_text("\n".join(skan) + "\n", encoding="utf-8")
print("\n".join(skan))
