"""波动率压缩 → 爆发？（用户 2026-08-29 提出）

「技术分析里的三角形形态，波动率压缩，震荡越来越小，趋向三角形后，
可能会产生波动巨大的行情。」

⚠️ 这是对我上一轮结论的方向性纠正：
   我测出「ATM IV 高 → 次日大波动率 23%，IV 低 → 40%」，当成"预判失败"记下了。
   实际它正是压缩效应本身 —— 不是 IV 高预示爆发，是 IV 低（被压到极致）预示爆发。
   同一份数字，读反了方向就成了相反的结论。

三个压缩维度（全部为 D 开盘前已知）：
  1. 期权端：ATM IV 处于自身历史低位
  2. 价格端：近 N 日真实波幅收缩（ATR 压缩）
  3. 形态端：近 N 日高低点区间收敛（三角形）
"""
import json
import random
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.cli import snapshot_from_payload          # noqa: E402
from undertow.collect.longbridge_kline import fetch_series  # noqa: E402
from undertow.collect.store import SnapshotStore        # noqa: E402
from undertow.core.config import load_config            # noqa: E402

random.seed(20260829)


def atm_iv(snap, snap_day, lo, hi):
    """[lo,hi) 天到期区间里，最贴近现价那几档的平均 IV（%）。"""
    s = snap.spot
    out = []
    for c in snap.contracts:
        d = (c.expiry - snap_day).days
        if lo <= d < hi and c.iv and c.open_interest:
            out.append((abs(c.strike / s - 1), c.iv))
    if len(out) < 4:
        return None
    out.sort()
    k = out[:8]
    return sum(v for _, v in k) / len(k) * 100


def tr_series(highs, lows, closes):
    out = []
    for i in range(1, len(closes)):
        out.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return out


def collect():
    cfg, store = load_config(), SnapshotStore()
    rows = []
    for key, inst in cfg.instruments.items():
        if inst.options is None:
            continue
        sym = inst.options.symbol
        ds_snap = store.dates("options", sym)
        if len(ds_snap) < 2:
            continue
        try:
            ser = fetch_series(f"{sym}.US", period="day", count=400)
        except Exception:
            continue
        keys = [str(d) for d in ser.dates]
        idx = {d: i for i, d in enumerate(keys)}
        for d in ds_snap:
            cs = str(d)
            if cs not in idx or idx[cs] < 65:
                continue
            j = idx[cs]
            ret = abs((ser.closes[j] / ser.closes[j - 1] - 1) * 100)
            # —— 只用【严格早于可交易日】的 K 线 ——
            H, L, C = ser.highs[:j], ser.lows[:j], ser.closes[:j]
            tr = tr_series(H, L, C)
            atr5 = sum(tr[-5:]) / 5
            atr60 = sum(tr[-60:]) / 60
            rng10 = (max(H[-10:]) - min(L[-10:])) / C[-1] * 100
            rng60 = (max(H[-60:]) - min(L[-60:])) / C[-1] * 100
            p = store.load("options", sym, d)
            iv = None
            if p is not None:
                try:
                    iv = atm_iv(snapshot_from_payload(p, key, sym), d, 5, 45)
                except Exception:
                    iv = None
            rows.append({
                "inst": key, "date": cs, "absret": ret,
                "iv": iv,
                "atr_squeeze": atr5 / atr60 if atr60 else None,   # <1 = 短期波幅被压
                "range_squeeze": rng10 / rng60 if rng60 else None,  # <1 = 区间收敛
            })
    return rows


rows = collect()
by = defaultdict(list)
for r in rows:
    by[r["inst"]].append(r["absret"])
thr = {k: sorted(v)[int(0.7 * len(v))] if len(v) >= 10 else 1.5 for k, v in by.items()}
for r in rows:
    r["big"] = r["absret"] >= thr[r["inst"]]

base = sum(1 for r in rows if r["big"]) / len(rows)
print(f"样本 {len(rows)}　基准大波动率 {base*100:.0f}%")
print(f"门槛（各品种自身 70 分位）：" + "、".join(f"{k} {v:.2f}%" for k, v in sorted(thr.items())))
print()


# ⚠️ 分位含未来信息（codex 2026-08-29 P1-5）：
# 下面按【整个回测区间】排序求分位，某一天的分位用到了它之后的数据。
# 目标变量"大波动"的门槛（各品种 |日涨跌| 70 分位）同样取自全区间。
# 因此本脚本的结论是【事后描述性】的，**不是盘前可复现的预测回测**。
# 要做成真回测须改用 expanding/rolling 分位，并先划训练期定门槛再冻结测试。
def rank_within(key):
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


print("=" * 80)
print("压缩程度 → 次日大波动率（压缩越狠 = 分位越低）")
print("=" * 80)
print(f"{'压缩维度':<20}{'样本':>5}{'最压缩1/3':>11}{'最舒张1/3':>11}{'差值':>9}{'日聚类95%区间':>18}")
print("-" * 80)
for key, name in (("iv", "ATM IV 低位"), ("atr_squeeze", "ATR5/ATR60 收缩"),
                  ("range_squeeze", "10日/60日区间收敛")):
    pairs = rank_within(key)
    if len(pairs) < 40:
        print(f"{name:<20}{len(pairs):>5}  样本不足")
        continue
    pairs.sort(key=lambda x: x[0])
    k = len(pairs) // 3
    tight = [r for _, r in pairs[:k]]      # 分位最低 = 压缩最狠
    loose = [r for _, r in pairs[-k:]]
    pt = sum(1 for r in tight if r["big"]) / len(tight)
    pl = sum(1 for r in loose if r["big"]) / len(loose)
    by_day = defaultdict(list)
    for q, r in pairs:
        by_day[r["date"]].append((q, r))
    days = list(by_day)
    diffs = []
    for _ in range(3000):
        pick = [random.choice(days) for _ in days]
        T = Lo = tb = lb = 0
        for d in pick:
            for q, r in by_day[d]:
                if q <= 0.33:
                    T += 1
                    tb += r["big"]
                elif q >= 0.67:
                    Lo += 1
                    lb += r["big"]
        if T and Lo:
            diffs.append((tb / T - lb / Lo) * 100)
    diffs.sort()
    ci = f"[{diffs[75]:+.0f}, {diffs[-76]:+.0f}]pp" if diffs else "—"
    star = " ✅" if diffs and diffs[75] > 0 else ""
    print(f"{name:<20}{len(pairs):>5}{pt*100:>10.0f}%{pl*100:>10.0f}%"
          f"{(pt-pl)*100:>+8.0f}pp{ci:>18}{star}")

print()
print("=" * 80)
print("三重压缩同时出现（IV 低位 + ATR 收缩 + 区间收敛，各取自身最压缩的 40%）")
print("=" * 80)
q = {}
for key in ("iv", "atr_squeeze", "range_squeeze"):
    for qq, r in rank_within(key):
        q.setdefault(id(r), {})[key] = qq
sel = [r for r in rows if id(r) in q and len(q[id(r)]) == 3
       and all(v <= 0.4 for v in q[id(r)].values())]
rest = [r for r in rows if id(r) in q and len(q[id(r)]) == 3 and r not in sel]
if sel and rest:
    ps = sum(1 for r in sel if r["big"]) / len(sel)
    pr = sum(1 for r in rest if r["big"]) / len(rest)
    print(f"  三重压缩日：{len(sel)} 个，大波动率 {ps*100:.0f}%")
    print(f"  其余日子　：{len(rest)} 个，大波动率 {pr*100:.0f}%")
    print(f"  差 {(ps-pr)*100:+.0f}pp　{'（样本 <50，仅供记录）' if len(sel) < 50 else ''}")
    if sel:
        print(f"  这些日子：" + "、".join(f"{r['inst']} {r['date'][5:]}({r['absret']:.1f}%)"
                                       for r in sorted(sel, key=lambda x: -x['absret'])[:8]))
