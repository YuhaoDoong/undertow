"""第五步：近价局部墙有没有支撑？—— 墙位破没破 vs 同距离任意价位破没破。

用户 2026-09-25：手动开的三笔卖的都是 1~5% 内的近墙（57P / 60C / 405C），
模块选的结构主墙在 5~15% 外。近墙的权利金是真的，但 20 年基准破墙率也高
（SLV 缓冲 1%、k=2：36%~40%）。那笔卖 60/买 61 平衡破墙率 27%、缓冲 1.1% —— 它赢，
只能是墙撑住了。**这件事可以直接测**：

  对每个可交易日 T：近墙 = ≤14 天到期、现价 ±5% 内该侧累计 OI 最大的行权价
  （gamma.local_wall，与 _layer_walls 同一聚合口径）。观察 T 起 k 个收盘有没有越过它。
  零假设：墙只是一个价位，破墙概率 = 同距离任意价位的破墙概率 F_k(b)。
  F_k(b) 用两套估：20 年全样本（精确）与快照同期（同 regime）。
  观察破墙数 O vs 期望 Σ F_k(b_d)，Monte Carlo 双侧 p（不重叠子样本）。

为什么这个检验有效而"指标 × 墙"没有：近墙缓冲小、破墙常见，65 天里事件有几十个，
不再是 n=1。smc.py 9/3 对订单块做的就是同一种检验（结论是否）。

口径：
  · 可交易日 T 由 captured_at → core.clock.decision_session 推出，盘中抓的一律剔除
    （codex 2026-08-29 P0：快照文件日 ≠ 可交易日）
  · 决策价 = C[T−1]，观察窗 = T..T+k−1 的收盘（= 第四步的 i+1..i+k，i=T−1）
  · 破墙 = 收盘越过，不看盘中
  · 单品种时序，不跨品种合并
用法：python3 scripts/step5_wall_hold.py [--emit]
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.analyze.gamma import local_wall                          # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource            # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload        # noqa: E402
from undertow.collect.store import SnapshotStore                       # noqa: E402
from undertow.core.clock import decision_session                       # noqa: E402
from undertow.core.config import load_config                           # noqa: E402

KS = (1, 2, 3, 4)
BUCKETS = ((0, 1), (1, 2), (2, 3), (3, 5))
MIN_DAYS = 20
SIMS = 20000


def k_moves(closes, k):
    """第 i 根之后 k 个收盘的最小/最大相对变动（只看 i+1..i+k）。"""
    n = len(closes)
    mn = [None] * n; mx = [None] * n
    for i in range(n - k):
        w = closes[i + 1:i + k + 1]
        mn[i] = min(w) / closes[i] - 1
        mx[i] = max(w) / closes[i] - 1
    return mn, mx


def base_rate(moves, b, lo, hi, *, down=True):
    """同距离任意价位的破墙率：i∈[lo,hi) 内 k 日变动越过 ±b 的比例。"""
    v = [m for m in moves[lo:hi] if m is not None]
    if not v:
        return float("nan")
    return (sum(1 for m in v if m < -b) if down else sum(1 for m in v if m > b)) / len(v)


def breached(window, strike, kind):
    return (min(window) < strike) if kind == "P" else (max(window) > strike)


def mc_p(probs, observed, sims=SIMS, seed=7):
    """观测破墙数在独立 Bernoulli(p_d) 之和下的双侧 p。"""
    rnd = random.Random(seed)
    lo = hi = 0
    for _ in range(sims):
        s = sum(1 for p in probs if rnd.random() < p)
        lo += s <= observed; hi += s >= observed
    return min(1.0, 2 * min(lo, hi) / sims)


def load_days(store, key, sym, tdays):
    """可交易日 T → payload（同一 T 多份取最晚抓的）。返回 (dict, 剔除数)。"""
    by_T, dropped = {}, 0
    for d in store.dates("options", sym):
        rec = json.loads(gzip.decompress(store.path_of("options", sym, d).read_bytes()))
        ca = rec.get("captured_at")
        T = decision_session(ca, tdays) if ca else None
        if T is None:
            dropped += 1; continue
        if T not in by_T or ca > by_T[T][0]:
            by_T[T] = (ca, rec["payload"])
    return by_T, dropped


def run(key, inst, store, src, mode="max"):
    ser = src.fetch_series(inst)
    tdays, c = list(ser.dates), list(ser.closes)
    idx = {d: i for i, d in enumerate(tdays)}
    by_T, dropped = load_days(store, key, inst.options.symbol, tdays)
    if len(by_T) < MIN_DAYS:
        return None
    moves = {k: k_moves(c, k) for k in KS}
    Ts = sorted(t for t in by_T if t in idx and idx[t] > 0)
    i_lo, i_hi = idx[Ts[0]] - 1, idx[Ts[-1]]
    rows = []
    for T in Ts:
        snap = snapshot_from_payload(by_T[T][1], key, inst.options.symbol)
        iT = idx[T]; spot = c[iT - 1]
        for kind in ("P", "C"):
            w = local_wall(snap, T, spot, kind, mode=mode)
            if not w:
                continue
            b = w["buf_pct"] / 100
            for k in KS:
                if iT + k - 1 >= len(c):
                    continue
                win = c[iT:iT + k]
                mn, mx = moves[k]
                down = kind == "P"
                rows.append({"T": T, "iT": iT, "kind": kind, "k": k, "strike": w["strike"],
                             "oi": w["oi"], "buf": b, "breach": breached(win, w["strike"], kind),
                             "F20": base_rate(mn if down else mx, b, 0, len(c), down=down),
                             "Fin": base_rate(mn if down else mx, b, i_lo, i_hi, down=down)})
    return {"symbol": inst.options.symbol, "n_days": len(Ts), "dropped_intraday": dropped,
            "span": f"{Ts[0]}→{Ts[-1]}", "rows": rows}


def summarize(res):
    out = {}
    rows = res["rows"]
    for kind in ("P", "C"):
        for k in KS:
            rr = [r for r in rows if r["kind"] == kind and r["k"] == k]
            if not rr:
                continue
            i0 = rr[0]["iT"]
            sub = [r for r in rr if (r["iT"] - i0) % k == 0]          # 不重叠
            O = sum(r["breach"] for r in sub); E20 = sum(r["F20"] for r in sub); Ein = sum(r["Fin"] for r in sub)
            out[(kind, k)] = {"n": len(sub), "n_all": len(rr), "O": O, "E20": E20, "Ein": Ein,
                              "buf_med": statistics.median(r["buf"] for r in sub) * 100,
                              "p20": mc_p([r["F20"] for r in sub], O), "pin": mc_p([r["Fin"] for r in sub], O)}
    # 分缓冲桶（k=2，重叠计数，描述）
    bk = {}
    for kind in ("P", "C"):
        for lo, hi in BUCKETS:
            rr = [r for r in rows if r["kind"] == kind and r["k"] == 2 and lo < r["buf"] * 100 <= hi]
            if rr:
                bk[(kind, lo, hi)] = {"n": len(rr), "O": sum(r["breach"] for r in rr), "E20": sum(r["F20"] for r in rr)}
    # 墙厚三分位（k=2）
    tr = {}
    for kind in ("P", "C"):
        rr = sorted([r for r in rows if r["kind"] == kind and r["k"] == 2], key=lambda r: r["oi"])
        if len(rr) >= 9:
            t = len(rr) // 3
            for name, part in (("薄", rr[:t]), ("中", rr[t:2 * t]), ("厚", rr[2 * t:])):
                tr[(kind, name)] = {"n": len(part), "O": sum(r["breach"] for r in part),
                                    "E20": sum(r["F20"] for r in part), "oi_med": statistics.median(r["oi"] for r in part)}
    return out, bk, tr


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", action="store_true")
    ap.add_argument("--mode", default="max", choices=("max", "nearest"), help="墙定义：带内最大 OI / 带内最近一堵")
    args = ap.parse_args()
    cfg = load_config(); store = SnapshotStore(); src = CboeHistorySource()
    results = {}
    for key, inst in cfg.instruments.items():
        if inst.options is None or inst.price is None:
            continue
        r = run(key, inst, store, src, mode=args.mode)
        if r:
            results[key] = r
    print("近墙 = ≤14 天到期、现价 ±5% 内该侧累计 OI 最大档（gamma.local_wall）。零假设：墙只是个价位，破墙率 = 同距离任意价位。")
    print("O = 观测破墙数；E20 = 按 20 年同距离基准率的期望；Ein = 按快照同期基准率的期望；O/E <1 = 墙比随机价位更难破。p = MC 双侧（不重叠子样本）\n")
    emit = {}
    for key, res in results.items():
        st, bk, tr = summarize(res)
        print(f"{'═'*100}\n{key} ({res['symbol']})  可交易日 {res['n_days']}  {res['span']}  剔除盘中抓取 {res['dropped_intraday']}\n{'═'*100}")
        print(f"  {'侧':4s}{'k':>2s}{'n':>4s}{'缓冲中位':>8s}{'O':>4s}{'E20':>6s}{'O/E20':>7s}{'p20':>7s}{'Ein':>6s}{'O/Ein':>7s}{'pin':>7s}   守住率 观测/期望20")
        for (kind, k), v in st.items():
            r20 = v["O"] / v["E20"] if v["E20"] else float("nan"); rin = v["O"] / v["Ein"] if v["Ein"] else float("nan")
            print(f"  {'put' if kind=='P' else 'call':4s}{k:2d}{v['n']:4d}{v['buf_med']:7.2f}%{v['O']:4d}{v['E20']:6.1f}{r20:7.2f}{v['p20']:7.3f}{v['Ein']:6.1f}{rin:7.2f}{v['pin']:7.3f}   {1-v['O']/v['n']:.0%} / {1-v['E20']/v['n']:.0%}")
        print("  分缓冲桶（k=2，重叠计数）:", "  ".join(f"{'put' if kd=='P' else 'call'}{lo}~{hi}%: n{v['n']} O{v['O']} E{v['E20']:.1f}" for (kd, lo, hi), v in bk.items()))
        if tr:
            print("  墙厚三分位（k=2）:", "  ".join(f"{'put' if kd=='P' else 'call'}·{nm}(OI中位{v['oi_med']:,.0f}) O/E={v['O']/v['E20'] if v['E20'] else float('nan'):.2f}" for (kd, nm), v in tr.items()))
        emit[key] = {"symbol": res["symbol"], "n_days": res["n_days"], "span": res["span"],
                     "dropped_intraday": res["dropped_intraday"],
                     "tests": {f"{kd}|k{k}": v for (kd, k), v in st.items()},
                     "buckets": {f"{kd}|{lo}-{hi}": v for (kd, lo, hi), v in bk.items()},
                     "terciles": {f"{kd}|{nm}": v for (kd, nm), v in tr.items()}}
        print()
    if args.emit:
        out = ROOT / "data" / "history" / "wall_spread" / ("wall_hold.json" if args.mode == "max" else f"wall_hold_{args.mode}.json")
        out.write_text(json.dumps({"schema": 1, "asof": date.today().isoformat(), "sims": SIMS,
                                   "definition": "local_wall band=5% dte<=14 min_oi=WALL_FLOW_MIN_OI",
                                   "instruments": emit}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已落盘 {out}")


if __name__ == "__main__":
    main()
