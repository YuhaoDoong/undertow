"""Supertrend 只做多 + 六种提前平仓方案的对照（用户 2026-09-04 提议）。

用户的问题：「趋势交易容易拿着拿着突然大回撤，如果能提前平仓，
是否会大幅改善？比如引入 MACD 让它提前平仓，保住收益。」

结论（三轮实验，见文件末尾的总结）：
  ① 提前平仓**确实能降回撤** —— GLD 14.8% → 5.7%，Calmar 2.56 → 7.74
  ② 但代价是**切断厚尾** —— SLV 最大单段 +52.7% → +15.0%，
     总收益从 +107.1% 变成 −17.8%
  ③ 加「浮盈门槛」能保住厚尾（SLV 浮盈>10% 才平一半 → +122.6%，优于基准），
     但 GLD 上浮盈门槛反而更差。**三个品种三个最优方案 = 过拟合**
  ④ **根本问题不在出场**：Supertrend 只在场 46~58% 的时间，
     而空仓期的涨幅在 GLD/USO 上比在场期还多（GLD 67.0% vs 37.8%）。
     任何让你更早离场的规则，只会把这个问题放大。
  ⑤ 改用「调仓位而非开关仓」（scripts/st_position_weight.py）验证：
     红色段仓位从 0% 加到 100%，三个上涨品种上**收益单调上升**，
     Calmar 最优出现在 red=75~100% —— 也就是几乎不减仓。

→ 这个方向优化不出来。不是方法没找对，是 Supertrend 在这些品种上
  没有可提取的方向信息，任何据此减少多头暴露的规则都在损害收益。



⚠️ 方案**预先全部列出**，跑完全部报告，不事后挑 —— 六个方案在 α=0.05 下
至少一个假阳性的概率约 26%。
⚠️ 入场统一：Supertrend 翻多。只做多（做空段已证伪：78~82% 在涨）。
⚠️ 平仓后必须等下一次 Supertrend 翻多才再进场，不许原地重进。
"""
import sys, statistics as st
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.analyze.ta import supertrend as ST, macd as M, ema, atr, highest

COST = 0.10


def build(sym, tf='day', n=1000):
    b = fetch_bars(sym, period=tf, count=n)
    return ([x['open'] for x in b], [x['high'] for x in b],
            [x['low'] for x in b], [x['close'] for x in b])


def exit_rules(o, h, l, c):
    """预先定义的六种出场触发器。返回 {名称: [每根是否触发]}。"""
    _, _, tr = ST.supertrend(h, l, c)
    m, sg, hist = M.macd_series(c)
    e20 = ema(c, 20)
    a = atr(h, l, c, 14)
    n = len(c)
    R = {}
    R['基准:ST翻空'] = [tr[i] == -1 for i in range(n)]
    R['MACD柱转负'] = [hist[i] is not None and hist[i] < 0 for i in range(n)]
    R['MACD死叉'] = [m[i] is not None and sg[i] is not None and m[i] < sg[i]
                     for i in range(n)]
    R['跌破EMA20'] = [e20[i] is not None and c[i] < e20[i] for i in range(n)]
    return R, tr, a, hist


def run(sym, rule, n=1000):
    o, h, l, c = build(sym, n=n)
    R, tr, a, hist = exit_rules(o, h, l, c)
    entries = [i for i in range(1, len(c))
               if tr[i] == 1 and tr[i - 1] == -1 and i + 1 < len(o)]
    eq, peak, mdd = 1.0, 1.0, 0.0
    segs, held = [], 0
    for k, i in enumerate(entries):
        ep = o[i + 1]
        # 出场：规则触发 或 ST 翻空（兜底）或 序列结束
        j = None
        for t in range(i + 1, len(c)):
            if rule == '回撤5%':
                hh = max(h[i + 1:t + 1])
                hit = c[t] < hh * 0.95
            elif rule == 'ATR3倍追踪':
                hh = max(h[i + 1:t + 1])
                hit = a[t] is not None and c[t] < hh - 3 * a[t]
            else:
                hit = R[rule][t]
            if hit or tr[t] == -1:
                j = t; break
        if j is None:
            j = len(c) - 1
        xp = o[j + 1] if j + 1 < len(o) else c[j]
        r = (xp / ep - 1) * 100 - COST
        segs.append(r); held += j - i
        eq *= 1 + r / 100
        peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
    if not segs:
        return None
    bh = (c[-1] / o[entries[0] + 1] - 1) * 100 if entries else 0
    return dict(n=len(segs), ret=(eq - 1) * 100, mdd=mdd * 100,
                win=sum(1 for x in segs if x > 0) / len(segs) * 100,
                best=max(segs), worst=min(segs),
                held=held / len(segs), bh=bh)


RULES = ['基准:ST翻空', 'MACD柱转负', 'MACD死叉', '跌破EMA20', '回撤5%', 'ATR3倍追踪']
for sym in ('GLD.US', 'SLV.US', 'USO.US', 'UUP.US'):
    print(f'\n══ {sym} （日线 1000 根，只做多，Supertrend 翻多入场）══')
    print(f'{"出场规则":<14}{"段数":>4}{"总收益":>9}{"最大回撤":>9}{"Calmar":>8}'
          f'{"胜率":>6}{"最大单段":>9}{"最差":>8}{"平均持有":>8}')
    base = None
    for rule in RULES:
        r = run(sym, rule)
        if r is None:
            continue
        if base is None:
            base = r
        cal = r['ret'] / r['mdd'] if r['mdd'] > 0 else float('inf')
        print(f'{rule:<14}{r["n"]:>4}{r["ret"]:>+8.1f}%{r["mdd"]:>8.1f}%{cal:>8.2f}'
              f'{r["win"]:>5.0f}%{r["best"]:>+8.1f}%{r["worst"]:>+7.1f}%{r["held"]:>7.0f}根')
    if base:
        print(f'{"同期买入持有":<14}{"":>4}{base["bh"]:>+8.1f}%')
