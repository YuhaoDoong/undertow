"""统一口径检验所有新指标的方向有效性。

设计要点：
· 信号只用第 i 根及之前的数据算出，测 close[i+k]/close[i] 的方向 —— 不含未来
· 基线不是 50%：样本期有趋势，用该品种该周期**实际的上涨根数占比**当基线
· 状态型（每根都有方向）与事件型（只在触发根有）分开统计，前者样本大得多
· 二项检验给 p 值
"""
import sys, math
sys.path.insert(0, '/Users/yhdong/Trading')
from collections import defaultdict
from undertow.analyze.ta import frames, ema, macd as M, supertrend as ST, ut_bot as UT
from undertow.analyze.ta import dmi as D, entries as E, stoch as S

SYMS = {'GLD.US': '黄金', 'SLV.US': '白银', 'USO.US': '原油', 'UUP.US': '美元'}
TFS = ('1h', '4h', '1d')
HORIZONS = (1, 3, 5)


def binom_p(k, n, p):
    """双尾二项检验。n 上千时精确式的组合数会溢出，用正态近似（带连续性校正）。"""
    if n == 0 or p <= 0 or p >= 1:
        return 1.0
    mu, sd = n * p, math.sqrt(n * p * (1 - p))
    if sd == 0:
        return 1.0
    z = (abs(k - mu) - 0.5) / sd
    if z <= 0:
        return 1.0
    # 双尾：2 × (1 − Φ(z))；Φ 用 erf
    return min(1.0, 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))))


def collect(sym, tf):
    b = frames.bars(sym, tf)
    o = [x['open'] for x in b]; h = [x['high'] for x in b]
    l = [x['low'] for x in b]; c = [x['close'] for x in b]
    sig = {}

    # ── 状态型：每根都有方向 ──
    _, _, tr = ST.supertrend(h, l, c)
    sig['ST趋势'] = [(i, tr[i]) for i in range(len(c)) if tr[i] is not None]

    _, pos = UT.ut_bot(h, l, c)
    sig['UT持仓'] = [(i, pos[i]) for i in range(len(c)) if pos[i]]

    m, sg, hist = M.macd_series(c)
    sig['MACD柱>0'] = [(i, 1 if hist[i] > 0 else -1)
                       for i in range(len(c)) if hist[i] is not None]
    sig['MACD>信号'] = [(i, 1 if m[i] >= sg[i] else -1)
                        for i in range(len(c)) if m[i] is not None and sg[i] is not None]

    dip, dim, adx = D.dmi(h, l, c)
    sig['DI方向'] = [(i, 1 if dip[i] > dim[i] else -1)
                     for i in range(len(c)) if dip[i] is not None and dim[i] is not None]
    reg = E.regime_from_dmi(dip, dim, adx, c, ema(c, 50))
    sig['DMI-regime'] = [(i, reg[i]) for i in range(len(c)) if reg[i]]

    k, dd = S.stoch_kd(c, h, l)
    sig['Stoch-K>50'] = [(i, 1 if k[i] > 50 else -1)
                         for i in range(len(c)) if k[i] is not None]

    # ── 事件型：只在触发根 ──
    sig['ST翻转'] = ST.flips(h, l, c)
    sig['UT翻转'] = UT.flips(h, l, c)
    mc = []
    for i in range(1, len(c)):
        if None in (m[i], sg[i], m[i-1], sg[i-1]):
            continue
        if m[i] > sg[i] and m[i-1] <= sg[i-1]:
            mc.append((i, 1))
        elif m[i] < sg[i] and m[i-1] >= sg[i-1]:
            mc.append((i, -1))
    sig['MACD交叉'] = mc
    pb = E.pullback(c, reg)
    sig['DMI回调入场'] = [(i, pb[i]) for i in range(len(c)) if pb[i]]
    sig['DI交叉'] = [(i, x) for i, x in
                     enumerate(E.di_crossover(dip, dim, reg)) if x]
    return sig, c


# 汇总：按指标 × 周期 × 时间跨度
res = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # [命中, 总数]
base = defaultdict(lambda: [0, 0])
for sym in SYMS:
    for tf in TFS:
        try:
            sig, c = collect(sym, tf)
        except Exception as e:
            print(f'{sym} {tf} 取数失败 {e}', file=sys.stderr); continue
        for k in HORIZONS:
            up = sum(1 for i in range(len(c)-k) if c[i+k] > c[i])
            base[(tf, k)][0] += up
            base[(tf, k)][1] += len(c) - k
        for name, pts in sig.items():
            for k in HORIZONS:
                for i, d in pts:
                    if i + k >= len(c) or d == 0:
                        continue
                    hit = (c[i+k] > c[i]) if d == 1 else (c[i+k] < c[i])
                    res[name][(tf, k)][0] += hit
                    res[name][(tf, k)][1] += 1

print('基线（该周期实际上涨根数占比 —— 不是 50%）')
for (tf, k), (u, n) in sorted(base.items()):
    print(f'  {tf:>3} +{k}根: {u/n*100:>5.1f}%  (n={n})')

ORDER = ['ST趋势','UT持仓','MACD柱>0','MACD>信号','DI方向','DMI-regime','Stoch-K>50',
         'ST翻转','UT翻转','MACD交叉','DMI回调入场','DI交叉']
for kind, names in (('状态型（每根都有方向）', ORDER[:7]), ('事件型（只在触发根）', ORDER[7:])):
    print(f'\n══ {kind} ══')
    print(f"{'指标':<14}{'跨度':<6}{'样本':>6}{'命中':>7}{'基线':>7}{'超额':>7}{'p值':>9}")
    for name in names:
        for k in HORIZONS:
            tot_h = sum(res[name][(tf, k)][0] for tf in TFS)
            tot_n = sum(res[name][(tf, k)][1] for tf in TFS)
            if tot_n < 30:
                continue
            b_u = sum(base[(tf, k)][0] for tf in TFS)
            b_n = sum(base[(tf, k)][1] for tf in TFS)
            bl = b_u / b_n
            acc = tot_h / tot_n
            p = binom_p(tot_h, tot_n, bl)
            star = ' *' if p < 0.05 else ''
            print(f'{name:<14}+{k}根{"":<2}{tot_n:>6}{acc*100:>6.1f}%{bl*100:>6.1f}%'
                  f'{(acc-bl)*100:>+6.1f}{p:>9.3f}{star}')
