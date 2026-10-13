"""统一高低点结构：普通结构突破 + 双顶/双底标签 + 头肩连续特征（structure-v0，探索；配置 data/backtest/structure-v0/pattern_config.json）。

纯计算。时序：第 t 根收盘时只用确认根 ≤ t 的局部点（分形，影线价）与背景点（2ATR 收盘之字形）；事件在 t 收盘后可知，t+1 开盘入场。
每个突破（以被跌破/突破的那个局部点为键）只记一次；形态是普通突破的子集标签，不另计一票。
"""
from __future__ import annotations

from undertow.analyze import price_structure as ps

EPS_TOPS_ATR = 0.5
DEPTH_MIN_ATR = 1.0
MAX_BARS_H1_H2 = 60


def _bg_state(zz_known: list) -> str:
    hs = [p.price for p in zz_known if p.kind == "H"][-2:]
    ls = [p.price for p in zz_known if p.kind == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return "none"
    if hs[1] > hs[0] and ls[1] > ls[0]:
        return "up"
    if hs[1] < hs[0] and ls[1] < ls[0]:
        return "down"
    return "none"


def _project(piv_sorted: list, upto: int) -> list:
    return ps.alternating([p for p in piv_sorted if p.conf <= upto])


def breaks(bars: list, atr: list | None = None) -> list:
    """逐根扫描普通结构突破。返回事件 dict 列表（含双顶/双底标签与头肩特征）。"""
    atr = atr if atr is not None else ps.wilder_atr(bars)
    frac = sorted(ps.fractal_pivots(bars), key=lambda p: (p.conf, p.idx))
    zz = ps.zigzag(bars, atr)
    C = [b[4] for b in bars]
    out, used = [], set()
    fi = zi = 0
    known_f: list = []
    known_z: list = []
    seq: list = []
    for t in range(len(bars)):
        changed = False
        while fi < len(frac) and frac[fi].conf <= t:
            known_f.append(frac[fi]); fi += 1; changed = True
        while zi < len(zz) and zz[zi].conf <= t:
            known_z.append(zz[zi]); zi += 1
        if changed:
            seq = ps.alternating(known_f)
        if len(seq) < 2 or t < ps.WARMUP or atr[t - 1] is None:
            continue
        last, prev = seq[-1], seq[-2]
        if last.kind == "H":            # 看跌突破：跌破最近高点之前的低点
            direction, lvl, s = "bearish", prev.price, -1
        else:
            direction, lvl, s = "bullish", prev.price, 1
        key = (direction, prev.idx)
        if key in used or not (s * C[t] > s * lvl):
            continue
        used.add(key)
        A = atr[t - 1]
        bg = _bg_state(known_z)
        ev = {"t": t, "direction": direction, "level": lvl, "level_idx": prev.idx, "level_conf": prev.conf,
              "last_idx": last.idx, "last_conf": last.conf, "last_price": last.price, "atr_geom": A,
              "background": bg, "reversal": (bg == "up" and direction == "bearish") or (bg == "down" and direction == "bullish"),
              "late_confirmation": last.conf == t, "break_depth_atr": round(abs(C[t] - lvl) / A, 4)}
        if ev["late_confirmation"]:
            first = next((i for i in range(last.idx, t + 1) if s * C[i] > s * lvl), t)
            ev["first_cross_idx"] = first
        # —— 双顶 / 双底：投影最后三点 X1 → M → X2 ——
        ev["double"] = None
        if len(seq) >= 3 and seq[-3].kind == last.kind:
            x1, x2 = seq[-3], last
            diff = abs(x2.price - x1.price) / A
            depth = (min(x1.price, x2.price) - lvl) / A if direction == "bearish" else (lvl - max(x1.price, x2.price)) / A
            gap = x2.idx - x1.idx
            ev["double"] = {"x1_idx": x1.idx, "x1_price": x1.price, "diff_atr": round(diff, 4), "depth_atr": round(depth, 4),
                            "bars_between": gap, "second_vs_first": "higher" if x2.price > x1.price else ("lower" if x2.price < x1.price else "equal"),
                            "is_pattern": diff <= EPS_TOPS_ATR and depth >= DEPTH_MIN_ATR and gap <= MAX_BARS_H1_H2}
        # —— 头肩：投影最后五点 X1 M1 X2 M2 X3，只记连续特征 ——
        ev["hs"] = None
        if len(seq) >= 5 and seq[-5].kind == last.kind:
            x1, m1, x2, m2, x3 = seq[-5:]
            sgn = 1 if last.kind == "H" else -1
            slope = (m2.price - m1.price) / (m2.idx - m1.idx) if m2.idx != m1.idx else 0.0
            neck_t = m1.price + slope * (t - m1.idx)
            ev["hs"] = {"head_prominence_atr": round(sgn * (x2.price - (max if sgn > 0 else min)(x1.price, x3.price)) / A, 4),
                        "shoulder_diff_atr": round(abs(x1.price - x3.price) / A, 4),
                        "neckline_slope_atr_per_bar": round(slope / A, 5),
                        "left_right_bars_ratio": round((x2.idx - x1.idx) / max(1, x3.idx - x2.idx), 4),
                        "close_vs_sloped_neckline_atr": round((C[t] - neck_t) / A, 4)}
        out.append(ev)
    return out


def outcome(bars: list, atr: list, t: int, direction: str, ks=(2.0, 1.0, 3.0), days: int = 10) -> dict | None:
    """t+1 开盘入场后的屏障与 MAE/MFE（单位 ATR[入场前一根]）。窗口不完整 → None。"""
    e = t + 1
    if e + days - 1 >= len(bars) or atr[e - 1] is None:
        return None
    d = "short" if direction == "bearish" else "long"
    s = -1 if d == "short" else 1
    o, A = bars[e][1], atr[e - 1]
    res = {}
    for k in ks:
        lvl = o - s * k * A
        res[f"{k:g}"] = ps.barrier_survival(bars, e, lvl, d, days)
    adv = [(o - bars[i][3]) if s > 0 else (bars[i][2] - o) for i in range(e, e + days)]
    fav = [(bars[i][2] - o) if s > 0 else (o - bars[i][3]) for i in range(e, e + days)]
    return {"survive": res, "mae_atr": round(max(adv) / A, 4), "mfe_atr": round(max(fav) / A, 4), "entry_i": e}
