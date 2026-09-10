#!/usr/bin/env python3
"""st_smc_combo 的参数稳健性 —— 结论是真的还是挑出来的？

默认参数下 A/C/S 看着都比纯 SuperTrend 强（3/8 vs 1/8 跑赢买入持有）。
但每个组合只有 2~6 段，这个量级里一两笔交易就能翻转排名。
扫 break_len × ST(period, mult)，看"哪个变体最好"是否稳定。
"""
import sys, statistics
sys.path.insert(0, "/Users/yhdong/Trading")
from undertow.analyze.ta import frames, supertrend as st, backtest
from st_smc_combo import struct_states, combo_flips

MODES = ("base", "A", "B", "C", "S")
GRID = [(bl, p, m) for bl in (5, 10, 15, 20)
                   for p in (7, 10, 14)
                   for m in (2.0, 3.0, 4.0)]

def main():
    combos = [("GLD.US","1d"),("GLD.US","4h"),("SLV.US","1d"),("SLV.US","4h"),
              ("USO.US","1d"),("USO.US","4h"),("QQQ.US","1d"),("QQQ.US","4h")]
    data = {}
    for sym, tf in combos:
        b = frames.bars(sym, tf)
        data[(sym,tf)] = ([x["open"] for x in b],[x["high"] for x in b],
                          [x["low"] for x in b],[x["close"] for x in b])

    # 每个参数点：各变体跑赢买入持有的品种数 + 平均超额
    win_by_mode = {m: [] for m in MODES}
    exc_by_mode = {m: [] for m in MODES}
    best_count  = {m: 0 for m in MODES}
    seg_by_mode = {m: [] for m in MODES}

    for bl, p, mu in GRID:
        wins = {m: 0 for m in MODES}
        excs = {m: [] for m in MODES}
        for key,(o,h,l,c) in data.items():
            _,_,tr = st.supertrend(h,l,c,period=p,mult=mu)
            stt = struct_states(h,l,c,bl)
            for m in MODES:
                r = backtest.run(o,c,combo_flips(tr,stt,m))
                if r.vs_buy_hold > 0: wins[m]+=1
                excs[m].append(r.vs_buy_hold)
                seg_by_mode[m].append(r.n)
        for m in MODES:
            win_by_mode[m].append(wins[m])
            exc_by_mode[m].append(statistics.mean(excs[m]))
        top = max(MODES, key=lambda m: statistics.mean(excs[m]))
        best_count[top]+=1

    print(f"参数网格 {len(GRID)} 个点（break_len×period×mult），每点 8 个品种×周期\n")
    print(f"{'变体':<6}{'跑赢数 中位':>12}{'跑赢 最差~最好':>16}{'平均超额 中位':>14}"
          f"{'平均超额 范围':>20}{'段数中位':>10}{'最优次数':>10}")
    for m in MODES:
        w = sorted(win_by_mode[m]); e = sorted(exc_by_mode[m]); s = sorted(seg_by_mode[m])
        print(f"{m:<6}{statistics.median(w):>10.1f}/8{f'{w[0]}~{w[-1]}':>16}"
              f"{statistics.median(e):>13.1f}%{f'{e[0]:+.1f}% ~ {e[-1]:+.1f}%':>20}"
              f"{statistics.median(s):>10.0f}{best_count[m]:>10}")

    print("\n—— 逐参数点：谁跑赢的品种最多（并列则都算）——")
    from collections import Counter
    cnt = Counter()
    for i,(bl,p,mu) in enumerate(GRID):
        mx = max(win_by_mode[m][i] for m in MODES)
        for m in MODES:
            if win_by_mode[m][i] == mx: cnt[m]+=1
    for m in MODES:
        print(f"  {m:<6} 在 {cnt[m]:>2}/{len(GRID)} 个参数点上并列第一")

if __name__ == "__main__":
    main()
