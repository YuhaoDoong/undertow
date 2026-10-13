"""ps-trigger-v1：更早的入场/离场确认，四种方式 × 四种「相同点位」门槛（≤2015 设计集；配置 data/backtest/ps-trigger-v1/config.json）。

  python3 scripts/ps_trigger.py --run-id run1

16 个版本全部报告，不挑最优。run-id 目录存在即拒绝。描述性，无 p 值，未扣费。
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
from undertow.analyze import price_structure as ps  # noqa: E402

BASE = ROOT / "data/backtest/ps-trigger-v1"
CFG = BASE / "config.json"
METHODS = ("bar", "two", "atr", "limit")
TOLS = ("0.25", "0.5", "1", "0.5%")
GROUPS = {"ETF": ("GLD", "SLV", "USO", "QQQ", "SPY"), "个股": ("GOOGL", "AAPL", "MSFT", "NVDA", "AMD", "INTC", "TSLA"), "连续期货": ("GC=F", "SI=F")}


def run_symbol(sym, ref_dates):
    d, _ = pe.fetch(sym)
    bars_all = pe.parse(d)[0]
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
            for tol in TOLS:
                busy = {"long": -1, "short": -1}
                for e in A_eps:
                    if e.break_at <= busy[e.direction]:
                        continue
                    r = ps.trigger_trade(bars, atr, e, loc, m, tol, last_i=last_i)
                    row = {"symbol": sym, "seg": si, "direction": e.direction, "break": str(bars[e.break_at][0]), "method": m, "tol": tol, "status": r["status"]}
                    if r["status"] == "entered":
                        busy[e.direction] = r["exit_i"]
                        s = 1 if e.direction == "long" else -1
                        ei, A = r["entry_i"], atr[r["entry_i"] - 1]
                        lows = [p for p in loc if p.kind == ("L" if s > 0 else "H") and p.idx > e.break_at and p.conf < ei]
                        sw = lows[-1] if lows else None
                        hold = ps.simulate_policy(bars, piv, ei, r["stop"], e.direction,
                                                  {"stop": "x", "target": False, "struct_exit": False, "breakeven": False, "hold": 250}, last_i=last_i)
                        row.update(entry_date=str(bars[ei][0]), exit_date=str(bars[r["exit_i"]][0]), exit_reason=r["exit_reason"],
                                   atr_units=s * (r["exit_price"] - r["entry"]) / A, days=r["days_held"],
                                   entry_from_low_atr=(s * (r["entry"] - sw.price) / A) if sw else None,
                                   entry_bars_after_low=(ei - sw.idx) if sw else None,
                                   hold_atr=(s * (hold["exit_price"] - hold["entry"]) / A) if hold.get("status") == "entered" else None,
                                   lh_from_hh_atr=(s * (r["signals"]["lh"]["ref"] - r["exit_price"]) / A) if r["signals"].get("lh") else None)
                    rows.append(row)
    return rows


def old_u6():
    """同一批突破上 ps-exit-v4 的 U6（旧 2ATR 确认入场）参考：按标的、方向、突破日对齐。"""
    f = ROOT / "data/backtest/ps-exit-v1/run5_v4/trades.jsonl"
    out = {}
    for x in f.read_text("utf-8").splitlines():
        r = json.loads(x)
        u = r["policies"]["U6"]
        if u["status"] == "entered":
            out[(r["symbol"], r["direction"], r["key"]["idx_date"])] = u["atr_units"]
    return out


def _r(x):
    return None if x is None else round(x, 3)


def summarize(rows):
    out = {}
    for g, syms in list(GROUPS.items()) + [("全部", sum(GROUPS.values(), ()))]:
        for d in ("long", "short"):
            for m in METHODS:
                for tol in TOLS:
                    rs = [r for r in rows if r["symbol"] in syms and r["direction"] == d and r["method"] == m and r["tol"] == tol]
                    ent = [r for r in rs if r["status"] == "entered"]
                    if not ent:
                        out[f"{g}|{d}|{m}|{tol}"] = {"n": 0, "candidates": len(rs)}; continue
                    a = [r["atr_units"] for r in ent]
                    hd = [r["atr_units"] - r["hold_atr"] for r in ent if r["hold_atr"] is not None]
                    out[f"{g}|{d}|{m}|{tol}"] = {
                        "candidates": len(rs), "n": len(ent), "not_entered": {k: sum(1 for r in rs if r["status"] == k) for k in {r["status"] for r in rs} - {"entered"}},
                        "atr_mean": _r(st.mean(a)), "atr_median": _r(st.median(a)), "win": _r(sum(1 for x in a if x > 0) / len(a)),
                        "days_median": st.median(r["days"] for r in ent),
                        "entry_from_low_atr_median": _r(st.median([r["entry_from_low_atr"] for r in ent if r["entry_from_low_atr"] is not None] or [0])),
                        "entry_bars_after_low_median": st.median([r["entry_bars_after_low"] for r in ent if r["entry_bars_after_low"] is not None] or [0]),
                        "exit_reasons": {k: sum(1 for r in ent if r["exit_reason"] == k) for k in sorted({r["exit_reason"] for r in ent})},
                        "minus_hold_mean": _r(st.mean(hd)) if hd else None, "hold_mean": _r(st.mean([r["hold_atr"] for r in ent if r["hold_atr"] is not None]))}
    return out


def report(summ, meta):
    L = [f"# ps-trigger-v1 {meta['run_id']}：更早的入场/离场确认（16 个版本全部列出；≤2015 设计集，描述）\n",
         "**性质：** 探索、无 p 值、同日相关、未扣费；16 个版本是用户要求「都试一下」的预登记网格，**不挑最优**，任何一组要算数须到 2016+ 保留集验证。"
         f"配置 sha256 {meta['config_sha256'][:12]}（运行前提交），HEAD `{meta['git_head'][:8]}`。单位：入场时 ATR。\n",
         "列：入场笔数 / 平均 / 中位 / 胜率 / 持有中位（根）/ 入场离局部低点（ATR，中位）/ 入场在低点后几根（中位）/ 相对「同入场只拿着」的差 / 出场原因\n"]
    for g in list(GROUPS) + ["全部"]:
        for d in ("long", "short"):
            L.append(f"\n## {g} · {'做多' if d == 'long' else '做空'}\n")
            L.append("| 确认 | 门槛 | n | 平均 | 中位 | 胜率 | 持有 | 离低点 | 低点后根数 | −只拿着 | 出场原因 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for m in METHODS:
                for tol in TOLS:
                    x = summ[f"{g}|{d}|{m}|{tol}"]
                    if not x["n"]:
                        L.append(f"| {m} | {tol} | 0 | | | | | | | | |"); continue
                    L.append(f"| {m} | {tol} | {x['n']} | {x['atr_mean']} | {x['atr_median']} | {x['win']} | {x['days_median']} | {x['entry_from_low_atr_median']} | "
                             f"{x['entry_bars_after_low_median']} | {x['minus_hold_mean']} | {', '.join(f'{k} {n}' for k, n in x['exit_reasons'].items())} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    ref = pe.fetch("SPY")[0]
    ref_dates = [b[0] for b in pe.parse(ref)[0]]
    rows = []
    for syms in GROUPS.values():
        for s in syms:
            rows += run_symbol(s, ref_dates)
    out.mkdir(parents=True)
    meta = {"run_id": a.run_id, "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip(),
            "config_sha256": hashlib.sha256(CFG.read_bytes()).hexdigest(),
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in ("undertow/analyze/price_structure.py", "scripts/ps_trigger.py")},
            "n_rows": len(rows)}
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
