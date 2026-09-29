"""成交价代理 vs v5 保守报价口径：描述性对照（Codex 024-5；用户 2026-09-28「按成交价可能更能反应价格变化」）。

  python3 scripts/trade_vs_quote.py                # 所有已有开盘窗报价且有当天逐分钟的 session
  python3 scripts/trade_vs_quote.py --session 2026-09-28

规则（事前写死，不改 v5、不改冻结主检验；输出只作描述，不称可执行收益）：
- 单位：v5 一个候选价差（leg_id，卖腿 + 买腿），session D，入场窗口 = v5 开盘窗 ET 10:00–10:20（含 10:00、不含 10:20 起点）。
- 报价口径：v5 window_leg(open, entry) 为 valid 时的 conservative 权利金（美元/组，卖腿 bid − 买腿 ask，×100）。
- 成交价代理：各腿窗口内【成交量 > 0】的分钟收盘价按成交量加权（「分钟收盘×量」代理，**不是 VWAP**：分钟内分布未知）；
  数据取当天逐分钟（option_intraday，price=分钟收盘）。代理权利金 =（卖腿代理 − 买腿代理）× 100。
- 两腿配对类别：both_same_minute（两腿在同一分钟都有成交）/ both_async（两腿都有成交但不在同一分钟）/
  one_leg / none。只有 both_* 计代理权利金；async 单列，不当作同时成交。
- 表一（覆盖）：品种 × 类别 × v5 报价是否有效 的计数；表二（共同样本）：两种口径都有效时的差值（代理 − 报价）
  均值 / 中位数 / 分位。手续费两种口径相同（v5 往返 $3.20/组），差值不含费。
只读、纯标准库、不联网。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
WINDOW = ((10, 0), (10, 20))


def leg_proxy(rows: list, day: date) -> tuple[float | None, set]:
    """窗口内 volume>0 的分钟收盘按量加权；返回 (代理价, 有成交的分钟集合)。rows=[time, price, volume, …]。"""
    lo = datetime(day.year, day.month, day.day, *WINDOW[0], tzinfo=ET)
    hi = datetime(day.year, day.month, day.day, *WINDOW[1], tzinfo=ET)
    num = vol = 0.0
    mins = set()
    for r in rows or []:
        t = datetime.fromisoformat(r[0].replace("Z", "+00:00"))
        v = float(r[2])
        if lo <= t < hi and v > 0:
            num += float(r[1]) * v; vol += v; mins.add(t)
    return (num / vol if vol else None), mins


def classify(ms: set, mb: set) -> str:
    if ms and mb:
        return "both_same_minute" if ms & mb else "both_async"
    return "one_leg" if (ms or mb) else "none"


def main():
    from undertow import shadow_cli as sc
    from undertow.analyze import shadow as sh
    from undertow.collect import jsonl_ledger as jl
    from undertow.collect import longbridge_bars as lbb
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    a = ap.parse_args()
    cov = Counter()
    diffs = defaultdict(list)
    for p in sorted(sc._vdir(False).glob("*.jsonl")):
        for r in jl.load(p, sc.KEY):
            if a.session and r["session"] != a.session:
                continue
            day = date.fromisoformat(r["session"])
            try:
                intra = lbb.load_day(lbb.path_of(r["symbol"], day, lbb.INTRADAY_DIR))
            except lbb.BarsFileCorrupt:
                intra = None
            if intra is None:
                continue
            for l in r["legs"]:
                if l.get("status") != "candidate":
                    continue
                w = sh.window_leg(r, l, f"{r['session']}|open", "entry")
                if w["status"] == "not_run":
                    continue
                q_ok = w["status"] == "valid"
                ss = lbb.option_symbol(r["symbol"], l["expiry"], l["side"], l["sell"])
                bs = lbb.option_symbol(r["symbol"], l["expiry"], l["side"], l["buy"])
                ps, ms = leg_proxy((intra["contracts"].get(ss) or {}).get("rows"), day)
                pb, mb = leg_proxy((intra["contracts"].get(bs) or {}).get("rows"), day)
                cat = classify(ms, mb)
                cov[(r["instrument"], cat, "报价有效" if q_ok else "报价无效")] += 1
                if q_ok and cat.startswith("both"):
                    proxy = round((ps - pb) * 100, 2)
                    diffs[cat].append(proxy - w["credit"]["conservative"])
    print("表一 覆盖（品种 × 成交类别 × v5 报价是否有效）")
    tot = Counter()
    for (inst, cat, q), n in sorted(cov.items()):
        print(f"  {inst:6s} {cat:17s} {q}：{n}")
        tot[(cat, q)] += n
    print("  合计：" + "；".join(f"{c}/{q} {n}" for (c, q), n in sorted(tot.items())))
    print("表二 共同样本：代理权利金 − v5 保守权利金（美元/组，不含费）")
    for cat, xs in sorted(diffs.items()):
        xs = sorted(xs)
        print(f"  {cat}: n={len(xs)} 均值 {statistics.mean(xs):+.2f} 中位 {statistics.median(xs):+.2f} "
              f"P10 {xs[len(xs)//10]:+.2f} P90 {xs[(9*len(xs))//10]:+.2f}" if xs else f"  {cat}: n=0")
    if not diffs:
        print("  （无共同样本）")
    print("注：描述性对照；代理 ≠ 可成交价（量是否够、两腿能否同时成交都未验证），不改 v5、不进冻结检验。")


if __name__ == "__main__":
    main()
