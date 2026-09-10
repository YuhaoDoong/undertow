#!/usr/bin/env python3
"""SuperTrend × SMC 结构确认（BOS/CHoCH）的组合回测。

**为什么试这个组合**（用户 2026-09-09 提出）：
两个脚本"图上看起来都不错"，但那是两种不同的假象——
  · SMC Liquidity Matrix 的高低点徽章是 pivot(20,20) 画在 −20 根，纯事后标注；
    liquidity zones 还会 box.delete 掉被打穿的，剩下的全是幸存者（实测 40~64% 已消失）。
  · SuperTrend 无重绘，但实测红段有 78~82% 在上涨（它卖在回调底部），
    瓶颈是"不在市"而不是"拿不住"。
把两个假象叠一起不会变成真的。**但 SMC 里唯一实时无重绘的 BOS/CHoCH，
恰好针对 SuperTrend 已量化的那个缺陷**：假翻转。这是可检验的，所以测它。

结构状态机（完全实时，右侧 R 根走完才确认 pivot，绝不回看）：
    close 上穿最近【已确认】pivot high → struct = +1
    close 下穿最近【已确认】pivot low  → struct = −1

四个变体：
    base   纯 SuperTrend
    A      离场需结构确认（ST 翻空但 struct 仍 +1 → 继续持多）
    B      入场需结构确认（ST 翻多但 struct 仍 −1 → 不进多）
    C      双向都要（ST 与 struct 一致才持有该方向）
    S      纯结构（只用 BOS/CHoCH，看结构自己值不值钱）

口径沿用 ta/backtest.py：次根开盘成交、强制成本、复利、同期买入持有。
"""
import sys
sys.path.insert(0, "/Users/yhdong/Trading")
from undertow.analyze.ta import frames, supertrend as st, backtest

BREAK_LEN = 10          # 脚本默认 break_len
ST_KW = dict(period=10, mult=3.0)


def struct_states(highs, lows, closes, L=BREAK_LEN):
    """逐根的结构方向 [-1/+1/0]，只用【已确认】的 pivot —— 无未来函数。

    pivot 在 i 处成立要等右侧 L 根走完，所以在 bar j 能引用的最新 pivot
    下标至多 j-L。这正是原脚本 `last_ph_b` 的语义（它在 pivot 确认那根
    才赋值），区别只在于原脚本把【标签】画到了 −L 根的位置上。
    """
    n = len(closes)
    out = [0] * n
    cur = 0
    last_ph = last_pl = None
    for j in range(n):
        i = j - L                      # 此刻刚确认的 pivot 候选
        if i - L >= 0 and i + L < n and i + L <= j:
            v = highs[i]
            if all(v > x for x in highs[i-L:i]) and all(v >= x for x in highs[i+1:i+1+L]):
                last_ph = v
            v = lows[i]
            if all(v < x for x in lows[i-L:i]) and all(v <= x for x in lows[i+1:i+1+L]):
                last_pl = v
        if last_ph is not None and closes[j] > last_ph and (j == 0 or closes[j-1] <= last_ph):
            cur = 1
            last_ph = None             # 原脚本同款：触发后置空，避免重复
        if last_pl is not None and closes[j] < last_pl and (j == 0 or closes[j-1] >= last_pl):
            cur = -1
            last_pl = None
        out[j] = cur
    return out


def combo_flips(tr, struct, mode):
    """把 SuperTrend 方向序列 + 结构序列合成持仓翻转 [(idx, dir)]。"""
    pos = 0
    out = []
    for j in range(len(tr)):
        want = tr[j]
        if want is None:
            continue
        s = struct[j]
        if mode == "base":
            new = want
        elif mode == "A":                       # 离场需结构确认
            new = want if (want == 1 or s == -1) else pos
        elif mode == "B":                       # 入场需结构确认
            new = want if (want == -1 or s == 1) else pos
        elif mode == "C":                       # 双向都要
            new = want if want == s else pos
        elif mode == "S":                       # 纯结构
            new = s
        if new and new != pos:
            pos = new
            out.append((j, new))
    return out


def main():
    combos = [("GLD.US", "1d"), ("GLD.US", "4h"), ("SLV.US", "1d"), ("SLV.US", "4h"),
              ("USO.US", "1d"), ("USO.US", "4h"), ("QQQ.US", "1d"), ("QQQ.US", "4h")]
    hdr = f"{'品种/周期':<14}{'变体':<6}{'段数':>5}{'净收益':>10}{'买入持有':>10}{'超额':>10}{'盈亏比':>8}"
    wins = {m: 0 for m in ("base", "A", "B", "C", "S")}
    tot = 0
    for sym, tf in combos:
        try:
            b = frames.bars(sym, tf)
        except Exception as e:
            print(f"{sym} {tf}: {type(e).__name__} {e}")
            continue
        o = [x["open"] for x in b]; h = [x["high"] for x in b]
        l = [x["low"] for x in b];  c = [x["close"] for x in b]
        _, _, tr = st.supertrend(h, l, c, **ST_KW)
        stt = struct_states(h, l, c)
        print(f"\n{'='*64}\n{sym} {tf}  {len(b)} 根")
        print(hdr)
        tot += 1
        for m in ("base", "A", "B", "C", "S"):
            r = backtest.run(o, c, combo_flips(tr, stt, m))
            if m != "base" and r.vs_buy_hold > 0:
                wins[m] += 1
            if m == "base" and r.vs_buy_hold > 0:
                wins[m] += 1
            pf = r.profit_factor
            pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
            print(f"{sym+' '+tf:<14}{m:<6}{r.n:>5}{r.net_pct:>9.1f}%{r.buy_hold_pct:>9.1f}%"
                  f"{r.vs_buy_hold:>9.1f}%{pf_s:>8}")
    print(f"\n{'='*64}\n跑赢同期买入持有的组合数（共 {tot} 个品种×周期）：")
    for m in ("base", "A", "B", "C", "S"):
        print(f"  {m:<6} {wins[m]}/{tot}")


if __name__ == "__main__":
    main()
