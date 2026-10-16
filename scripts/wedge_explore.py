"""wedge-explore-v1：复现 Bulkowski 下降楔形统计（≤2015 设计期日线，2016 起不取、不算）。

  python3 scripts/wedge_explore.py --run-id run1     # → data/backtest/wedge-explore-v1/run1/
  run-id 必填；目录已存在则拒绝运行。输入复用 ps-explore-v0.3 已存日线并核对 sha256。
口径：docs/prereg/2026-10-16_falling_wedge_explore_v1.md。纯标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import zlib
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dz_explore import load, pb_z, wilson_lo  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import wedge as wg  # noqa: E402

RUN = "wedge-explore-v1"
BASE = ROOT / "data/backtest" / RUN
SPEC = ROOT / "docs/prereg/2026-10-16_falling_wedge_explore_v1.md"
STOCK = ("AAPL", "MSFT", "GOOGL", "NVDA", "AMD", "INTC", "TSLA")
ETF = ("GLD", "SLV", "USO", "QQQ", "SPY", "IWM", "TLT", "GC=F", "SI=F")
VARIANTS = {"main": {"k": 3, "tol": 0.3}, "k2": {"k": 2, "tol": 0.3}, "tol0.5": {"k": 3, "tol": 0.5}}


def wilson_hi(k: int, n: int, z: float = 1.96) -> float | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    return (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den


def run_symbol(sym: str, mode: str, k: int, tol: float) -> list:
    bars = load(sym)
    atr = ps.wilder_atr(bars)
    rows = []
    for j, (w, d, t) in enumerate(wg.find(bars, atr, mode, k=k, tol_atr=tol)):
        r = {"symbol": sym, "first": bars[w.first][0].isoformat(), "last": bars[w.last][0].isoformat(),
             "known": bars[min(w.known, len(bars) - 1)][0].isoformat(), "span": w.last - w.first, "touches": w.touches,
             "direction": d, "breakout": None if t is None else bars[t][0].isoformat()}
        if d in ("up", "down"):
            start = (w.last if mode == "descriptive" else w.known) + 1
            r["break_pos"] = round((t - w.first) / (w.apex - w.first), 4)
            r.update(wg.outcome(bars, w, d, t))
            r["placebo_up"] = wg.placebo_up_share(bars, atr, w, start, seed=zlib.crc32(repr((sym, w.first, mode, k, tol)).encode()))
        rows.append(r)
    return rows


def summarize(rows: list) -> dict:
    br = [r for r in rows if r["direction"] in ("up", "down")]
    up = [r for r in br if r["direction"] == "up"]
    dn = [r for r in br if r["direction"] == "down"]
    pr = [((r["direction"] == "up") * 1, r["placebo_up"]) for r in br if r.get("placebo_up") is not None]
    diffs = [a - b for a, b in pr]
    z, zp = pb_z([a for a, _ in pr], [b for _, b in pr])

    def rate(rs, key):
        c = [r for r in rs if r.get("complete")]
        return None if not c else round(sum(r[key] for r in c) / len(c), 4)
    pl = [r["placebo_up"] for r in br if r.get("placebo_up") is not None]
    return {
        "detected": len(rows), "no_break": sum(r["direction"] == "no_break" for r in rows),
        "broke_before_known": sum(r["direction"] == "broke_before_known" for r in rows),
        "incomplete": sum(r["direction"] == "incomplete" for r in rows),
        "up": len(up), "down": len(dn), "up_share": None if not br else round(len(up) / len(br), 4),
        "up_ci": None if not br else [round(wilson_lo(len(up), len(br)), 4), round(wilson_hi(len(up), len(br)), 4)],
        "placebo_up_mean": None if not pl else round(sum(pl) / len(pl), 4),
        "paired_mean": None if not diffs else round(sum(diffs) / len(diffs), 4),
        "pb_z": None if z is None else round(z, 2), "pb_p": None if zp is None else round(zp, 5),
        "fail5_up": rate(up, "fail5"), "fail5_down": rate(dn, "fail5"),
        "throwback_up": rate(up, "throwback"), "pullback_down": rate(dn, "throwback"),
        "target_up": rate(up, "target_hit"), "target_down": rate(dn, "target_hit"),
        "break_pos_median": None if not br else round(sorted(r["break_pos"] for r in br)[len(br) // 2], 4),
    }


def pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def report_md(res: dict) -> str:
    L = [f"# {RUN} / {res['run_id']}（≤2015 设计期，探索；口径 {SPEC.name} sha256 {res['spec_sha256'][:12]}）", "",
         "Bulkowski（牛市、800+ 笔）：向上 68%｜失败率 上 26% 下 29%｜回抽 上 62% 下 74%｜达标 上 62% 下 29%｜突破位置 61–62%", ""]
    for var in VARIANTS:
        for mode in ("descriptive", "causal"):
            L.append(f"## {var} · {mode}")
            L.append("")
            L.append("| 组 | 识别 | 未突破 | 认出前已破 | 上 | 下 | 向上占比 [95%] | 安慰剂向上 | 配对差 | z（对安慰剂） | p | 失败率 上/下 | 回抽 上/下 | 达标 上/下 | 突破位置中位 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for g, s in res["results"][var][mode]["groups"].items():
                ci = "" if not s["up_ci"] else f" [{pct(s['up_ci'][0])}–{pct(s['up_ci'][1])}]"
                L.append(f"| {g} | {s['detected']} | {s['no_break']} | {s['broke_before_known']} | {s['up']} | {s['down']} | {pct(s['up_share'])}{ci} | "
                         f"{pct(s['placebo_up_mean'])} | {s['paired_mean']} | {s['pb_z']} | {s['pb_p']} | {pct(s['fail5_up'])}/{pct(s['fail5_down'])} | "
                         f"{pct(s['throwback_up'])}/{pct(s['pullback_down'])} | {pct(s['target_up'])}/{pct(s['target_down'])} | {s['break_pos_median']} |")
            L.append("")
    L.append("安慰剂 = 同一对轨道与起点价，换本品种其它日期的收盘路径（ATR% 缩放）各 50 条；配对差 = 真实向上(0/1) − 安慰剂向上占比。"
             "z = Σ(真实 − 安慰剂概率)/√Σp(1−p)，以安慰剂概率为零假设（run1 的符号检验用错，见研究决策日志）。跨品种同日不独立，合并的 z 偏乐观，只作描述。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    out = BASE / a.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在，拒绝覆盖")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    res = {"run": RUN, "run_id": a.run_id, "git_head": head, "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
           "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in ("undertow/analyze/wedge.py", "scripts/wedge_explore.py")},
           "ran_at": datetime.now(timezone.utc).isoformat(), "results": {}}
    for var, prm in VARIANTS.items():
        res["results"][var] = {}
        for mode in ("descriptive", "causal"):
            rows = {s: run_symbol(s, mode, prm["k"], prm["tol"]) for s in (*STOCK, *ETF)}
            groups = {"个股": [r for s in STOCK for r in rows[s]], "ETF与期货": [r for s in ETF for r in rows[s]]}
            groups["全部"] = groups["个股"] + groups["ETF与期货"]
            res["results"][var][mode] = {"groups": {g: summarize(v) for g, v in groups.items()},
                                         "by_symbol": {s: summarize(v) for s, v in rows.items()}, "rows": rows}
    out.mkdir(parents=True)
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report_md(res), "utf-8")
    print(report_md(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
