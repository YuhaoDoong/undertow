"""Golden Trident（Swing-Anchored VWAP Trend System）的独立复现与检验。

用户 2026-09-04 提供。这份脚本比之前几个专业得多，**它的设计选择恰好避开了
我们实测发现的所有坑**：

  · 默认**只做多**（allowShort=false）—— 我们实测 Supertrend 做空段 78~82% 在涨
  · 结构翻转才出场，不用指标提前平 —— 我们实测提前平仓切断厚尾
  · 8×ATR 只当灾难保护，作者自己标注 "catastrophe-only" —— 不干扰趋势
  · 写了手续费(0.02%)与滑点(2 tick)，仓位写明 20% 权益 —— 前几个脚本都没有
  · 结构判定用 `ta.highestbars(high,n)==0` 而非 `ta.pivothigh(src,n,n)`，
    **实时、无重绘** —— 不像上一个脚本的 BUY/SELL 徽章要等右侧 n 根

## 但有两处名不副实（实测）

① **Chop filter 是摆设**：`(highest(high,20)−lowest(low,20)) > atr(20)*0.8`
   的通过率是 **100.0%**（980/980）—— 20 根的区间本来就远大于单根 ATR。
② **anchoredVWAP 不参与决策**：longCond 里只有 dir / EMA / chop / 时间窗，
   VWAP 只用于画图。策略名里的核心组件实际没进交易逻辑。

所以真实逻辑简化为：**结构向上 + 站上 EMA200 + 只做多 + 结构翻转出场**。

## 实测结论（1000 根日线，逐根权益曲线，含 0.05% 往返成本）

  GLD  策略 +98.4% 回撤 17.8% Calmar 5.5  |  持有 +156.7% 回撤 26.4% Calmar 5.9
  SLV  策略 +75.2% 回撤 49.0% Calmar 1.5  |  持有 +249.6% 回撤 52.3% Calmar 4.8

收益跑输，**风险调整后也不优于买入持有**。在场时间 GLD 59% / SLV 47%。
样本只有 5~8 笔，任何结论都不牢靠。

⚠️ 这里差点犯一个会给出反向结论的错：最初按**笔**算回撤（只在平仓时更新权益），
GLD 得到 3.7%，据此算出 Calmar 31 —— 是买入持有的 5 倍。
改成逐根后回撤是 17.8%，Calmar 5.5，优势消失。**按笔算回撤会严重低估**，
因为段内浮亏完全没被计入。与 ta/backtest.py 那次（各段相加 vs 权益递推）同类。
"""
import sys
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.analyze.ta import ema

ROUND_TRIP = 0.05      # 0.02% 手续费 + 2 tick 滑点，往返约 0.05%


def structure_dir(h, l, swing=30):
    """ta.highestbars(high,n)==0 → 当前 bar 即近 n 根最高。实时，无重绘。
    dir=1 当最近一次创新高比最近一次创新低更晚发生。"""
    phL = plL = 0
    out = []
    for i in range(len(h)):
        if i >= swing - 1:
            if h[i] >= max(h[i - swing + 1:i + 1]):
                phL = i
            if l[i] <= min(l[i - swing + 1:i + 1]):
                plL = i
        out.append(1 if phL > plL else -1)
    return out


def run(o, h, l, c, *, swing=30, ema_len=200, use_ema=True,
        weight=1.0, cost=ROUND_TRIP):
    """逐根建权益曲线。

    ⚠️ **回撤必须逐根算，不能按笔算。** 2026-09-04 实测：
    按笔（只在平仓时更新权益）给出 GLD 回撤 3.7%，逐根是 17.8% —— 低估 5 倍，
    因为段内的浮亏完全没被计入。差点据此得出「Calmar 是买入持有的 5 倍」的反向结论。
    这与 ta/backtest.py 那次（各段相加 vs 权益递推）是同一类错误。
    """
    d = structure_dir(h, l, swing)
    e = ema(c, ema_len)
    eq, peak, mdd = 1.0, 1.0, 0.0
    inpos, prev, entry = False, False, None
    segs, days = [], 0
    for i in range(1, len(c)):
        if inpos:                                  # 持仓期逐根计入盈亏
            eq *= 1 + weight * (c[i] / c[i-1] - 1)
            days += 1
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        ok = (d[i-1] == 1) and (not use_ema or (e[i-1] is not None and c[i-1] > e[i-1]))
        if not inpos and ok and not prev:
            inpos, entry = True, o[i] if i < len(o) else c[i-1]
            eq *= 1 - weight * cost / 200          # 单边成本（往返的一半）
        elif inpos and d[i-1] == -1:
            inpos = False
            eq *= 1 - weight * cost / 200
            if entry:
                segs.append((c[i-1] / entry - 1) * 100)
        prev = ok
    if inpos and entry:
        segs.append((c[-1] / entry - 1) * 100)
    if not segs:
        return None
    win = sum(1 for x in segs if x > 0)
    return dict(n=len(segs), ret=(eq - 1) * 100, mdd=mdd * 100,
                win=win / len(segs) * 100, best=max(segs), worst=min(segs),
                expo=days / (len(c) - 1) * 100)


def main():
    for sym in ('GLD.US', 'SLV.US'):
        b = fetch_bars(sym, period='day', count=1000)
        o = [x['open'] for x in b]; h = [x['high'] for x in b]
        l = [x['low'] for x in b]; c = [x['close'] for x in b]
        bh = (c[-1] / c[0] - 1) * 100
        print(f'\n══ {sym}  {str(b[0]["ts"])[:10]} → {str(b[-1]["ts"])[:10]} ══')
        print(f'  买入持有 {bh:+.1f}%')
        # 买入持有也逐根算回撤，两边同口径
        beq, bp, bmdd = 1.0, 1.0, 0.0
        for i in range(1, len(c)):
            beq *= c[i] / c[i-1]
            bp = max(bp, beq); bmdd = max(bmdd, 1 - beq / bp)
        print(f'  买入持有回撤 {bmdd*100:.1f}%  Calmar {bh/(bmdd*100):.1f}')
        print(f'  {"配置":<26}{"笔数":>4}{"在场":>6}{"收益":>10}{"回撤":>8}{"Calmar":>8}{"胜率":>6}')
        for lab, kw in (('原版 20% 仓位', dict(weight=0.2)),
                        ('满仓（便于对比）', dict(weight=1.0)),
                        ('满仓 · 去掉 EMA200 过滤', dict(weight=1.0, use_ema=False)),
                        ('满仓 · swing=15', dict(weight=1.0, swing=15)),
                        ('满仓 · swing=50', dict(weight=1.0, swing=50))):
            r = run(o, h, l, c, **kw)
            if r is None:
                print(f'  {lab:<28}无信号'); continue
            cal = r["ret"] / r["mdd"] if r["mdd"] > 0 else float("inf")
            print(f'  {lab:<26}{r["n"]:>4}{r["expo"]:>5.0f}%{r["ret"]:>+9.1f}%'
                  f'{r["mdd"]:>7.1f}%{cal:>8.1f}{r["win"]:>5.0f}%')


if __name__ == '__main__':
    main()
