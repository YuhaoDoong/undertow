"""第四步·参数扫描：布林带 / ATR / RSI / 随机 RSI 的参数放开后，哪些配置能提示破墙？

用户 2026-09-25：「布林带或者其他技术指标，参数都可以设置，不是固定布林带 20 日。」
对。step4_filters 用的是默认参数（布林 20、ATR14 + 5 日比、RSI14），这里放开扫。

⚠️ 扫参数就是在做多重比较。判据不是"哪一格最好"（那是过拟合），而是：
  · 在 **ATR 口径**下是否仍 >1（排除"波动大所以 % 动得大"的缩放效应）
  · 是否在 5 个相关簇里**复现**
  · 整个参数族的**中位数**长什么样 —— 最好那格若远离族中位，就是噪音
全部格子都落盘（--emit → data/history/wall_spread/filter_sweep.json），不只留最好的。

另附：用户实际手动交易的口径（缓冲 1~3%、DTE 1~4）的 20 年基准破墙率。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step4_filters as s4                                              # noqa: E402
from undertow.analyze.stretch import _atr_series                        # noqa: E402
from undertow.analyze.stretch_backtest import build_samples             # noqa: E402
from undertow.analyze.technicals import bb_width_series, rsi_series     # noqa: E402
from undertow.collect.cboe_history import CboeHistorySource             # noqa: E402
from undertow.core.config import load_config                            # noqa: E402

KS = (1, 2, 3, 4)
PCT_BUFS = (0.01, 0.02, 0.03, 0.05, 0.07)
ATR_BUFS = (1.5, 2.0, 3.0)
K_MAIN = 3
GRID = {
    "布林带宽扩张": [("bb", n, lk, thr) for n in (5, 10, 20) for lk in (3, 5, 10) for thr in (1.2, 1.3, 1.5)],
    "ATR扩张":     [("atr", n, lk, thr) for n in (5, 10, 14) for lk in (3, 5, 10) for thr in (1.2, 1.3, 1.5)],
    "RSI":         [("rsi", n, lo, hi) for n in (6, 9, 14) for lo, hi in ((20, 80), (30, 70))],
    "随机RSI":     [("srsi", 14, lo, hi) for lo, hi in ((20, 80), (30, 70))],
}


def _stoch_rsi(rsi, n=14):
    out = [None] * len(rsi)
    for i in range(len(rsi)):
        w = [x for x in rsi[max(0, i - n + 1):i + 1] if x is not None]
        if len(w) < n or rsi[i] is None:
            continue
        lo, hi = min(w), max(w)
        out[i] = 50.0 if hi == lo else (rsi[i] - lo) / (hi - lo) * 100
    return out


def frame(ser):
    """一个品种：宇宙（stretch 样本）+ 每根的前瞻结果 + 原始序列。结果只算一次。"""
    c, h, l = ser.closes, ser.highs, ser.lows
    samples = build_samples(ser.symbol, h, l, c, horizons=KS)
    atr14 = _atr_series(h, l, c, 14)
    idx = [s.i for s in samples if atr14[s.i]]
    out = {i: s4.breach_outcomes(c, atr14[i], i, ks=KS, pct_bufs=PCT_BUFS, atr_bufs=ATR_BUFS) for i in idx}
    return {"c": c, "h": h, "l": l, "idx": idx, "out": out, "dates": ser.dates}


def states_for(cfg, fr):
    """返回 {i: 状态名}，只标尾部状态；其余 None。"""
    kind, n, a, b = cfg
    c, h, l = fr["c"], fr["h"], fr["l"]
    if kind in ("bb", "atr"):
        base = bb_width_series(c, n) if kind == "bb" else _atr_series(h, l, c, n)
        r = s4._ratio_series(base, a)
        return {i: ("扩张" if r[i] >= b else None) for i in fr["idx"] if r[i] is not None}
    rsi = rsi_series(c, n)
    v = rsi if kind == "rsi" else _stoch_rsi(rsi, 14)
    return {i: ("超卖" if v[i] <= a else "超买" if v[i] >= b else None) for i in fr["idx"] if v[i] is not None}


def ratio(fr, st, name, side, k, buf):
    key = (side, k, buf)
    allr = [fr["out"][i][key] for i in fr["idx"] if i in st]
    inr = [fr["out"][i][key] for i in fr["idx"] if st.get(i) == name]
    if not inr or not allr:
        return float("nan"), 0
    b = sum(allr) / len(allr)
    return ((sum(inr) / len(inr)) / b if b else float("nan")), len(inr)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--emit", action="store_true")
    ap.add_argument("--output", type=Path, default=None, help="--emit 的产物路径（默认旧路径；重算请指定新路径，不覆盖旧产物）")
    args = ap.parse_args()
    cfg = load_config(); src = CboeHistorySource()
    keys = [k for k, v in cfg.instruments.items() if v.price is not None]
    frames = {k: frame(src.fetch_series(cfg.get(k))) for k in keys}
    syms = {k: cfg.get(k).price.symbol for k in keys}

    # ── A. 用户实际口径的基准率 ──
    print("A. 20 年基准破墙率（不看指标）：缓冲 1~3% × DTE 1~4，重叠计数。这是你手动开仓那种结构的真实底噪")
    for key in ("silver", "gold"):
        fr = frames[key]; n = len(fr["idx"])
        print(f"\n  {key} ({syms[key]}, {n:,} 根)   " + "".join(f"{'↓'+f'{b:.0%}':>7s}{'↑'+f'{b:.0%}':>7s}" for b in (0.01, 0.02, 0.03)))
        for k in KS:
            line = f"    k={k}  "
            for b in (0.01, 0.02, 0.03):
                d = sum(fr["out"][i][("down", k, f"{b:.0%}")] for i in fr["idx"]) / n
                u = sum(fr["out"][i][("up", k, f"{b:.0%}")] for i in fr["idx"]) / n
                line += f"{d:7.1%}{u:7.1%}"
            print(line)

    # ── B. 参数扫描 ──
    results = {}
    for fam, cfgs in GRID.items():
        rows = []
        for c in cfgs:
            names = ("扩张",) if c[0] in ("bb", "atr") else ("超卖", "超买")
            st_all = {k: states_for(c, frames[k]) for k in keys}
            for nm in names:
                for side in ("down", "up"):
                    r5 = {k: ratio(frames[k], st_all[k], nm, side, K_MAIN, "5%")[0] for k in keys}
                    ra = {k: ratio(frames[k], st_all[k], nm, side, K_MAIN, "2ATR")[0] for k in keys}
                    cov = statistics.median([sum(1 for v in st_all[k].values() if v == nm) / max(1, len(st_all[k])) for k in keys])
                    def med(d): 
                        v = [x for x in d.values() if not math.isnan(x)]; return statistics.median(v) if v else float("nan")
                    def clus(d, up=True):
                        n = 0
                        for members in s4.CLUSTERS.values():
                            v = [d[m] for m in members if m in d and not math.isnan(d[m])]
                            if v and ((statistics.median(v) >= 1.25) if up else (statistics.median(v) <= 0.8)): n += 1
                        return n
                    rows.append({"cfg": c, "state": nm, "side": side, "coverage": cov,
                                 "med5": med(r5), "medATR": med(ra),
                                 "n5_ge": sum(1 for x in r5.values() if x >= 1.25),
                                 "nATR_ge": sum(1 for x in ra.values() if x >= 1.25),
                                 "clus5": clus(r5), "clusATR": clus(ra),
                                 "r5": {syms[k]: r5[k] for k in keys}, "rATR": {syms[k]: ra[k] for k in keys}})
        results[fam] = rows

    print(f"\n\nB. 参数扫描（k={K_MAIN}，比值 = 状态内破墙率 ÷ 基准率，15 品种中位数）。判据：ATR 口径仍 >1 且簇复现，不是 5% 口径最好看")
    for fam, rows in results.items():
        def fmt(c):
            return (f"n{c[1]} 回看{c[2]} ≥{c[3]}" if c[0] in ("bb", "atr") else f"n{c[1]} {c[2]}/{c[3]}")
        rows_s = sorted(rows, key=lambda r: -(r["medATR"] if not math.isnan(r["medATR"]) else -9))
        fam_med5 = statistics.median([r["med5"] for r in rows if not math.isnan(r["med5"])])
        fam_medA = statistics.median([r["medATR"] for r in rows if not math.isnan(r["medATR"])])
        print(f"\n  ── {fam}：{len(rows)} 格 · 族中位数 5%口径 {fam_med5:.2f} / ATR口径 {fam_medA:.2f} ──")
        print(f"  {'配置':22s}{'状态':4s}{'侧':6s}{'覆盖':>6s}{'5%中位':>7s}{'≥1.25':>6s}{'簇':>3s}{'ATR中位':>8s}{'≥1.25':>6s}{'簇':>3s}")
        show = rows_s[:6] + ([rows_s[-1]] if len(rows_s) > 6 else [])
        for r in show:
            print(f"  {fmt(r['cfg']):22s}{r['state']:4s}{('↓put' if r['side']=='down' else '↑call'):6s}"
                  f"{r['coverage']:6.1%}{r['med5']:7.2f}{r['n5_ge']:6d}{r['clus5']:3d}{r['medATR']:8.2f}{r['nATR_ge']:6d}{r['clusATR']:3d}")
        if len(rows_s) > 6: print("  （…中间省略，最后一行是最差格；全部格子见 --emit 落盘）")

    if args.emit:
        out = args.output or (ROOT / "data" / "history" / "wall_spread" / "filter_sweep.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "asof": date.today().isoformat(), "k": K_MAIN,
                   "grid_sizes": {f: len(v) for f, v in GRID.items()},
                   "results": {f: [{**r, "cfg": list(r["cfg"])} for r in rows] for f, rows in results.items()}}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已落盘 {out}")


if __name__ == "__main__":
    main()
