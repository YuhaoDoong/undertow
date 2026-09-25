"""第四步：各项技术指标能否提示破墙？—— 脱离期权定价与实盘，在长日线上直接检验。

用户 2026-09-25：「卖墙价差理论上就是少数亏损吃掉全部盈利，能否用 RSI / 随机 RSI /
布林带 / MACD 等指标筛选剔除」「布林带突然扩大的时候，可能行情走向极端，破墙概率变大？」
「各个技术指标跟期权墙都联合测试一下，在所有品种里做综合回测」「先不要带入期权实盘，
就单独先测试破墙」「就看各项技术指标能否提示破墙」。

═══════════════════════════════════════════════════════════════════════
为什么不能在卖墙价差回测上直接测
═══════════════════════════════════════════════════════════════════════
最终规则格（墙上·DTE 2~4·宽 2~3·权利金≥$6.40）SLV 共 81 笔，亏损 4 笔，
**全是同一个事件**（8/3、8/4 开仓，三天 +9.8% 破 55C）。一个独立亏损事件 = n=1。
任何指标都能被调到"恰好躲过那一次"—— 那是把一次事故写成规则，不是验证。
期权快照只有 ~65 个交易日（个股 1 天），"指标 × 墙"的联合样本在统计上不存在。

═══════════════════════════════════════════════════════════════════════
拆开来测：破墙不需要期权墙
═══════════════════════════════════════════════════════════════════════
卖方价差亏钱只有一种方式：价格在 DTE 天内逆向收盘走过缓冲。这个事件只需要日线，
15 个品种各有 3,500~5,700 根。问题变成：**指标处于状态 S 时，「k 天内逆向收盘
突破缓冲」的概率是否与其余时候不同？** 与 stretch.py 的 33 年校准同一思路，
只是目标从"均值收益"换成"尾部突破概率"—— 卖方结构要的是后者。

口径（事前定死）：
  · 主检验 k=3（DTE 2~4 中点）；缓冲两套：固定 %（策略实际用的口径）与 ATR 倍数
    （跨品种可比 —— 5% 对 TLT 是三倍 ATR、对 TSLA 不到一倍；沿用 stretch_backtest
    卖方口径的 DEFAULT_DISTANCES 先例）。跨品种汇总用 2×ATR。
  · 只检验尾部状态（每指标两端），中间只描述
  · 单品种内检验：不重叠子样本（每 k 根取 1 根）+ 两比例 z；
    族 = 9 指标 × 2 端 × 2 侧 = 36 → Bonferroni α = 0.05/36
  · **不跨品种合并**（金银 0.89、QQQ/TQQQ 0.99、科技股同板块）；跨品种只数"复现"：
    某效应在多少个品种里方向一致。相关品种簇视为同一份证据。
  · 指标第 i 位只用 ≤i 的数据；序列版由 tests/test_no_drift 钉住与研报同源

用法：python3 scripts/step4_filters.py [--emit] [--detail]
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
sys.path.insert(0, str(ROOT))

from undertow.analyze.stretch import BANDS, _atr_series                  # noqa: E402
from undertow.analyze.stretch_backtest import (_pct_rank_series,          # noqa: E402
                                               build_samples)
from undertow.analyze.ta.stoch import stoch_kd                            # noqa: E402
from undertow.analyze.technicals import (bb_width_series,                 # noqa: E402
                                         macd_hist_series, pctb_series,
                                         rsi_series)
from undertow.collect.cboe_history import CboeHistorySource               # noqa: E402
from undertow.core.config import load_config                              # noqa: E402

KS = (2, 3, 4)
PCT_BUFS = (0.03, 0.05, 0.07)
ATR_BUFS = (1.5, 2.0, 3.0)
K_PRIMARY, ATR_PRIMARY = 3, 2.0
EXPAND_N = 5                     # 扩张比的回看根数
BREAKEVEN = (0.029, 0.048)       # 与 wall_spread.PARAMS["silver"]["breakeven_rate"] 同源，只作对照
# 相关品种簇：跨品种"复现"计数按簇算，同簇多个品种只算一份证据
CLUSTERS = {"贵金属": ("gold", "silver"), "能源": ("wti",), "利率": ("tlt",),
            "股指": ("qqq", "tqqq", "spy", "iwm"),
            "科技股": ("googl", "tsla", "nvda", "intc", "amd", "msft", "aapl")}


def _band(p):
    for hi, name in BANDS:
        if p < hi:
            return name
    return BANDS[-1][1]


def _tri(lo, hi, lo_name, hi_name, mid="中"):
    return lambda v: lo_name if v <= lo else (hi_name if v >= hi else mid)


# 名称 → (状态函数, 两端尾部状态)
INDICATORS = {
    "RSI14":    (_tri(30, 70, "超卖≤30", "超买≥70"), ("超卖≤30", "超买≥70")),
    "布林%B":    (_tri(0.0, 1.0, "下轨外", "上轨外"), ("下轨外", "上轨外")),
    "MACD柱":   (lambda v: "柱≤0" if v <= 0 else "柱>0", ("柱≤0", "柱>0")),
    "StochK":   (_tri(20, 80, "K≤20", "K≥80"), ("K≤20", "K≥80")),
    "超买超卖":   (_band, ("极超卖", "极超买")),
    "带宽分位":   (_tri(0.10, 0.90, "低波≤10%", "高波≥90%"), ("低波≤10%", "高波≥90%")),
    "带宽扩张比":  (_tri(0.80, 1.30, "收缩≤0.8", "扩张≥1.3"), ("收缩≤0.8", "扩张≥1.3")),
    "ATR分位":   (_tri(0.10, 0.90, "低波≤10%", "高波≥90%"), ("低波≤10%", "高波≥90%")),
    "ATR扩张比":  (_tri(0.80, 1.30, "收缩≤0.8", "扩张≥1.3"), ("收缩≤0.8", "扩张≥1.3")),
}
FAMILY = len(INDICATORS) * 2 * 2
ALPHA_BONF = 0.05 / FAMILY


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def two_prop(k1, n1, k2, n2):
    """两比例 z（合并方差）。返回 (z, 双侧 p)；样本不足 (0, 1)。"""
    if n1 < 5 or n2 < 5:
        return 0.0, 1.0
    p = (k1 + k2) / (n1 + n2)
    den = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if den == 0:
        return 0.0, 1.0
    z = (k1 / n1 - k2 / n2) / den
    return z, 2 * (1 - _phi(abs(z)))


def _ratio_series(xs, n):
    out = [None] * len(xs)
    for i in range(n, len(xs)):
        a, b = xs[i], xs[i - n]
        if a is not None and b is not None and b > 0:
            out[i] = a / b
    return out


def build(ser):
    """每根 bar：指标状态 + 前瞻突破结果。宇宙 = stretch 样本（统一预热、带 regime）。"""
    c, h, l = ser.closes, ser.highs, ser.lows
    samples = build_samples(ser.symbol, h, l, c, horizons=KS)
    atr = _atr_series(h, l, c, 14)
    bw = bb_width_series(c, 20, 2.0)
    sk, _ = stoch_kd(c, h, l)
    series = {
        "RSI14": rsi_series(c, 14), "布林%B": pctb_series(c, 20, 2.0),
        "MACD柱": macd_hist_series(c), "StochK": sk,
        "带宽分位": _pct_rank_series(bw), "带宽扩张比": _ratio_series(bw, EXPAND_N),
        "ATR分位": _pct_rank_series(atr), "ATR扩张比": _ratio_series(atr, EXPAND_N),
    }
    rows = []
    for s in samples:
        i = s.i
        vals = {k: v[i] for k, v in series.items()}
        vals["超买超卖"] = s.pctile
        if any(v is None for v in vals.values()) or not atr[i]:
            continue
        out = breach_outcomes(c, atr[i], i)
        rows.append({"i": i, "date": ser.dates[i], "regime": s.regime, "vals": vals,
                     "states": {n: INDICATORS[n][0](v) for n, v in vals.items()},
                     "out": out, "expand": vals["带宽扩张比"]})
    return rows


def breach_outcomes(closes, atr_i, i, *, ks=KS, pct_bufs=PCT_BUFS, atr_bufs=ATR_BUFS):
    """第 i 根的前瞻突破结果：只看 i+1..i+k，**不含第 i 根自己**（含了就是前视）。

    step4_filters 与 step4_sweep 共用这一个实现（AGENTS.md：同一个量不许算两遍）。
    键：(side, k, 缓冲标签)；缓冲标签 "5%" 或 "2ATR"。
    """
    out = {}
    c0 = closes[i]
    for k in ks:
        fwd = closes[i + 1:i + k + 1]
        lo, hi = min(fwd), max(fwd)
        for b in pct_bufs:
            out[("down", k, f"{b:.0%}")] = lo < c0 * (1 - b)
            out[("up", k, f"{b:.0%}")] = hi > c0 * (1 + b)
        for m in atr_bufs:
            out[("down", k, f"{m:g}ATR")] = lo < c0 - m * atr_i
            out[("up", k, f"{m:g}ATR")] = hi > c0 + m * atr_i
    return out


def rate(rows, key):
    n = len(rows)
    return (sum(1 for r in rows if r["out"][key]) / n) if n else float("nan")


def analyse(rows):
    """一个品种：基准率 + 主检验（k=3, 2×ATR）+ 全 (k, buf) 比值矩阵。"""
    keys = [(s, k, b) for s in ("down", "up") for k in KS
            for b in [f"{x:.0%}" for x in PCT_BUFS] + [f"{m:g}ATR" for m in ATR_BUFS]]
    base = {key: rate(rows, key) for key in keys}
    first = rows[0]["i"]
    sub = [r for r in rows if (r["i"] - first) % K_PRIMARY == 0]          # 不重叠
    tests = {}
    for name, (_, tails) in INDICATORS.items():
        for st in tails:
            for side in ("down", "up"):
                key = (side, K_PRIMARY, f"{ATR_PRIMARY:g}ATR")
                a = [r for r in sub if r["states"][name] == st]
                o = [r for r in sub if r["states"][name] != st]
                ka, ko = sum(r["out"][key] for r in a), sum(r["out"][key] for r in o)
                z, p = two_prop(ka, len(a), ko, len(o))
                full = [r for r in rows if r["states"][name] == st]
                tests[(name, st, side)] = {
                    "n_state": len(a), "rate_state": ka / len(a) if a else float("nan"),
                    "rate_rest": ko / len(o) if o else float("nan"), "z": z, "p": p,
                    "bonf": p < ALPHA_BONF,
                    "ratio": (rate(full, key) / base[key]) if base[key] else float("nan"),
                    "n_full": len(full),
                    "ratios_all": {f"{s}|k{k}|{b}": (rate(full, (s, k, b)) / base[(s, k, b)]
                                                     if base[(s, k, b)] else float("nan"))
                                   for (s, k, b) in keys},
                }
    # 带宽扩张比五分位（剂量-反应，直接回答"突然扩大"的假设）
    ex = sorted(r["expand"] for r in rows)
    cuts = [ex[int(len(ex) * q)] for q in (0.2, 0.4, 0.6, 0.8)]
    quint = {}
    for qi in range(5):
        lo = cuts[qi - 1] if qi > 0 else -1e9
        hi = cuts[qi] if qi < 4 else 1e9
        grp = [r for r in rows if lo <= r["expand"] < hi]
        quint[qi] = {"n": len(grp), "lo": lo, "hi": hi,
                     "down": rate(grp, ("down", K_PRIMARY, f"{ATR_PRIMARY:g}ATR")),
                     "up": rate(grp, ("up", K_PRIMARY, f"{ATR_PRIMARY:g}ATR"))}
    return base, tests, quint, len(sub)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--detail", action="store_true", help="金银逐 (k,buf) 明细")
    ap.add_argument("--buffer", default=f"{ATR_PRIMARY:g}ATR",
                    help="矩阵 B 的缓冲口径：2ATR（默认，跨品种可比）或 5%% / 3%% / 7%%（策略实际口径）")
    ap.add_argument("--sections", default="ABCDE", help="只打印哪些章节，如 BE")
    args = ap.parse_args()
    SEC = set(args.sections.upper())
    cfg = load_config(); src = CboeHistorySource()
    keys = [k for k, v in cfg.instruments.items() if v.price is not None]
    res = {}
    for key in keys:
        ser = src.fetch_series(cfg.get(key))
        rows = build(ser)
        base, tests, quint, n_sub = analyse(rows)
        res[key] = {"symbol": ser.symbol, "n": len(rows), "n_sub": n_sub,
                    "span": f"{rows[0]['date']}→{rows[-1]['date']}",
                    "base": base, "tests": tests, "quint": quint, "rows": rows}

    pk = f"{ATR_PRIMARY:g}ATR"
    bk = args.buffer
    print(f"检验族每品种 {FAMILY} 项 → Bonferroni α = {ALPHA_BONF:.5f}；显著性检验口径 k={K_PRIMARY}、缓冲 {pk}；"
          f"矩阵 B 比值口径 缓冲 {bk}")

    # ── A. 基准突破率 ──
    if "A" in SEC: print(f"\n{'═'*110}\nA. 基准突破率（k=3，重叠计数）  对照：模块平衡破墙率 {BREAKEVEN[0]:.1%}~{BREAKEVEN[1]:.1%}（按期权定价能承受的破墙率）\n{'═'*110}")
    cols = ["3%", "5%", "7%", "1.5ATR", "2ATR", "3ATR"]
    if "A" in SEC: print(f"{'品种':7s}{'根数':>6s}  " + "  ".join(f"{'↓'+c:>7s}{'↑'+c:>7s}" for c in cols))
    for key in (keys if "A" in SEC else []):
        r = res[key]
        line = f"{key:7s}{r['n']:6d}  "
        for c in cols:
            line += f"{r['base'][('down',3,c)]:7.1%}{r['base'][('up',3,c)]:7.1%}  "
        print(line)

    # ── B. 跨品种矩阵 ──
    if "B" in SEC:
        print(f"\n{'═'*110}\nB. 指标尾部状态下的突破率 ÷ 基准率（k=3, 缓冲 {bk}）。>1 更危险 <1 更安全；* p<0.05  ** 过 Bonferroni（检验口径 {pk}，品种内不重叠子样本）\n{'═'*110}")
        print(f"{'指标·状态·侧':24s}" + "".join(f"{res[k]['symbol']:>7s}" for k in keys) + "   ≥1.25 ≤0.80 中位  簇复现")
    summary = []
    for name, (_, tails) in INDICATORS.items():
        for st in tails:
            for side, lab in (("down", "↓put"), ("up", "↑call")):
                line = f"{name}·{st}·{lab}"[:24].ljust(24)
                ratios = []
                for k in keys:
                    t = res[k]["tests"][(name, st, side)]
                    r = t["ratios_all"][f"{side}|k{K_PRIMARY}|{bk}"]; ratios.append(r)
                    mark = "**" if t["bonf"] else ("*" if t["p"] < 0.05 else "")
                    line += f"{(f'{r:.2f}{mark}' if not math.isnan(r) else '—'):>7s}"
                up = sum(1 for r in ratios if r >= 1.25); dn = sum(1 for r in ratios if r <= 0.80)
                med = statistics.median([r for r in ratios if not math.isnan(r)])
                # 簇复现：簇内中位比值 ≥1.25 的簇数 / ≤0.8 的簇数
                cu = cd = 0
                for members in CLUSTERS.values():
                    rs = [res[m]["tests"][(name, st, side)]["ratios_all"][f"{side}|k{K_PRIMARY}|{bk}"] for m in members if m in res]
                    rs = [x for x in rs if not math.isnan(x)]
                    if not rs: continue
                    mm = statistics.median(rs)
                    cu += mm >= 1.25; cd += mm <= 0.80
                line += f"   {up:5d} {dn:5d} {med:5.2f}  {cu}↑/{cd}↓ of {len(CLUSTERS)}"
                if "B" in SEC: print(line)
                summary.append({"indicator": name, "state": st, "side": side, "median_ratio": med,
                                "n_ge_1_25": up, "n_le_0_80": dn, "clusters_up": cu, "clusters_down": cd,
                                "buffer": bk,
                                "ratios": {res[k]["symbol"]: res[k]["tests"][(name, st, side)]["ratios_all"][f"{side}|k{K_PRIMARY}|{bk}"] for k in keys},
                                "bonf_pass": [res[k]["symbol"] for k in keys if res[k]["tests"][(name, st, side)]["bonf"]]})

    # ── C. 带宽扩张比 剂量-反应 ──
    if "C" in SEC: print(f"\n{'═'*110}\nC. 布林带宽 5 日扩张比 五分位 → 突破率（k=3, {pk}）。Q1 最收缩 … Q5 最扩张。回答「突然扩大是否更危险」\n{'═'*110}")
    if "C" in SEC: print(f"{'品种':7s}" + "".join(f"{'Q'+str(q+1)+'↓':>8s}{'Q'+str(q+1)+'↑':>7s}" for q in range(5)) + "   Q5/Q1↓  Q5/Q1↑")
    for key in (keys if "C" in SEC else []):
        q = res[key]["quint"]; line = f"{key:7s}"
        for qi in range(5):
            line += f"{q[qi]['down']:8.1%}{q[qi]['up']:7.1%}"
        r_d = q[4]["down"] / q[0]["down"] if q[0]["down"] else float("nan")
        r_u = q[4]["up"] / q[0]["up"] if q[0]["up"] else float("nan")
        line += f"   {r_d:6.2f}  {r_u:6.2f}"
        print(line)
    if "C" in SEC: print(f"   （五分位边界按各品种自身分布定；Q5 阈值示例 SLV {res['silver']['quint'][4]['lo']:.2f} / GLD {res['gold']['quint'][4]['lo']:.2f}）")

    # ── E. 波动率状态四行的稳健性：全 (k, 缓冲) 下 15 品种比值的中位数 ──
    if "E" in SEC:
        print(f"\n{'═'*110}\nE. 稳健性：各指标尾部状态在全部 (k, 缓冲) 下【15 品种比值的中位数】。同一格里 3%/5%/7% 与 ATR 口径若反号 = 缩放效应，不是信号\n{'═'*110}")
        combos = [(k, b) for k in KS for b in ["3%", "5%", "7%", "1.5ATR", "2ATR", "3ATR"]]
        print(f"{'指标·状态·侧':24s}" + "".join(f"{f'k{k} {b}':>9s}" for k, b in combos))
        for name, (_, tails) in INDICATORS.items():
            for st in tails:
                for side, lab in (("down", "↓put"), ("up", "↑call")):
                    line = f"{name}·{st}·{lab}"[:24].ljust(24)
                    for k, b in combos:
                        vals = [res[kk]["tests"][(name, st, side)]["ratios_all"][f"{side}|k{k}|{b}"] for kk in keys]
                        vals = [v for v in vals if not math.isnan(v)]
                        line += f"{statistics.median(vals):9.2f}"
                    print(line)

    # ── D. 描述性：回测唯一亏损事件与用户开仓日 ──
    if "D" in SEC: print(f"\n{'═'*110}\nD. 描述性（不是检验）：决策日 = 开仓日上一交易日收盘，各指标当时的状态\n{'═'*110}")
    events = [("silver", "2026-08-03", "up", "回测唯一亏损事件（8/3 开仓，3 天 +9.8% 破 55C）"),
              ("silver", "2026-08-04", "up", "同一事件，8/4 开仓"),
              ("silver", "2026-09-14", "down", "实盘 卖57P/买56P，对"),
              ("silver", "2026-09-17", "up", "模拟 卖60C，对（盘中两次被穿）"),
              ("gold", "2026-09-17", "up", "模拟 卖405C ×2，对")]
    for key, entry, side, note in (events if "D" in SEC else []):
        prior = [r for r in res[key]["rows"] if r["date"] < date.fromisoformat(entry)]
        if not prior: continue
        r = prior[-1]
        hit = "破" if r["out"][(side, 3, pk)] else "未破"
        flags = [f"{n}={r['states'][n]}" for n in INDICATORS if r["states"][n] in INDICATORS[n][1]]
        print(f"  {key:6s} 开仓 {entry}（决策日 {r['date']}）{note}\n"
              f"         3 天内逆向 {pk}：{hit}   尾部状态：{'、'.join(flags) or '无（全部中性）'}   带宽扩张比 {r['expand']:.2f}")

    if args.detail:
        for key in ("silver", "gold"):
            print(f"\n{'═'*110}\n{key} 逐 (k, 缓冲) 比值明细（重叠计数）\n{'═'*110}")
            ks_ = [f"{s}|k{k}|{b}" for s in ("down", "up") for k in KS for b in ["3%", "5%", "7%", "2ATR"]]
            print(f"{'指标·状态':22s}" + "".join(f"{x[:10]:>11s}" for x in ks_))
            for name, (_, tails) in INDICATORS.items():
                for st in tails:
                    t = res[key]["tests"][(name, st, "down")]
                    print(f"{(name+'·'+st)[:22]:22s}" + "".join(f"{t['ratios_all'][x]:11.2f}" for x in ks_) + f"  n={t['n_full']}")

    if args.emit:
        out = ROOT / "data" / "history" / "wall_spread" / "filter_test.json"
        payload = {"schema": 2, "asof": date.today().isoformat(),
                   "primary": {"k": K_PRIMARY, "buffer": pk}, "family": FAMILY, "alpha_bonf": ALPHA_BONF,
                   "clusters": CLUSTERS, "summary": summary,
                   # 两套口径都落盘：2ATR 是跨品种可比的，5% 是策略实际用的。
                   # 同一格在两套口径下反号 = 缩放效应（波动大所以 % 移动大），不是信号。
                   "summary_by_buffer": {bb: [
                       {"indicator": n, "state": st, "side": sd,
                        "median_ratio": statistics.median([v for v in
                            (res[k]["tests"][(n, st, sd)]["ratios_all"][f"{sd}|k{K_PRIMARY}|{bb}"] for k in keys)
                            if not math.isnan(v)]),
                        "n_ge_1_25": sum(1 for k in keys if res[k]["tests"][(n, st, sd)]["ratios_all"][f"{sd}|k{K_PRIMARY}|{bb}"] >= 1.25),
                        "n_le_0_80": sum(1 for k in keys if res[k]["tests"][(n, st, sd)]["ratios_all"][f"{sd}|k{K_PRIMARY}|{bb}"] <= 0.80)}
                       for n, (_, tails) in INDICATORS.items() for st in tails for sd in ("down", "up")]
                       for bb in (pk, "5%")},
                   "instruments": {k: {"symbol": v["symbol"], "n": v["n"], "n_sub": v["n_sub"], "span": v["span"],
                                       "base": {f"{s}|k{kk}|{b}": x for (s, kk, b), x in v["base"].items()},
                                       "tests": {f"{n}|{st}|{s}": {kk: vv for kk, vv in t.items() if kk != "ratios_all"}
                                                 for (n, st, s), t in v["tests"].items()},
                                       "quint": v["quint"]} for k, v in res.items()}}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已落盘 {out}")


if __name__ == "__main__":
    main()
