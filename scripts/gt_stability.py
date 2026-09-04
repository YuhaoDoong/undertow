"""子样本稳定性 + 与 Supertrend 同口径直接对比。"""
import sys
sys.path.insert(0, '/Users/yhdong/Trading')
sys.path.insert(0, '/Users/yhdong/Trading/scripts')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.analyze.ta import supertrend as ST
from golden_trident import run, structure_dir

SYMS = {'GLD.US': '黄金', 'SLV.US': '白银', 'USO.US': '原油', 'QQQ.US': '纳指'}
COST = 0.05


def bh(c):
    eq, p, m = 1.0, 1.0, 0.0
    for i in range(1, len(c)):
        eq *= c[i]/c[i-1]; p = max(p, eq); m = max(m, 1-eq/p)
    return (eq-1)*100, m*100


def st_run(o, h, l, c):
    """Supertrend 只做多，逐根权益，同口径。"""
    _, _, tr = ST.supertrend(h, l, c)
    eq, peak, mdd, inpos = 1.0, 1.0, 0.0, False
    for i in range(1, len(c)):
        if inpos:
            eq *= c[i]/c[i-1]
        peak = max(peak, eq); mdd = max(mdd, 1-eq/peak)
        want = tr[i-1] == 1
        if want != inpos:
            eq *= 1 - COST/200
            inpos = want
    return (eq-1)*100, mdd*100


print('══ 子样本稳定性（前 500 根 / 后 500 根）══')
print(f'{"":6}{"":>10}{"前半段":>22}{"后半段":>22}')
print(f'{"":6}{"":>10}{"GT":>10}{"持有":>11}{"GT":>10}{"持有":>11}')
for sym, nm in SYMS.items():
    b = fetch_bars(sym, period='day', count=1000)
    o = [x['open'] for x in b]; h = [x['high'] for x in b]
    l = [x['low'] for x in b]; c = [x['close'] for x in b]
    line = f'{nm:<6}{"":>10}'
    for sl in (slice(0, 500), slice(500, 1000)):
        oo, hh, ll, cc = o[sl], h[sl], l[sl], c[sl]
        r = run(oo, hh, ll, cc, swing=30, weight=1.0)
        bhr, _ = bh(cc)
        line += f'{(r["ret"] if r else 0):>+9.0f}%{bhr:>+10.0f}%'
    print(line)

print('\n══ 同口径三方对比（全 1000 根，只做多，逐根权益，0.05% 往返）══')
print(f'{"":6}{"Golden Trident":>20}{"Supertrend只做多":>20}{"买入持有":>18}')
print(f'{"":6}{"收益":>10}{"Calmar":>10}{"收益":>10}{"Calmar":>10}{"收益":>9}{"Calmar":>9}')
for sym, nm in SYMS.items():
    b = fetch_bars(sym, period='day', count=1000)
    o = [x['open'] for x in b]; h = [x['high'] for x in b]
    l = [x['low'] for x in b]; c = [x['close'] for x in b]
    g = run(o, h, l, c, swing=30, weight=1.0)
    sr, sd = st_run(o, h, l, c)
    br, bd = bh(c)
    gc = g['ret']/g['mdd'] if g and g['mdd'] > 0 else 0
    print(f'{nm:<6}{g["ret"]:>+9.0f}%{gc:>10.1f}{sr:>+9.0f}%{sr/sd if sd else 0:>10.1f}'
          f'{br:>+8.0f}%{br/bd if bd else 0:>9.1f}')
