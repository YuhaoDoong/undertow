"""按品种拆开重做 —— 检查合并样本是否掩盖了跨序列相关性。

上一版把四品种×三周期的段混在一起 bootstrap，但金银高度相关、
1h/4h/1d 是同一段行情的不同切片，有效独立单元远少于名义段数。
"""
import sys, math, random
sys.path.insert(0, '/Users/yhdong/Trading')
from collections import defaultdict
from undertow.analyze.ta import frames, ema, macd as M, supertrend as ST, ut_bot as UT
from undertow.analyze.ta import dmi as D, entries as E, stoch as S

SYMS = {'GLD.US': '黄金', 'SLV.US': '白银', 'USO.US': '原油', 'UUP.US': '美元'}
TFS = ('1h', '4h', '1d')
K = 3
random.seed(20260904)


def signals(sym, tf):
    b = frames.bars(sym, tf)
    h = [x['high'] for x in b]; l = [x['low'] for x in b]; c = [x['close'] for x in b]
    out = {}
    _, _, tr = ST.supertrend(h, l, c); out['ST趋势'] = tr
    _, pos = UT.ut_bot(h, l, c); out['UT持仓'] = pos
    m, sg, hist = M.macd_series(c)
    out['MACD柱>0'] = [None if v is None else (1 if v > 0 else -1) for v in hist]
    dip, dim, adx = D.dmi(h, l, c)
    out['DI方向'] = [None if (dip[i] is None or dim[i] is None) else
                     (1 if dip[i] > dim[i] else -1) for i in range(len(c))]
    k, _ = S.stoch_kd(c, h, l)
    out['Stoch-K>50'] = [None if v is None else (1 if v > 50 else -1) for v in k]
    return out, c


def segments(states, closes, k=K):
    segs, cur, prev = [], [], None
    for i, d in enumerate(states):
        if d is None or d == 0:
            if cur: segs.append(cur); cur = []
            prev = None; continue
        if d != prev and cur:
            segs.append(cur); cur = []
        prev = d
        if i + k < len(closes):
            hit = (closes[i+k] > closes[i]) if d == 1 else (closes[i+k] < closes[i])
            cur.append(1 if hit else 0)
    if cur: segs.append(cur)
    return [(sum(s), len(s)) for s in segs if s]


def boot_ci(segs, n=4000):
    if len(segs) < 5: return None
    out = []
    for _ in range(n):
        pick = [segs[random.randrange(len(segs))] for _ in segs]
        h = sum(x[0] for x in pick); t = sum(x[1] for x in pick)
        if t: out.append(h / t)
    out.sort()
    return out[int(.025*len(out))], out[int(.975*len(out))]


NAMES = ['ST趋势', 'UT持仓', 'MACD柱>0', 'DI方向', 'Stoch-K>50']
per = defaultdict(lambda: defaultdict(list))   # [指标][品种] -> segs
basel = {}
for sym, nm in SYMS.items():
    bh = bn = 0
    for tf in TFS:
        try:
            sig, c = signals(sym, tf)
        except Exception:
            continue
        bh += sum(1 for i in range(len(c)-K) if c[i+K] > c[i]); bn += len(c) - K
        for name in NAMES:
            per[name][nm] += segments(sig[name], c)
    basel[nm] = bh / bn if bn else 0.5

print('各品种基线（一直做多，+3根）:', '  '.join(f'{k} {v*100:.1f}%' for k, v in basel.items()))
print()
print(f"{'指标':<12}{'品种':<6}{'段数':>5}{'命中':>7}{'基线':>7}{'超额':>7}  {'95%CI':<20}结论")
verd_count = defaultdict(int)
for name in NAMES:
    for nm in SYMS.values():
        segs = per[name][nm]
        if not segs: continue
        h = sum(x[0] for x in segs); n = sum(x[1] for x in segs)
        if not n: continue
        acc = h/n; bl = basel[nm]; ci = boot_ci(segs)
        if ci is None:
            v = '段数不足'; cis = '—'
        else:
            cis = f'[{ci[0]*100:>5.1f}, {ci[1]*100:>5.1f}]'
            v = ('⛔ 劣于' if ci[1] < bl else '✅ 优于' if ci[0] > bl else '— 无区别')
        verd_count[v] += 1
        print(f'{name:<12}{nm:<6}{len(segs):>5}{acc*100:>6.1f}%{bl*100:>6.1f}%'
              f'{(acc-bl)*100:>+6.1f}  {cis:<20}{v}')
    print()
print('汇总:', dict(verd_count))
