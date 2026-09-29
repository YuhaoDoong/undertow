"""到期日磁吸与墙定义的【历史探索】计算（Codex 025 §三；草案 docs/prereg/2026-09-29_expiry_pin_v0.md）。

纯计算、无 I/O。探索性描述，不是检验：这批快照在墙位研究里看过，参数都是事前拍定的设计值（未校准），不按结果调。

- pull = (|open − K| − |close − K|) / ATR14：正值 = 收盘比开盘更靠近 K。ATR14 只用该段开始【之前】的日线。
- K* 由开始前可得的 OI 决定（快照 = 开始前一交易日收盘结算），参考价 = 开始前最后一个收盘，只看 ±BAND 内行权价。
- 安慰剂（同日、同方向、同距离）：同一组可挂牌行权价中，与 K* 在开盘价同一侧、|open − K|/ATR 落在同一距离分箱、
  且不是 K* 本身的全部行权价；其 pull 等权平均。墙的增量 = pull(K*) − 安慰剂均值。分箱内没有别的行权价 → 不匹配（报告数量）。
- 三种墙定义（只比较，不择优）：
  W0 = D 起 ≤ 14 天所有到期的 OI 按行权价加总取最大（C+P 合并）；
  W1 = D 起最近一个 M/Q 到期自己的最大 OI 行权价；
  W3 = D 起 ≤ 14 天 Σ|Γ|×OI 按行权价取最大；C、P 各算一份再合并一份。它是 gamma-OI 强度代理，不是做市商净 gamma。
"""
from __future__ import annotations

import random
import statistics
from collections import defaultdict
from datetime import date

from undertow.analyze.expiry_type import classify

BAND = 0.10
BINS = (0.5, 1.0, 2.0)          # |open−K|/ATR 的距离分箱界（设计值，未校准）
NEAR_DAYS = 14


def pull(open_: float, close: float, k: float, atr: float) -> float:
    return (abs(open_ - k) - abs(close - k)) / atr


def dist_bin(x: float) -> int:
    return sum(x >= b for b in BINS)


def _argmax(weights: dict, ref: float):
    """权重最大的行权价；并列取离参考价近的，再并列取低的（确定性）。"""
    if not weights:
        return None
    return min(weights, key=lambda k: (-weights[k], abs(k - ref), k))


def _in_band(k: float, ref: float, band: float = BAND) -> bool:
    return abs(k / ref - 1) <= band


def max_oi_strike(contracts, expiries: set, ref: float):
    w = defaultdict(float)
    for c in contracts:
        if c.expiry in expiries and _in_band(c.strike, ref):
            w[c.strike] += c.open_interest
    w = {k: v for k, v in w.items() if v > 0}
    return _argmax(w, ref)


def near_expiries(contracts, d: date, days: int = NEAR_DAYS) -> set:
    return {c.expiry for c in contracts if 0 <= (c.expiry - d).days <= days}


def wall_w0(contracts, d: date, ref: float):
    return max_oi_strike(contracts, near_expiries(contracts, d), ref)


def wall_w1(contracts, d: date, ref: float):
    mq = sorted({c.expiry for c in contracts if c.expiry >= d and (classify(c.expiry)["type"] in ("M", "Q"))})
    return (max_oi_strike(contracts, {mq[0]}, ref), mq[0]) if mq else (None, None)


def wall_w3(contracts, d: date, ref: float, kind: str | None = None):
    ex = near_expiries(contracts, d)
    w = defaultdict(float)
    for c in contracts:
        if c.expiry in ex and _in_band(c.strike, ref) and (kind is None or c.kind == kind):
            w[c.strike] += abs(c.gamma) * c.open_interest
    return _argmax({k: v for k, v in w.items() if v > 0}, ref)


def listed_strikes(contracts, expiries: set, ref: float) -> list:
    return sorted({c.strike for c in contracts if c.expiry in expiries and _in_band(c.strike, ref)})


def placebos(strikes, open_: float, k_star: float, atr: float) -> list:
    side = 1 if k_star >= open_ else -1
    b = dist_bin(abs(open_ - k_star) / atr)
    return [k for k in strikes if k != k_star and (1 if k >= open_ else -1) == side
            and dist_bin(abs(open_ - k) / atr) == b]


def wall_row(open_: float, close: float, atr: float, k_star: float, strikes) -> dict:
    pw = pull(open_, close, k_star, atr)
    pl = placebos(strikes, open_, k_star, atr)
    pp = statistics.fmean(pull(open_, close, k, atr) for k in pl) if pl else None
    return {"k": k_star, "dist_atr": round(abs(open_ - k_star) / atr, 4), "bin": dist_bin(abs(open_ - k_star) / atr),
            "pull_wall": pw, "pull_placebo": pp, "diff": (pw - pp) if pp is not None else None, "n_placebo": len(pl),
            "placebos": pl, "open": open_, "close": close, "atr": atr}


def cluster_bootstrap(rows: list, key, cluster, *, B: int = 2000, seed: int = 20260929) -> dict:
    """按簇（日期）重抽样的均值与 95% 百分位区间。簇内样本不独立（同日多品种、同一到期多品种）。"""
    by = defaultdict(list)
    for r in rows:
        v = key(r)
        if v is not None:
            by[cluster(r)].append(v)
    cl = list(by.values())
    xs = [v for c in cl for v in c]
    if not xs:
        return {"n": 0, "clusters": 0, "mean": None, "median": None, "lo": None, "hi": None}
    if len(cl) < MIN_CLUSTERS:                 # Codex 026：单簇/少簇的零宽或窄区间不代表精确
        return {"n": len(xs), "clusters": len(cl), "mean": statistics.fmean(xs), "median": statistics.median(xs),
                "lo": None, "hi": None, "interval": "not_estimable（簇数不足，只作案例）"}
    rnd = random.Random(seed)
    means = []
    for _ in range(B):
        pick = [v for _ in cl for v in rnd.choice(cl)]
        means.append(statistics.fmean(pick))
    means.sort()
    return {"n": len(xs), "clusters": len(cl), "mean": statistics.fmean(xs), "median": statistics.median(xs),
            "lo": means[int(0.025 * B)], "hi": means[int(0.975 * B) - 1]}


MIN_CLUSTERS = 5          # 簇数少于此 → 不给区间（只作案例）；设计值


def block_bootstrap(rows: list, key, date_of, *, block: int, B: int = 2000, seed: int = 20260929) -> dict:
    """跨品种同步的移动块 bootstrap：把所有出现过的日期排序，按长度 block 的连续日期块重抽样，块内全部行一起进样本。
    用于持有期跨日重叠的统计（如 E−2 → E）；块长需做敏感性。簇数 < MIN_CLUSTERS → 不给区间。"""
    by = defaultdict(list)
    for r in rows:
        v = key(r)
        if v is not None:
            by[date_of(r)].append(v)
    ds = sorted(by)
    xs = [v for d in ds for v in by[d]]
    out = {"n": len(xs), "clusters": len(ds), "block": block,
           "mean": statistics.fmean(xs) if xs else None, "lo": None, "hi": None}
    if len(ds) < MIN_CLUSTERS or not xs:
        out["interval"] = "not_estimable（簇数不足，只作案例）"
        return out
    b = min(block, len(ds))
    starts = list(range(len(ds) - b + 1))
    rnd = random.Random(seed)
    means = []
    for _ in range(B):
        pick = []
        while len(pick) < len(xs):
            i = rnd.choice(starts)
            for d in ds[i:i + b]:
                pick.extend(by[d])
        means.append(statistics.fmean(pick[:len(xs)]))
    means.sort()
    out.update(lo=means[int(0.025 * B)], hi=means[int(0.975 * B) - 1], interval="moving_block")
    return out


# ═══ v3 同品种事前匹配（docs/prereg/2026-09-29_expiry_pin_v3_match_protocol.md；探索，已看过 v1/v2）═══
MATCH_WINDOW = 20          # 前后交易日
VOL_LOOKBACK = 60          # 波动档参照：截至 D−1（含）的 60 个交易日的 ATR14/收盘


def _atr14(h, lo, c):
    from undertow.analyze.technicals import _atr
    return _atr(h, lo, c, 14)


def match_features(dates: list, ohlc: dict, d, k: float, event: str = "unknown") -> dict | None:
    """D−1 收盘时已知的匹配字段；历史不足 → None。dates = 该品种全部日线日期（升序），ohlc[日] = (开,高,低,收)。"""
    import bisect
    i = bisect.bisect_left(dates, d)                         # dates[:i] 严格早于 D
    need = VOL_LOOKBACK + 15
    if i < max(need, 21):
        return None
    H = [ohlc[x][1] for x in dates[:i]]; L = [ohlc[x][2] for x in dates[:i]]; C = [ohlc[x][3] for x in dates[:i]]
    ratios = []
    for j in range(i - VOL_LOOKBACK, i):                     # 截至 D−1（含）的 60 个交易日
        a = _atr14(H[:j + 1], L[:j + 1], C[:j + 1])
        if a is None:
            return None
        ratios.append(a / C[j])
    atr = _atr14(H, L, C)
    v = atr / C[-1]
    q1, q2 = statistics.quantiles(ratios, n=3, method="exclusive")
    c_prev = C[-1]
    return {"c_prev": c_prev, "atr": atr, "vol_ratio": v, "vol_tercile": 0 if v < q1 else (1 if v < q2 else 2),
            "direction": "above" if k > c_prev else ("below" if k < c_prev else "equal"),
            "dist_prev_atr": abs(c_prev - k) / atr, "dist_bin": dist_bin(abs(c_prev - k) / atr),
            "trend20": (C[-1] > C[-21]) - (C[-1] < C[-21]), "event": event, "day_index": i}


def match_key(f: dict) -> tuple:
    return (f["direction"], f["dist_bin"], f["vol_tercile"], f["trend20"], f["event"])


def match_pairs(rows: list, window: int = MATCH_WINDOW) -> dict:
    """rows：{sym, d, is_expiry, feat(None=历史不足), …}。每个到期日在同品种、前后 window 个交易日内取字段全同的最近非到期日；
    距离相同取日期早者；对照可复用并计次；无对照记 unmatched（原因分三类），不放宽。"""
    pairs, unmatched, reuse = [], [], {}
    ctrl = [r for r in rows if not r["is_expiry"] and r["feat"] is not None]
    for e in sorted((r for r in rows if r["is_expiry"]), key=lambda r: (r["sym"], r["d"])):
        if e["feat"] is None:
            unmatched.append({**e, "why": "history_lt_60"}); continue
        same = [c for c in ctrl if c["sym"] == e["sym"] and match_key(c["feat"]) == match_key(e["feat"])]
        near = [c for c in same if abs(c["feat"]["day_index"] - e["feat"]["day_index"]) <= window]
        if not near:
            unmatched.append({**e, "why": "no_control_in_window" if same else "no_control_same_cell"}); continue
        c = min(near, key=lambda c: (abs(c["feat"]["day_index"] - e["feat"]["day_index"]), c["d"]))
        key = (c["sym"], c["d"])
        reuse[key] = reuse.get(key, 0) + 1
        pairs.append({"expiry": e, "control": c, "gap_days": c["feat"]["day_index"] - e["feat"]["day_index"]})
    for p in pairs:
        p["control_reuse"] = reuse[(p["control"]["sym"], p["control"]["d"])]
    cells = {}
    for r in rows:
        if r["feat"] is None:
            continue
        k = (r["sym"], *match_key(r["feat"]))
        c = cells.setdefault(k, {"expiry": 0, "nonexpiry": 0})
        c["expiry" if r["is_expiry"] else "nonexpiry"] += 1
    return {"pairs": pairs, "unmatched": unmatched, "cells": cells, "reuse": reuse}
