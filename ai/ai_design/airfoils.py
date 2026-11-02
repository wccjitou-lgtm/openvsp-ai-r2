# -*- coding: utf-8 -*-
"""翼型库与自动选择。

数据来源优先级：
1. 环境变量 ``OPENVSP_AIRFOIL_LIBRARY`` 指向的 JSON；
2. ``%LOCALAPPDATA%/OpenVSP-R2/airfoils/profili_library.json``（从本机
   已授权的 Profili 安装导入，不随安装包分发）；
3. ``ai/data/profili_library.json``（开发者本机导入，打包时排除）；
4. 程序内置的公开 NACA 4 位翼型（可离线使用）。

Profili 数据库属于商业软件；本模块只读取用户本机的授权安装并为用户本机
生成缓存，不修改、不重新分发 Profili 二进制或数据库。
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

_CACHE: dict[str, Any] | None = None
_CACHE_PATH: str | None = None

_PROFILE_KEYWORDS = {
    "flying_wing": ["reflex", "tailless", "horten", "zagi", "s5010", "plank",
                    "mh ", "mh-", "hs ", "pw ", "eh ", "flying"],
    "high_speed": ["64a", "65a", "6-series", "6 series", "naca 16", "laminar",
                   "nlf", "rg", "shark", "fx ", "eppler"],
    "low_re": ["ag ", "ag-", "e ", "fx ", "clark", "s1223", "goe", "mh ",
               "sg ", "be50", "dae"],
    "endurance": ["mh ", "ag ", "e ", "fx ", "naca 64", "naca 65", "laminar"],
}


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("OPENVSP_AIRFOIL_LIBRARY")
    if env:
        paths.append(Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(Path(local) / "OpenVSP-R2" / "airfoils" / "profili_library.json")
    paths.append(Path(__file__).resolve().parent.parent / "data" / "profili_library.json")
    return paths


def _naca4(name: str) -> dict[str, Any] | None:
    digits = re.sub(r"\D", "", name)
    if len(digits) != 4:
        return None
    m = int(digits[0]) / 100.0
    p = int(digits[1]) / 10.0 if int(digits[1]) > 0 else 0.4
    t = int(digits[2:]) / 100.0
    if t <= 0 or t > 0.3:
        return None
    upper: list[list[float]] = []
    lower: list[list[float]] = []
    for i in range(41):
        x = round(i / 40.0, 5)
        yt = 5.0 * t * (0.2969 * math.sqrt(x) - 0.1260 * x - 0.3516 * x * x
                        + 0.2843 * x ** 3 - 0.1015 * x ** 4)
        if m <= 0:
            yc = 0.0
            dy = 0.0
        elif x < p:
            yc = m / (p * p) * (2.0 * p * x - x * x)
            dy = 2.0 * m / (p * p) * (p - x)
        else:
            yc = m / ((1 - p) ** 2) * ((1 - 2 * p) + 2 * p * x - x * x)
            dy = 2.0 * m / ((1 - p) ** 2) * (p - x)
        theta = math.atan(dy)
        xu = x - yt * math.sin(theta)
        yu = yc + yt * math.cos(theta)
        xl = x + yt * math.sin(theta)
        yl = yc - yt * math.cos(theta)
        upper.append([round(xu, 5), round(yu, 5)])
        lower.append([round(xl, 5), round(yl, 5)])
    return {
        "id": 0,
        "name": name.upper(),
        "thickness_pct": t * 100.0,
        "camber_pct": m * 100.0,
        "camber_pos_pct": p * 100.0 if m > 0 else 0.0,
        "upper": upper,
        "lower": lower,
        "source": "builtin-naca4",
    }


def _builtin_library() -> list[dict[str, Any]]:
    result = []
    for name in ("NACA0009", "NACA0012", "NACA2412", "NACA4412",
                 "NACA23012", "NACA64012", "NACA65012"):
        item = _naca4(name)
        if item:
            result.append(item)
    return result


def load_library(force: bool = False) -> dict[str, Any]:
    global _CACHE, _CACHE_PATH
    if _CACHE is not None and not force:
        return _CACHE
    for path in _candidate_paths():
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("airfoils") if isinstance(data, dict) else data
            if not isinstance(items, list) or not items:
                continue
            clean = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                upper = item.get("upper") or []
                lower = item.get("lower") or []
                if len(upper) < 8 or len(lower) < 8:
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                clean.append({
                    "id": item.get("id"),
                    "name": name,
                    "thickness_pct": float(item.get("thickness_pct") or 0.0),
                    "camber_pct": float(item.get("camber_pct") or 0.0),
                    "camber_pos_pct": float(item.get("camber_pos_pct") or 0.0),
                    "upper": [[float(p[0]), float(p[1])] for p in upper if len(p) >= 2],
                    "lower": [[float(p[0]), float(p[1])] for p in lower if len(p) >= 2],
                    "source": "profili-local",
                })
            if clean:
                _CACHE = {"path": str(path), "items": clean,
                          "source": data.get("source") if isinstance(data, dict) else ""}
                _CACHE_PATH = str(path)
                return _CACHE
        except Exception:
            continue
    _CACHE = {"path": "", "items": _builtin_library(), "source": "builtin"}
    _CACHE_PATH = ""
    return _CACHE


def library_info() -> dict[str, Any]:
    lib = load_library()
    return {"path": lib["path"], "count": len(lib["items"]), "source": lib["source"]}


def find_airfoil(name: str) -> dict[str, Any] | None:
    query = str(name or "").strip().lower()
    if not query:
        return None
    items = load_library()["items"]
    for item in items:
        if item["name"].lower() == query:
            return item
    # 允许 "NACA2412" 与 "NACA 2412"、Profili 名称后缀 "MH 60  10,08%" 等
    compact = re.sub(r"[\s_\-]+", "", query)
    for item in items:
        if re.sub(r"[\s_\-]+", "", item["name"].lower()) == compact:
            return item
        if item["name"].lower().startswith(query + " "):
            return item
    naca = _naca4(name) if re.search(r"\d{4,5}", name or "") else None
    return naca


def _atmosphere(altitude_m: float) -> tuple[float, float, float]:
    h = max(-500.0, min(float(altitude_m), 11000.0))
    temperature = 288.15 - 0.0065 * h
    pressure = 101325.0 * (temperature / 288.15) ** (9.80665 / (287.05287 * 0.0065))
    rho = pressure / (287.05287 * temperature)
    mu = 1.458e-6 * temperature ** 1.5 / (temperature + 110.4)
    sound = math.sqrt(1.4 * 287.05287 * temperature)
    return rho, mu, sound


def estimate_conditions(spec: Any, geometry: dict[str, Any] | None = None) -> dict[str, float]:
    altitude = float(getattr(spec, "altitude_m", 0.0) or 0.0)
    velocity = float(getattr(spec, "speed_cruise_ms", 0.0) or 0.0)
    mac = 0.0
    if geometry:
        sizing = geometry.get("sizing") or {}
        wing = geometry.get("wing") or {}
        mac = float(sizing.get("mac_m") or wing.get("root_chord_m") or 0.0)
    rho, mu, sound = _atmosphere(altitude)
    reynolds = rho * max(velocity, 0.1) * max(mac, 0.05) / max(mu, 1e-12)
    mach = velocity / max(sound, 1e-6)
    return {"density_kgm3": rho, "viscosity": mu, "sound_speed_ms": sound,
            "reynolds": reynolds, "mach": mach, "mac_m": mac,
            "velocity_ms": velocity, "altitude_m": altitude}


def classify_purpose(spec: Any, conditions: dict[str, float]) -> str:
    config = str(getattr(spec, "configuration", "") or "")
    if config == "flying_wing":
        return "flying_wing"
    if conditions["mach"] >= 0.42 or conditions["velocity_ms"] >= 100.0:
        return "high_speed"
    if conditions["reynolds"] < 180000.0:
        return "low_re"
    ar = float(getattr(spec, "aspect_ratio", 0.0) or 0.0)
    if config in ("conventional", "vtail") and conditions["velocity_ms"] <= 45.0 \
            and ar >= 10.0:
        return "endurance"
    return "general"


def _keyword_hit(name: str, purpose: str) -> bool:
    low = name.lower()
    return any(keyword in low for keyword in _PROFILE_KEYWORDS.get(purpose, []))


def _score(item: dict[str, Any], purpose: str, conditions: dict[str, float]) -> tuple[float, str]:
    t = float(item["thickness_pct"])
    c = float(item["camber_pct"])
    targets = {
        "flying_wing": (10.0, 2.0),
        "high_speed": (8.5, 1.2),
        "low_re": (12.0, 4.2),
        "endurance": (12.0, 3.2),
        "general": (11.5, 3.0),
    }
    target_t, target_c = targets[purpose]
    score = 100.0 - 2.2 * abs(t - target_t) - 7.0 * abs(c - target_c)
    reasons = [f"t/c={t:.1f}% (目标{target_t:.1f}%)",
               f"camber={c:.1f}% (目标{target_c:.1f}%)"]
    if _keyword_hit(item["name"], purpose):
        score += 18.0
        reasons.append("名称/系列匹配任务")
    if purpose == "flying_wing":
        low = item["name"].lower()
        if any(k in low for k in ("reflex", "horten", "mh ", "hs ", "pw ", "s5010", "zagi", "eh ")):
            score += 30.0
            reasons.append("适合无尾/飞翼的反射翼型系列")
        if c > 4.0:
            score -= 15.0
            reasons.append("camber 偏高，飞翼配平不利")
    if purpose == "high_speed":
        if t > 13.0:
            score -= 30.0
            reasons.append("厚度偏大，高速阻力高")
        if c > 3.0:
            score -= 20.0
            reasons.append("camber 偏大，高速不利")
    if purpose == "low_re":
        if t < 8.0:
            score -= 20.0
            reasons.append("低 Re 下过薄，升力不足")
        if c < 1.5:
            score -= 12.0
            reasons.append("低 Re 下 camber 偏小")
    if "joukow" in item["name"].lower():
        score -= 25.0
        reasons.append("Joukowski 基准翼型，工程适用性低")
    if t > 18.0 or c > 8.0:
        score -= 20.0
    return score, "; ".join(reasons)


def shortlist(purpose: str = "", reynolds: float | None = None,
              mach: float | None = None, count: int = 10,
              thickness_min: float | None = None,
              thickness_max: float | None = None) -> dict[str, Any]:
    purpose = purpose or "general"
    if purpose not in _PROFILE_KEYWORDS and purpose not in ("general",):
        purpose = "general"
    items = load_library()["items"]
    fake_spec = type("S", (), {
        "configuration": "flying_wing" if purpose == "flying_wing" else "conventional",
        "aspect_ratio": 12.0 if purpose == "endurance" else 7.0,
        "altitude_m": 0.0, "speed_cruise_ms": 30.0,
    })()
    conditions = {"mach": mach if mach is not None else 0.1,
                  "reynolds": reynolds if reynolds is not None else 250000.0,
                  "velocity_ms": 30.0}
    scored = []
    for item in items:
        t = float(item["thickness_pct"])
        if thickness_min is not None and t < thickness_min:
            continue
        if thickness_max is not None and t > thickness_max:
            continue
        score, reason = _score(item, purpose, conditions)
        scored.append((score, item, reason))
    scored.sort(key=lambda row: row[0], reverse=True)
    result = []
    for score, item, reason in scored[:max(1, int(count))]:
        result.append({
            "name": item["name"],
            "thickness_pct": round(float(item["thickness_pct"]), 2),
            "camber_pct": round(float(item["camber_pct"]), 2),
            "camber_pos_pct": round(float(item["camber_pos_pct"]), 1),
            "score": round(score, 1),
            "reason": reason,
            "source": item.get("source", ""),
        })
    return {"ok": True, "purpose": purpose, "reynolds": reynolds, "mach": mach,
            "library": library_info(), "candidates": result}


def select_airfoil(spec: Any, geometry: dict[str, Any] | None = None,
                   logger: Any = None) -> dict[str, Any] | None:
    conditions = estimate_conditions(spec, geometry)
    purpose = classify_purpose(spec, conditions)
    result = shortlist(purpose=purpose, reynolds=conditions["reynolds"],
                       mach=conditions["mach"], count=8)
    candidates = result.get("candidates") or []
    if not candidates:
        return None
    if logger:
        logger.status(
            f"[airfoil] 自动选择: purpose={purpose}, Re={conditions['reynolds']:.0f}, "
            f"Ma={conditions['mach']:.3f}, 库={result['library']['count']} 个翼型"
            f"（来源: {result['library']['source'] or 'unknown'}）")
    chosen = dict(candidates[0])
    chosen.update({"purpose": purpose, "candidates": candidates,
                   "reynolds": round(conditions["reynolds"], 1),
                   "mach": round(conditions["mach"], 4),
                   "library_source": result["library"]["source"],
                   "library_path": result["library"]["path"]})
    if logger:
        logger.status(f"[airfoil] 选用 {chosen['name']}：{chosen['reason']}")
    return chosen


def write_selig_dat(name: str, out_dir: str | Path) -> str:
    item = find_airfoil(name)
    if item is None:
        raise ValueError(f"翼型库中找不到: {name!r}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", item["name"]).strip("_") or "airfoil"
    path = out / f"{safe}.dat"
    upper = sorted(item["upper"], key=lambda p: p[0])
    lower = sorted(item["lower"], key=lambda p: p[0])
    lines = [item["name"]]
    # Selig: upper from TE to LE, then lower from LE to TE.
    for x, y in reversed(upper):
        lines.append(f"{x:.6f} {y:.6f}")
    for idx, (x, y) in enumerate(lower):
        if idx == 0 and abs(x) < 1e-9:
            continue
        lines.append(f"{x:.6f} {y:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


__all__ = ["load_library", "library_info", "find_airfoil", "shortlist",
           "select_airfoil", "estimate_conditions", "write_selig_dat"]
