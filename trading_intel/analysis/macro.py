"""宏观背景分析（确定性计算，无 I/O）——黄金/白银的【基本面驱动】。

逻辑（教科书级、可解释）：
  * **10年实际利率(TIPS, DFII10)**：黄金无息，实际利率=持有黄金的机会成本。
    实际利率【下行】→利多金/银（最强驱动）；上行→利空。
  * **美元指数(DTWEXBGS)**：金以美元计价，美元【走弱】→利多；走强→利空。
  * **通胀预期(T10YIE 盈亏平衡)**：上行→避险/抗通胀需求→利多（较弱）。
  原油：实际利率不直接驱动，仅保留美元一项（较弱）。

立场：宏观是【背景/交叉验证】，不是择时。2024–2026 金价因央行购金一度与实际利率
脱钩，故标【中/低】可信度，仅与持仓/期权微观结构共振时才加重。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 驱动注册表：invert=True 表示该指标【下行】利多金
_METAL_DRIVERS = [
    {"key": "real_yield", "name": "10年实际利率(TIPS)", "series": "DFII10",
     "unit": "%", "invert": True, "thr": 0.05, "weight": 1.5, "rel": "中", "pct": False},
    {"key": "dollar", "name": "美元指数(广义)", "series": "DTWEXBGS",
     "unit": "", "invert": True, "thr": 0.4, "weight": 1.0, "rel": "中", "pct": True},
    {"key": "breakeven", "name": "通胀预期(10y盈亏平衡)", "series": "T10YIE",
     "unit": "%", "invert": False, "thr": 0.03, "weight": 0.6, "rel": "低", "pct": False},
]
_ENERGY_DRIVERS = [
    {"key": "dollar", "name": "美元指数(广义)", "series": "DTWEXBGS",
     "unit": "", "invert": True, "thr": 0.4, "weight": 0.6, "rel": "低", "pct": True},
]


def drivers_for(asset_class: str) -> list[dict]:
    return _ENERGY_DRIVERS if asset_class == "energy" else _METAL_DRIVERS


def series_ids_for(asset_class: str) -> list[str]:
    return [d["series"] for d in drivers_for(asset_class)]


@dataclass(frozen=True)
class MacroDriver:
    key: str
    name: str
    series_id: str
    latest: float
    chg_20d: float       # 原始单位变化（利率=pp；美元=%）
    chg_60d: float
    unit: str
    vote_sign: int       # +1 利多金/银 · -1 利空 · 0 中性
    weight: float
    reliability: str
    detail: str


@dataclass(frozen=True)
class MacroAnalysis:
    asof: str
    asset_class: str
    drivers: list[MacroDriver] = field(default_factory=list)
    macro_bias: str = "中性"
    macro_score: float = 0.0


def _change(series: list[tuple], n: int, pct: bool) -> float:
    if len(series) < 2:
        return 0.0
    latest = series[-1][1]
    base = series[max(0, len(series) - 1 - n)][1]
    if pct:
        return (latest / base - 1.0) * 100.0 if base else 0.0
    return latest - base


def analyze_macro(series_map: dict[str, list[tuple]], *, asset_class: str) -> MacroAnalysis:
    drivers: list[MacroDriver] = []
    asof = ""
    score = 0.0
    for spec in drivers_for(asset_class):
        series = series_map.get(spec["series"])
        if not series:
            continue
        asof = max(asof, series[-1][0].isoformat())
        latest = series[-1][1]
        c20 = _change(series, 20, spec["pct"])
        c60 = _change(series, 60, spec["pct"])
        # 方向：超过噪音阈值才投票
        sign = 0
        if abs(c20) >= spec["thr"]:
            raw = 1 if c20 > 0 else -1
            sign = -raw if spec["invert"] else raw  # invert: 下行(=负)利多(+1)
        unit = spec["unit"] or ("" if not spec["pct"] else "")
        chg_str = f"{c20:+.2f}{'%' if spec['pct'] else 'pp'}（近20日）"
        dir_cn = "利多" if sign > 0 else ("利空" if sign < 0 else "中性")
        latest_str = f"{latest:.2f}{spec['unit']}"
        drivers.append(MacroDriver(
            key=spec["key"], name=spec["name"], series_id=spec["series"],
            latest=latest, chg_20d=c20, chg_60d=c60, unit=spec["unit"],
            vote_sign=sign, weight=spec["weight"] if sign else 0.0, reliability=spec["rel"],
            detail=f"{spec['name']} {latest_str}，{chg_str} → {dir_cn}",
        ))
        score += (spec["weight"] if sign else 0.0) * sign

    if score >= 1.5:
        bias = "偏多"
    elif score <= -1.5:
        bias = "偏空"
    else:
        bias = "中性"
    return MacroAnalysis(asof=asof, asset_class=asset_class, drivers=drivers,
                         macro_bias=bias, macro_score=round(score, 2))
