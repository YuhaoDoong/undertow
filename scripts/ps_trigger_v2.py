"""ps-trigger-v2：快入场（四种确认）+ 慢离场（2ATR 上膛式 LH，U6）。配置 data/backtest/ps-trigger-v2/config.json。

  python3 scripts/ps_trigger_v2.py --run-id run1

用户 2026-10-14：「入场离场可以分开来」。run-id 目录存在即拒绝。描述性，无 p 值，未扣费。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics as st
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from scripts.ps_trigger import GROUPS, METHODS  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

BASE = ROOT / "data/backtest/ps-trigger-v2"
CFG = BASE / "config.json"
TOL = "0.5"
HOLD = {"stop": "extreme", "target": False, "struct_exit": False, "breakeven": False, "hold": 250}


MIRROR = False                                             # --mirror：镜像检验（ps-mirror-v1，用户：「直接把 K 线倒过来」）


def mirror(bars):
    """P' = K/P，K = 首根收盘²：高低互换后取倒数，对数收益正负对调，波幅与形态不变。"""
    K = bars[0][4] ** 2
    return [(b[0], K / b[1], K / b[3], K / b[2], K / b[4]) for b in bars]


def run_symbol(sym, ref_dates):
    bars_all = pe.parse(pe.fetch(sym)[0])[0]
    if MIRROR:
        bars_all = mirror(bars_all)
    assert bars_all[-1][0] <= pe.DESIGN_END
    segs, _ = pe.segments(bars_all, [b[0] for b in bars_all] if sym in pe.FUTURES else ref_dates)
    rows = []
    for si, bars in enumerate(segs):
        if len(bars) < ps.WARMUP + 5:
            continue
        atr = ps.wilder_atr(bars)
        piv, eps = ps.detect(bars, atr)
        last_i = len(bars) - 1
        A_eps = sorted([e for e in eps if e.kind == "A"], key=lambda e: e.break_at)
        for m in METHODS:
            loc = ps.local_swings(bars, atr, "bar" if m == "limit" else m)
            busy = {"long": -1, "short": -1}
            for e in A_eps:
                if e.break_at <= busy[e.direction]:
                    continue
                s = 1 if e.direction == "long" else -1
                fe = ps.fast_entry(bars, atr, e, loc, m, TOL, last_i=last_i)
                row = {"symbol": sym, "direction": e.direction, "break": str(bars[e.break_at][0]), "method": m, "status": fe["status"]}
                if fe["status"] == "entered":
                    ei, epx = fe["entry_i"], fe["entry"]
                    stop = e.extreme.price - s * ps.STOP_BUF * atr[ei - 1]
                    slow = ps.simulate_policy(bars, piv, ei, stop, e.direction, ps.POLICIES["U6"], last_i=last_i, entry_price=epx)
                    row["status"] = slow["status"]
                    if slow["status"] == "entered":
                        busy[e.direction] = slow["exit_i"]
                        A = atr[ei - 1]
                        hold = ps.simulate_policy(bars, piv, ei, stop, e.direction, HOLD, last_i=last_i, entry_price=epx)
                        fast = ps.trigger_trade(bars, atr, e, loc, m, TOL, last_i=last_i)
                        lows = [p for p in loc if p.kind == ("L" if s > 0 else "H") and p.idx > e.break_at and p.conf < ei]
                        row.update(entry_date=str(bars[ei][0]), exit_date=str(bars[slow["exit_i"]][0]), exit_reason=slow["exit_reason"],
                                   days=slow["days_held"], atr_units=s * (slow["exit_price"] - epx) / A,
                                   hold_atr=s * (hold["exit_price"] - epx) / A if hold["status"] == "entered" else None,
                                   fast_atr=s * (fast["exit_price"] - fast["entry"]) / atr[fast["entry_i"] - 1] if fast.get("status") == "entered" else None,
                                   entry_from_low_atr=s * (epx - lows[-1].price) / A if lows else None)
                rows.append(row)
    return rows


def _r(x):
    return None if x is None else round(x, 3)


def summarize(rows):
    out = {}
    for g, syms in list(GROUPS.items()) + [("全部", sum(GROUPS.values(), ()))]:
        for d in ("long", "short"):
            for m in METHODS:
                ent = [r for r in rows if r["symbol"] in syms and r["direction"] == d and r["method"] == m and r["status"] == "entered"]
                if not ent:
                    out[f"{g}|{d}|{m}"] = {"n": 0}; continue
                a = [r["atr_units"] for r in ent]
                h = [r for r in ent if r["hold_atr"] is not None]
                f = [r for r in ent if r["fast_atr"] is not None]
                out[f"{g}|{d}|{m}"] = {"n": len(ent), "atr_mean": _r(st.mean(a)), "atr_median": _r(st.median(a)),
                                      "win": _r(sum(1 for x in a if x > 0) / len(a)), "days_median": st.median(r["days"] for r in ent),
                                      "hold_mean": _r(st.mean(r["hold_atr"] for r in h)) if h else None,
                                      "minus_hold": _r(st.mean(r["atr_units"] - r["hold_atr"] for r in h)) if h else None,
                                      "fast_mean_same_entries": _r(st.mean(r["fast_atr"] for r in f)) if f else None,
                                      "entry_from_low_median": _r(st.median([r["entry_from_low_atr"] for r in ent if r["entry_from_low_atr"] is not None] or [0])),
                                      "exit_reasons": {k: sum(1 for r in ent if r["exit_reason"] == k) for k in sorted({r["exit_reason"] for r in ent})}}
    return out


def report(summ, meta):
    L = [f"# ps-trigger-v2 {meta['run_id']}：快入场 + 慢离场（≤2015 设计集，描述）\n",
         "**性质：** 探索、无 p 值、同日相关、未扣费；入场门槛固定 0.5 ATR（运行前登记，不按 v1 结果改选）；四种入场确认全部列出、不挑最优。"
         f"配置 sha256 {meta['config_sha256'][:12]}，HEAD `{meta['git_head'][:8]}`。单位：入场时 ATR。\n",
         "列：笔数 / 平均 / 中位 / 胜率 / 持有中位 / 入场离低点 / 同入场只拿着 / 本版 − 只拿着 / 同确认快离场（v1，同入场）/ 出场原因\n"]
    for g in list(GROUPS) + ["全部"]:
        for d in ("long", "short"):
            L.append(f"\n## {g} · {'做多' if d == 'long' else '做空'}\n")
            L.append("| 入场确认 | n | 平均 | 中位 | 胜率 | 持有 | 离低点 | 只拿着 | −只拿着 | 快离场 | 出场原因 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for m in METHODS:
                x = summ[f"{g}|{d}|{m}"]
                if not x["n"]:
                    L.append(f"| {m} | 0 |" + " |" * 9); continue
                L.append(f"| {m} | {x['n']} | {x['atr_mean']} | {x['atr_median']} | {x['win']} | {x['days_median']} | {x['entry_from_low_median']} | "
                         f"{x['hold_mean']} | {x['minus_hold']} | {x['fast_mean_same_entries']} | {', '.join(f'{k} {n}' for k, n in x['exit_reasons'].items())} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--mirror", action="store_true")
    a = ap.parse_args()
    global MIRROR, BASE, CFG
    if a.mirror:
        MIRROR, BASE = True, ROOT / "data/backtest/ps-mirror-v1"
        CFG = BASE / "config.json"
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    ref_dates = [b[0] for b in pe.parse(pe.fetch("SPY")[0])[0]]
    rows = [r for syms in GROUPS.values() for s in syms for r in run_symbol(s, ref_dates)]
    out.mkdir(parents=True)
    meta = {"run_id": a.run_id, "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
            "config_sha256": hashlib.sha256(CFG.read_bytes()).hexdigest(), "mirror": MIRROR,
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in ("undertow/analyze/price_structure.py", "scripts/ps_trigger_v2.py")}}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    with open(out / "trades.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = summarize(rows)
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report(summ, meta), "utf-8")
    print(f"{len(rows)} 行 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
