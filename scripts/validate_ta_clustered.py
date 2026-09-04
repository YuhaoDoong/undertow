"""带聚类校正的有效性检验。

⚠️ 上一版的 p 值不可信：状态型指标每根算一个样本，而相邻根的状态几乎相同
（Supertrend 一段趋势能连着几十根），样本高度自相关，有效样本量远小于名义值，
p 值会虚低、制造假显著。这里按**连续同向段**聚类，段内取平均命中率，
再对段做 bootstrap —— 与我们做闸门检验时的 35-cluster bootstrap 同法。
"""
import sys, math, random
sys.path.insert(0, '/Users/yhdong/Trading')
from collections import defaultdict
from undertow.analyze.ta import frames, ema, macd as M, supertrend as ST, ut_bot as UT
from undertow.analyze.ta import dmi as D, entries as E, stoch as S

SYMS = ('GLD.US', 'SLV.US', 'USO.US', 'UUP.US')
TFS = ('1h', '4h', '1d')
K = 3                      # 时间跨度：+3 根
random.seed(20260904)


def signals(sym, tf):
    b = frames.bars(sym, tf)
    h = [x['high'] for x in b]; l = [x['low'] for x in b]; c = [x['close'] for x in b]
    out = {}
    _, _, tr = ST.supertrend(h, l, c)
    out['ST趋势'] = tr
    _, pos = UT.ut_bot(h, l, c)
    out['UT持仓'] = pos
    m, sg, hist = M.macd_series(c)
    out['MACD柱>0'] = [None if v is None else (1 if v > 0 else -1) for v in hist]
    dip, dim, adx = D.dmi(h, l, c)
    out['DI方向'] = [None if (dip[i] is None or dim[i] is None) else
                     (1 if dip[i] > dim[i] else -1) for i in range(len(c))]
    out['DMI-regime'] = [x or None for x in
                         E.regime_from_dmi(dip, dim, adx, c, ema(c, 50))]
    k, _ = S.stoch_kd(c, h, l)
    out['Stoch-K>50'] = [None if v is None else (1 if v > 50 else -1) for v in k]
    return out, c


def segments(states, closes, k=K):
    """把连续同向的根切成段；每段返回 (命中数, 总数)。"""
    segs, cur, prev = [], [], None
    for i, d in enumerate(states):
        if d is None or d == 0:
            if cur:
                segs.append(cur); cur = []
            prev = None
            continue
        if d != prev and cur:
            segs.append(cur); cur = []
        prev = d
        if i + k < len(closes):
            hit = (closes[i+k] > closes[i]) if d == 1 else (closes[i+k] < closes[i])
            cur.append(1 if hit else 0)
    if cur:
        segs.append(cur)
    return [(sum(s), len(s)) for s in segs if s]


def boot_ci(segs, n=4000):
    """段级 bootstrap 的命中率 95% CI。"""
    if len(segs) < 3:
        return None
    out = []
    for _ in range(n):
        pick = [segs[random.randrange(len(segs))] for _ in segs]
        h = sum(x[0] for x in pick); t = sum(x[1] for x in pick)
        if t:
            out.append(h / t)
    out.sort()
    return out[int(.025 * len(out))], out[int(.975 * len(out))]


allsegs = defaultdict(list)
base_h = base_n = 0
for sym in SYMS:
    for tf in TFS:
        try:
            sig, c = signals(sym, tf)
        except Exception:
            continue
        base_h += sum(1 for i in range(len(c)-K) if c[i+K] > c[i])
        base_n += len(c) - K
        for name, st in sig.items():
            allsegs[name] += segments(st, c)

bl = base_h / base_n
print(f'基线（一直做多的命中率，+{K}根）: {bl*100:.1f}%   n={base_n}\n')
print(f"{'指标':<12}{'段数':>5}{'名义样本':>9}{'命中':>7}{'基线':>7}{'超额':>8}   "
      f"{'段级 bootstrap 95%CI':<24}结论")
for name, segs in allsegs.items():
    h = sum(x[0] for x in segs); n = sum(x[1] for x in segs)
    if not n:
        continue
    acc = h / n
    ci = boot_ci(segs)
    if ci is None:
        verd = '段数不足'
        cis = '—'
    else:
        cis = f'[{ci[0]*100:>5.1f}%, {ci[1]*100:>5.1f}%]'
        if ci[1] < bl:
            verd = '⛔ 显著劣于一直做多'
        elif ci[0] > bl:
            verd = '✅ 显著优于'
        else:
            verd = '— 与基线无区别'
    print(f'{name:<12}{len(segs):>5}{n:>9}{acc*100:>6.1f}%{bl*100:>6.1f}%'
          f'{(acc-bl)*100:>+7.1f}   {cis:<24}{verd}')
