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
            "pull_wall": pw, "pull_placebo": pp, "diff": (pw - pp) if pp is not None else None, "n_placebo": len(pl)}


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
    rnd = random.Random(seed)
    means = []
    for _ in range(B):
        pick = [v for _ in cl for v in rnd.choice(cl)]
        means.append(statistics.fmean(pick))
    means.sort()
    return {"n": len(xs), "clusters": len(cl), "mean": statistics.fmean(xs), "median": statistics.median(xs),
            "lo": means[int(0.025 * B)], "hi": means[int(0.975 * B) - 1]}
