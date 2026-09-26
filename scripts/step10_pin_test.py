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

═══ 2026-09-26 修订（Codex 005 R10）：降为探索，改逐日配对 ═══
原检验的缺陷：同一个 E 日的真墙与安慰剂共享同一个收盘，却被当成两个独立秩和样本；两组各自过距离过滤，
日期集合不一致；起点距墙不匹配；K=K' 时根本没有对照；秩和方差未校正 ties；4 个品种未做多重处理。
现在：
- 只用同一个 E 日【两者都】过 0.5 ATR 过滤、且 K≠K' 的日子，逐日配对；K=K' 的日子单独计数（同墙无对照）。
- 统计量：Δ = (d1_真 − d0_真) − (d1_安 − d0_安)，d0 = |C[E−1] − K|/ATR、d1 = |C[E] − K|/ATR，
  即「收盘距离相对起点的变化」之差；pin → Δ < 0。
- 检验：精确符号检验（单侧，Δ=0 的 ties 剔除并计数）；4 品种 Bonferroni α = 0.05/4 = 0.0125 并列。
- 身份：**探索性**。全样本已看过；即使显著，也只检验「到期日收盘向最大 OI 档聚集」，
  不支持「2–4 DTE 不破墙」或「价差盈利」。
逐日明细用 --emit 落盘（K、K'、起点与收盘距离、有符号距离、OI 集中度）。
- 检验力局限：价格路径（C[E−1]→C[E]）若同在 K 与 K' 的同一侧，两者的 d1−d0 相等，Δ 结构上恒为 0，
  作为 ties 剔除。所以本检验只在价格落在两档之间或穿越其一时有信息量。
"""
from __future__ import annotations

import argparse
import json
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


def max_oi_strike(snap, expiry, ref, with_share=False):
    oi = {}
    for x in snap.contracts:
        if x.expiry == expiry and abs(x.strike / ref - 1) <= BAND and x.open_interest:
            oi[x.strike] = oi.get(x.strike, 0) + x.open_interest
    if not oi:
        return (None, None) if with_share else None
    K = max(oi, key=oi.get)
    return (K, oi[K] / sum(oi.values())) if with_share else K


def sign_test_p(n_neg: int, n_pos: int) -> float | None:
    """单侧精确符号检验 P(X ≤ n_neg 的反面)：H1 为 Δ<0 占多数 → p = P(Bin(n, 0.5) ≥ n_neg)。"""
    n = n_neg + n_pos
    if n == 0:
        return None
    return sum(math.comb(n, i) for i in range(n_neg, n + 1)) / 2 ** n


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
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", type=Path)
    args = ap.parse_args()
    cfg = load_config(); store = SnapshotStore(); src = CboeHistorySource()
    alpha_b = 0.05 / 4
    print(f"【探索性】逐日配对：同一 E 日真墙 K 与安慰剂 K'（下一到期最大 OI 档）都距 C[E−1] ≤ {NEAR_ATR} ATR 且 K≠K'；")
    print(f"Δ = (d1−d0)_真 − (d1−d0)_安，pin → Δ<0；单侧精确符号检验，ties 剔除；Bonferroni α={alpha_b:.4f}（4 品种）")
    emit = {"schema": 2, "identity": "exploratory", "near_atr": NEAR_ATR, "band": BAND, "instruments": {}}
    for key in ("gold", "silver", "wti", "qqq"):
        inst = cfg.get(key); ser = src.fetch_series(inst)
        tdays, c = list(ser.dates), list(ser.closes); idx = {d: i for i, d in enumerate(tdays)}
        atr = _atr_series(ser.highs, ser.lows, c, 14)
        by_T, _ = s5.load_days(store, key, inst.options.symbol, tdays)
        days, same, only_one = [], 0, 0
        for E in sorted(by_T):
            if E not in idx or idx[E] == 0 or not atr[idx[E] - 1]:
                continue
            snap = snapshot_from_payload(by_T[E][1], key, inst.options.symbol)
            prev, close, a = c[idx[E] - 1], c[idx[E]], atr[idx[E] - 1]
            exps = sorted({x.expiry for x in snap.contracts if x.expiry >= E})
            if not exps or exps[0] != E or len(exps) < 2:
                continue                                   # 当天不是到期日，或无下一到期作对照
            K, shK = max_oi_strike(snap, E, prev, with_share=True)
            K2, shK2 = max_oi_strike(snap, exps[1], prev, with_share=True)
            okK = K is not None and abs(prev - K) <= NEAR_ATR * a
            okK2 = K2 is not None and abs(prev - K2) <= NEAR_ATR * a
            if not (okK and okK2):
                only_one += okK or okK2
                continue
            if K == K2:
                same += 1; continue                        # 同墙：无对照
            d0, d1 = abs(prev - K) / a, abs(close - K) / a
            p0, p1 = abs(prev - K2) / a, abs(close - K2) / a
            days.append({"E": E.isoformat(), "K": K, "K_placebo": K2, "prev": prev, "close": close, "atr": a,
                         "d0": d0, "d1": d1, "d0_placebo": p0, "d1_placebo": p1,
                         "signed_close_minus_K": (close - K) / a, "signed_close_minus_Kp": (close - K2) / a,
                         "oi_share_K": shK, "oi_share_Kp": shK2, "delta": (d1 - d0) - (p1 - p0)})
        neg = sum(d["delta"] < 0 for d in days); pos = sum(d["delta"] > 0 for d in days)
        ties = len(days) - neg - pos
        pv = sign_test_p(neg, pos)
        med = st.median(d["delta"] for d in days) if days else None
        v = ("样本不足" if len(days) < 5 else
             "探索·名义过 Bonferroni（n 小、设计后改，不作结论）" if pv < alpha_b else
             "探索·仅名义显著" if pv < 0.05 else "探索·未检出")
        print(f"  {key:7s} 配对日 {len(days):2d}（Δ<0 {neg}、Δ>0 {pos}、ties {ties}）｜同墙无对照 {same}｜只一侧过滤 {only_one}"
              f"｜Δ 中位 {'—' if med is None else f'{med:+.2f}'}｜p={'—' if pv is None else f'{pv:.3f}'}  {v}")
        emit["instruments"][key] = {"pairs": days, "n_pairs": len(days), "neg": neg, "pos": pos, "ties": ties,
                                    "same_strike_no_control": same, "only_one_side_near": only_one,
                                    "median_delta": med, "p_sign_one_sided": pv, "verdict": v}
    if args.emit:
        args.emit.write_text(json.dumps(emit, ensure_ascii=False, indent=1, default=str), "utf-8")
        print(f"已落盘 {args.emit}")


if __name__ == "__main__":
    main()
