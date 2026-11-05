"""超买超卖统一回测（探索性研究，只读；2026-11-05 用户「统一回测一下超买超卖指标」）。

逐品种（GLD/SLV/USO/QQQ/TQQQ/SPY/IWM/TLT）+ 池化日期块 bootstrap（同时处理跨品种同日相关与
5 日窗口重叠，回应 stretch.CALIB_META caveat ①）+ 前后半段稳定性 + 绝对方向准确率 + 块长敏感性。

不改 stretch.py 的 CALIB；信号与超额口径完全复用 stretch_backtest.build_samples（无前视、局部去趋势）。
逐品种显著性沿用现行口径：不重叠子样本（i % 5 == 0）Welch 双样本 t，对照同 regime 中性桶。
当日未完成 bar 先剔除（与 tech/report 同口径）。

用法：python3 scripts/stretch_unified_backtest.py OUT.json
结果说明：data/history/stretch_calib/2026-11-05_unified.md
"""
import json, math, random, statistics, sys
from collections import defaultdict
from undertow.collect.yahoo_futures import YahooFuturesSource
from undertow.analyze import stretch_backtest as sb
from undertow.analyze.stretch import band_of
from undertow.cli import _drop_incomplete_bar
from undertow.core.clock import market_today

H = 5
SYMS = ["GLD", "SLV", "USO", "QQQ", "TQQQ", "SPY", "IWM", "TLT"]
POOL5 = ["GLD", "SLV", "USO", "QQQ", "SPY"]          # 现行 CALIB 面板
DEEP = ("极超卖", "强超卖")
random.seed(20261105)

src = YahooFuturesSource()
data = {}
for s in SYMS:
    ser = _drop_incomplete_bar(src.fetch_series(s, rng="max", use_cache=False), market_today())
    smp = sb.build_samples(s, ser.highs, ser.lows, ser.closes, horizons=(H,))
    data[s] = (ser, smp)
    print(f"{s}: {ser.dates[0]}→{ser.dates[-1]} {len(ser.closes)} 根, 样本 {len(smp)}", file=sys.stderr)


def nov(xs):
    return [x for x in xs if x.i % H == 0]


def edge_welch(tgt, neu):
    a = [x.excess[H] for x in nov(tgt)]
    b = [x.excess[H] for x in nov(neu)]
    d, se, t = sb.welch(a, b)
    return d, se, t, len(a)


def deep_sel(x):
    return band_of(x.pctile) in DEEP


def agree_sel(x):
    return x.p_stretch <= 0.10 and x.p_dd <= 0.10


def neutral_sel(x):
    return band_of(x.pctile) == "中性"


def overbought_sel(x):
    return band_of(x.pctile) in ("极超买", "强超买")


def per_symbol(smp, sel, regime=None):
    pool = [x for x in smp if regime is None or x.regime == regime]
    tgt = [x for x in pool if sel(x)]
    # 中性基准同 regime；regime=None 时各自 regime 去中性均值后合并
    if regime is not None:
        neu = [x for x in pool if neutral_sel(x)]
        return edge_welch(tgt, neu)
    # 合并两 regime：超额减去各自 regime 中性（不重叠）均值
    adj_t, adj_n = [], []
    for rg in ("牛", "熊"):
        p = [x for x in smp if x.regime == rg]
        neu = [x.excess[H] for x in nov(p) if neutral_sel(x)]
        if len(neu) < 30:
            continue
        m = statistics.fmean(neu)
        adj_t += [x.excess[H] - m for x in nov(p) if sel(x)]
        adj_n += [v - m for v in neu]
    d, se, t = sb.welch(adj_t, adj_n)
    return d, se, t, len(adj_t)


out = {"per_symbol": {}, "halves": {}, "pooled": {}, "direction": {}}

# —— 1. 逐品种 ——
for s in SYMS:
    ser, smp = data[s]
    row = {}
    for rg in ("牛", "熊"):
        row[f"deep_{rg}"] = per_symbol(smp, deep_sel, rg)
        row[f"ob_{rg}"] = per_symbol(smp, overbought_sel, rg)
    row["deep_all"] = per_symbol(smp, deep_sel)
    row["agree_all"] = per_symbol(smp, agree_sel)
    # 绝对方向（不重叠）
    c = ser.closes
    up_deep = [c[x.i + H] > c[x.i] for x in nov(smp) if deep_sel(x)]
    up_all = [c[x.i + H] > c[x.i] for x in nov(smp)]
    row["dir"] = (sum(up_deep) / len(up_deep) * 100 if up_deep else None, len(up_deep),
                  sum(up_all) / len(up_all) * 100)
    out["per_symbol"][s] = row

# —— 2. 前后半段（按每个品种自己的样本时间中点切）——
for s in SYMS:
    ser, smp = data[s]
    mid = smp[len(smp) // 2].i
    a = [x for x in smp if x.i < mid]
    b = [x for x in smp if x.i >= mid]
    out["halves"][s] = {
        "split": str(ser.dates[mid]),
        "first": per_symbol(a, deep_sel),
        "second": per_symbol(b, deep_sel),
    }

# —— 3. 池化 + 日期块 bootstrap（跨品种同日相关 + 5 日重叠一起处理）——
def pooled(symbols, sel, label, block_days=14, reps=600):
    rows = []   # (block, regime, is_tgt, is_neu, excess)
    for s in symbols:
        ser, smp = data[s]
        for x in smp:
            d = ser.dates[x.i]
            blk = d.toordinal() // block_days
            rows.append((blk, x.regime, sel(x), neutral_sel(x), x.excess[H]))

    def edge(rs):
        tot_n, tot = 0, 0.0
        for rg in ("牛", "熊"):
            neu = [r[4] for r in rs if r[1] == rg and r[3]]
            tg = [r[4] for r in rs if r[1] == rg and r[2]]
            if len(neu) < 30 or not tg:
                continue
            m = statistics.fmean(neu)
            tot += sum(v - m for v in tg)
            tot_n += len(tg)
        return tot / tot_n if tot_n else None

    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    blocks = list(by)
    n_blk_tgt = len({r[0] for r in rows if r[2]})
    point = edge(rows)
    boots = []
    for _ in range(reps):
        pick = [by[random.choice(blocks)] for _ in blocks]
        e = edge([r for g in pick for r in g])
        if e is not None:
            boots.append(e)
    boots.sort()
    se = statistics.pstdev(boots)
    lo, hi = boots[int(0.025 * len(boots))], boots[int(0.975 * len(boots))]
    return {"label": label, "edge": point, "se": se, "t_boot": point / se if se else None,
            "ci": (lo, hi), "n_trig": sum(1 for r in rows if r[2]), "n_blocks_with_trig": n_blk_tgt,
            "p_le0": sum(1 for b in boots if b <= 0) / len(boots)}

for name, syms in (("现行面板5", POOL5), ("全部8（含TQQQ/IWM/TLT）", SYMS)):
    out["pooled"][name] = [pooled(syms, deep_sel, "深度超卖（极+强）"),
                           pooled(syms, agree_sel, "两维都超卖"),
                           pooled(syms, overbought_sel, "深度超买（极+强）")]

# —— 4. 块长敏感性（现行面板）——
out["block_sensitivity"] = {
    str(bd): [pooled(POOL5, deep_sel, "深度超卖（极+强）", block_days=bd, reps=400),
              pooled(POOL5, overbought_sel, "深度超买（极+强）", block_days=bd, reps=400)]
    for bd in (30, 91, 182)}

json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1, default=str)
print("done", file=sys.stderr)
