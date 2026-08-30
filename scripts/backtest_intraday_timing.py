"""跳空是在哪个时段走出来的 —— 决定 24 小时品种能不能吃到。

我们的快照在 ET 01:00~08:30 捕获（实测多在 02:00~08:00），
而 GLD 要等 ET 09:30 才开盘。这中间期货一直在交易。
"""
import json, math, sys, urllib.request
from datetime import datetime, timezone, timedelta
sys.path.insert(0, '.')
import pathlib

ET = timezone(timedelta(hours=-4))
FUT = {'gold': 'GC=F', 'silver': 'SI=F', 'wti': 'CL=F',
       'qqq': 'NQ=F', 'tqqq': 'NQ=F', 'spy': 'ES=F', 'iwm': 'RTY=F'}


def hourly(sym):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range=3mo&interval=1h")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read())
    r = d["chart"]["result"][0]
    ts, q = r["timestamp"], r["indicators"]["quote"][0]
    out = {}
    for i, t in enumerate(ts):
        dt = datetime.fromtimestamp(t, ET)
        c = q["close"][i]
        if c is None:
            continue
        out.setdefault(dt.strftime('%Y-%m-%d'), {})[dt.hour] = c
    return out


SIG = json.loads(pathlib.Path('data/history/strong_signal_days.json').read_text())
cache = {}
segs = {'ET02→ET08 盘前': [], 'ET08→ET09:30 临开盘': [], 'ET09:30→16:00 美股时段': [],
        'ET02→ET16 全程': []}
detail = []
for r in SIG:
    f = FUT.get(r['inst'])
    if not f:
        continue
    if f not in cache:
        try:
            cache[f] = hourly(f)
        except Exception:
            cache[f] = {}
    h = cache[f].get(r['date'])
    if not h:
        continue
    sgn = -1 if r['dir'] == '看跌' else 1
    need = [2, 8, 9, 16]
    if not all(any(abs(k - n) <= 1 for k in h) for n in need):
        continue

    def at(hr):
        ks = sorted(h, key=lambda k: abs(k - hr))
        return h[ks[0]] if abs(ks[0] - hr) <= 1 else None
    p2, p8, p9, p16 = at(2), at(8), at(9), at(16)
    if None in (p2, p8, p9, p16):
        continue
    a = sgn * (p8 / p2 - 1) * 100
    b = sgn * (p9 / p8 - 1) * 100
    c = sgn * (p16 / p9 - 1) * 100
    d = sgn * (p16 / p2 - 1) * 100
    segs['ET02→ET08 盘前'].append(a)
    segs['ET08→ET09:30 临开盘'].append(b)
    segs['ET09:30→16:00 美股时段'].append(c)
    segs['ET02→ET16 全程'].append(d)
    detail.append((r['date'], r['inst'], r['dir'], a, b, c, d))


def bp(k, n, p=0.5):
    return min(1.0, 2 * sum(math.comb(n, i) * p**i * (1-p)**(n-i)
                            for i in range(k, n + 1)))


print("期货 24 小时数据：信号发出后各时段的顺向收益\n")
print(f"{'时段':<24}{'次数':>5}{'胜率':>8}{'累计':>10}{'平均':>9}{'二项p':>9}")
print("-" * 68)
for nm, v in segs.items():
    if not v:
        continue
    w = sum(1 for x in v if x > 0)
    n = len(v)
    print(f"{nm:<24}{n:>5}{w/n*100:>7.0f}%{sum(v):>+9.2f}%{sum(v)/n:>+8.2f}%{bp(w,n):>9.4f}")
print()
print("逐次明细：")
print(f"{'日期':<12}{'品种':<7}{'方向':<6}{'盘前':>9}{'临开盘':>9}{'美股时段':>10}{'全程':>9}")
print("-" * 66)
for d, k, dr, a, b, c, t in detail:
    print(f"{d:<12}{k:<7}{dr:<6}{a:>+8.2f}%{b:>+8.2f}%{c:>+9.2f}%{t:>+8.2f}%")
