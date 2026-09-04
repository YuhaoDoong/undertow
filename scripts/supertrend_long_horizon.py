"""Supertrend 四年长历史复盘 —— 回答「图上看着覆盖每一段，为什么收益不行」。

用户 2026-09-04 在 TradingView 上看到两份 Supertrend 策略脚本，
一份报 16%、一份报 0.06%，问「买点似乎一样，有区别吗」。

结论有两层：

一、**两份脚本的信号完全一样，收益差 260 倍全来自仓位设置**

    脚本A  strategy(..., default_qty_type=strategy.percent_of_equity,
                     default_qty_value=15)      每笔 15% 权益
    脚本B  strategy("SuperTrend STRATEGY", overlay=true)   什么都没写
           → Pine 默认 strategy.fixed / qty=1，**每笔固定 1 股**

    Pine 默认 initial_capital=1,000,000。GLD 约 400 美元/股，
    1 股敞口 = 0.04% 初始资金，与 15% 相差 375 倍。
    反推：16% × (0.04/15) = 0.043% —— 与看到的 0.06% 同量级。

二、**16% 这个数字必须和同期买入持有比**

    2022-09-09 → 2026-09-03（1000 根日线）：

      GLD  买入持有 +164.8%   Supertrend 满仓 −28.7%（0 成本）
      SLV  买入持有 +253.3%   Supertrend 满仓 +13.9%

    图上"覆盖每一段"是真的 —— 但拆开看钱去哪了：

      GLD  做多 21 段 胜率 57% 贡献 +40.8%
           做空 22 段 胜率 **18%** 贡献 **−49.3%**
      SLV  做多 18 段 胜率 61% 贡献 +110.8%
           做空 18 段 胜率 **22%** 贡献 **−46.0%**

    绿色段确实赚钱，红色段把利润全吃掉了。而 strategy.entry 是
    always-in-market：反向信号自动平仓反手，**没有空仓这个选项**。

    更关键的是：即使只做多不做空，GLD 也只有 +40.8%，仍远低于 +164.8%。
    做空段按方向计平均 −2.98%，意味着那些"红色段"里价格其实平均**涨了** 2.98%
    —— 在这四年的牛市里，Supertrend 的红色段根本不是下跌段，只是回调，
    而回调很快被买回。

    这与 validation.py 的 ta_indicators_direction 是同一件事的两个侧面。
"""
import sys
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.analyze.ta import supertrend as ST, backtest as B


def equity(segs):
    e = 1.0
    for s in segs:
        e *= 1 + s.ret_pct / 100
    return (e - 1) * 100


def main():
    for sym in ('GLD.US', 'SLV.US'):
        b = fetch_bars(sym, period='day', count=1000)
        o = [x['open'] for x in b]; h = [x['high'] for x in b]
        l = [x['low'] for x in b]; c = [x['close'] for x in b]
        r = B.run(o, c, ST.flips(h, l, c), cost_pct=0.0)
        L = [s for s in r.segments if s.direction == 1]
        S = [s for s in r.segments if s.direction == -1]
        print(f'\n══ {sym}  {str(b[0]["ts"])[:10]} → {str(b[-1]["ts"])[:10]} ══')
        print(f'  同期买入持有 {r.buy_hold_pct:+.1f}%   Supertrend 满仓 {r.net_pct:+.1f}%')
        for segs, lab in ((L, '做多段'), (S, '做空段')):
            w = sum(1 for s in segs if s.ret_pct > 0)
            print(f'  {lab}  {len(segs):>2} 段  胜率 {w/len(segs)*100:>3.0f}%  '
                  f'复利贡献 {equity(segs):>+8.1f}%  平均 {sum(s.ret_pct for s in segs)/len(segs):>+5.2f}%')
        print(f'  只做多不做空 {equity(L):+.1f}%  ← 仍远低于买入持有')


if __name__ == '__main__':
    main()
