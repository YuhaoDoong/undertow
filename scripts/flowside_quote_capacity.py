"""flow-side v1.1 报价采样的容量测量（Codex 032 O2：先测容量，再定事前采样规则；不改现行采样、不改协议）。

  python3 scripts/flowside_quote_capacity.py [--n 120] [--workers 1,4]

问题：quote_past 主模式需要 F 腿的盘中买卖报价，而现行盘中采样只报价影子账候选腿（9/29 黄金 1/26、白银 1/11 有报价）。
要把 flow-side 采集计划（金银，现价 ±5%、Q/M/W ≤45 天 + D ≤10 天）纳入盘中报价，先量：
  - 去重后的计划代码数（按品种、到期类型）；
  - 长桥 depth 一次只吃一个代码 → 逐个请求的耗时分布、失败率；不同并发数下的吞吐与失败率（看是否撞频率限制）；
  - 按实测吞吐推算「每轮全量」需要多久，与最大报价年龄 15 分钟比较。
另记：`longbridge option quote` 可批量但不返回 bid/ask（2026-10-02 实测），不能替代 depth。

只读行情；结果写 data/history/flow_side/capacity/<ET日>_<HHMM>.json（原子写），不碰任何台账。应在常规交易时段运行。
测量样本 = 计划代码按字典序等距抽 n 个（与成交量无关，不用当日成交量挑样本）。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
OUT = ROOT / "data/history/flow_side/capacity"


def _one(sym: str) -> dict:
    from undertow.collect.longbridge_quote import fetch_depth
    t0 = time.monotonic()
    try:
        d = fetch_depth([sym]).get(sym)
        err = getattr(d, "error", None)
        ok = not err and (getattr(d, "bid", None) is not None or getattr(d, "ask", None) is not None)   # error 无错时是空串
        return {"sym": sym, "s": round(time.monotonic() - t0, 3), "ok": ok, "error": err,
                "two_sided": getattr(d, "bid", None) is not None and getattr(d, "ask", None) is not None}
    except Exception as e:                           # 全部失败时 fetch_depth 抛错：如实记为失败
        return {"sym": sym, "s": round(time.monotonic() - t0, 3), "ok": False, "error": f"{type(e).__name__}: {e}"[:80],
                "two_sided": False}


def measure(syms: list, workers: int) -> dict:
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(_one, syms))
    wall = time.monotonic() - t0
    lat = [r["s"] for r in res]
    return {"workers": workers, "n": len(res), "wall_s": round(wall, 2), "per_symbol_wall_s": round(wall / max(1, len(res)), 3),
            "latency_median_s": round(statistics.median(lat), 3) if lat else None,
            "latency_p90_s": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 3) if lat else None,
            "ok": sum(r["ok"] for r in res), "two_sided": sum(r["two_sided"] for r in res),
            "errors": dict(Counter((r["error"] or "")[:40] for r in res if not r["ok"]))}


def main() -> int:
    from undertow.analyze.expiry_type import classify
    from undertow.core.clock import market_today
    from undertow.shadow_cli import flowside_plan
    args = sys.argv[1:]
    n = int(args[args.index("--n") + 1]) if "--n" in args else 120
    workers = [int(x) for x in (args[args.index("--workers") + 1] if "--workers" in args else "1,4").split(",")]
    day = market_today()
    plan, by_type = {}, Counter()
    for k in ("gold", "silver"):
        root, syms, spot = flowside_plan(k, day)
        plan[k] = syms
        for s in syms:                                # GLD261016C400000.US → 到期 2026-10-16
            body = s.split(".")[0][len(root):]
            exp = datetime.strptime("20" + body[:6], "%Y%m%d").date()
            by_type[(k, classify(exp)["type"])] += 1
    allsyms = sorted(set(plan["gold"]) | set(plan["silver"]))
    if not allsyms:
        print("计划为空（快照未到？），不测量"); return 3
    step = max(1, len(allsyms) // n)
    sample = allsyms[::step][:n]
    now = datetime.now(ET)
    runs = [measure(sample, w) for w in workers]
    best = min(runs, key=lambda r: r["per_symbol_wall_s"])
    rec = {"schema": 1, "measured_at": now.isoformat(), "rth": now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0),
           "plan_unique_symbols": len(allsyms), "plan_by_instrument_type": {f"{a}/{b}": v for (a, b), v in sorted(by_type.items())},
           "sample_n": len(sample), "sample_rule": "计划代码字典序等距抽样（与成交量无关）", "runs": runs,
           "full_round_estimate_s": {r["workers"]: round(r["per_symbol_wall_s"] * len(allsyms), 1) for r in runs},
           "quote_max_age_s": 900, "note": "option quote 批量接口无 bid/ask，不能替代 depth（2026-10-02 实测）",
           "best_workers": best["workers"]}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{now:%Y-%m-%d_%H%M}.json"
    fd, tmp = tempfile.mkstemp(dir=OUT, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    print(json.dumps({k: rec[k] for k in ("plan_unique_symbols", "full_round_estimate_s", "rth")}, ensure_ascii=False))
    for r in runs:
        print(f"  workers={r['workers']}: {r['n']} 个 {r['wall_s']}s，中位 {r['latency_median_s']}s，成功 {r['ok']}，双边 {r['two_sided']}，错误 {r['errors'] or '无'}")
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
