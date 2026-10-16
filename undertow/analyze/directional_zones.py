"""按来向区分的压力/支撑区间（外部作者二）——探索口径 dz-explore-v1。纯计算，无 I/O。

我们从作者附图读出的做法（推断，作者本人没有给可操作定义）：
  向上时压力（R 区）= 前一段下跌途中的第一段横盘整理区；
  向下时支撑（S 区）= 急涨段起涨那根大阳线的实体中段。
定义、检验与局限见 docs/prereg/2026-10-16_directional_zones_explore_v1.md；全部参数未校准。
bars 为 (date, open, high, low, close) 元组列表；atr 与 price_structure.wilder_atr 同口径。
"""
from __future__ import annotations

from dataclasses import dataclass

BOX_N = 5            # 横盘窗口根数
BOX_C = 1.5          # 窗口高低差 ≤ BOX_C·ATR
BOX_FOLLOW = 1.0     # 之后又跌 ≥ BOX_FOLLOW·ATR（不是底部横盘）
LAUNCH_K = 1.0       # 起涨阳线实体 ≥ LAUNCH_K·ATR
WINDOW = 120         # 启用后等待触及的交易日
REV_X = 1.5          # 掉头门槛（ATR）
BREAK_Y = 0.5        # 突破门槛（ATR，越过远沿）
HORIZON = 20         # 结果判定根数（含触及根）
RET_DAYS = 10        # 带符号位移的天数
PLACEBO_SHIFT = 2.0  # 安慰剂平移（ATR[启用根]）
DEDUPE = 5           # 同类区间触及根最小间隔


@dataclass
class Zone:
    kind: str        # "R"（从下方来测试的压力）/ "S"（从上方来测试的支撑）
    lo: float
    hi: float
    formed: int      # 区间最后一根的下标
    active: int      # 启用根：该段终点摆动点的确认根（收盘后可知）
    atr: float       # ATR[启用根]
    leg: tuple       # (起点极值下标, 终点极值下标)


def find_zones(bars: list, atr: list, piv: list) -> list:
    """按相邻已确认摆动点逐段找 R / S 区。piv 为 price_structure.zigzag 的输出。"""
    O = [b[1] for b in bars]
    H = [b[2] for b in bars]
    L = [b[3] for b in bars]
    C = [b[4] for b in bars]
    out = []
    for p0, p1 in zip(piv, piv[1:]):
        a_act = atr[p1.conf]
        if a_act is None:
            continue
        if p0.kind == "H" and p1.kind == "L":
            for s in range(p0.idx + 1, p1.idx - BOX_N + 1):
                a = atr[s - 1]
                if a is None:
                    continue
                e = s + BOX_N - 1
                hi = max(H[s:e + 1])
                lo = min(L[s:e + 1])
                if hi - lo <= BOX_C * a and hi < C[p0.idx] and C[p1.idx] <= lo - BOX_FOLLOW * a:
                    out.append(Zone("R", lo, hi, e, p1.conf, a_act, (p0.idx, p1.idx)))
                    break
        elif p0.kind == "L" and p1.kind == "H":
            for t in range(p0.idx + 1, p1.idx + 1):
                a = atr[t - 1]
                if a is not None and C[t] - O[t] >= LAUNCH_K * a:
                    body = C[t] - O[t]
                    out.append(Zone("S", O[t] + 0.25 * body, O[t] + 0.75 * body, t, p1.conf, a_act,
                                    (p0.idx, p1.idx)))
                    break
    return out


def shifted(z: Zone, away: int) -> Zone:
    """安慰剂：同宽平移 PLACEBO_SHIFT·ATR[启用根]。away=+1 离启用价更远，−1 更近。"""
    sign = 1 if z.kind == "R" else -1
    d = away * sign * PLACEBO_SHIFT * z.atr
    return Zone(z.kind, z.lo + d, z.hi + d, z.formed, z.active, z.atr, z.leg)


def evaluate(bars: list, atr: list, z: Zone, *, rev_x: float = REV_X) -> dict:
    """启用 → 首次触及 → 首次通过结果。返回 status / touch / outcome / ret10_atr。"""
    H = [b[2] for b in bars]
    L = [b[3] for b in bars]
    C = [b[4] for b in bars]
    n = len(bars)
    a = z.active
    r = z.kind == "R"
    if (C[a] >= z.lo) if r else (C[a] <= z.hi):
        return {"status": "crossed_before"}
    touch = None
    for t in range(a + 1, min(a + 1 + WINDOW, n)):
        if (H[t] >= z.lo) if r else (L[t] <= z.hi):
            touch = t
            break
    if touch is None:
        return {"status": "no_touch" if a + WINDOW < n else "incomplete"}
    A = atr[touch - 1]
    if A is None or A <= 0:
        return {"status": "incomplete"}
    outcome = None
    for j in range(touch, touch + HORIZON):
        if j >= n:
            outcome = "incomplete"
            break
        c = C[j]
        if r:
            if c >= z.hi + BREAK_Y * A:
                outcome = "broken"
            elif c <= z.lo - rev_x * A:
                outcome = "reversed"
        else:
            if c <= z.lo - BREAK_Y * A:
                outcome = "broken"
            elif c >= z.hi + rev_x * A:
                outcome = "reversed"
        if outcome:
            break
    if outcome is None:
        outcome = "none"
    edge = z.lo if r else z.hi
    k = touch + RET_DAYS
    ret = None if k >= n else (-1 if r else 1) * (C[k] - edge) / A
    return {"status": "event" if outcome != "incomplete" else "incomplete",
            "touch": touch, "outcome": outcome, "ret10_atr": ret}


def dedupe(events: list) -> list:
    """events: [(zone, result)]（status=event），按触及根排序，同类相距 < DEDUPE 只留第一个。"""
    kept, last = [], {}
    for z, res in sorted(events, key=lambda e: (e[1]["touch"], e[0].kind)):
        p = last.get(z.kind)
        if p is not None and res["touch"] - p < DEDUPE:
            continue
        last[z.kind] = res["touch"]
        kept.append((z, res))
    return kept
