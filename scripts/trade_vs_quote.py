"""成交价代理 vs v5 保守报价口径：描述性对照（Codex 024-5；用户 2026-09-28「按成交价可能更能反应价格变化」）。

  python3 scripts/trade_vs_quote.py                # 所有已有开盘窗报价且有当天逐分钟的 session
  python3 scripts/trade_vs_quote.py --session 2026-09-28

规则（事前写死，不改 v5、不改冻结主检验；输出只作描述，不称可执行收益）：
- 单位：v5 一个候选价差（leg_id，卖腿 + 买腿），session D，入场窗口 = v5 开盘窗 ET 10:00–10:20（含 10:00、不含 10:20 起点）。
- 报价口径：v5 window_leg(open, entry) 为 valid 时的 conservative 权利金（美元/组，卖腿 bid − 买腿 ask，×100）。
- 成交价代理：各腿窗口内【成交量 > 0】的分钟收盘价按成交量加权（「分钟收盘×量」代理，**不是 VWAP**：分钟内分布未知）；
  数据取当天逐分钟（option_intraday，price=分钟收盘）。代理权利金 =（卖腿代理 − 买腿代理）× 100。
- 两腿配对类别（Codex 025-5 改名）：has_overlap（两腿成交分钟集合有交集，**不代表**所用价格同步）/
  both_async（都有成交但没有同一分钟）/ one_leg / none / no_intraday_file（当天逐分钟文件缺失，计入分母）。
- 两种代理：①窗口代理 = 两腿各自整个窗口的量加权（has_overlap 与 both_async 都算，时点可能不同）；
  ②同步代理（仅 has_overlap）= 只取两腿【同一分钟】都有成交的分钟，逐分钟价差等权平均（权重事前固定为等权、两腿共用）。
  v5 报价是开盘窗那一刻的盘口，与成交分钟不同时 —— 时点差异导致的差值无法与「口径差异」分开，单列说明，不下结论。
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


def minute_prices(rows: list, day: date) -> dict:
    """窗口内 volume>0 的 {分钟: 分钟收盘价}。"""
    lo = datetime(day.year, day.month, day.day, *WINDOW[0], tzinfo=ET)
    hi = datetime(day.year, day.month, day.day, *WINDOW[1], tzinfo=ET)
    out = {}
    for r in rows or []:
        t = datetime.fromisoformat(r[0].replace("Z", "+00:00"))
        if lo <= t < hi and float(r[2]) > 0:
            out[t] = float(r[1])
    return out


def sync_proxy(rows_s: list, rows_b: list, day: date) -> tuple[float | None, int]:
    """同步代理：两腿同一分钟都有成交的分钟，逐分钟（卖 − 买）等权平均；返回 (每股价差, 交集分钟数)。"""
    a, b = minute_prices(rows_s, day), minute_prices(rows_b, day)
    common = sorted(set(a) & set(b))
    if not common:
        return None, 0
    return sum(a[t] - b[t] for t in common) / len(common), len(common)


def classify(ms: set, mb: set) -> str:
    if ms and mb:
        return "has_overlap" if ms & mb else "both_async"
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
    common_n = []
    for p in sorted(sc._vdir(False).glob("*.jsonl")):
        for r in jl.load(p, sc.KEY):
            if a.session and r["session"] != a.session:
                continue
            day = date.fromisoformat(r["session"])
            try:
                intra = lbb.load_day(lbb.path_of(r["symbol"], day, lbb.INTRADAY_DIR))
            except lbb.BarsFileCorrupt:
                intra = None
            for l in r["legs"]:
                if l.get("status") != "candidate":
                    continue
                w = sh.window_leg(r, l, f"{r['session']}|open", "entry")
                if w["status"] == "not_run":
                    cov[(r["instrument"], "window_not_run", "—")] += 1
                    continue
                q_ok = w["status"] == "valid"
                if intra is None:                                  # 缺文件也进分母（Codex 025-5）
                    cov[(r["instrument"], "no_intraday_file", "报价有效" if q_ok else "报价无效")] += 1
                    continue
                ss = lbb.option_symbol(r["symbol"], l["expiry"], l["side"], l["sell"])
                bs = lbb.option_symbol(r["symbol"], l["expiry"], l["side"], l["buy"])
                ps, ms = leg_proxy((intra["contracts"].get(ss) or {}).get("rows"), day)
                pb, mb = leg_proxy((intra["contracts"].get(bs) or {}).get("rows"), day)
                cat = classify(ms, mb)
                cov[(r["instrument"], cat, "报价有效" if q_ok else "报价无效")] += 1
                if q_ok and cat in ("has_overlap", "both_async"):
                    proxy = round((ps - pb) * 100, 2)
                    diffs[f"窗口代理/{cat}"].append(proxy - w["credit"]["conservative"])
                if q_ok and cat == "has_overlap":
                    sp, n_common = sync_proxy((intra["contracts"].get(ss) or {}).get("rows"),
                                              (intra["contracts"].get(bs) or {}).get("rows"), day)
                    diffs["同步代理/has_overlap"].append(round(sp * 100, 2) - w["credit"]["conservative"])
                    common_n.append(n_common)
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
    if common_n:
        print(f"  同步代理的交集分钟数：中位 {statistics.median(common_n):g}，最少 {min(common_n)}，最多 {max(common_n)}")
    print("注：v5 报价取自开盘窗那一刻，成交分钟与之不同时；差值混合了口径差异与这段时间的价格变动，二者无法分开。")
    print("注：描述性对照；代理 ≠ 可成交价（量是否够、两腿能否同时成交都未验证），不改 v5、不进冻结检验。")


if __name__ == "__main__":
    main()
