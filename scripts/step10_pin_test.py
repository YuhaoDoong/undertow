"""P6：到期日 pin 的预登记检验（2026-09-26 写于看子集结果之前）。

背景：学术上被证实的「墙效应」是到期日收盘向行权价聚集（Ni, Pearson, Poteshman 2005，JFE，个股），
不是「几天内挡住价格」。W04 测的是后者。本脚本测前者，但只在它最可能出现的子集：
  到期前一交易日收盘 C[E−1] 距「该到期自己的最大 OI 档 K*」（±3% 带内）不超过 0.5×ATR14。
  ⚠️ 2026-09-26 Claude 已做过一次不分子集的探索（142 个到期日，更靠近 K* 的比例 50%），
     本子集定义写在看子集结果之前，但全样本已看过 —— 结论只能算半预登记。

对照（安慰剂）：同一份 E 日盘前快照里、下一个到期 E' 的最大 OI 档 K'（同样要求 |C[E−1]−K'| ≤ 0.5 ATR）。
E 日对 K' 没有到期对冲压力；若 pin 存在，|C[E]−K*| 应系统性小于 |C[E]−K'|。
统计：d = |C_E − K| / ATR14[E−1]；两组秩和检验（正态近似，单侧：真墙 < 安慰剂）；另报「收在 ±0.25 ATR 内」的比例。
单品种报告，不合并。
"""
from __future__ import annotations

import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step5_wall_hold as s5                                       # noqa: E402
from undertow.analyze.stretch import _atr_series                   # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource        # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload    # noqa: E402
from undertow.collect.store import SnapshotStore                   # noqa: E402
from undertow.core.config import load_config                       # noqa: E402

NEAR_ATR, BAND, PIN_ATR = 0.5, 0.03, 0.25


def max_oi_strike(snap, expiry, ref):
    oi = {}
    for x in snap.contracts:
        if x.expiry == expiry and abs(x.strike / ref - 1) <= BAND and x.open_interest:
            oi[x.strike] = oi.get(x.strike, 0) + x.open_interest
    return max(oi, key=oi.get) if oi else None


def ranksum_z(a, b):
    """Mann-Whitney U 的正态近似 z（a 越小 z 越负）。样本不足返回 None。"""
    if len(a) < 5 or len(b) < 5:
        return None
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, i = {}, 0
    r = [0.0] * len(allv)
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            r[k] = (i + j) / 2 + 1
        i = j + 1
    ra = sum(r[k] for k, (_, g) in enumerate(allv) if g == 0)
    n1, n2 = len(a), len(b)
    u = ra - n1 * (n1 + 1) / 2
    mu, sd = n1 * n2 / 2, math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    return (u - mu) / sd


def main():
    cfg = load_config(); store = SnapshotStore(); src = CboeHistorySource()
    print(f"子集：C[E−1] 距 K ≤ {NEAR_ATR} ATR；d=|C_E−K|/ATR；单侧秩和 z<−1.645 支持 pin")
    for key in ("gold", "silver", "wti", "qqq"):
        inst = cfg.get(key); ser = src.fetch_series(inst)
        tdays, c = list(ser.dates), list(ser.closes); idx = {d: i for i, d in enumerate(tdays)}
        atr = _atr_series(ser.highs, ser.lows, c, 14)
        by_T, _ = s5.load_days(store, key, inst.options.symbol, tdays)
        real, plac = [], []
        for E in sorted(by_T):
            if E not in idx or idx[E] == 0 or not atr[idx[E] - 1]:
                continue
            snap = snapshot_from_payload(by_T[E][1], key, inst.options.symbol)
            prev, close, a = c[idx[E] - 1], c[idx[E]], atr[idx[E] - 1]
            exps = sorted({x.expiry for x in snap.contracts if x.expiry >= E})
            if not exps or exps[0] != E:
                continue                                   # 当天不是到期日
            K = max_oi_strike(snap, E, prev)
            if K is not None and abs(prev - K) <= NEAR_ATR * a:
                real.append(abs(close - K) / a)
            if len(exps) > 1:
                K2 = max_oi_strike(snap, exps[1], prev)
                if K2 is not None and abs(prev - K2) <= NEAR_ATR * a:
                    plac.append(abs(close - K2) / a)
        z = ranksum_z(real, plac)
        f = lambda xs: (f"n={len(xs):2d} 中位 {st.median(xs):.2f}  ±{PIN_ATR}ATR 内 {sum(x <= PIN_ATR for x in xs)/len(xs):.0%}"
                        if xs else "n= 0")
        verdict = "样本不足" if z is None else ("支持 pin" if z < -1.645 else "未检出")
        print(f"  {key:7s} 真墙 {f(real)} ｜ 安慰剂 {f(plac)} ｜ z={'—' if z is None else f'{z:+.2f}'}  {verdict}")


if __name__ == "__main__":
    main()
