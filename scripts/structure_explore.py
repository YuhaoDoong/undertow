"""structure-v0 设计集探索：普通结构突破 vs 双顶/双底突破（≤2015）。配置 data/backtest/structure-v0/pattern_config.json。

  python3 scripts/structure_explore.py --run-id run1

输入复用 ps-explore-v0.3 的 Yahoo 原始文件（同一设计集，sha256 核对）。run-id 必填，目录存在即拒绝。只作描述，无 p 值。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import structure_patterns as sp  # noqa: E402

BASE = ROOT / "data/backtest/structure-v0"
CONFIG = BASE / "pattern_config.json"


def run_symbol(sym: str, ref_dates: list) -> dict:
    d, sha = pe.fetch(sym)
    bars_all, bad, actions = pe.parse(d)
    if bars_all[-1][0] > pe.DESIGN_END:
        raise SystemExit(f"{sym}：含 2016 起数据，拒绝")
    sc = pe.split_check(bars_all, actions)
    if any(x["check"] == "UNADJUSTED_UNIT_BREAK" for x in sc):
        return {"symbol": sym, "status": "paused_unit_break"}
    segs, missing = pe.segments(bars_all, [b[0] for b in bars_all] if sym in pe.FUTURES else ref_dates)
    rows = []
    for si, bars in enumerate(segs):
        if len(bars) < 100:
            continue
        atr = ps.wilder_atr(bars)
        dates = [b[0] for b in bars]
        evs = sp.breaks(bars, atr)
        cache = {}
        for ev in evs:
            oc = sp.outcome(bars, atr, ev["t"], ev["direction"])
            if oc is None:
                ev["outcome"] = None
            else:
                ev["outcome"] = oc
                # 同年随机日参考：同方向、同年每天开盘入场、同 k 屏障
                yr = dates[oc["entry_i"]].year
                ck = (yr, ev["direction"])
                if ck not in cache:
                    lo = bisect.bisect_left(dates, date(yr, 1, 1)); hi = bisect.bisect_right(dates, date(yr, 12, 31))
                    surv = []
                    for u in range(max(lo, ps.WARMUP + 1), hi):
                        o = sp.outcome(bars, atr, u - 1, ev["direction"])
                        if o and o["survive"]["2"] is not None:
                            surv.append(o["survive"]["2"])
                    cache[ck] = sum(surv) / len(surv) if surv else None
                ev["year_ref_survive2"] = cache[ck]
            ev.update(symbol=sym, segment=si, date=str(dates[ev["t"]]))
            rows.append(ev)
    return {"symbol": sym, "status": "ok", "input_sha256": sha, "rows": rows}


GROUPS = {"ETF": ("GLD", "SLV", "USO", "QQQ", "SPY"), "个股": ("GOOGL", "AAPL", "MSFT", "NVDA", "AMD", "INTC", "TSLA"),
          "连续期货": ("GC=F", "SI=F")}


def stats(rows: list) -> dict:
    ok = [r for r in rows if r["outcome"]]
    def surv(rs, k="2"):
        xs = [r["outcome"]["survive"][k] for r in rs if r["outcome"]["survive"][k] is not None]
        return (sum(xs), len(xs))
    out = {}
    for k in ("2", "1", "3"):
        a, n = surv(ok, k)
        out[f"survive_{k}atr"] = f"{a}/{n}"
        out[f"survive_{k}atr_rate"] = round(a / n, 4) if n else None
    out["n"] = len(ok)
    out["mae_atr_median"] = _med([r["outcome"]["mae_atr"] for r in ok])
    out["mfe_atr_median"] = _med([r["outcome"]["mfe_atr"] for r in ok])
    yr = [r["year_ref_survive2"] for r in ok if r.get("year_ref_survive2") is not None]
    out["year_ref_survive_2atr"] = round(sum(yr) / len(yr), 4) if yr else None
    out["late_confirmation"] = sum(1 for r in ok if r["late_confirmation"])
    return out


def _med(xs):
    xs = sorted(x for x in xs if x is not None)
    return round(xs[len(xs) // 2], 4) if xs else None


def summarize(all_rows: list) -> dict:
    res = {}
    for gname, syms in list(GROUPS.items()) + [("全部", tuple(sorted({r["symbol"] for r in all_rows})))]:
        g = [r for r in all_rows if r["symbol"] in syms]
        res[gname] = {}
        for direction in ("bearish", "bullish"):
            rev = [r for r in g if r["direction"] == direction and r["reversal"]]
            pat = [r for r in rev if r["double"] and r["double"]["is_pattern"]]
            non = [r for r in rev if not (r["double"] and r["double"]["is_pattern"])]
            sp_, sn = stats(pat), stats(non)
            res[gname][direction] = {"all_reversal_breaks": stats(rev), "double_pattern": sp_, "non_pattern": sn,
                                     "diff_survive_2atr_pattern_minus_non": (round(sp_["survive_2atr_rate"] - sn["survive_2atr_rate"], 4)
                                                                             if sp_["survive_2atr_rate"] is not None and sn["survive_2atr_rate"] is not None else None),
                                     "background_counts": _count([r["background"] for r in g if r["direction"] == direction])}
    per = {}
    for s in sorted({r["symbol"] for r in all_rows}):
        per[s] = {}
        for direction in ("bearish", "bullish"):
            rev = [r for r in all_rows if r["symbol"] == s and r["direction"] == direction and r["reversal"]]
            pat = [r for r in rev if r["double"] and r["double"]["is_pattern"]]
            non = [r for r in rev if not (r["double"] and r["double"]["is_pattern"])]
            per[s][direction] = {"pattern": stats(pat), "non_pattern": stats(non)}
    return {"groups": res, "per_symbol": per}


def _count(xs):
    c = {}
    for x in xs:
        c[x] = c.get(x, 0) + 1
    return c


def report(summary: dict, cfg: dict) -> str:
    L = [f"# structure-v0 {cfg['run_id']}：普通结构突破 vs 双顶/双底突破（≤2015 设计集，描述）\n",
         "**性质：** 探索，无 p 值；同日相关；期货连续合约未做换月调整；个股为事后挑选、无退市样本。主比较 = 逆背景的突破（看跌突破要求 2ATR 背景为 up，看涨要求 down）。"
         "终点 = t+1 开盘入场后 10 根内收盘是否越过 k×ATR 屏障（2ATR 为主，1/3ATR 敏感性）；不是交易收益，也不是价差胜率。\n",
         f"配置 `{cfg['config_version']}`（sha256 {cfg['config_sha256'][:12]}，运行前已提交）；代码 HEAD `{cfg['git_head'][:8]}`。\n"]
    for g, v in summary["groups"].items():
        L.append(f"\n## {g}\n")
        L.append("| 方向 | 组 | n | 2ATR 守住 | 守住率 | 1ATR | 3ATR | MAE 中位(ATR) | MFE 中位(ATR) | 同年随机 2ATR |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for d, x in v.items():
            for name, key in (("双顶/双底", "double_pattern"), ("非形态普通突破", "non_pattern"), ("全部逆背景突破", "all_reversal_breaks")):
                s = x[key]
                L.append(f"| {'看跌' if d == 'bearish' else '看涨'} | {name} | {s['n']} | {s['survive_2atr']} | {s['survive_2atr_rate']} | "
                         f"{s['survive_1atr_rate']} | {s['survive_3atr_rate']} | {s['mae_atr_median']} | {s['mfe_atr_median']} | {s['year_ref_survive_2atr']} |")
            L.append(f"| {'看跌' if d == 'bearish' else '看涨'} | **形态 − 非形态（2ATR 守住率）** | | | **{x['diff_survive_2atr_pattern_minus_non']}** | | | | | |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--symbols", default="GLD,SLV,USO,QQQ,SPY,GC=F,SI=F,GOOGL,AAPL,MSFT,NVDA,AMD,INTC,TSLA")
    a = ap.parse_args()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    t0 = time.time()
    out.mkdir(parents=True)
    ref, _ = pe.fetch("SPY")
    ref_dates = [b[0] for b in pe.parse(ref)[0]]
    results = {s: run_symbol(s, ref_dates) for s in a.symbols.split(",")}
    rows = [r for v in results.values() for r in v.get("rows", [])]
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    cfg = json.loads(CONFIG.read_text("utf-8"))
    meta = {"run_id": a.run_id, "command": " ".join(sys.argv), "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": head, "config_version": cfg["version"], "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
            "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in
                            ("undertow/analyze/structure_patterns.py", "undertow/analyze/price_structure.py", "scripts/structure_explore.py")},
            "inputs": {s: v.get("input_sha256") for s, v in results.items()}, "n_events": len(rows)}
    (out / "config.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summ = summarize(rows)
    (out / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report(summ, meta), "utf-8")
    print(f"{len(rows)} 个突破事件，用时 {time.time() - t0:.1f} 秒 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
