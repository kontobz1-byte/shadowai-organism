"""Prawda sprzętowa per-node + hook relokacji zadań (organizm Shadow).

Operator 2026-06-18 (poligon AGI): każdy node czytany ze SWOJEGO NATYWNEGO
źródła — "hardware real source", nie kłamliwy Win32 Klient (pamięta 4GB dla
APU). Zasada: relokuj zadanie do node'a z REALNIE wolnym compute, z uwzględnieniem
transport_cost (warm model = tani, cross-node cold = drogi).

Kanały (zweryfikowane):
  .10  RTX 5060Ti 16G  → nvidia-smi LOKALNIE (gpu_probe)            — instant
  .253 RTX 5080  16G   → nvidia-smi przez winrm (ręka Shadow)       — wolne → cache
  .172 AMD 395 AI+ 64G → ollama /api/ps (rezydencja=realne UMA)     — HTTP, szybkie
  .172 host (CPU/RAM/GTT/board/dyski) → win_telemetry SSH (Linux od 29.10) — cache-first, tło
  .237 GTX 1650Ti 4G   → win_telemetry WinRM + nvidia-smi           — cache-first, tło
  .197 RTX 4060 8G     → win_telemetry WinRM (host bywa wyłączony)  — uczciwe „nieosiągalny"
  130  RTX 5070 12G    → shadow-winagent :8002 (bearer, 10.0.0.130) — HTTP, szybkie

Operator 2026-08-23: „Shadow ma WIDZIEĆ, ROZUMIEĆ i WPŁYWAĆ na zasoby". Węzły .237/.197
przestają być białą plamą — wchodzą do tej samej prawdy sprzętowej co rdzeń, więc widzi je
brain_core (topologia w prompcie) i hook relokacji. Kanał WinRM na .172/.237 zweryfikowany
LIVE 2026-08-23 — wcześniejsza notatka „winrm port closed" na .172 była nieprawdziwa.

Board-sensory (temps/fany/napięcia ASUS Armoury / Gigabyte) = osobny kanał
(LibreHardwareMonitor web-JSON, do wdrożenia) — TU nie są wymagane do relokacji.

Cache TTL bo winrm .253 mule — lekcja regresu /api/network/devices (12.5s).
Moduł jest CZYTANY przez router/Shadow (propose), NIE nadpisuje cursed ollama_router.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TTL = 15.0
_CPU_BRAND_10: Optional[str] = None

# Statyczna prawda sprzętowa (total) + kanał. free_gb liczone na żywo.
# inference = czy node ma runtime modeli (ollama). Bramka relokacji: NIE wysyłaj modelu tam,
#   gdzie nie ma go czym uruchomić (.237/.197 to węzły encode/render, nie inference).
# owner   = kto ma pierwszeństwo do zasobu. "operator" → gaming-gate (człowiek > optymalizacja);
#   "shadow" → maszyna wyłącznie Shadow (.237, potwierdzone przez operatora 2026-08-23).
#   .172 od 29.10 = Linux, czysty węzeł modeli → "shadow" (operator 29.10 „1.”): lock_in 1.6 i powrót ×1.5
#   liczone z WŁASNEGO GPU Shadowa karały węzeł, przy którym nikt nie siedzi.
NODES: dict[str, dict[str, Any]] = {
    "10":  {"ip": "10.0.0.10",  "gpu": "RTX 5060Ti",      "total_gb": 16.0, "channel": "nvidia-smi-local", "kind": "nvidia",  "inference": True,  "owner": "shadow",   "role": "Core / Backend / Inference"},
    "253": {"ip": "10.0.0.253", "gpu": "RTX 5080",        "total_gb": 16.0, "channel": "nvidia-smi-winrm", "kind": "nvidia",  "inference": True,  "owner": "operator", "role": "Senses / Frontend / Gaming"},
    "172": {"ip": "10.0.0.172", "gpu": "AMD 395 AI+ UMA", "total_gb": 64.0, "channel": "ollama-uma",       "kind": "amd-uma", "inference": True,  "owner": "shadow",   "role": "Heavy Brain / Reasoning"},
    "237": {"ip": "10.0.0.237", "gpu": "GTX 1650 Ti",     "total_gb": 4.0,  "channel": "winrm-cim",        "kind": "nvidia",  "inference": False, "owner": "shadow",   "role": "Encode / NVENC"},
    "197": {"ip": "10.0.0.197", "gpu": "RTX 4060 Laptop", "total_gb": 8.0,  "channel": "winrm-cim",        "kind": "nvidia",  "inference": False, "owner": "family",   "role": "Render / NVENC (laptop Dziecko)"},
    # Operator 2026-08-30: SHADOWGUEST = organ 2.8, osobna podsieć .2.x, własny agent
    # (shadow-winagent 1.1.0 :8002, bearer). Wchodzi do TEJ SAMEJ prawdy sprzętowej co rdzeń —
    # bez tego był białą plamą: robił rendery/montaże, a organizm go nie widział ani nie umiał
    # mu nic zlecić (hook relokacji nie znał węzła => zero wpływu na decyzje).
    "130": {"ip": "10.0.0.130", "gpu": "RTX 5070",        "total_gb": 12.0, "channel": "winagent-guest",   "kind": "nvidia",  "inference": True,  "owner": "shadow",   "role": "Czat gemma4 / Render / Montaż UE5 / NVENC"},
}


def _cache_get(key: str) -> Optional[dict[str, Any]]:
    hit = _CACHE.get(key)
    if hit and (time.monotonic() - hit[0]) < _TTL:
        return hit[1]
    return None


def _cache_put(key: str, val: dict[str, Any]) -> None:
    _CACHE[key] = (time.monotonic(), val)


def _cpu_brand_10() -> Optional[str]:
    """model name z /proc/cpuinfo — czytane raz, nie zmienia sie w trakcie zycia procesu."""
    global _CPU_BRAND_10
    if _CPU_BRAND_10 is None:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        _CPU_BRAND_10 = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
        _CPU_BRAND_10 = _CPU_BRAND_10 or ""
    return _CPU_BRAND_10 or None


def _gigabyte_board_10() -> dict[str, Any]:
    """PELNE sensory plyty Gigabyte B650M GAMING X AX na .10 (lm-sensors: ITE + AMD CCD).

    Tccd = REALNA temp rdzeni; Tctl ma offset (project_amd_tctl_temp_misreport_fix).

    Operator 2026-08-31: wczesniej ta funkcja brala z calej plyty TYLKO Tccd1/Tccd2 i
    wyrzucala reszte — organizm nie widzial ani jednego wentylatora, wiec nie mial jak
    zauwazyc zapchanego chlodzenia. Super-I/O ITE tej plyty nie jest rozpoznawany przez
    in-tree it87 (0x8689/0x8686/0x8688 => "No such device"), wchodzi dopiero jako
    force_id=0x8628 (/etc/modprobe.d/it87-gigabyte-b650m.conf). Mapa rejestrow
    zweryfikowana krzyzowo: temp1-6 z it8628 pokrywaja sie z gigabyte_wmi
    (38/46/62/39/44/45 vs 37/46/63/39/44/45) => to nie sa smieci.

    UWAGA na napiecia: in0..in6 to SUROWE ADC bez dzielnikow tej plyty (wszystkie ~1.2-2.0 V,
    max 3.06 V) — NIE sa to szyny 12V/5V/Vcore i nie wolno ich tak etykietowac.
    Realnie przeskalowane przez sterownik sa tylko 3VSB i Vbat — i tylko one ida jako `volts`.

    Ksztalt zgodny z .253 (cpu_tctl_c / fans_rpm / mb / cpu), bo mozg ma widziec KAZDY wezel
    w jednym ksztalcie.
    """
    try:
        import json as _json
        import subprocess
        raw = subprocess.check_output(["sensors", "-j"], text=True, timeout=4)
        data = _json.loads(raw)
    except Exception:
        return {}

    out: dict[str, Any] = {"mb": "Gigabyte B650M GAMING X AX", "bios": "FB9",
                           "cpu": _cpu_brand_10()}
    fans: dict[str, int] = {}
    temps: dict[str, float] = {}
    volts: dict[str, float] = {}
    volts_raw: dict[str, float] = {}
    nvme_c: list[float] = []

    def _first(sub: dict) -> Optional[float]:
        """pierwsza wartosc *_input z bloku cechy (reszta to progi min/max/alarm)."""
        for k, v in sub.items():
            if k.endswith("_input") and isinstance(v, (int, float)):
                return round(float(v), 1)
        return None

    for chip, vals in (data or {}).items():
        if not isinstance(vals, dict):
            continue
        for feat, sub in vals.items():
            if not isinstance(sub, dict):
                continue
            val = _first(sub)
            if val is None:
                continue
            if chip.startswith("k10temp"):
                if feat == "Tctl":
                    out["cpu_tctl_c"] = val
                elif feat == "Tccd1":
                    out["cpu_tccd1_c"] = val
                elif feat == "Tccd2":
                    out["cpu_tccd2_c"] = val
            elif chip.startswith("it8"):
                if feat.startswith("fan"):
                    fans[feat] = int(val)
                elif feat.startswith("temp"):
                    temps[feat] = val
                elif feat in ("3VSB", "Vbat"):
                    volts[feat] = val
                elif feat.startswith("in"):
                    volts_raw[feat] = val
            elif chip.startswith("nvme"):
                if feat == "Composite":
                    nvme_c.append(val)
            elif chip.startswith("r8169"):
                out["lan_c"] = val
            elif chip.startswith("gigabyte_wmi") and not temps:
                # fallback gdy it87 nie wstal: WMI daje te same 6 temp, ale zero fanow
                temps[feat] = val

    if fans:
        out["fans"] = fans
        out["fans_rpm"] = [fans[k] for k in sorted(fans)]
        out["fans_stopped"] = [k for k, v in fans.items() if v == 0]
    if temps:
        out["board_temps_c"] = temps
        out["board_temp_max_c"] = max(temps.values())
    if volts:
        out["volts"] = volts
    if volts_raw:
        out["volts_raw_adc"] = volts_raw
    if nvme_c:
        out["nvme_c"] = nvme_c
    out["sensors_source"] = "it87(force_id=0x8628)+k10temp" if fans else "gigabyte_wmi+k10temp"
    return out


def _host_10() -> dict[str, Any]:
    """CPU/RAM/dysk/uptime .10 z psutil — ten sam kształt `host` co węzły WinRM.

    Operator 2026-08-23: mózg ma widzieć KAŻDY węzeł w jednym kształcie, nie rdzeń inaczej
    niż resztę. cpu_percent(interval=None) = odczyt bez blokowania (różnica od ostatniego wołania).
    """
    try:
        import psutil, platform as _pl, time as _t
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        freq = psutil.cpu_freq()
        return {
            "hostname": _pl.node(), "platform": f"{_pl.system()} {_pl.release()}",
            "cpu_usage_pct": psutil.cpu_percent(interval=None),
            "cpu_count_physical": psutil.cpu_count(logical=False),
            "cpu_count_logical": psutil.cpu_count(logical=True),
            "cpu_freq_mhz": round(freq.current, 1) if freq else None,
            "cpu_freq_max_mhz": round(freq.max, 1) if freq and freq.max else None,
            "memory_total_gb": round(vm.total / 1024**3, 1),
            "memory_used_gb": round((vm.total - vm.available) / 1024**3, 1),
            "memory_available_gb": round(vm.available / 1024**3, 1),
            "memory_usage_pct": vm.percent,
            "disk_total_gb": round(du.total / 1024**3, 1),
            "disk_used_gb": round(du.used / 1024**3, 1), "disk_usage_pct": du.percent,
            "uptime_seconds": int(_t.time() - psutil.boot_time()),
        }
    except Exception as exc:
        return {"error": f"psutil: {exc!r}"}


async def _host_winagent(ip: str, port: int = 8002) -> dict[str, Any]:
    """CPU/RAM/dysk/uptime z WinAgenta (.253 :8002) → wspólny kształt `host`.

    Timeout 8 s, nie 3.5: WinAgent liczy metryki ~2 s, a przy równoległych sondach
    (LHM + WinRM w tle) potrafi się rozjechać — za krótki timeout gubił cały blok hosta .253.
    """
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            d = (await c.get(f"http://{ip}:{port}/metrics")).json()
    except Exception as exc:
        return {"error": f"winagent :{port}: {exc!r}"}
    return {
        "hostname": d.get("hostname"), "platform": d.get("platform"),
        "platform_version": d.get("platform_version"),
        "cpu_brand": d.get("cpu_brand"), "cpu_usage_pct": d.get("cpu_usage_pct"),
        "cpu_count_physical": d.get("cpu_count_physical"),
        "cpu_count_logical": d.get("cpu_count_logical"),
        "cpu_freq_mhz": d.get("cpu_freq_mhz"), "cpu_freq_max_mhz": d.get("cpu_freq_max_mhz"),
        "memory_total_gb": d.get("memory_total_gb"), "memory_used_gb": d.get("memory_used_gb"),
        "memory_available_gb": d.get("memory_available_gb"),
        "memory_usage_pct": d.get("memory_usage_pct"),
        "disk_total_gb": d.get("disk_total_gb"), "disk_used_gb": d.get("disk_used_gb"),
        "disk_usage_pct": d.get("disk_usage_pct"), "disks": d.get("disks"),
        "uptime_seconds": d.get("uptime_seconds"),
    }


async def _probe_10() -> dict[str, Any]:
    """nvidia-smi lokalnie przez gpu_probe + sensory płyty Gigabyte (Tccd) + host psutil."""
    from app.services.gpu_probe import probe_gpus
    res = probe_gpus()
    res = await res if asyncio.iscoroutine(res) else res
    g = (res or {}).get("gpu") or {}
    free = float(g.get("memory_free_gb") or 0.0)
    board = _gigabyte_board_10()
    return {"free_gb": round(free, 2), "temp_c": g.get("temp_c"),
            "util_pct": g.get("utilization_pct"), "resident": [], "ok": bool(g),
            "board": board or None, "host": _host_10()}


def _num(v: str) -> Optional[float]:
    """LHM Value '1410,0 MB' → 1410.0 (przecinek dziesiętny PL)."""
    try:
        return float(str(v).split()[0].replace(",", "."))
    except Exception:
        return None


def _lhm_hw_node(node: dict, want: str) -> Optional[dict]:
    """Znajdź węzeł-hardware (np. 'NVIDIA') w drzewie LHM."""
    if want.lower() in (node.get("Text", "")).lower() and node.get("Children"):
        return node
    for c in node.get("Children", []):
        r = _lhm_hw_node(c, want)
        if r:
            return r
    return None


def _lhm_find(node: dict, text_exact: str) -> list[str]:
    out = []
    if node.get("Text", "").strip() == text_exact and node.get("Value", "").strip():
        out.append(node["Value"])
    for c in node.get("Children", []):
        out += _lhm_find(c, text_exact)
    return out


async def _lhm_253() -> Optional[dict[str, Any]]:
    """ASUS board + GPU via LibreHardwareMonitor web-JSON :8085 (szybkie, bogate)."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            data = (await c.get("http://10.0.0.253:8085/data.json")).json()
    except Exception:
        return None
    gpu = _lhm_hw_node(data, "NVIDIA")
    cpu = _lhm_hw_node(data, "Ryzen") or _lhm_hw_node(data, "AMD Ryzen")
    free_mb = next((_num(v) for v in _lhm_find(gpu or {}, "GPU Memory Free") if _num(v)), None)
    gpu_temp = next((_num(v) for v in _lhm_find(gpu or {}, "GPU Core") if v and "°C" in v), None)
    cpu_temp = next((_num(v) for v in _lhm_find(cpu or {}, "Core (Tctl/Tdie)") if _num(v)), None)
    fans = [v for i in range(1, 8) for v in _lhm_find(data, f"Fan #{i}") if "RPM" in v]
    if free_mb is None:
        return None
    # GPU Package = pobór karty w LHM („34,8 W”; _num zjada przecinek) — przegląd Claude 03.11
    gpu_w = next((_num(v) for v in _lhm_find(gpu or {}, "GPU Package") if v and "W" in v), None)
    return {"free_gb": round(free_mb / 1024, 2), "temp_c": gpu_temp, "util_pct": None,
            "resident": [], "ok": True, "power_w": gpu_w,
            "board": {"cpu_tctl_c": cpu_temp, "gpu_core_c": gpu_temp,
                      "fans_rpm": [int(_num(f)) for f in fans if _num(f) is not None],
                      "mb": "ASUS ROG STRIX X870-F", "cpu": "Ryzen 7 9800X3D"}}


def _moc_w(v: Any) -> Optional[float]:
    """Pobór mocy w W albo None ([N/A], pusto, śmieci, NaN) — nigdy wyjątek, nigdy zgadywanie."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return round(f, 2) if 0.0 <= f < 100000.0 else None


def _moc_hwmon_w(tekst: Any) -> Optional[float]:
    """amdgpu hwmon power1_average (mikrowaty, linia na plik) → W z pierwszej czytelnej linii."""
    for linia in str(tekst or "").splitlines():
        try:
            uw = int(linia.strip())
        except ValueError:
            continue
        if uw >= 0:
            return round(uw / 1e6, 2)
    return None


def _czujniki_253_board() -> Optional[dict[str, Any]]:
    """Świeże czujniki LHM ze wspólnej sesji SSH .253."""
    from app.services.smarthome.aio_253 import _czujniki

    c = dict(_czujniki)
    if c.get("stan") != "czyta" or time.time() - c.get("t", 0.0) > 10:
        return None
    return {
        "cpu_tctl_c": c.get("cpu_tctl"),
        "cpu_ccd1_c": c.get("cpu_t"),
        "cpu_w": c.get("cpu_w"),
        "cpu_load_pct": c.get("cpu_load"),
        "cpu_mhz": c.get("cpu_mhz"),
        "cpu_mhz_max": c.get("cpu_mhz_max"),
        "cpu_mhz_eff": c.get("cpu_mhz_eff"),
        "vcore_v": c.get("vcore"),
        "fans_rpm": [int(v) for v in (c.get("wentylatory") or [])],
        "mb_temps_c": c.get("plyta_t") or [],
        "dimm_c": c.get("dimm_t") or [],
        "mb": "ASUS ROG STRIX X870-F",
        "cpu": "Ryzen 7 9800X3D",
        "zrodlo": "LHM przez SSH .253",
    }


async def _probe_253() -> dict[str, Any]:
    """ASUS ROG: LHM web-JSON :8085 → nvidia-smi winrm, czujniki płyty także przez SSH."""
    cached = _cache_get("253")
    if cached:
        return cached
    # Równolegle: sensory płyty/GPU (LHM) i host CPU/RAM/dysk (WinAgent) — koszt = max, nie suma.
    lhm, host = await asyncio.gather(_lhm_253(), _host_winagent("10.0.0.253"))
    if lhm and lhm.get("ok"):
        board = dict(lhm.get("board") or {})
        board.update({k: v for k, v in (_czujniki_253_board() or {}).items() if v is not None})
        lhm = {**lhm, "board": board or None, "host": host,
               "power_w": _moc_w(lhm.get("power_w")), "cpu_w": _moc_w(board.get("cpu_w"))}
        _cache_put("253", lhm)
        return lhm
    # fallback: nvidia-smi przez rękę Shadow (gdy LHM padł)
    from app.services.lan_actuator import run_winrm
    script = ("nvidia-smi --query-gpu=memory.free,temperature.gpu,utilization.gpu,power.draw "
              "--format=csv,noheader,nounits")
    try:
        out, rc = await asyncio.wait_for(run_winrm("10.0.0.253", script), timeout=40)
        parts = [p.strip() for p in (out or "").splitlines()[0].split(",")] if out else []
        if rc == 0 and len(parts) >= 3:
            board_253 = _czujniki_253_board()
            data = {"free_gb": round(float(parts[0]) / 1024, 2), "temp_c": float(parts[1]),
                    "util_pct": float(parts[2]), "resident": [], "ok": True,
                    "board": board_253, "host": host,
                    "power_w": _moc_w(parts[3]) if len(parts) >= 4 else None,
                    "cpu_w": _moc_w((board_253 or {}).get("cpu_w")),
                    "source_note": "winrm-fallback (LHM down)"}
            _cache_put("253", data)
            return data
        return {"free_gb": 0.0, "ok": False, "error": f"rc={rc} out={out!r}"}
    except Exception as e:
        return {"free_gb": 0.0, "ok": False, "error": repr(e)}


def _host_fields(t: dict[str, Any]) -> dict[str, Any]:
    """Wspólne pola hosta z win_telemetry → kształt konsumowany przez UI/brain (bez GPU-free)."""
    if not t or not t.get("ok"):
        return {}
    return {
        "host": {
            "hostname": t.get("hostname"), "platform": t.get("platform"),
            "platform_version": t.get("platform_version"),
            "cpu_brand": t.get("cpu_brand"), "cpu_usage_pct": t.get("cpu_usage_pct"),
            "cpu_count_physical": t.get("cpu_count_physical"),
            "cpu_count_logical": t.get("cpu_count_logical"),
            "cpu_freq_mhz": t.get("cpu_freq_mhz"), "cpu_freq_max_mhz": t.get("cpu_freq_max_mhz"),
            "cpu_mhz_eff": t.get("cpu_mhz_eff"),
            "cpu_mhz": t.get("cpu_mhz"),
            "cpu_mhz_base": t.get("cpu_mhz_base"),
            "cpu_temp_c": t.get("cpu_temp_c"),
            "memory_total_gb": t.get("memory_total_gb"), "memory_used_gb": t.get("memory_used_gb"),
            "memory_available_gb": t.get("memory_available_gb"),
            "memory_usage_pct": t.get("memory_usage_pct"),
            "disks": t.get("disks"), "disk_total_gb": t.get("disk_total_gb"),
            "disk_used_gb": t.get("disk_used_gb"), "disk_usage_pct": t.get("disk_usage_pct"),
            "gpus": t.get("gpus"), "uptime_seconds": t.get("uptime_seconds"),
            "age_s": t.get("age_s"), "stale": t.get("stale"),
        },
        "board": t.get("board") or None,
    }


#: Pula GPU Z13 na Linuksie = GTT amdgpu (amdgpu.gttsize=53248, 29.10). Rezerwa tabletu operatora (4 GB za
#: Windowsa) odpadła — Z13 jest węzłem modeli, a GTT used obejmuje już Bielika i wszystko poza Ollamą.
Z13_PULA_GB = 52.0


async def _llamacpp_resident_172() -> tuple[list[str], str]:
    """Rezydenci llama-server na Z13 (Bielik v3 :8081) — poza Ollamą, więc /api/ps ich nie widzi.

    Claude 2026-10-31: bez tego pick_node liczył dla bielik-11b-v3 pełny koszt ładowania (bez warm 0.15),
    choć model stał w pamięci. /health != ok (z13_watch gasi llama-server po 20 min ciszy) → NIE rezydent.
    Nigdy nie rzuca — błąd tej sondy nie może zepsuć wyniku Ollamy.
    """
    try:
        from app.config import settings
        if not getattr(settings, "llamacpp_z13_enabled", False):
            return [], "llama-server: wyłączony w konfiguracji"
        url = str(getattr(settings, "llamacpp_z13_url", "") or "").rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        if not url:
            return [], "llama-server: brak adresu"
        async with httpx.AsyncClient(timeout=3.0) as c:
            h = await c.get(f"{url}/health")
            if h.status_code != 200:
                return [], f"llama-server: /health HTTP {h.status_code}"
            try:
                hj = h.json()
            except Exception:
                hj = None
            if not isinstance(hj, dict) or hj.get("status") != "ok":
                return [], "llama-server: /health nie ok"
            d = (await c.get(f"{url}/v1/models")).json()
        nazwy: list[str] = []
        if isinstance(d, dict):
            for m in d.get("models") or []:
                n = (m.get("name") or m.get("model")) if isinstance(m, dict) else None
                if n and n not in nazwy:
                    nazwy.append(str(n))
            for m in d.get("data") or []:
                n = m.get("id") if isinstance(m, dict) else None
                if n and n not in nazwy:
                    nazwy.append(str(n))
        if not nazwy:
            return [], "llama-server: /v1/models bez modeli"
        return nazwy, "+ llama-server: " + ", ".join(nazwy)
    except Exception as e:
        return [], f"llama-server: brak odpowiedzi ({type(e).__name__})"


async def _probe_172() -> dict[str, Any]:
    """AMD 395 AI+ APU: ollama /api/ps → rezydencja; free = min(GTT − rezydencja, GTT total − GTT used).

    Claude 2026-10-29: Z13 na Linuksie — host-telemetria (CPU/RAM/GTT/board/dyski) z win_telemetry przez SSH
    (cache-first, refresh w tle → ta sonda zostaje szybka). GTT used z amdgpu widzi wszystko na karcie, także
    Bielika (llama-server :8081) poza Ollamą; /api/ps zostaje źródłem listy rezydentów.
    """
    from app.services import win_telemetry as wt
    host = await wt.host_telemetry("10.0.0.172")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("http://10.0.0.172:11434/api/ps")
            models = r.json().get("models", [])
        # ZWERYFIKOWANE 2026-06-18: na APU 395 ollama raportuje size_vram == size (pełna
        # alokacja UMA liczona jako "vram"), więc to REALNY UMA zajęty, nie przybliżenie.
        used = sum((m.get("size_vram") or m.get("size") or 0) for m in models) / 1e9
        note = ("UMA real (size_vram=size); host CPU/RAM/GTT/board/dyski z SSH"
                if host.get("ok") else
                f"UMA real (size_vram=size); host-telemetria SSH niedostępna: {host.get('error') or 'odczyt w toku'}")
        # Claude-10 2026-09-30 (plan napraw §8): „pula − Σ/api/ps” nie widzi llama-server :8081 (Bielik)
        # — router zawyżał wolne UMA. Bierzemy mniejszą z dwóch miar.
        free_uma = max(0.0, Z13_PULA_GB - used)
        avail = host.get("memory_available_gb") if host.get("ok") else None
        gpu_total = host.get("gpu_mem_total_mb") if host.get("ok") else None
        gpu_used = host.get("gpu_mem_used_mb") if host.get("ok") else None
        if (isinstance(gpu_total, (int, float)) and gpu_total > 0
                and isinstance(gpu_used, (int, float)) and gpu_used > 0):
            # GTT: total − used obejmuje Bielika i bufory poza Ollamą.
            free_uma = min(free_uma, max(0.0, (gpu_total - gpu_used) / 1024))
            note += (f" · free = max(0, min({Z13_PULA_GB:g}−Σps, GTT ({gpu_total:g} − {gpu_used:g})"
                     f" / 1024))")
        elif isinstance(avail, (int, float)):
            free_uma = min(free_uma, max(0.0, float(avail)))
            note += f" · free = min({Z13_PULA_GB:g}−Σps, dostępne hosta {float(avail):.1f} GB)"
        resident = [m.get("name") for m in models]
        # Osobna sonda (sama łapie wyjątki) — used/free bez zmian, GTT już obejmuje Bielika.
        llama, llama_note = await _llamacpp_resident_172()
        resident += [n for n in llama if n not in resident]
        note += f" · {llama_note}"
        return {"free_gb": round(free_uma, 2), "used_gb": round(used, 2),
                "temp_c": (host.get("cpu_temp_c") if host.get("ok") else None),
                "util_pct": (host.get("gpu_util_pct") if host.get("ok") else None),
                # hwmon power1_average (µW) z tej samej sesji SSH win_telemetry → W; brak = None
                "power_w": (_moc_hwmon_w(host.get("gpu_power1_average")) if host.get("ok") else None),
                "resident": resident, "ok": True,
                "note": note, **({"board": None} | _host_fields(host))}
    except Exception as e:
        # Ollama padła, ale host może żyć — oddaj co wiadomo, nie zeruj całego node'a.
        llama, llama_note = await _llamacpp_resident_172()
        return {"free_gb": 0.0, "ok": bool(host.get("ok")), "error": repr(e),
                "note": f"ollama :11434 nie odpowiada — free-compute UMA nieznane · {llama_note}",
                "resident": list(llama), "temp_c": host.get("cpu_temp_c"), **_host_fields(host)}


async def _probe_winrm_host(ip: str, total_gb: float) -> dict[str, Any]:
    """Node bez WinAgenta/LHM (.237 Murcia, .197 laptop Dziecko) — pełna telemetria przez WinRM.

    free_gb = realnie wolna VRAM z nvidia-smi (gdy jest); bez nvidia-smi zostaje None, bo
    zgadywana liczba byłaby gorsza niż uczciwy brak. Host wyłączony → ok=False z powodem.
    """
    from app.services import win_telemetry as wt
    t = await wt.host_telemetry(ip)
    if not t.get("ok"):
        return {"free_gb": 0.0, "ok": False, "resident": [],
                "error": t.get("error") or ("odczyt WinRM w toku" if t.get("pending") else "brak danych"),
                "reachable": t.get("reachable"),
                "note": ("host nieosiągalny — telemetria niedostępna (nie zgaduję)"
                         if t.get("reachable") is False else None)}
    smi = t.get("nvidia") or {}
    free_mb = smi.get("memory_free_mb")
    return {
        "free_gb": round(free_mb / 1024, 2) if free_mb is not None else 0.0,
        "used_gb": round((smi.get("memory_used_mb") or 0) / 1024, 2) if smi else None,
        "temp_c": smi.get("temp_c") if smi.get("temp_c") is not None else t.get("cpu_temp_c"),
        "util_pct": smi.get("utilization_pct") if smi else t.get("gpu_util_pct"),
        "resident": [], "ok": True, "reachable": True,
        "gpu_measured": smi.get("name"),
        "note": (None if smi else "brak nvidia-smi na hoście — VRAM nieznana (CPU/RAM/dyski realne)"),
        **_host_fields(t),
    }


def normalize_guest_metrics(raw: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Kształt shadow-winagent 1.1.0 (zagnieżdżony, bajty) → płaska postać WindowsPCMetrics,
    tej samej co /api/network/windows-pc — jeden renderer karty węzła na /network.

    Agent gościa daje cpu:{logical,percent}, memory:{total_bytes,...}, gpu:[{util_percent,...}] —
    inaczej niż agent .253. Surowiec zostaje pod `raw` (diagnostyka + pola mode/inference).
    """
    GB = 1e9
    cpu = raw.get("cpu") or {}
    mem = raw.get("memory") or {}
    disk = raw.get("disk") or {}
    gpus = raw.get("gpu") or []
    g0 = gpus[0] if gpus else {}

    def _gb(v: Any) -> float | None:
        return round(v / GB, 1) if isinstance(v, (int, float)) else None

    return {
        **base,
        "agent_online": True,
        "hostname": raw.get("node") or base.get("hostname"),
        "agent_version": raw.get("agent_version"),
        "mode": raw.get("mode"),
        "inference": raw.get("inference"),
        "exec_enabled": raw.get("exec_enabled"),
        "cpu_usage_pct": cpu.get("percent"),
        "cpu_count_logical": cpu.get("logical"),
        "cpu_count_physical": cpu.get("physical") or cpu.get("cores"),
        "memory_total_gb": _gb(mem.get("total_bytes")),
        "memory_used_gb": _gb(mem.get("used_bytes")),
        "memory_available_gb": _gb(mem.get("avail_bytes")),
        "memory_usage_pct": mem.get("percent"),
        "disk_total_gb": _gb(disk.get("total_bytes")),
        "disk_used_gb": _gb(disk.get("used_bytes")),
        "disk_free_gb": _gb(disk.get("free_bytes")),
        "disk_usage_pct": disk.get("percent"),
        "uptime_seconds": raw.get("uptime_s"),
        "agent_uptime_seconds": raw.get("agent_uptime_s"),
        "gpu": {
            "name": g0.get("name"),
            "temp_c": g0.get("temp_c"),
            "util_pct": g0.get("util_percent"),
            "memory_used_mb": g0.get("mem_used_mb"),
            "memory_total_mb": g0.get("mem_total_mb"),
            "clock_mhz": g0.get("clock_mhz"),
            "power_w": g0.get("power_w"),
            "fan_pct": g0.get("fan_percent"),
        } if g0 else None,
        "gpu_error": raw.get("gpu_error"),
        "raw": raw,
    }


async def guest_metrics(timeout: float = 6.0) -> dict[str, Any]:
    """Żywe metryki SHADOWGUEST (2.8) z jego winagenta — JEDNO źródło dla /network i prawdy
    sprzętowej. Cache TTL wspólny z resztą węzłów: karta + hardware-truth = jeden strzał.

    Brak tokena / agent offline = uczciwe `agent_online=False` z powodem, nie zgadywanie.
    """
    cached = _cache_get("130:metrics")
    if cached:
        return cached
    from datetime import datetime, timezone

    from app.config import settings

    base = {
        "agent_online": False,
        "hostname": "SHADOWGUEST",
        "platform": "Windows 11",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    token = <UKRYTE>
    url = settings.shadowguest_agent_url
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=timeout) as c:
            # /metrics NIE zwraca exec_enabled — to pole żyje tylko w /health. Bez tego dashboard
            # twierdził „zdalne wykonanie wyłączone" przy agencie, który je ma włączone (kłamstwo
            # o WPŁYWIE = najgorszy rodzaj kłamstwa w tym organizmie). Oba strzały równolegle.
            r, h = await asyncio.gather(
                c.get(f"{url}/metrics", headers=headers),
                c.get(f"{url}/health", headers=headers),
                return_exceptions=True,
            )
            if isinstance(r, Exception):
                raise r
            r.raise_for_status()
        raw = r.json()
        if not isinstance(h, Exception) and h.status_code == 200:
            try:
                health = h.json()
                raw = {**health, **raw, "exec_enabled": health.get("exec_enabled")}
            except Exception:
                pass
        out = normalize_guest_metrics(raw, base)
    except Exception as exc:
        return {**base, "error": str(exc) or exc.__class__.__name__}
    _cache_put("130:metrics", out)
    return out


_GUEST_SENSORS_130_CACHE: Optional[tuple[float, dict[str, Any]]] = None
_GUEST_SENSORS_130_LOCK = asyncio.Lock()
_GUEST_SENSORS_130_FIELDS = (
    "cpu_mhz_eff", "cpu_mhz", "cpu_mhz_max", "cpu_mhz_base",
    "cpu_temp_c", "board_name", "fans_rpm", "mb_temps_c",
)


def _parse_guest_sensors_130(stdout: str) -> dict[str, Any]:
    import json

    raw = json.loads(stdout.strip().lstrip("\ufeff").strip())
    if not isinstance(raw, dict):
        raise ValueError("czujniki .130: oczekiwano obiektu JSON")
    out = {k: raw.get(k) for k in _GUEST_SENSORS_130_FIELDS}
    for key in ("cpu_mhz_eff", "cpu_mhz", "cpu_mhz_max", "cpu_mhz_base", "cpu_temp_c"):
        out[key] = _num(out[key]) if out[key] is not None else None
    if out["cpu_mhz_max"] is None:
        out["cpu_mhz_max"] = out["cpu_mhz"]
    for key in ("fans_rpm", "mb_temps_c"):
        values = out[key]
        if values is not None:
            values = values if isinstance(values, list) else [values]
            out[key] = [n for v in values if (n := _num(v)) is not None]
    return out


async def _guest_sensors_130() -> dict[str, Any]:
    """Zegary CIM i sensory LHM przez API agenta; cache także dla błędów."""
    global _GUEST_SENSORS_130_CACHE

    async with _GUEST_SENSORS_130_LOCK:
        cached = _GUEST_SENSORS_130_CACHE
        if cached is not None and time.monotonic() - cached[0] < 60.0:
            return dict(cached[1])
        out = dict.fromkeys(_GUEST_SENSORS_130_FIELDS)
        try:
            from app.services.render_router import _exec_130
            from app.services.win_telemetry import _PS_CPU_CLOCKS

            script = _PS_CPU_CLOCKS + r"""
$ErrorActionPreference='Stop'
$computer=$null
$board_name=$null
$cpu_temp_c=$null
$script:cpuTemps=@()
$script:fans=@()
$script:mbTemps=@()
function Read-GuestHardware($hw, [bool]$cpu, [bool]$board){
 $hw.Update()
 $cpu=$cpu -or ($hw.HardwareType.ToString() -eq 'Cpu')
 $board=$board -or ($hw.HardwareType.ToString() -eq 'Motherboard')
 foreach($sensor in $hw.Sensors){
  if($null -eq $sensor.Value){continue}
  $type=$sensor.SensorType.ToString()
  if($cpu -and $type -eq 'Temperature'){
   $script:cpuTemps += [double]$sensor.Value
  }
  if($board -and $type -eq 'Fan'){
   $script:fans += [double]$sensor.Value
  }
  if($board -and $type -eq 'Temperature'){
   $script:mbTemps += [double]$sensor.Value
  }
 }
 foreach($child in $hw.SubHardware){
  Read-GuestHardware $child $cpu $board
 }
}
try{
 Add-Type -Path 'C:\shadowAI\lhm\app\LibreHardwareMonitorLib.dll'
 $computer=New-Object LibreHardwareMonitor.Hardware.Computer
 $computer.IsCpuEnabled=$true
 $computer.IsMotherboardEnabled=$true
 $computer.Open()
 foreach($hw in $computer.Hardware){
  if($hw.HardwareType.ToString() -eq 'Motherboard'){$board_name=$hw.Name}
  Read-GuestHardware $hw $false $false
 }
 if($script:cpuTemps.Count -gt 0){
  $cpu_temp_c=($script:cpuTemps|Measure-Object -Maximum).Maximum
 }
}catch{
 $board_name=$null
 $cpu_temp_c=$null
 $script:fans=$null
 $script:mbTemps=$null
}finally{
 if($null -ne $computer){try{$computer.Close()}catch{}}
}
[pscustomobject]@{
 cpu_mhz_eff=$cpu_mhz_eff
 cpu_mhz=$cpu_mhz
 cpu_mhz_max=$cpu_mhz
 cpu_mhz_base=$cpu_mhz_base
 cpu_temp_c=$cpu_temp_c
 board_name=$board_name
 fans_rpm=$script:fans
 mb_temps_c=$script:mbTemps
}|ConvertTo-Json -Klient 4 -Compress
"""
            result = await asyncio.to_thread(_exec_130, script, 15)
            if result.get("exit") == 0 and not result.get("timed_out"):
                out = _parse_guest_sensors_130(result.get("stdout") or "")
        except Exception:
            pass
        _GUEST_SENSORS_130_CACHE = (time.monotonic(), out)
        return dict(out)


async def _probe_28() -> dict[str, Any]:
    """SHADOWGUEST: GPU i host z agenta, zegary CIM i sensory LHM przez jego API exec.

    Node bez runtime modeli (inference=False): kandydat na render/enkod (NVENC RTX 5070 12GB),
    nie na LLM. Odczyt dodatkowych czujników ma osobny cache 60 s, także dla błędów.
    """
    m = await guest_metrics()
    if not m.get("agent_online"):
        return {"free_gb": 0.0, "ok": False, "resident": [], "reachable": False,
                "error": m.get("error") or "winagent :8002 nieosiągalny",
                "note": "SHADOWGUEST offline — telemetria niedostępna (nie zgaduję)"}
    g = m.get("gpu") or {}
    used_mb, total_mb = g.get("memory_used_mb"), g.get("memory_total_mb")
    free_gb = (round((total_mb - used_mb) / 1024, 2)
               if isinstance(used_mb, (int, float)) and isinstance(total_mb, (int, float)) else 0.0)
    raw_disk = (m.get("raw") or {}).get("disk") or {}
    mode, agent_v = m.get("mode"), m.get("agent_version")
    sensors = await _guest_sensors_130()
    return {
        "free_gb": free_gb,
        "used_gb": round(used_mb / 1024, 2) if isinstance(used_mb, (int, float)) else None,
        "temp_c": g.get("temp_c"), "util_pct": g.get("util_pct"),
        "power_w": _moc_w(g.get("power_w")),  # nvidia-smi power.draw z agenta .130 (przegląd Claude 03.11)
        "resident": [], "ok": True, "reachable": True,
        "gpu_measured": g.get("name"),
        "note": (f"winagent {agent_v} · tryb {mode}"
                 + (" · bez runtime modeli (render/montaż/NVENC)" if NODES["130"]["inference"] is False else "")
                 + (f" · GPU: {m['gpu_error']}" if m.get("gpu_error") else "")),
        "host": {
            "hostname": m.get("hostname"), "platform": m.get("platform"),
            "cpu_usage_pct": m.get("cpu_usage_pct"),
            "cpu_count_physical": m.get("cpu_count_physical"),
            "cpu_count_logical": m.get("cpu_count_logical"),
            "cpu_mhz_eff": sensors.get("cpu_mhz_eff"),
            "cpu_mhz": sensors.get("cpu_mhz"),
            "cpu_mhz_base": sensors.get("cpu_mhz_base"),
            "cpu_freq_mhz": sensors.get("cpu_mhz_eff"),
            "cpu_freq_max_mhz": sensors.get("cpu_mhz"),
            "cpu_temp_c": sensors.get("cpu_temp_c"),
            "memory_total_gb": m.get("memory_total_gb"),
            "memory_used_gb": m.get("memory_used_gb"),
            "memory_available_gb": m.get("memory_available_gb"),
            "memory_usage_pct": m.get("memory_usage_pct"),
            "disk_total_gb": m.get("disk_total_gb"), "disk_used_gb": m.get("disk_used_gb"),
            "disk_usage_pct": m.get("disk_usage_pct"),
            "disks": ([{"drive": raw_disk.get("drive"),
                        "total_gb": m.get("disk_total_gb"), "used_gb": m.get("disk_used_gb"),
                        "free_gb": m.get("disk_free_gb"), "usage_pct": m.get("disk_usage_pct")}]
                      if raw_disk else None),
            "gpus": [{"name": g.get("name")}] if g.get("name") else None,
            "uptime_seconds": m.get("uptime_seconds"),
        },
        "board": {**sensors, "mb": sensors.get("board_name")},
    }


async def _probe_237() -> dict[str, Any]:
    return await _probe_winrm_host("10.0.0.237", 4.0)


async def _probe_197() -> dict[str, Any]:
    return await _probe_winrm_host("10.0.0.197", 8.0)


_PROBES = {"10": _probe_10, "253": _probe_253, "172": _probe_172,
           "237": _probe_237, "197": _probe_197, "130": _probe_28}


#: Claude-10 2026-09-30 (plan napraw §8): `resident=[]` było wpisane NA SZTYWNO w 5/6 sond, więc
#: człon „ciepły model” równania przenoszenia (warm 0.15) nigdy nie działał poza Z13, a autodiagnoza
#: nie widziała, co zajmuje VRAM. Dla węzłów z Ollamą bierzemy prawdę z /api/ps (size_vram > 0).
_OLLAMA_PS = {"10": "http://127.0.0.1:11434", "253": "http://10.0.0.253:11434",
              "130": "http://10.0.0.130:11434"}


async def _resident_z_ollamy(url: str) -> tuple[list[str] | None, str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            modele = (await c.get(f"{url}/api/ps")).json().get("models", [])
        return [m.get("name") for m in modele if (m.get("size_vram") or 0) > 0], "ollama /api/ps"
    except Exception as e:
        return None, f"brak pomiaru ({type(e).__name__})"


_W_LOCIE_CACHE: dict[tuple[str, tuple[int, ...]], tuple[float, int]] = {}


def _w_locie(ip: str, porty) -> int:
    """Lokalne połączenia inference oczekujące na odpowiedź lub odbierające dane."""
    import logging
    import re
    import subprocess

    porty = tuple(sorted(set(porty)))
    key = (ip, porty)
    cached = _W_LOCIE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < 3.0:
        return cached[1]

    liczba = 0
    try:
        wynik = subprocess.run(
            ["ss", "-tniH", "state", "established", "dst", ip],
            capture_output=True, text=True, check=True, timeout=2.0,
        )
        polaczenia = []
        for linia in wynik.stdout.splitlines():
            pola = linia.split()
            if pola and pola[0] == "ESTAB":
                pola = pola[1:]
            if len(pola) >= 4 and pola[0].isdigit() and pola[1].isdigit():
                polaczenia.append([pola[3], linia])
            elif polaczenia:
                polaczenia[-1][1] += " " + linia

        for peer, opis in polaczenia:
            adres, _, port = peer.rpartition(":")
            if adres.strip("[]") != ip or not port.isdigit() or int(port) not in porty:
                continue
            lastsnd = re.search(r"\blastsnd:(\d+)\b", opis)
            lastrcv = re.search(r"\blastrcv:(\d+)\b", opis)
            wyslano = any(
                int(wartosc) > 0
                for wartosc in re.findall(r"\b(?:bytes_sent|data_segs_out):(\d+)\b", opis)
            )
            if ("bytes_received" in opis or wyslano) and (
                "bytes_received" not in opis
                or (lastsnd is not None and lastrcv is not None
                    and int(lastsnd[1]) <= int(lastrcv[1]))
                or (lastrcv is not None and int(lastrcv[1]) < 2000)
            ):
                liczba += 1
    except Exception as exc:
        logging.getLogger("shadowai.node_truth").warning(
            "w_locie: ss dla %s: %s", ip, type(exc).__name__,
        )
        liczba = 0

    _W_LOCIE_CACHE[key] = (time.monotonic(), liczba)
    return liczba


async def node_truth() -> dict[str, Any]:
    """Realny stan compute KAŻDEGO node'a (mesh: każdy widzi każdego przez .10)."""
    keys = list(NODES)
    results, rezydencja = await asyncio.gather(
        asyncio.gather(*[_PROBES[k]() for k in keys], return_exceptions=True),
        asyncio.gather(*[_resident_z_ollamy(u) for u in _OLLAMA_PS.values()], return_exceptions=True))
    rez = {k: (r if not isinstance(r, Exception) else (None, "brak pomiaru")) for k, r in zip(_OLLAMA_PS, rezydencja)}
    adresy = {
        "10": ("127.0.0.1", "10.0.0.10"),
        "253": ("10.0.0.253",),
        "172": ("10.0.0.172",),
        "130": ("10.0.0.130",),
    }
    nodes = []
    for k, res in zip(keys, results):
        meta = NODES[k]
        if isinstance(res, Exception):
            res = {"free_gb": 0.0, "ok": False, "error": repr(res)}
        if k in rez and not res.get("resident"):
            lista, zrodlo = rez[k]
            res = {**res, "resident": lista or [], "resident_zrodlo": zrodlo}
        wpis = {"node": k, **meta, **res,
                "free_pct": round(100 * res.get("free_gb", 0) / meta["total_gb"], 1)}
        if wpis.get("inference", True):
            wpis["w_locie"] = sum(await asyncio.gather(*[
                asyncio.to_thread(_w_locie, ip, (11434, 11435, 8081))
                for ip in adresy.get(str(k), ())
            ]))
        nodes.append(wpis)
    return {"ts": time.time(), "nodes": nodes}


import math

# ── Trajektoria decyzji (człon PRZESZŁOŚCI w równaniu przenoszenia w czasie) ──
# Operator 2026-08-31: „nie tylko transport cost do przodu — do tyłu i teraźniejszość".
# Bez trwałego śladu człon przeszłości byłby zmyślony, więc trzymamy REALNY dziennik decyzji
# i ich skutków. Jedna linia JSONL na zdarzenie; czytamy ostatnie N per węzeł.
# To jest dane wejściowe „time-travel synthesizera" z docs/personas-and-decision-triangle:
# widzi trajektorię, nie stop-frame, i wykrywa CTL (rotację wokół zepsutego węzła).
from pathlib import Path as _Path

_LEDGER = _Path(__file__).resolve().parents[3] / "logs" / "relocate_trajectory.jsonl"
_TRAJ_OKNO = 8          # ile ostatnich zdarzeń per node bierzemy pod uwagę
_CTL_PROG = 3           # tyle porażek z rzędu = closed timelike loop → sygnał BREAK
_POLOWA_ZANIKU_H = 24.0  # po tylu godzinach skutek waży o połowę mniej (rozpad połowiczny)
_KV_REZYDENTA_GB = 0.5  # model już w pamięci węzła potrzebuje tylko zapasu na kontekst/KV, nie całej wagi
# Kanały, którymi free_gb płynie RAZEM z telemetrią hosta. Tylko dla nich nieświeżość
# hosta jest nieświeżością liczby, na której opieramy decyzję.
_KANALY_Z_TELEMETRII_HOSTA = {"winrm-cim", "nvidia-smi-winrm"}


def _waga_wieku(ts: Any) -> float:
    """Rozpad połowiczny skutku. Porażka sprzed godzin waży mniej niż sprzed minut.

    Bez tego CTL był stanem POCHŁANIAJĄCYM: węzeł ukarany za 3 porażki przestawał być
    wybierany, więc nie miał jak zapracować na `ok`, więc kara nigdy nie gasła.
    """
    try:
        wiek_h = max(0.0, (time.time() - float(ts)) / 3600.0)
    except Exception:
        return 1.0
    return 0.5 ** (wiek_h / _POLOWA_ZANIKU_H)


def _traj_wpisy(node_key: str, limit: int = _TRAJ_OKNO,
                outcomes: tuple[str, ...] | None = None,
                need_gb: float | None = None) -> list[dict[str, Any]]:
    """Ostatnie zdarzenia dla węzła. Brak pliku = brak historii (uczciwe, nie błąd).

    `need_gb` wybiera wpisy z liczbowym need_gb >= połowy zapotrzebowania,
    również PRZED odcięciem okna; None zachowuje wszystkie klasy zadań.

    `outcomes` filtruje PRZED odcięciem okna. Bez tego wpisy `picked` (jeden na każdą
    decyzję) wypychały z okna realne `ok`/`fail` — im częściej węzeł pracował, tym
    szybciej organizm zapominał, jak mu poszło, a człon przeszłości wracał do 1.0.
    """
    try:
        import json as _j
        with open(_LEDGER, "r", encoding="utf-8", errors="replace") as fh:
            wiersze = fh.readlines()[-2000:]
    except FileNotFoundError:
        return []
    except Exception:
        return []
    out = []
    for w in reversed(wiersze):
        w = w.strip()
        if not w:
            continue
        try:
            e = _j.loads(w)
        except Exception:
            continue
        if str(e.get("node")) == str(node_key):
            if outcomes and e.get("outcome") not in outcomes:
                continue
            if need_gb is not None:
                recorded_need = e.get("need_gb")
                if (type(recorded_need) not in (int, float)
                        or not recorded_need >= 0.5 * need_gb):
                    continue
            out.append(e)
            if len(out) >= limit:
                break
    return list(reversed(out))


def _trajectory(node_key: str, need_gb: float | None = None) -> dict[str, Any]:
    """Człon PRZESZŁOŚCI: mnożnik kosztu z historii + detekcja CTL.

    Zwraca `retro` (mnożnik >=0.85), `ctl` (bool) i `opis` do logu decyzji.
    Zasada: sukcesy lekko TANIEJĄ węzeł (sprawdzona ścieżka), porażki z rzędu go DROŻĄ
    wykładniczo — po `_CTL_PROG` porażkach organizm ma przestać tam wracać (BREAK), zamiast
    rotować wokół zepsutego węzła i za każdym razem uznawać to za lokalnie sensowne.
    """
    wpisy = _traj_wpisy(node_key, outcomes=("ok", "fail", "timeout"), need_gb=need_gb)
    if not wpisy:
        return {"retro": 1.0, "ctl": False, "opis": "brak historii (neutralnie)"}
    # Porażki z rzędu liczone od najświeższej, ale WAŻONE wiekiem — licznik jest ułamkowy,
    # bo trzy porażki sprzed tygodnia to nie to samo co trzy porażki sprzed godziny.
    pod_rzad = 0.0
    ile_fail = 0
    for e in reversed(wpisy):
        if e.get("outcome") in ("fail", "timeout"):
            pod_rzad += _waga_wieku(e.get("ts"))
            ile_fail += 1
        else:
            break
    ile_ok = sum(_waga_wieku(e.get("ts")) for e in wpisy if e.get("outcome") == "ok")
    # CTL = co najmniej _CTL_PROG porażek z rzędu I ich waga po rozpadzie wciąż ≥ połowy progu
    # (czyli seria nie starsza niż ~jeden okres półtrwania). Sama waga nie wystarczy:
    # trzy porażki sprzed minut ważą 2.99, nie 3.0 — próg na floacie nigdy by nie zapalał.
    if ile_fail >= _CTL_PROG and pod_rzad >= _CTL_PROG / 2:
        return {"retro": float(2 ** max(pod_rzad, _CTL_PROG / 2)), "ctl": True,
                "opis": f"CTL: {ile_fail} porażek z rzędu (waga {pod_rzad:.2f} po rozpadzie) — "
                        f"organizm rotuje wokół tego węzła (BREAK)"}
    if pod_rzad > 0.05:
        return {"retro": 1.0 + 0.6 * pod_rzad, "ctl": False,
                "opis": f"{ile_fail} porażek, waga po rozpadzie {pod_rzad:.2f}; "
                        f"{ile_ok:.2f} sukcesów w oknie"}
    if ile_fail:
        return {"retro": 1.0, "ctl": False,
                "opis": f"{ile_fail} porażek, ale przedawnionych (waga {pod_rzad:.2f}) — neutralnie"}
    # sama historia sukcesów: delikatna premia, nigdy poniżej 0.85 (nie chcemy lock-inu w jeden node)
    return {"retro": max(0.85, 1.0 - 0.05 * ile_ok), "ctl": False,
            "opis": f"{ile_ok:.2f} sukcesów z rzędu (sprawdzona ścieżka)"}


def skutek_juz_zapisany(node_key: str, znacznik: str, limit: int = 40) -> bool:
    """Czy skutek z tym znacznikiem (np. id zadania) już jest w trajektorii?

    Zadania odczepione są ODPYTYWANE wielokrotnie — bez tej bramki jeden render zapisałby
    dziesiątki 'ok' i sztucznie wyzerował człon przeszłości. Uczymy się z faktu, nie z liczby
    zapytań o niego.
    """
    if not znacznik:
        return False
    for e in _traj_wpisy(node_key, limit=limit):
        if e.get("outcome") in ("ok", "fail") and znacznik in str(e.get("note") or ""):
            return True
    return False


def node_key_for_ip(ip: str) -> Optional[str]:
    """IP → klucz węzła w NODES ('10', '253', '130'...). None = adres spoza organizmu.

    Potrzebne, bo strona wykonawcza (lan_actuator, ręce) operuje ADRESAMI, a trajektoria
    jest prowadzona per WĘZEŁ. Bez tego mapowania skutek nie trafiłby do właściwego wpisu.
    """
    if not ip:
        return None
    for k, meta in NODES.items():
        if meta.get("ip") == ip:
            return k
    return None


def record_outcome(node_key: str, outcome: str, note: str = "", need_gb: float | None = None) -> None:
    """Zamknij pętlę: zapisz SKUTEK relokacji. Bez tego człon przeszłości nigdy się nie uczy.

    Woła to strona wykonawcza (executor/ręka) po zakończeniu zadania: outcome='ok'|'fail'.
    Zapis jest best-effort — dziennik nie może wywrócić decyzji, ale ma krzyczeć do logu.
    """
    try:
        import json as _j
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "node": str(node_key), "outcome": outcome,
               "note": note[:300], "need_gb": need_gb}
        with open(_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(_j.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:
        import logging as _lg
        _lg.getLogger("shadowai.node_truth").warning("trajektoria: zapis padł: %r", exc)


def _transport_cost(n: dict[str, Any], need_gb: float,
                    prefer_model: Optional[str]) -> tuple[float, dict[str, Any]]:
    """transport_cost(t) w przestrzeni NIEEUKLIDESOWEJ (logika discussion 507ee387:
    trójkąt decyzyjny persona×maszyna×czas, entropia Rényi-Tsallis + geom. hiperboliczna).

    Koszt = pojemność_hiperboliczna × czas(warm/cold) × entropia_termiczna.
    Mniejszy = bliżej (tańsza relokacja). inf = poza zdolnością (brzeg przestrzeni).
    """
    free = n.get("free_gb", 0.0)
    # Claude-10 2026-09-30 (operator: „trójkąt nieeuklidesowy i przenoszenie w czasie, via żelazo”): model
    # REZYDENTNY na węźle już zajmuje swoją pamięć — `free_gb` jej nie zawiera. Bez tej poprawki węzeł, na
    # którym model jest ciepły, wypadał jako „poza zdolnością” (zmierzone: .130 z gemma4:12b 8,4 GB w VRAM
    # miał free 0,83 < 8,7), a premia warm ×0,15 nie mogła zadziałać nigdy tam, gdzie model naprawdę siedzi.
    rezydentny = bool(prefer_model and prefer_model in (n.get("resident") or []))
    need_eff = min(need_gb, _KV_REZYDENTA_GB) if rezydentny else need_gb
    if not n.get("ok") or free < need_eff:
        return math.inf, {"reason": "poza zdolnością (free<need)"}
    key = str(n.get("node") or "")
    eps = 0.5

    # ── TERAŹNIEJSZOŚĆ: gdzie system JEST teraz ─────────────────────────────
    # Hiperboliczna kara pojemności: ~0 gdy dużo zapasu, →∞ gdy free≈need (brzeg przestrzeni).
    capacity = need_eff / max(free - need_eff, eps)
    # Entropia termiczna (Rényi-Tsallis proxy): gorętszy node = wyższy koszt (ryzyko throttle).
    temp = n.get("temp_c")
    if temp is None:
        # Niewiedza NIE jest darmowa — spójnie ze `stale`. Poprzednio brak odczytu (a także
        # temp=0.0, bo falsy) wyglądał jak węzeł zimny, więc milczenie czujnika było nagrodą.
        thermal = 1.15
    else:
        thermal = 1.0 + max(0.0, (float(temp) - 50.0) / 40.0)
    # Świeżość prawdy: node, którego telemetria jest przeterminowana, jest ryzykowny TERAZ —
    # decydujemy na podstawie nieaktualnego stanu. Stale = +50%.
    host = n.get("host") or {}
    # Kara za nieświeżość ma dotyczyć LICZBY, która wchodzi do decyzji. .172 czyta free_gb
    # z ollama /api/ps (żywe HTTP), a `host.stale` opisuje WinRM-ową telemetrię CPU/RAM —
    # karanie decyzji VRAM-owej za nieświeżość innego kanału przesuwało wybór bez powodu.
    # Wiek jest też ciągły: 31 s i 31 min nie mogą kosztować tyle samo.
    stale = 1.0
    if str(n.get("channel") or "") in _KANALY_Z_TELEMETRII_HOSTA:
        wiek_s = host.get("age_s")
        if wiek_s is not None:
            stale = 1.0 + min(1.0, max(0.0, float(wiek_s)) / 1800.0)
        elif host.get("stale"):
            stale = 1.5
    present = capacity * thermal * stale

    # ── PRZYSZŁOŚĆ: co decyzja OTWIERA i ZAMYKA w t+Δ ───────────────────────
    # Model rezydentny = warm = tani; cold load = drogi (klasyczny człon transportu).
    warm = 0.15 if (prefer_model and prefer_model in (n.get("resident") or [])) else 1.0
    # Lock-in / koszt re-entry wg właściciela zasobu (doc: „nie udawaj że decyzja jest
    # reversible, jeśli przenosi w punkt, z którego powrót jest droższy"). Wejście na maszynę
    # człowieka zamyka ją na jego powrót — to realny koszt w przyszłości, nie metafora.
    owner = (n.get("owner") or "shadow").lower()
    lock_in = {"shadow": 1.0, "operator": 1.6, "family": 1.9}.get(owner, 1.2)
    # Człon nazwany PRZYSZŁOŚCIĄ nie miał żadnej zmiennej czasowej — był stałą tablicą.
    # Prawdopodobieństwo POWROTU człowieka szacujemy z REALNEJ aktywności hosta (telemetria,
    # nie zgadywany grafik): maszyna, na której ktoś właśnie pracuje, jest droższa do zajęcia
    # niż ta sama maszyna bezczynna. Dla węzłów Shadow nie ma kogo wypychać, więc 1.0.
    powrot = 1.0
    if owner != "shadow":
        obciazenie = max(float(host.get("cpu_usage_pct") or 0.0),
                         float(n.get("util_pct") or 0.0))
        powrot = 1.0 + min(0.5, obciazenie / 100.0)
    future = warm * lock_in * powrot

    # ── PRZESZŁOŚĆ: krzywizna trajektorii (time-travel synthesizer) ─────────
    # Doc trójkąta: „synthesizer musi widzieć krzywiznę, nie tylko styczną". Node, który
    # ostatnio POWTARZALNIE zawodził, jest droższy — inaczej organizm rotuje wokół zepsutego
    # węzła (closed timelike loop) i każdy krok wygląda lokalnie sensownie.
    tr = _trajectory(key, need_gb=need_gb)
    past = tr["retro"]

    w_locie = n.get("w_locie", 0)
    present *= 1 + w_locie
    cost = present * future * past
    detail = {
        "w_locie": w_locie,
        "capacity": round(capacity, 3), "thermal": round(thermal, 3), "stale": stale,
        "warm": warm, "lock_in": lock_in, "owner": owner, "powrot": round(powrot, 3),
        "present": round(present, 3), "future": round(future, 3), "past": round(past, 3),
        "cost": round(cost, 3),
        "trajektoria": tr["opis"],
    }
    if tr["ctl"]:
        # CTL wykryty → to nie jest „trochę drożej", to sygnał BREAK z trójkąta.
        detail["ctl"] = True
        detail["reason_ctl"] = tr["opis"]
    return cost, detail


def pick_node(nodes: list[dict[str, Any]], need_gb: float,
              prefer_model: Optional[str] = None,
              require_inference: bool = True,
              zapisz_wybor: bool = False) -> dict[str, Any]:
    """Hook relokacji 507ee387: minimalizuj transport_cost (nieeuklidesowy), nie naiwny max-free.

    Bramka: tylko node z realnie wolnym compute >= need_gb. Wśród nich wybór po
    najniższym transport_cost (warm model + zapas + chłód). Zwraca decyzję + pełny
    rozkład kosztów (bezcenny log do nauki Shadow).
    """
    # BRAMKA OPERATORA (twarda, nie koszt): operator gra na .253 → wyklucz, ustąp GPU.
    # is_win_available()=False gdy vram_manager.set_gaming_mode(True). Człowiek > optymalizacja.
    win_blocked = False
    try:
        from app.services.vram_manager import is_win_available
        win_blocked = not is_win_available()
    except Exception:
        win_blocked = False

    guest_blocked = False
    try:
        from app.services.vram_manager import is_guest_available
        guest_blocked = not is_guest_available()
    except Exception:
        guest_blocked = False

    scored = []
    for n in nodes:
        # BRAMKA ZDOLNOŚCI: .237/.197 nie mają runtime modeli — model wysłany tam nie ruszy.
        # Dla zadań nie-inference (encode/render NVENC) wołaj z require_inference=False.
        if require_inference and not n.get("inference", True):
            scored.append({"node": n.get("node"), "gpu": n.get("gpu"), "free_gb": n.get("free_gb"),
                           "temp_c": n.get("temp_c"), "transport_cost": math.inf,
                           "reason": f"WYKLUCZONY: brak runtime modeli ({n.get('role') or 'nie-inference'})"})
            continue
        if n.get("node") == "253" and win_blocked:
            scored.append({"node": "253", "gpu": n.get("gpu"), "free_gb": n.get("free_gb"),
                           "temp_c": n.get("temp_c"), "transport_cost": math.inf,
                           "reason": "WYKLUCZONY: gaming-mode (.253 należy do operatora)"})
            continue
        if n.get("node") == "130" and guest_blocked:
            scored.append({"node": "130", "gpu": n.get("gpu"), "free_gb": n.get("free_gb"),
                           "temp_c": n.get("temp_c"), "transport_cost": math.inf,
                           "reason": "WYKLUCZONY: gra gościa na .130 — gość ma pierwszeństwo"})
            continue
        cost, breakdown = _transport_cost(n, need_gb, prefer_model)
        scored.append({"node": n["node"], "gpu": n.get("gpu"), "free_gb": n.get("free_gb"),
                       "temp_c": n.get("temp_c"), "transport_cost": cost, **breakdown})
    fit = [s for s in scored if s["transport_cost"] != math.inf]
    if not fit:
        return {"decision": None, "reason": f"żaden node nie ma {need_gb}GB wolnego",
                "scored": scored}
    best = min(fit, key=lambda s: s["transport_cost"])
    warm_note = " warm" if best.get("warm", 1.0) < 1.0 else ""
    # Wierzchołek trójkąta: normalnie COMMIT (idź w wybrany węzeł). Gdy KAŻDY zdolny węzeł
    # siedzi w CTL, to nie jest wybór „najmniej złego" — to sygnał BREAK: zatrzymaj rotację
    # i oddaj decyzję wyżej, zamiast dokładać kolejny obrót wokół zepsutego miejsca.
    wszystkie_ctl = bool(fit) and all(s.get("ctl") for s in fit)
    wierzcholek = "BREAK" if wszystkie_ctl else "COMMIT"
    decyzja = None if wszystkie_ctl else best["node"]
    powod = (
        f"BREAK: wszystkie zdolne węzły w CTL (rotacja wokół porażek) — "
        f"nie relokuję, napraw przyczynę zamiast iterować"
        if wszystkie_ctl else
        f"min transport_cost={best['transport_cost']} "
        f"(free {best['free_gb']}GB{warm_note}, teraz×{best.get('present')}, "
        f"przyszłość×{best.get('future')}, przeszłość×{best.get('past')})"
    )
    # `zapisz_wybor` domyślnie False: pick_node bywa wołane z GET-a (podgląd rankingu), a
    # ODCZYT NIE MOŻE ZMIENIAĆ STANU UCZENIA. Wcześniej każde zapytanie dopisywało `picked`
    # — w dzienniku 2 z 3 wpisów były śladami po samym sprawdzaniu, nie po pracy.
    if decyzja is not None and zapisz_wybor:
        record_outcome(decyzja, "picked", f"need={need_gb}GB cost={best['transport_cost']}", need_gb)
    return {"decision": decyzja, "vertex": wierzcholek, "reason": powod,
            "free_gb": best["free_gb"], "transport_cost": best["transport_cost"],
            "czas": {"przeszlosc": best.get("past"), "terazniejszosc": best.get("present"),
                     "przyszlosc": best.get("future"), "trajektoria": best.get("trajektoria")},
            "scored": sorted(scored, key=lambda s: s["transport_cost"])}
