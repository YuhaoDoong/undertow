"""指标强度回测的统计分析 —— 严守项目三条纪律。

1. 局部去趋势：减掉同期同品种的平均漂移，否则单边行情里"永远看多"也会赢。
2. 按品种抽稀不重叠：同品种相邻样本高度相关，直接当独立样本会把 p 值算得过于乐观。
3. n≥50 且显著才认；达不到就说【样本不足】，不说【无效】。
"""
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROWS = json.loads(Path("data/history/strength_backtest.json").read_text())
KEYS = ("flow", "vol", "price")
MIN_N = 50


def detrend(rows):
    """减掉每个品种自己的样本均值 —— 局部去趋势。"""
    m = defaultdict(list)
    for r in rows:
        m[r["inst"]].append(r["ret"])
    avg = {k: sum(v) / len(v) for k, v in m.items()}
    for r in rows:
        r["adj"] = r["ret"] - avg[r["inst"]]
    return rows


def thin(rows, gap=1):
    """按品种抽稀成不重叠样本。

    ⚠️ gap=1（前瞻 1 日）时【不需要抽稀】：收益窗口 [D−1收盘, D收盘] 与下一个样本
    [D收盘, D+1收盘] 天然不重叠。抽稀是为前瞻 5/10 日那种重叠窗口设计的。
    早先这里无条件抽稀，且用日期末两位算间隔（跨月就断），把 138 个样本砍成 28 个
    —— 白白丢掉八成数据，还让每个结论都卡在"样本不足"。

    ⚠️ 仍存在的问题：跨品种同日高度相关（金银 0.89、QQQ/TQQQ 0.99），
    所以 n 看着够、有效独立样本数远小于它，p 值偏乐观。结论里必须提这一条。
    """
    if gap <= 1:
        return list(rows)
    from datetime import date as _d
    by = defaultdict(list)
    for r in rows:
        by[r["inst"]].append(r)
    out = []
    for k, rs in by.items():
        rs.sort(key=lambda x: x["date"])
        last = None
        for r in rs:
            cur = _d.fromisoformat(r["date"])
            if last is None or (cur - last).days >= gap:
                out.append(r)
                last = cur
    return out


def welch(a, b):
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se else 0.0


def wilson_lo(hit, n, z=1.96):
    if not n:
        return 0.0
    p = hit / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d


rows = detrend(ROWS)
print(f"样本 {len(rows)} 个（{len(set(r['inst'] for r in rows))} 个品种）")
print(f"品种分布：" + "、".join(f"{k}×{v}" for k, v in sorted(
    ((k, sum(1 for r in rows if r['inst'] == k)) for k in set(r['inst'] for r in rows)),
    key=lambda x: -x[1])))
print()

print("=" * 78)
print("① 单指标：方向命中率（去趋势后，收益符号 vs 指标符号）")
print("=" * 78)
print(f"{'指标':<8}{'样本':>6}{'命中':>6}{'命中率':>9}{'Wilson下界':>12}  结论")
print("-" * 78)
for k in KEYS:
    sub = [r for r in rows if r.get(k) is not None and abs(r[k]) > 1e-9]
    sub = thin(sub)
    if not sub:
        print(f"{k:<8}{'无样本':>6}")
        continue
    hit = sum(1 for r in sub if r[k] * r["adj"] > 0)
    n = len(sub)
    lo = wilson_lo(hit, n)
    verdict = ("✅ 可下结论" if (n >= MIN_N and lo > 0.5)
               else f"❌ 样本不足(需 n≥{MIN_N} 且下界>50%)")
    print(f"{k:<8}{n:>6}{hit:>6}{hit/n*100:>8.1f}%{lo*100:>11.1f}%  {verdict}")

print()
print("=" * 78)
print("② 强度是否有价值：高强度组 vs 低强度组（同一指标内部对比）")
print("=" * 78)
print(f"{'指标':<8}{'高强组n':>8}{'命中率':>9}{'低强组n':>8}{'命中率':>9}{'差值':>9}  结论")
print("-" * 78)
for k in KEYS:
    sub = [r for r in rows if r.get(k) is not None and abs(r[k]) > 1e-9]
    sub = thin(sub)
    if len(sub) < 20:
        print(f"{k:<8} 样本 {len(sub)} 个，太少")
        continue
    sub.sort(key=lambda r: abs(r[k]))
    half = len(sub) // 2
    lows, highs = sub[:half], sub[half:]
    hh = sum(1 for r in highs if r[k] * r["adj"] > 0) / len(highs)
    hl = sum(1 for r in lows if r[k] * r["adj"] > 0) / len(lows)
    d = (hh - hl) * 100
    verdict = ("✅ 强度有区分度" if (len(highs) >= MIN_N and d > 10)
               else "❌ 样本不足/无区分")
    print(f"{k:<8}{len(highs):>8}{hh*100:>8.1f}%{len(lows):>8}{hl*100:>8.1f}%{d:>+8.1f}pp  {verdict}")

print()
print("=" * 78)
print("③ 极端读数：|强度| ≥ 0.7 的样本单独看（黄金 8/28 那种）")
print("=" * 78)
for k in KEYS:
    sub = thin([r for r in rows if r.get(k) is not None and abs(r[k]) >= 0.7])
    if not sub:
        print(f"{k:<8} 无极端样本")
        continue
    hit = sum(1 for r in sub if r[k] * r["adj"] > 0)
    n = len(sub)
    mean = sum(r["adj"] * (1 if r[k] > 0 else -1) for r in sub) / n
    print(f"{k:<8} n={n:<4} 命中 {hit}/{n} = {hit/n*100:.0f}%   "
          f"顺信号方向的平均去趋势收益 {mean:+.2f}%   "
          f"{'（n 太小，仅供记录）' if n < MIN_N else ''}")

print()
print("=" * 78)
print("④ 加权综合分 vs 等权（用户要问的：按强度加权是否更好）")
print("=" * 78)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze.strength import GROUP_W  # noqa: E402
both = thin([r for r in rows if all(r.get(k) is not None for k in KEYS)])
if len(both) < 20:
    print(f"三组齐全的样本仅 {len(both)} 个，不足以比较")
else:
    w_hit = e_hit = 0
    for r in both:
        w = sum(r[k] * GROUP_W[k] for k in KEYS)          # 强度加权
        e = sum((1 if r[k] > 0 else -1) * GROUP_W[k] for k in KEYS)  # 只用方向
        w_hit += (w * r["adj"] > 0)
        e_hit += (e * r["adj"] > 0)
    n = len(both)
    print(f"三组齐全样本 n={n}")
    print(f"  按强度加权：命中 {w_hit}/{n} = {w_hit/n*100:.1f}%")
    print(f"  只用方向  ：命中 {e_hit}/{n} = {e_hit/n*100:.1f}%")
    print(f"  差值 {(w_hit-e_hit)/n*100:+.1f}pp   "
          f"{'（n<50，不足以下结论）' if n < MIN_N else ''}")

print()
print("=" * 78)
print("⑤ 日期聚类 bootstrap —— 修正跨品种同日相关（金银 0.89、QQQ/TQQQ 0.99）")
print("=" * 78)
print("同一天所有品种一起重采样：同日样本不是独立的，直接算 Wilson 会高估把握。")
print()
import random
random.seed(20260829)          # 固定种子，结果可复现


def cluster_boot(rows, key, iters=4000):
    by_day = defaultdict(list)
    for r in rows:
        if r.get(key) is not None and abs(r[key]) > 1e-9:
            by_day[r["date"]].append(r)
    days = list(by_day)
    if len(days) < 10:
        return None
    accs = []
    for _ in range(iters):
        pick = [random.choice(days) for _ in days]     # 按【天】有放回重采样
        hit = tot = 0
        for d in pick:
            for r in by_day[d]:
                tot += 1
                hit += (r[key] * r["adj"] > 0)
        if tot:
            accs.append(hit / tot)
    accs.sort()
    return (len(days), accs[int(0.025 * len(accs))], accs[len(accs) // 2],
            accs[int(0.975 * len(accs))])


print(f"{'指标':<8}{'日聚类数':>9}{'命中率中位':>11}{'95% 区间':>20}  结论")
print("-" * 78)
for k in KEYS:
    res = cluster_boot(rows, k)
    if res is None:
        print(f"{k:<8} 天数太少")
        continue
    nd, lo, mid, hi = res
    verdict = "✅ 区间下界 >50%，可下结论" if lo > 0.5 else "❌ 区间跨过 50%，仍不能下结论"
    print(f"{k:<8}{nd:>9}{mid*100:>10.1f}%   [{lo*100:>5.1f}%, {hi*100:>5.1f}%]   {verdict}")

print()
print("⑥ 同样方法检验「强度加权 vs 只用方向」")
print("-" * 78)
by_day = defaultdict(list)
for r in rows:
    if all(r.get(k) is not None for k in KEYS):
        by_day[r["date"]].append(r)
days = list(by_day)
diffs = []
for _ in range(4000):
    pick = [random.choice(days) for _ in days]
    w = e = tot = 0
    for d in pick:
        for r in by_day[d]:
            tot += 1
            w += (sum(r[k] * GROUP_W[k] for k in KEYS) * r["adj"] > 0)
            e += (sum((1 if r[k] > 0 else -1) * GROUP_W[k] for k in KEYS) * r["adj"] > 0)
    if tot:
        diffs.append((w - e) / tot * 100)
diffs.sort()
lo, mid, hi = diffs[100], diffs[len(diffs)//2], diffs[-101]
print(f"日聚类数 {len(days)}　加权减等权的命中率差：中位 {mid:+.1f}pp　"
      f"95% 区间 [{lo:+.1f}pp, {hi:+.1f}pp]")
print("✅ 加权确有增益" if lo > 0 else "❌ 区间跨 0 —— 加权【没有】被证明更好")
