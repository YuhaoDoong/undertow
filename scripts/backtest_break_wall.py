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


cfg, store = load_config(), SnapshotStore()
rows, fails = [], defaultdict(int)
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
        if cs not in px:
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
if fails:
    print("失败：" + "、".join(f"{k}×{v}" for k, v in fails.items()), file=sys.stderr)

n_fire = sum(1 for r in rows if r["fired"])
print(f"样本 {len(rows)}（有 put 墙的品种日）　信号触发 {n_fire} 次\n")
print("=" * 72)
print("① 触发 vs 未触发：当日收盘跌破 put 墙的比例")
print("=" * 72)
print(f"{'分组':<12}{'样本':>6}{'跌破':>6}{'跌破率':>9}{'二项p':>10}")
print("-" * 72)
for tag, sel in (("信号触发", True), ("未触发", False)):
    sub = [r for r in rows if r["fired"] is sel]
    if not sub:
        continue
    b = sum(1 for r in sub if r["broke"])
    print(f"{tag:<12}{len(sub):>6}{b:>6}{b/len(sub)*100:>8.0f}%"
          f"{binom_p(b, len(sub)):>10.4f}")

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
