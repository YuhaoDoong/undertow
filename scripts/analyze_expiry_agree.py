"""到期桶方向一致性 → 加总读数是否可信（用户 2026-08-29 追问引出）。

假设：pressure 是 45 天内所有到期加总的。若各到期桶方向一致，加总代表全曲线共识；
若打架，加总只是把矛盾抹平，方向读数应当不可信。

⚠️ 关键：**桶是否同向在 D 开盘前就能算出来**，不需要知道当天涨跌 ——
   所以这不是事后分层，是可执行的闸门。这一点与「大波动日 vs 横盘日」不同。
"""
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.flow import (analyze_flow, expiry_split,          # noqa: E402
                                   expiry_split_conflict)
from undertow.cli import snapshot_from_payload                          # noqa: E402
from undertow.collect.longbridge_kline import fetch_series              # noqa: E402
from undertow.collect.store import SnapshotStore                        # noqa: E402
from undertow.core.config import load_config                            # noqa: E402


def _prev_wd(d):
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def binom_p(k, n, p=0.5):
    return min(1.0, 2 * sum(math.comb(n, i) * p**i * (1 - p)**(n - i)
                            for i in range(k, n + 1)))


def wilson_lo(k, n, z=1.96):
    if not n:
        return 0.0
    ph = k / n
    d = 1 + z * z / n
    return (ph + z*z/(2*n) - z*math.sqrt(ph*(1-ph)/n + z*z/(4*n*n))) / d


def rolling_adj(rows, window=60, min_past=20):
    """局部去趋势：减掉【信号日之前】window 天的滚动漂移，窗口不足则剔除。

    ⚠️ codex 2026-08-29 P1-5：用整个区间的品种均值去趋势，等于让当天的调整量
    用到它之后的收益 —— 既是 lookahead，也把不同 regime 混在一起。
    **不补零**：补零等于假装漂移为 0，会把趋势期的样本悄悄算成"中性"。
    """
    from collections import defaultdict
    import sys as _s
    by = defaultdict(list)
    for r in rows:
        by[r["inst"]].append(r)
    out, dropped = [], 0
    for k, rs in by.items():
        rs.sort(key=lambda x: x["date"])
        for i, r in enumerate(rs):
            past = rs[max(0, i - window):i]
            if len(past) < min_past:
                dropped += 1
                continue
            r["adj"] = r["ret"] - sum(x["ret"] for x in past) / len(past)
            out.append(r)
    if dropped:
        print(f"[去趋势] 剔除 {dropped} 个前置窗口不足 {min_past} 天的样本（不补零）",
              file=_s.stderr)
    return out


cfg, store = load_config(), SnapshotStore()
rows = []
for key, inst in cfg.instruments.items():
    if inst.options is None:
        continue
    sym = inst.options.symbol
    ds = store.dates("options", sym)
    if len(ds) < 2:
        continue
    try:
        ser = fetch_series(f"{sym}.US", period="day", count=400)
    except Exception:
        continue
    px = {str(d): c for d, c in zip(ser.dates, ser.closes)}
    keys = sorted(px)
    for i in range(1, len(ds)):
        dp, dc = ds[i-1], ds[i]
        cs = str(dc)
        if cs not in px:
            continue
        j = keys.index(cs)
        if j == 0:
            continue
        ret = (px[cs] / px[keys[j-1]] - 1) * 100
        pp, cp = store.load("options", sym, dp), store.load("options", sym, dc)
        if pp is None or cp is None:
            continue
        try:
            fa = analyze_flow(snapshot_from_payload(pp, key, sym),
                              snapshot_from_payload(cp, key, sym),
                              today=_prev_wd(dc), prev_date=str(dp), curr_date=cs,
                              horizon_days=45)
        except Exception:
            continue
        sp = expiry_split(fa)
        if not sp:
            continue
        up, dn = fa.upside_pressure, fa.downside_pressure
        if not (up or dn):
            continue
        rows.append({"inst": key, "date": cs, "ret": ret,
                     "sign": 1 if up > dn else -1,
                     "agree": not expiry_split_conflict(sp),
                     "buckets": len(sp)})

rows = rolling_adj(rows)

n_agree = sum(1 for r in rows if r["agree"])
print(f"样本 {len(rows)}：各到期桶【同向】 {n_agree} 个 / 【打架】 {len(rows)-n_agree} 个")
print(f"（同向占比 {n_agree/len(rows)*100:.0f}% —— 闸门在盘前可算，不是事后分层）\n")

print("=" * 76)
print("全样本：桶同向 vs 桶打架（跨品种合并，须看日聚类）")
print("=" * 76)
print(f"{'分组':<10}{'样本':>6}{'命中':>6}{'命中率':>9}{'Wilson下界':>12}")
print("-" * 76)
for tag, sel in (("桶同向", True), ("桶打架", False)):
    sub = [r for r in rows if r["agree"] is sel]
    if not sub:
        continue
    hit = sum(1 for r in sub if r["sign"] * r["adj"] > 0)
    print(f"{tag:<10}{len(sub):>6}{hit:>6}{hit/len(sub)*100:>8.1f}%"
          f"{wilson_lo(hit, len(sub))*100:>11.1f}%")

print()
print("=" * 76)
print("单品种时序（同品种不同日不重叠，二项检验适用）")
print("=" * 76)
print(f"{'品种':<8}{'分组':<8}{'样本':>5}{'命中':>5}{'命中率':>8}{'二项p':>9}{'下界':>8}")
print("-" * 76)
for k in sorted({r["inst"] for r in rows}):
    for tag, sel in (("同向", True), ("打架", False)):
        sub = [r for r in rows if r["inst"] == k and r["agree"] is sel]
        if len(sub) < 8:
            continue
        hit = sum(1 for r in sub if r["sign"] * r["adj"] > 0)
        n = len(sub)
        print(f"{k:<8}{tag:<8}{n:>5}{hit:>5}{hit/n*100:>7.0f}%"
              f"{binom_p(hit, n):>9.4f}{wilson_lo(hit, n)*100:>7.1f}%")

print()
print("=" * 76)
print("日聚类 bootstrap（跨品种合并必须做 —— 同日样本不独立）")
print("=" * 76)
import random
random.seed(20260829)


def boot(sel, iters=4000):
    by_day = defaultdict(list)
    for r in rows:
        if r["agree"] is sel:
            by_day[r["date"]].append(r)
    days = list(by_day)
    if len(days) < 8:
        return None
    accs = []
    for _ in range(iters):
        pick = [random.choice(days) for _ in days]
        hit = tot = 0
        for d in pick:
            for r in by_day[d]:
                tot += 1
                hit += (r["sign"] * r["adj"] > 0)
        if tot:
            accs.append(hit / tot)
    accs.sort()
    return len(days), accs[int(.025*len(accs))], accs[len(accs)//2], accs[int(.975*len(accs))]


for tag, sel in (("桶同向", True), ("桶打架", False)):
    res = boot(sel)
    if not res:
        print(f"{tag}: 天数不足")
        continue
    nd, lo, mid, hi = res
    print(f"{tag}：{nd} 个日聚类　命中率中位 {mid*100:.1f}%　"
          f"95% 区间 [{lo*100:.1f}%, {hi*100:.1f}%]　"
          f"{'✅ 下界>50%' if lo > .5 else '❌ 跨 50%'}")

# 差值的区间
by_day_a, by_day_c = defaultdict(list), defaultdict(list)
for r in rows:
    (by_day_a if r["agree"] else by_day_c)[r["date"]].append(r)
days = sorted(set(by_day_a) | set(by_day_c))
diffs = []
for _ in range(4000):
    pick = [random.choice(days) for _ in days]
    A = C = ah = ch = 0
    for d in pick:
        for r in by_day_a.get(d, []):
            A += 1
            ah += (r["sign"] * r["adj"] > 0)
        for r in by_day_c.get(d, []):
            C += 1
            ch += (r["sign"] * r["adj"] > 0)
    if A and C:
        diffs.append((ah/A - ch/C) * 100)
diffs.sort()
print(f"\n同向减打架的命中率差：中位 {diffs[len(diffs)//2]:+.1f}pp　"
      f"95% 区间 [{diffs[100]:+.1f}pp, {diffs[-101]:+.1f}pp]")
print("✅ 区间不跨 0 —— 同向确实更准" if diffs[100] > 0
      else "❌ 区间跨 0 —— 样本不足以下结论")
print()
print("⚠️ 表述纪律（codex 2026-08-29 P1-6）：")
print("   ✅ 可以说：这个分层【盘前可算】，不同于「大波动日 vs 横盘日」那种事后分层。")
print("   ❌ 不可以说：「打架时应降低置信度 / 转铁鹰」—— 增量价值区间跨 0，未证实。")
print("   在新样本验证前，它只是【描述性分层】，不构成任何策略切换建议。")
