"""TA 指标的方向增量检验 —— 配对差值 + moving-block bootstrap。

2026-09-04 codex review 推翻了前两版方法（P0-1/P0-2/P0-3），这一版按它的意见重做。

前两版错在哪
------------
① 跨周期汇总：+3 根在 1h 是 3 小时、在 1d 是 3 个交易日，检验对象根本不一致；
   4h 还是从 1h 聚合来的，两者高度重叠。按品种拆了仍然不够，必须**按 (品种,周期)
   分别报告**，不做跨周期汇总。
② 基线当成无误差常数：它同样是这批数据估出来的随机量，且与指标的可用观测集合
   不完全重合（指标有 warm-up）。
③ 段级 bootstrap 把内生形成、相邻仍相关的趋势段当成 iid 簇；而 close[i+k]/close[i]
   的窗口本身跨越段边界，相邻段共享未来价格。

这一版的做法
------------
在**每个完全相同的可用时点**算配对差值

    d_i = 1[指标方向命中] − 1[一直做多命中]

对按时间排列的 d 序列做 moving-block bootstrap（块长 ≥ horizon，另做敏感性）。
零假设 E[d] = 0，即「指标不比同期一直做多更准」。

注意 d 只在**指标做空**时非零 —— 做多时两者一模一样。所以这个检验实质是在问：
指标的做空段，是不是比一直持有更好。

⚠️ 这个检验回答的是**业务问题**（能否战胜趋势基准），不是「指标是否含方向信息」。
一个与未来完全独立、一半做多一半做空的指标，在上涨率 54% 的市场里预期命中率
约 50%，本来就会输给一直做多 —— 输了不等于有负预测力。信息检验见 block_permute()。
"""
import sys, math, random
sys.path.insert(0, '/Users/yhdong/Trading')
from undertow.analyze.ta import frames, ema, macd as M, supertrend as ST, ut_bot as UT
from undertow.analyze.ta import dmi as D, entries as E, stoch as S

SYMS = {'GLD.US': '黄金', 'SLV.US': '白银', 'USO.US': '原油', 'UUP.US': '美元'}
TFS = ('1h', '4h', '1d')
NAMES = ['ST趋势', 'UT持仓', 'MACD柱>0', 'DI方向', 'Stoch-K>50']
random.seed(20260904)


def states(sym, tf):
    b = frames.bars(sym, tf)
    h = [x['high'] for x in b]; l = [x['low'] for x in b]; c = [x['close'] for x in b]
    out = {}
    _, _, tr = ST.supertrend(h, l, c); out['ST趋势'] = tr
    _, pos = UT.ut_bot(h, l, c); out['UT持仓'] = pos
    _, _, hist = M.macd_series(c)
    out['MACD柱>0'] = [None if v is None else (1 if v > 0 else -1) for v in hist]
    dip, dim, adx = D.dmi(h, l, c)
    out['DI方向'] = [None if (dip[i] is None or dim[i] is None) else
                     (1 if dip[i] > dim[i] else -1) for i in range(len(c))]
    k, _ = S.stoch_kd(c, h, l)
    out['Stoch-K>50'] = [None if v is None else (1 if v > 50 else -1) for v in k]
    return out, c


def paired(st, closes, k):
    """配对差值序列。只在指标做空时非零 —— 做多时与一直做多完全相同。"""
    d = []
    for i in range(len(closes) - k):
        s = st[i]
        if s is None or s == 0:
            continue
        up = closes[i + k] > closes[i]
        d.append(int(up if s == 1 else not up) - int(up))
    return d


def mbb_ci(d, block, n=4000):
    """moving-block bootstrap 的均值 95% CI。块长保留块内自相关。"""
    L = len(d)
    if L < block * 3:
        return None
    nb = math.ceil(L / block)
    out = []
    for _ in range(n):
        s = []
        for _ in range(nb):
            st = random.randrange(L - block + 1)
            s.extend(d[st:st + block])
        s = s[:L]
        out.append(sum(s) / len(s))
    out.sort()
    return out[int(.025 * n)], out[int(.975 * n)]


def block_permute(st, closes, k, block=20, n=2000):
    """信息检验：保持信号的段结构，把它整块平移到别处对齐未来收益。

    若指标含方向信息，真实排列的命中率应显著高于置换分布。
    这回答的是「有没有信息」，与「能否战胜一直做多」是两个问题。
    """
    idx = [i for i in range(len(closes) - k) if st[i] not in (None, 0)]
    if len(idx) < block * 3:
        return None
    real = sum(int((closes[i+k] > closes[i]) if st[i] == 1 else
                   (closes[i+k] <= closes[i])) for i in idx) / len(idx)
    L = len(closes) - k
    null = []
    for _ in range(n):
        shift = random.randrange(1, L)
        hit = sum(int((closes[j+k] > closes[j]) if st[i] == 1 else
                      (closes[j+k] <= closes[j]))
                  for i in idx for j in [(i + shift) % L])
        null.append(hit / len(idx))
    null.sort()
    p = sum(1 for x in null if x >= real) / n
    return real, null[int(.95 * n)], p


def main():
    K = {'1h': 3, '4h': 3, '1d': 3}
    print('配对差值 = 指标命中 − 同期一直做多命中；moving-block bootstrap，块长 = max(4×horizon, 20)')
    print('⚠️ 不做跨周期汇总 —— +3根在 1h 是 3 小时、在 1d 是 3 天，不是一回事\n')
    print(f"{'品种':<5}{'周期':<5}{'指标':<12}{'n':>6}{'配对差':>8}   {'95%CI':<20}结论")
    tally = {}
    for sym, nm in SYMS.items():
        for tf in TFS:
            try:
                sig, c = states(sym, tf)
            except Exception:
                continue
            k = K[tf]; block = max(4 * k, 20)
            for name in NAMES:
                d = paired(sig[name], c, k)
                if not d:
                    continue
                mean = sum(d) / len(d)
                ci = mbb_ci(d, block)
                if ci is None:
                    v = '样本不足'; cis = '—'
                else:
                    cis = f'[{ci[0]*100:>+6.2f}, {ci[1]*100:>+6.2f}]'
                    v = ('⛔ 劣于一直做多' if ci[1] < 0 else
                         '✅ 优于' if ci[0] > 0 else '— 无区别')
                tally[v] = tally.get(v, 0) + 1
                print(f'{nm:<5}{tf:<5}{name:<12}{len(d):>6}{mean*100:>+7.2f}%   {cis:<20}{v}')
        print()
    print('汇总:', tally)


if __name__ == '__main__':
    main()
