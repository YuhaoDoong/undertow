"""用趋势指标**调仓位**而不是开关仓。

前两轮发现：瓶颈不在出场规则，在「不在场」——
Supertrend 只在场 46~58% 的时间，而空仓期(红色段)的涨幅在 GLD/USO 上
比在场期还多。任何让你更早离场的规则，只会把这个问题放大。

换个用法：绿色段满仓，红色段**减仓而非清仓**。
red=100% 就是买入持有，red=0% 就是只做多，中间是连续谱。
"""
import sys
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.analyze.ta import supertrend as ST

COST = 0.10


def run(sym, red_w, green_w=1.0, n=1000):
    b = fetch_bars(sym, period='day', count=n)
    c = [x['close'] for x in b]; h = [x['high'] for x in b]; l = [x['low'] for x in b]
    _, _, tr = ST.supertrend(h, l, c)
    eq, peak, mdd, pos = 1.0, 1.0, 0.0, None
    for i in range(1, len(c)):
        if tr[i-1] is None:
            continue
        w = green_w if tr[i-1] == 1 else red_w
        if pos is not None and w != pos:
            eq *= 1 - abs(w - pos) * COST / 100      # 调仓成本按变动幅度计
        pos = w
        eq *= 1 + w * (c[i]/c[i-1] - 1)
        peak = max(peak, eq); mdd = max(mdd, 1 - eq/peak)
    return (eq-1)*100, mdd*100


print('绿色段满仓，红色段按不同权重减仓（red=100% 即买入持有，red=0% 即只做多）\n')
for sym in ('GLD.US', 'SLV.US', 'USO.US', 'UUP.US'):
    print(f'══ {sym} ══')
    print(f'{"红色段仓位":<12}{"总收益":>10}{"最大回撤":>10}{"Calmar":>9}')
    best = None
    for red in (0.0, 0.25, 0.5, 0.75, 1.0):
        r, d = run(sym, red)
        cal = r/d if d > 0 else 0
        tag = ''
        if red == 0.0:
            tag = '  ← 只做多'
        elif red == 1.0:
            tag = '  ← 买入持有'
        if best is None or cal > best[1]:
            best = (red, cal)
        print(f'{red*100:>8.0f}%{"":<4}{r:>+9.1f}%{d:>9.1f}%{cal:>9.2f}{tag}')
    print(f'  Calmar 最优在 red={best[0]*100:.0f}%\n')
