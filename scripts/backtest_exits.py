"""四种出场口径 × 分方向统计。

用户 2026-08-30：
  「开盘进后，如果大幅度盈利，收盘前就出了…同时可以看看看跌期权的点位位置是否达到。
    如果当天没有大跌，也可以看看是否等第二天？然后不只看跌，看涨侧有统计吗」

⚠️ 止盈阈值是【拍的】，所以下面同时跑 0.5/1.0/1.5% 三档，看结论稳不稳。
⚠️ 用标的 ETF 的价格，未计期权点差/时间价值 —— 期权上的实际结果会差不少。
"""
import json, math, pathlib, sys
from collections import defaultdict
sys.path.insert(0, '.')
from undertow.collect.longbridge_kline import fetch_bars
from undertow.core.config import load_config

SIG = json.loads(pathlib.Path('data/history/strong_signal_days.json').read_text())
cfg = load_config()
sym_of = {k: i.options.symbol for k, i in cfg.instruments.items() if i.options}
BARS = {}
for k, sym in sym_of.items():
    try:
        BARS[k] = fetch_bars(f"{sym}.US", period="1h", count=200)
    except Exception:
        pass


def day_of(k, d):
    return [b for b in BARS.get(k, []) if str(b['ts'])[:10] == d]


def days_of(k):
    return sorted({str(b['ts'])[:10] for b in BARS.get(k, [])})


def bp(k, n, p=0.5):
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) * p**i * (1-p)**(n-i)
                            for i in range(k, n + 1)))


def run(tp=None, hold_next=False):
    """tp: 日内止盈阈值(%)，None=不止盈；hold_next: 未止盈时是否持有到次日收盘。"""
    out = []
    for r in SIG:
        k, d = r['inst'], r['date']
        db = day_of(k, d)
        if len(db) < 2:
            continue
        sgn = -1 if r['dir'] == '看跌' else 1
        entry = db[0]['open']
        # 日内是否触及止盈（做空看 low，做多看 high）
        hit = False
        if tp is not None:
            for b in db:
                px = b['low'] if sgn < 0 else b['high']
                if sgn * (px / entry - 1) * 100 >= tp:
                    hit = True
                    break
        if hit:
            pnl = tp                       # 以止盈价成交（乐观：假设能成交在阈值）
            exitd = d + " 盘中"
        else:
            if hold_next:
                nd = [x for x in days_of(k) if x > d]
                if not nd:
                    continue
                nb = day_of(k, nd[0])
                if not nb:
                    continue
                pnl = sgn * (nb[-1]['close'] / entry - 1) * 100
                exitd = nd[0] + " 收盘"
            else:
                pnl = sgn * (db[-1]['close'] / entry - 1) * 100
                exitd = d + " 收盘"
        out.append({**r, 'pnl': pnl, 'tp_hit': hit, 'exit': exitd})
    return out


def report(rows, name):
    if not rows:
        print(f"{name:<34} 无样本")
        return
    n = len(rows)
    w = sum(1 for r in rows if r['pnl'] > 0)
    tot = sum(r['pnl'] for r in rows)
    nhit = sum(1 for r in rows if r['tp_hit'])
    print(f"{name:<34}{n:>4}{w/n*100:>7.0f}%{tot:>+9.2f}%{tot/n:>+8.2f}%"
          f"{bp(w,n):>8.3f}{nhit:>6}")


print("=== 全部信号（看跌+看涨）===")
print(f"{'口径':<34}{'次数':>4}{'胜率':>7}{'累计':>9}{'平均':>8}{'p':>8}{'止盈':>6}")
print("-" * 78)
report(run(None, False), "A 开盘进 → 当日收盘出")
report(run(None, True), "B 开盘进 → 次日收盘出")
for tp in (0.5, 1.0, 1.5):
    report(run(tp, False), f"C 止盈{tp}% 否则当日收盘出")
for tp in (0.5, 1.0, 1.5):
    report(run(tp, True), f"D 止盈{tp}% 否则持有到次日收盘")

for dirn in ('看跌', '看涨'):
    print()
    print(f"=== 只看【{dirn}】===")
    print(f"{'口径':<34}{'次数':>4}{'胜率':>7}{'累计':>9}{'平均':>8}{'p':>8}{'止盈':>6}")
    print("-" * 78)
    report([r for r in run(None, False) if r['dir'] == dirn], "A 开盘进 → 当日收盘出")
    report([r for r in run(None, True) if r['dir'] == dirn], "B 开盘进 → 次日收盘出")
    for tp in (0.5, 1.0):
        report([r for r in run(tp, False) if r['dir'] == dirn],
               f"C 止盈{tp}% 否则当日收盘出")
        report([r for r in run(tp, True) if r['dir'] == dirn],
               f"D 止盈{tp}% 否则持有到次日")
