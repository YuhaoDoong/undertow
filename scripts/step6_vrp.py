"""第六步：波动率风险溢价（VRP）—— 卖墙价差剩下的唯一可能的边，在哪些状态下存在？

第五步证明近墙不提供支撑，卖方价差的边只可能来自「期权收的 IV 系统性高于事后实现波动」。
仓库已有 vrp_history（30 天指数 vs 其后 21 日实现波动，前视对齐）：金 +2.7pp / 银 +4.9pp，
81%~83% 的日子为正。本步补两件它没回答的事：

  A. **按状态拆**：ATR 扩张 / ATR 高低分位 / IV 分位 —— 第四步说 ATR 扩张时破墙率约 2×，
     那时 VRP 还在不在？卖方的边是不是恰好在最该躲的日子消失？
  B. **短到期**：策略卖的是 2~7 DTE，指数是 30 天。用快照 ATM IV（3~10 DTE）vs 该到期内实现波动。
     样本只有 ~57 个可交易日，只作描述。

口径：VRP = IV − 其后实现波动（年化 pp，前视对齐，复用 vrp_history.forward_realized_vol）；
显著性用不重叠子样本（每 21 根取 1）；单品种不合并；状态用决策日之前的数据算（无前视）。
用法：python3 scripts/step6_vrp.py [--emit]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step5_wall_hold as s5                                            # noqa: E402
from undertow.analyze.stretch import _atr_series                        # noqa: E402
from undertow.analyze.stretch_backtest import _pct_rank_series          # noqa: E402
from undertow.analyze.vrp_history import forward_realized_vol           # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource             # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload         # noqa: E402
from undertow.collect.cboe_vol import CboeVolSource                     # noqa: E402
from undertow.collect.store import SnapshotStore                        # noqa: E402
from undertow.core.config import load_config                            # noqa: E402

WINDOW = 21
ANN = math.sqrt(252.0)


def one_sample_t(xs):
    if len(xs) < 3:
        return 0.0
    sd = st.stdev(xs)
    return st.mean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else 0.0


def welch(a, b):
    if len(a) < 3 or len(b) < 3:
        return 0.0
    va, vb = st.variance(a), st.variance(b)
    den = math.sqrt(va / len(a) + vb / len(b))
    return (st.mean(a) - st.mean(b)) / den if den > 0 else 0.0


def describe(vals):
    if not vals:
        return None
    s = sorted(vals)
    return {"n": len(vals), "mean": st.mean(vals), "median": st.median(vals),
            "pos": sum(1 for v in vals if v > 0) / len(vals),
            "p5": s[int(0.05 * (len(s) - 1))], "p95": s[int(0.95 * (len(s) - 1))]}


def long_history(inst, iv_src, px_src):
    iv = dict(iv_src.fetch_series(inst.vol_index))
    ser = px_src.fetch_series(inst)
    dates, c, h, l = list(ser.dates), list(ser.closes), list(ser.highs), list(ser.lows)
    fwd = forward_realized_vol(dates, c, WINDOW)
    atr = _atr_series(h, l, c, 14)
    atr_pct = _pct_rank_series(atr)
    iv_list = [iv.get(d) for d in dates]
    iv_pct = _pct_rank_series(iv_list)
    rows = []
    for i, d in enumerate(dates):
        if d not in iv or d not in fwd or i < 20 or not atr[i] or not atr[i - 5]:
            continue
        rows.append({"i": i, "d": d, "vrp": iv[d] - fwd[d], "iv": iv[d], "rv": fwd[d],
                     "atr_x": atr[i] / atr[i - 5], "atr_pct": atr_pct[i], "iv_pct": iv_pct[i]})
    states = {
        "ATR扩张≥1.3": lambda r: r["atr_x"] >= 1.3,
        "ATR扩张<1.3": lambda r: r["atr_x"] < 1.3,
        "ATR分位≥90%": lambda r: r["atr_pct"] is not None and r["atr_pct"] >= 0.9,
        "ATR分位≤10%": lambda r: r["atr_pct"] is not None and r["atr_pct"] <= 0.1,
        "IV分位≥70%": lambda r: r["iv_pct"] is not None and r["iv_pct"] >= 0.7,
        "IV分位≤30%": lambda r: r["iv_pct"] is not None and r["iv_pct"] <= 0.3,
    }
    i0 = rows[0]["i"]
    sub = [r for r in rows if (r["i"] - i0) % WINDOW == 0]
    out = {"index": inst.vol_index, "span": f"{rows[0]['d']}→{rows[-1]['d']}", "n": len(rows),
           "all": describe([r["vrp"] for r in rows]),
           "t_nonoverlap": one_sample_t([r["vrp"] for r in sub]), "n_nonoverlap": len(sub), "states": {}}
    base_sub = [r["vrp"] for r in sub]
    for name, f in states.items():
        sel = [r for r in rows if f(r)]
        ssel = [r["vrp"] for r in sub if f(r)]
        srest = [r["vrp"] for r in sub if not f(r)]
        out["states"][name] = {**(describe([r["vrp"] for r in sel]) or {}),
                               "share": len(sel) / len(rows), "n_nov": len(ssel),
                               "welch_vs_rest": welch(ssel, srest)}
    return out


def short_dte(key, inst, store, px_src):
    """快照 ATM IV（3~10 DTE，最近一个到期）vs 该到期内实现波动。"""
    ser = px_src.fetch_series(inst)
    tdays, c = list(ser.dates), list(ser.closes)
    idx = {d: i for i, d in enumerate(tdays)}
    by_T, _ = s5.load_days(store, key, inst.options.symbol, tdays)
    rows = []
    for T in sorted(by_T):
        if T not in idx:
            continue
        snap = snapshot_from_payload(by_T[T][1], key, inst.options.symbol)
        cands = [ct for ct in snap.contracts if 3 <= (ct.expiry - T).days <= 10
                 and abs(ct.strike / snap.spot - 1) < 0.02 and ct.iv and ct.iv > 0]
        if not cands:
            continue
        exp = min(ct.expiry for ct in cands)
        ivs = [ct.iv for ct in cands if ct.expiry == exp]
        dte = (exp - T).days
        iT = idx[T]
        # 实现波动：T−1 收盘起到 到期日 收盘（到期日不在日线里就用其前最后一根）
        end = max(i for i, d in enumerate(tdays) if d <= exp) if any(d <= exp for d in tdays) else None
        if end is None or end <= iT - 1 or end >= len(c):
            continue
        rets = [math.log(c[j] / c[j - 1]) for j in range(iT, end + 1)]
        if len(rets) < 2:
            continue
        rv = st.pstdev(rets) * ANN * 100
        ivp = st.mean(ivs) * 100
        rows.append({"T": T.isoformat(), "dte": dte, "iv": ivp, "rv": rv, "vrp": ivp - rv})
    return rows


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", action="store_true"); args = ap.parse_args()
    cfg = load_config(); iv_src = CboeVolSource(); px_src = CboeHistorySource(); store = SnapshotStore()
    emit = {"schema": 1, "asof": date.today().isoformat(), "window": WINDOW, "long": {}, "short": {}}
    print(f"A. 长历史 VRP = 30 天 IV 指数 − 其后 {WINDOW} 日实现波动（年化 pp，前视对齐）。t 用不重叠子样本；"
          f"「状态 vs 其余」为 Welch t。p5 = 最差 5% 的 VRP（卖方的尾巴）")
    for key, inst in cfg.instruments.items():
        if not inst.vol_index or not inst.price:
            continue
        r = long_history(inst, px_src=px_src, iv_src=iv_src)
        emit["long"][key] = r
        a = r["all"]
        print(f"\n{'═'*100}\n{key} ({r['index']})  {r['span']}  n={r['n']:,}（不重叠 {r['n_nonoverlap']}）"
              f"  均值 {a['mean']:+.2f}pp  中位 {a['median']:+.2f}  正比例 {a['pos']:.0%}  p5 {a['p5']:+.1f}  t={r['t_nonoverlap']:.1f}\n{'═'*100}")
        print(f"  {'状态':12s}{'占比':>6s}{'n':>6s}{'均值VRP':>9s}{'中位':>7s}{'正比例':>7s}{'p5':>7s}{'vs其余 t':>9s}")
        for name, s in r["states"].items():
            if not s.get("n"):
                continue
            print(f"  {name:12s}{s['share']:6.1%}{s['n']:6d}{s['mean']:+9.2f}{s['median']:+7.2f}{s['pos']:7.0%}{s['p5']:+7.1f}{s['welch_vs_rest']:9.2f}")

    print(f"\n\nB. 短到期（快照 ATM IV 3~10 DTE vs 该到期内实现波动）—— 描述性，n≈可交易日数")
    for key in ("silver", "gold", "wti", "qqq"):
        inst = cfg.get(key)
        rows = short_dte(key, inst, store, px_src)
        emit["short"][key] = rows
        if not rows:
            print(f"  {key}: 无样本"); continue
        v = [r["vrp"] for r in rows]; d = describe(v)
        print(f"  {key:7s} n={d['n']:3d}  DTE 中位 {st.median(r['dte'] for r in rows):.0f}  IV 中位 {st.median(r['iv'] for r in rows):.1f}  "
              f"RV 中位 {st.median(r['rv'] for r in rows):.1f}  VRP 均值 {d['mean']:+.2f}pp 中位 {d['median']:+.2f}  正比例 {d['pos']:.0%}  "
              f"p5 {d['p5']:+.1f}  t={one_sample_t(v):.1f}")
    if args.emit:
        out = ROOT / "data" / "history" / "wall_spread" / "vrp_states.json"
        out.write_text(json.dumps(emit, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"\n已落盘 {out}")


if __name__ == "__main__":
    main()
