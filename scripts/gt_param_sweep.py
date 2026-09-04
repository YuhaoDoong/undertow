"""Golden Trident 的参数敏感性 —— 过拟合的第一道检测。

若只有 swing=30 好、邻近值差，就是参数挑出来的，不是策略有效。
"""
import sys
sys.path.insert(0, '/Users/yhdong/Trading')
sys.path.insert(0, '/Users/yhdong/Trading/scripts')
from undertow.collect.longbridge_kline import fetch_bars
from golden_trident import run

SYMS = {'GLD.US': '黄金', 'SLV.US': '白银', 'USO.US': '原油',
        'UUP.US': '美元', 'QQQ.US': '纳指'}
SWINGS = (10, 15, 20, 25, 30, 35, 40, 50, 60)

print('满仓、含 0.05% 往返成本、逐根权益。表内为 净收益% (Calmar)\n')
print(f'{"":6}{"持有":>16}' + ''.join(f'{f"sw={s}":>15}' for s in SWINGS))
for sym, nm in SYMS.items():
    try:
        b = fetch_bars(sym, period='day', count=1000)
    except Exception:
        continue
    o = [x['open'] for x in b]; h = [x['high'] for x in b]
    l = [x['low'] for x in b]; c = [x['close'] for x in b]
    beq, bp, bmdd = 1.0, 1.0, 0.0
    for i in range(1, len(c)):
        beq *= c[i]/c[i-1]; bp = max(bp, beq); bmdd = max(bmdd, 1-beq/bp)
    bh = (beq-1)*100
    row = f'{nm:<6}{bh:>+9.0f}%({bh/(bmdd*100):>4.1f})'
    for s in SWINGS:
        r = run(o, h, l, c, swing=s, weight=1.0)
        if r is None:
            row += f'{"—":>15}'; continue
        cal = r['ret']/r['mdd'] if r['mdd'] > 0 else 0
        row += f'{r["ret"]:>+9.0f}%({cal:>4.1f})'
    print(row)
