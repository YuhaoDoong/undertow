"""各候选特征能否事前预判「今天会不会大波动」。

判据：把样本按该特征分成高/低两半，看【大波动日占比】差多少。
⚠️ 特征必须按品种内部排序 —— 原油的 IV 天生比黄金高，跨品种直接比就是在比品种。
"""
import json
import math
import random
from collections import defaultdict
from pathlib import Path

ROWS = json.loads(Path("data/history/regime_predict.json").read_text())
FEATS = [("atm_iv", "ATM IV 绝对水平"), ("d_atm", "IV 昨日变化"),
         ("skew25", "25Δ skew 陡峭度"), ("d_skew", "skew 昨日变化"),
         ("add_ratio", "增仓 / 存量 OI"), ("vol_oi", "成交 / 存量 OI"),
         ("d_spot", "昨日已实现波动")]
random.seed(20260829)

# 大波动门槛：各品种自身 |日涨跌| 的 70 分位
by = defaultdict(list)
for r in ROWS:
    by[r["inst"]].append(r["absret"])
thr = {k: sorted(v)[int(0.7 * len(v))] if len(v) >= 10 else 1.5 for k, v in by.items()}
for r in ROWS:
    r["big"] = r["absret"] >= thr[r["inst"]]

nb = sum(1 for r in ROWS if r["big"])
print(f"样本 {len(ROWS)}：大波动 {nb} 个（{nb/len(ROWS)*100:.0f}%）/ 横盘 {len(ROWS)-nb} 个")
print("各品种门槛：" + "、".join(f"{k} {v:.2f}%" for k, v in sorted(thr.items())))
print()
print("=" * 84)
print("各特征的【事前】预判力：按品种内部分位切高/低两半，比大波动日占比")
print("=" * 84)
print(f"{'特征':<18}{'样本':>5}{'高组大波动率':>13}{'低组大波动率':>13}{'差值':>9}{'日聚类95%区间':>20}")
print("-" * 84)


# ⚠️ 分位含未来信息（codex 2026-08-29 P1-5）：
# 下面按【整个回测区间】排序求分位，某一天的分位用到了它之后的数据。
# 目标变量"大波动"的门槛（各品种 |日涨跌| 70 分位）同样取自全区间。
# 因此本脚本的结论是【事后描述性】的，**不是盘前可复现的预测回测**。
# 要做成真回测须改用 expanding/rolling 分位，并先划训练期定门槛再冻结测试。
def rank_within(rows, key):
    """按品种内部把特征转成 0~1 分位 —— 跨品种直接比 IV 就是在比品种。"""
    g = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            g[r["inst"]].append(r)
    out = []
    for k, rs in g.items():
        rs = sorted(rs, key=lambda x: x[key])
        n = len(rs)
        if n < 8:
            continue
        for i, r in enumerate(rs):
            out.append((i / max(n - 1, 1), r))
    return out


for key, name in FEATS:
    pairs = rank_within(ROWS, key)
    if len(pairs) < 40:
        print(f"{name:<18}{len(pairs):>5}  样本不足")
        continue
    hi = [r for q, r in pairs if q >= 0.5]
    lo = [r for q, r in pairs if q < 0.5]
    ph = sum(1 for r in hi if r["big"]) / len(hi)
    pl = sum(1 for r in lo if r["big"]) / len(lo)
    # 日聚类 bootstrap
    by_day = defaultdict(list)
    for q, r in pairs:
        by_day[r["date"]].append((q, r))
    days = list(by_day)
    diffs = []
    for _ in range(3000):
        pick = [random.choice(days) for _ in days]
        H = L = hb = lb = 0
        for d in pick:
            for q, r in by_day[d]:
                if q >= 0.5:
                    H += 1
                    hb += r["big"]
                else:
                    L += 1
                    lb += r["big"]
        if H and L:
            diffs.append((hb / H - lb / L) * 100)
    diffs.sort()
    ci = f"[{diffs[75]:+.0f}, {diffs[-76]:+.0f}]pp" if diffs else "—"
    star = " ✅" if diffs and diffs[75] > 0 else ""
    print(f"{name:<18}{len(pairs):>5}{ph*100:>12.0f}%{pl*100:>12.0f}%"
          f"{(ph-pl)*100:>+8.0f}pp{ci:>20}{star}")

print()
print("=" * 84)
print("组合：IV 水平高 且 增仓放量 —— 「有人在提前布局」的直觉写成条件")
print("=" * 84)
iv = dict(rank_within(ROWS, "atm_iv"))
ad = dict(rank_within(ROWS, "add_ratio"))
iv_q = {id(r): q for q, r in rank_within(ROWS, "atm_iv")}
ad_q = {id(r): q for q, r in rank_within(ROWS, "add_ratio")}
both = [r for r in ROWS if id(r) in iv_q and id(r) in ad_q]
if len(both) >= 40:
    sel = [r for r in both if iv_q[id(r)] >= 0.6 and ad_q[id(r)] >= 0.6]
    rest = [r for r in both if r not in sel]
    if sel and rest:
        ps = sum(1 for r in sel if r["big"]) / len(sel)
        pr = sum(1 for r in rest if r["big"]) / len(rest)
        print(f"  IV 与增仓都在自身前 40% 的日子：{len(sel)} 个，大波动率 {ps*100:.0f}%")
        print(f"  其余日子：{len(rest)} 个，大波动率 {pr*100:.0f}%")
        print(f"  差 {(ps-pr)*100:+.0f}pp　{'（样本 <50，仅供记录）' if len(sel) < 50 else ''}")
