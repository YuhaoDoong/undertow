"""Supertrend 的红绿段真相 + 杠杆能不能救 —— 回答用户 2026-09-04 的三问。

用户问：
  ① 红色段平均确实是下降的吗？绿色段是上升的吗？
     「这样的话应该是有意义的呀，因为它的买点和卖点不是事后设置的。」
  ② 如果是这样，为什么会比不过单纯持有？
  ③ 如果是期货杠杆交易呢，是否能放大收益？

═══════════════════════════════════════════════════════════════════
① 红色段**不是**下降的 —— 这是全部问题的根源
═══════════════════════════════════════════════════════════════════
2022-09-09 → 2026-09-03，1000 根日线，段内**价格**变动（不按持仓方向计）：

              段数   价格均值   中位数   价格上涨占比
  GLD 绿色段    21    +1.87%   +0.74%      57%
  GLD 红色段    22    +2.98%   +3.10%      **82%**
  SLV 绿色段    18    +5.08%   +3.30%      61%
  SLV 红色段    18    +3.14%   +4.23%      **78%**

红色段的价格不但不跌，78~82% 的时候在涨，GLD 上甚至比绿色段涨得还多。

机制：Supertrend 翻空的条件是收盘跌破下轨。在上涨行情里，这个条件通常
在**回调的低点附近**才满足 —— 于是它恰好卖在坑底，然后价格反弹，
一路涨到突破上轨才翻多。所以「红色段」实际是「回调末端到新高」这一段。

用户说得对：买卖点确实不是事后设置的，信号是实时的、不含未来。
问题不在前视偏差，而在**牛市里跌破下轨这件事本身就不预示继续下跌**。

═══════════════════════════════════════════════════════════════════
② 为什么比不过单纯持有
═══════════════════════════════════════════════════════════════════
  · 红色段做空，而红色段 78~82% 在涨 → 做空段是纯亏损（胜率仅 18~22%）
  · 绿色段虽然赚，但上涨占比 57~61%，只比「一直持有」的基线高一点点
  · strategy.entry 是 always-in-market：反向信号自动反手，**没有空仓选项**

即使改成「只做多、红色段空仓」，GLD 也只有 +40.8%，仍远低于持有的 +156.7%
—— 因为空仓期恰好踏空了那 82% 的上涨。

═══════════════════════════════════════════════════════════════════
③ 杠杆救不了，而且会更快归零
═══════════════════════════════════════════════════════════════════
逐根建权益曲线（含每次换仓 0.1% 成本）：

  GLD    Supertrend                买入持有
   1x    −33.0%  回撤 51%          +156.7%  回撤 26%
   2x    −61.7%  回撤 78%          +461.4%  回撤 49%
   3x    −81.5%  回撤 91%          +942.0%  回撤 66%
   5x    −97.5%  回撤 99%         +2024.1%  回撤 88%
  10x    爆仓（第850根）            爆仓（第850根）

  SLV    Supertrend                买入持有
   1x    −13.9%  回撤 55%          +249.6%  回撤 52%
   2x    −63.6%  回撤 83%          +487.7%  回撤 84%
   3x    −95.0%  回撤 97%          +206.7%  回撤 98%
   5x    爆仓                       爆仓

**杠杆放大的是收益率，不改变正负号。** 策略本身是负的，杠杆只让它更负。

还有一个反直觉之处：SLV 买入持有 2x 是 +487.7%，3x 反而只有 +206.7% ——
这是**波动率拖累**。杠杆 L 的几何收益 ≈ L·μ − L²σ²/2，二次项随 L 增长更快，
超过 μ/σ² 后加杠杆反而减少收益：

  GLD  日均 +0.102%  日波动 1.25%  →  最优杠杆 6.5x
  SLV  日均 +0.158%  日波动 2.50%  →  最优杠杆 2.5x

白银波动是黄金的两倍，能承受的杠杆只有黄金的三分之一。
而这还是**假设策略为正**的情况下 —— Supertrend 是负的，最优杠杆是 0。
"""
import sys, statistics as st
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.analyze.ta import supertrend as ST, backtest as B


def seg_stats(sym):
    b = fetch_bars(sym, period='day', count=1000)
    o = [x['open'] for x in b]; h = [x['high'] for x in b]
    l = [x['low'] for x in b]; c = [x['close'] for x in b]
    r = B.run(o, c, ST.flips(h, l, c), cost_pct=0.0)
    print(f'\n══ {sym} 段内价格变动（不按方向计）══')
    for d, lab in ((1, '绿色段'), (-1, '红色段')):
        segs = [s for s in r.segments if s.direction == d]
        px = [(s.exit_px / s.entry_px - 1) * 100 for s in segs]
        up = sum(1 for x in px if x > 0) / len(px) * 100
        print(f'  {lab} {len(segs):>2} 段  均值 {st.mean(px):>+6.2f}%  '
              f'中位 {st.median(px):>+6.2f}%  上涨占比 {up:>3.0f}%')


def leverage(sym):
    b = fetch_bars(sym, period='day', count=1000)
    o = [x['open'] for x in b]; h = [x['high'] for x in b]
    l = [x['low'] for x in b]; c = [x['close'] for x in b]
    _, _, tr = ST.supertrend(h, l, c)
    rets = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
    mu, sd = st.mean(rets), st.pstdev(rets)
    print(f'\n══ {sym} 杠杆 ══  日均 {mu*100:+.3f}%  日波动 {sd*100:.2f}%  '
          f'最优杠杆 μ/σ² = {mu/sd**2:.1f}x')

    def run(lev, strat):
        eq, peak, mdd, pos = 1.0, 1.0, 0.0, 0
        for i in range(1, len(c)):
            if strat and tr[i - 1] is None:
                continue
            p = tr[i - 1] if strat else 1
            if pos != 0 or not strat:
                eq *= 1 + lev * (p if strat else 1) * (c[i] / c[i - 1] - 1)
            if eq <= 0:
                return None, 100.0
            if strat and p != pos:
                eq *= 1 - lev * 0.001
                pos = p
            peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
        return (eq - 1) * 100, mdd * 100
    for lev in (1, 2, 3, 5):
        a, ad = run(lev, True); bq, bd = run(lev, False)
        fa = '爆仓' if a is None else f'{a:+.1f}%'
        fb = '爆仓' if bq is None else f'{bq:+.1f}%'
        print(f'  {lev}x  Supertrend {fa:>9} (回撤{ad:.0f}%)   '
              f'买入持有 {fb:>9} (回撤{bd:.0f}%)')


if __name__ == '__main__':
    for s in ('GLD.US', 'SLV.US'):
        seg_stats(s)
    for s in ('GLD.US', 'SLV.US'):
        leverage(s)
