"""按行情类型分层的指标回测。

用户 2026-08-29：「回测你也要区别一下样本的，横盘的日期和大涨大跌的日期要区分。
横盘的时候，指标乱是正常的。」—— 这个方法论改进是对的，之前的回测把两者混在一起。

⚠️ 必须先划清一条界限：
   按【当日实际涨跌幅】分层，用的是事后才知道的信息。所以本文件的结论分两类：
     A. 描述性（可以说）：「指标在大波动日更准」——回答"它到底有没有信息"。
     B. 可执行（不能直接说）：「只在大波动日用它」——事前并不知道哪天会大波动。
   要把 A 变成 B，必须找到【事前可知】的代理来预判波动，见文末第 ④ 节。
"""
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROWS = json.loads(Path("data/history/strength_backtest.json").read_text())
KEYS = ("flow", "vol", "price")
random.seed(20260829)


def detrend(rows):
    m = defaultdict(list)
    for r in rows:
        m[r["inst"]].append(r["ret"])
    avg = {k: sum(v) / len(v) for k, v in m.items()}
    for r in rows:
        r["adj"] = r["ret"] - avg[r["inst"]]
    return rows


def cluster_boot(rows, key, iters=4000):
    """日聚类 bootstrap：同一天所有品种一起重采样。"""
    by_day = defaultdict(list)
    for r in rows:
        if r.get(key) is not None and abs(r[key]) > 1e-9:
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
                hit += (r[key] * r["adj"] > 0)
        if tot:
            accs.append(hit / tot)
    accs.sort()
    n = sum(len(v) for v in by_day.values())
    return (len(days), n, accs[int(0.025 * len(accs))],
            accs[len(accs) // 2], accs[int(0.975 * len(accs))])


rows = detrend(ROWS)

# 波动门槛：按【每个品种自己的】日波动分布定，不用统一的 1.5%
#   —— 原油日常波动就比黄金大，一刀切会把原油的横盘算成大波动。
by_inst = defaultdict(list)
for r in rows:
    by_inst[r["inst"]].append(abs(r["ret"]))
thr = {}
for k, v in by_inst.items():
    v = sorted(v)
    thr[k] = v[int(0.7 * len(v))] if len(v) >= 10 else 1.5   # 各品种自身的 70 分位
for r in rows:
    r["big"] = abs(r["ret"]) >= thr[r["inst"]]

print("各品种「大波动」门槛（自身 |日涨跌| 的 70 分位）：")
print("  " + "、".join(f"{k} {v:.2f}%" for k, v in sorted(thr.items())))
nb = sum(1 for r in rows if r["big"])
print(f"\n样本 {len(rows)}：大波动 {nb} 个 / 横盘 {len(rows)-nb} 个\n")

print("=" * 80)
print("① 分层命中率（日聚类 bootstrap 95% 区间）")
print("=" * 80)
print(f"{'指标':<7}{'分层':<8}{'日数':>5}{'样本':>6}{'命中率':>9}{'95% 区间':>20}  结论")
print("-" * 80)
for k in KEYS:
    for tag, sub in (("大波动", [r for r in rows if r["big"]]),
                     ("横盘", [r for r in rows if not r["big"]])):
        res = cluster_boot(sub, k)
        if res is None:
            print(f"{k:<7}{tag:<8} 天数不足")
            continue
        nd, n, lo, mid, hi = res
        v = "✅ 站得住" if lo > 0.5 else ("⚠️ 贴边" if hi > 0.5 and mid > 0.5 else "❌ 不成立")
        print(f"{k:<7}{tag:<8}{nd:>5}{n:>6}{mid*100:>8.1f}%   "
              f"[{lo*100:>5.1f}%, {hi*100:>5.1f}%]   {v}")
    print()

print("=" * 80)
print("② 黄金单独看（样本最多、用户点名的三个节点都在里面）")
print("=" * 80)
g = [r for r in rows if r["inst"] == "gold"]
for tag, sub in (("大波动", [r for r in g if r["big"]]), ("横盘", [r for r in g if not r["big"]])):
    s = [r for r in sub if r.get("flow") is not None and abs(r["flow"]) > 1e-9]
    if not s:
        continue
    hit = sum(1 for r in s if r["flow"] * r["adj"] > 0)
    print(f"  增仓 · {tag}：{hit}/{len(s)} = {hit/len(s)*100:.0f}%")
print()
print("  用户点名的节点：")
for d in ("2026-08-19", "2026-08-28"):
    r = next((x for x in g if x["date"] == d), None)
    if r:
        ok = "✅" if r["flow"] * r["adj"] > 0 else "❌"
        print(f"    {d}  实际 {r['ret']:+.2f}%  增仓 {r['flow']:+.2f}"
              f"（{r['flow_raw']:.1f}×） {ok}")
print("    2026-06 大跌：快照最早 2026-06-25，该节点在数据起点之前，无法回测")

print()
print("=" * 80)
print("③ 大波动日里，强度是否终于有区分度了？")
print("=" * 80)
big = [r for r in rows if r["big"] and r.get("flow") is not None and abs(r["flow"]) > 1e-9]
big.sort(key=lambda r: abs(r["flow"]))
h = len(big) // 2
lows, highs = big[:h], big[h:]
hh = sum(1 for r in highs if r["flow"] * r["adj"] > 0) / max(len(highs), 1)
hl = sum(1 for r in lows if r["flow"] * r["adj"] > 0) / max(len(lows), 1)
print(f"  大波动日内：高强度组 {len(highs)} 个 命中 {hh*100:.1f}%　"
      f"低强度组 {len(lows)} 个 命中 {hl*100:.1f}%　差 {(hh-hl)*100:+.1f}pp")
print(f"  {'（样本仍不足以下结论）' if len(highs) < 50 else ''}")

print()
print("=" * 80)
print("④ 能否【事前】预判今天是不是大波动日？—— 这一步不成，上面的结论就不可执行")
print("=" * 80)
print("候选代理（都必须是当日开盘前已知的）：")
print("  a. 前一交易日的 |涨跌|（波动聚集性）")
print("  b. 当日是否有 🔴 高影响事件（事件日历，前一天就知道）")
print()
# a. 前日波动 → 今日波动
by_i = defaultdict(list)
for r in rows:
    by_i[r["inst"]].append(r)
pairs = []
for k, rs in by_i.items():
    rs.sort(key=lambda x: x["date"])
    for i in range(1, len(rs)):
        pairs.append((abs(rs[i - 1]["ret"]), rs[i]["big"]))
if len(pairs) >= 30:
    pairs.sort(key=lambda x: -x[0])
    top = pairs[:len(pairs) // 3]
    bot = pairs[-len(pairs) // 3:]
    pt = sum(1 for _, b in top if b) / len(top)
    pb = sum(1 for _, b in bot if b) / len(bot)
    print(f"  a. 前日波动最大的三分之一 → 今日为大波动日的比例 {pt*100:.0f}%")
    print(f"     前日波动最小的三分之一 → {pb*100:.0f}%　差 {(pt-pb)*100:+.0f}pp")
    print(f"     {'✅ 有一定预判力' if pt-pb > 0.15 else '❌ 预判力弱，不足以据此择时'}")
