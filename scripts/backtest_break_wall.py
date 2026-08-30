"""破墙预警回测 —— 触发日次日是否真的跌破了墙。

用户 2026-08-29：「跌破了 413 这个最大的看跌墙，这里需要我们看一下，
就是之前说的破墙之前是否有信号。」

信号逻辑先写死再回测（不看结果调参数）：
  ① 墙下方有 ≥2,000 张买方增仓；
  ② 墙下方加权相对 IV 比墙上高出 ≥0.5pp（市场在给"跌破"定价）；
  ③ （只记录不作条件）墙上方保护是否在撤。

时点：快照 D 描述交易日 D−1，D 开盘可用 → 检验 D 当天收盘是否跌破墙。
"""
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.flow import analyze_flow, break_warning      # noqa: E402
from undertow.analyze.gamma import analyze_gamma                   # noqa: E402
from undertow.cli import snapshot_from_payload                     # noqa: E402
from undertow.collect.longbridge_kline import fetch_series         # noqa: E402
from undertow.collect.store import SnapshotStore                   # noqa: E402
from undertow.core.config import load_config                       # noqa: E402


def _prev_wd(d):
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def binom_p(k, n, p=0.5):
    return min(1.0, 2 * sum(math.comb(n, i) * p**i * (1 - p)**(n - i)
                            for i in range(k, n + 1)))


def fisher_exact(a, b, c, d):
    """2x2 Fisher 精确检验（双尾）—— 纯标准库。

    表：  [[a, b],   a=触发且跌破  b=触发未跌破
          [c, d]]   c=未触发且跌破 d=未触发未跌破

    ⚠️ codex 2026-08-29 P1-3：上一版对两组各做 p=0.5 的二项检验，
    那检验的是「该组跌破率是否 ≠50%」，**根本不能回答「两组是否有差异」**，
    更不能由 p=1.0 推出「两组没有差别」。
    """
    n = a + b + c + d
    if n == 0:
        return 1.0

    def _p(x):
        y = a + b - x
        z = a + c - x
        w = d - (a - x)
        if min(x, y, z, w) < 0:
            return 0.0
        return (math.comb(a + b, x) * math.comb(c + d, a + c - x)
                / math.comb(n, a + c))
    obs = _p(a)
    lo, hi = max(0, a + c - (c + d)), min(a + b, a + c)
    return min(1.0, sum(_p(x) for x in range(lo, hi + 1)
                        if _p(x) <= obs * (1 + 1e-9)))


cfg, store = load_config(), SnapshotStore()
rows, fails = [], defaultdict(int)
# ⚠️ 逐品种记账：静默丢弃会让最终数字看不出哪些品种/日期被系统性排除
# （codex 2026-08-29 P1-9）
tally = defaultdict(lambda: {"pairs": 0, "no_px": 0, "gap": 0, "err": 0, "ok": 0})
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
    for i in range(1, len(ds)):
        dp, dc = ds[i - 1], ds[i]
        cs = str(dc)
        tally[key]["pairs"] += 1
        # ⚠️ 存档列表相邻 ≠ 交易日相邻。中间缺快照时，diff 会把多日变化
        # 当成 D−1 单日变化（codex P1-9）。>4 天视为跨周末以外的缺口。
        if (dc - dp).days > 4:
            tally[key]["gap"] += 1
            continue
        if cs not in px:
            tally[key]["no_px"] += 1
            continue
        pp, cp = store.load("options", sym, dp), store.load("options", sym, dc)
        if pp is None or cp is None:
            continue
        try:
            prev = snapshot_from_payload(pp, key, sym)
            curr = snapshot_from_payload(cp, key, sym)
            obs = _prev_wd(dc)
            fa = analyze_flow(prev, curr, today=obs, prev_date=str(dp),
                              curr_date=cs, horizon_days=45)
            ga = analyze_gamma(curr, multiplier=1.0,
                               proxy_quality=inst.options.proxy_quality,
                               today=obs, horizon_days=45)
        except Exception as e:
            fails[type(e).__name__] += 1
            tally[key]["err"] += 1
            continue
        wall = ga.put_wall
        if not wall or wall <= 0:
            continue
        close = px[cs]
        w = break_warning(fa, wall, "P")
        rows.append({
            "inst": key, "date": cs, "wall": wall, "close": close,
            "broke": close < wall,                       # 当日收盘是否跌破墙
            "fired": w is not None,
            "gap": (w or {}).get("gap"),
            "next_line": (w or {}).get("next_line"),
            "retreat": (w or {}).get("inner_retreat"),
            # 若给了下一道防线，收盘离它多远（负=还没到）
            "to_next": ((close / w["next_line"] - 1) * 100
                        if (w and w.get("next_line")) else None),
        })
for k, v in rows and [] or []:
    pass
for r in rows:
    tally[r["inst"]]["ok"] += 1
print("逐品种记账（预期对数 / 成功 / 缺行情 / 日期缺口 / 分析失败）：")
bad = []
for k in sorted(tally):
    v = tally[k]
    print(f"  {k:<8} {v['pairs']:>4} / {v['ok']:>4} / {v['no_px']:>3} / "
          f"{v['gap']:>3} / {v['err']:>3}")
    if v["pairs"] and not v["ok"]:
        bad.append(k)
if fails:
    print("失败类型：" + "、".join(f"{k}×{v}" for k, v in fails.items()), file=sys.stderr)
if bad:
    print(f"❌ 整品种失败：{bad}", file=sys.stderr)
    sys.exit(1)
print()

n_fire = sum(1 for r in rows if r["fired"])
print(f"样本 {len(rows)}（有 put 墙的品种日）　信号触发 {n_fire} 次\n")
print("=" * 72)
print("① 触发 vs 未触发：当日收盘跌破 put 墙的比例")
print("=" * 72)
print(f"{'分组':<12}{'样本':>6}{'跌破':>6}{'跌破率':>9}")
print("-" * 72)
cnt = {}
for tag, sel in (("信号触发", True), ("未触发", False)):
    sub = [r for r in rows if r["fired"] is sel]
    if not sub:
        continue
    b = sum(1 for r in sub if r["broke"])
    cnt[sel] = (b, len(sub) - b)
    print(f"{tag:<12}{len(sub):>6}{b:>6}{b/len(sub)*100:>8.0f}%"
          f"{'':>10}")
if True in cnt and False in cnt:
    a, bb = cnt[True]
    c, d = cnt[False]
    p = fisher_exact(a, bb, c, d)
    print()
    print(f"两组差异的 Fisher 精确检验（双尾）p = {p:.4f}")
    print("⚠️ 正确的结论表述：" +
          ("没有证据表明触发组的跌破率与未触发组不同（**不是**「两组没有差别」——"
           "样本极稀疏，检验功效近乎为零）" if p >= 0.05
           else "两组跌破率存在差异"))

print()
print("=" * 72)
print("② 单品种（同品种不同日不重叠，二项检验适用）")
print("=" * 72)
for k in sorted({r["inst"] for r in rows}):
    f = [r for r in rows if r["inst"] == k and r["fired"]]
    nf = [r for r in rows if r["inst"] == k and not r["fired"]]
    if not f:
        continue
    bf = sum(1 for r in f if r["broke"])
    bn = sum(1 for r in nf if r["broke"]) if nf else 0
    print(f"  {k:<8} 触发 {bf}/{len(f)} 跌破"
          f"（{bf/len(f)*100:.0f}%）　未触发 {bn}/{max(len(nf),1)}"
          f"（{bn/max(len(nf),1)*100:.0f}%）")

print()
print("=" * 72)
print("③ 触发日明细：给的「下一道防线」离当日收盘多远")
print("=" * 72)
for r in sorted([r for r in rows if r["fired"]], key=lambda x: x["date"]):
    hit = "✅跌破" if r["broke"] else "❌没破"
    nx = (f"下一道防线 {r['next_line']:.0f}，收盘离它 {r['to_next']:+.1f}%"
          if r["next_line"] else "")
    print(f"  {r['date']} {r['inst']:<7} 墙 {r['wall']:.0f} 收 {r['close']:.2f} "
          f"{hit}　gap {r['gap']:+.2f}pp　{nx}")
