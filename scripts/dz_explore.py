"""dz-explore-v1：按来向区分的压力/支撑区间（外部作者二）设计期探索回测（≤2015，2016 起不取、不算）。

  python3 scripts/dz_explore.py --run-id run1     # → data/backtest/dz-explore-v1/run1/
  run-id 必填；目录已存在则拒绝运行（不覆盖已看过的结果）。

输入复用 ps-explore-v0.3 已存的 Yahoo 日线（截止 2015-12-31），逐个与其首次记录的 sha256 核对，不重新取数。
口径：docs/prereg/2026-10-16_directional_zones_explore_v1.md。纯标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics as st
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import directional_zones as dz  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

RUN = "dz-explore-v1"
BASE = ROOT / "data/backtest" / RUN
SRC = ROOT / "data/backtest/ps-explore-v0.3"
SPEC = ROOT / "docs/prereg/2026-10-16_directional_zones_explore_v1.md"
PRIMARY = ("GC=F", "SI=F", "USO")
REPLICA = ("GLD", "SLV")
ALPHA = 0.05 / 6
SENS_X = (1.0, 2.0)


def load(sym: str) -> list:
    raw = (SRC / "inputs" / f"{sym.replace('=', '_')}.json").read_bytes()
    want = json.loads((SRC / "inputs_sha256.json").read_text("utf-8"))[sym]
    got = hashlib.sha256(raw).hexdigest()
    if got != want:
        raise SystemExit(f"{sym}：输入哈希与 ps-explore 首次记录不符（{want[:12]} → {got[:12]}），拒绝计算")
    r = json.loads(raw)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(r["timestamp"]):
        row = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if any(x is None for x in row):
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        if d.year <= 2015:
            bars.append((d, *row))
    return bars


def wilson_lo(k: int, n: int, z: float = 1.96) -> float | None:
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den


def sign_p(pos: int, neg: int) -> float | None:
    n = pos + neg
    if n == 0:
        return None
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def pb_z(xs: list, ps_: list) -> tuple[float | None, float | None]:
    """零假设：第 i 个事件以安慰剂概率 p_i 成功（Poisson-二项），正态近似 z = Σ(x−p)/√Σp(1−p)，双尾 p。
    run1 用的符号检验对「0/1 − 概率」的差不适用（差的正负只由 x 决定，等价于检验成功率 vs 50%），run2 起改用本检验。
    p_i 本身是少量安慰剂的估计，此近似未计入其抽样误差（偏乐观）。"""
    v = sum(p * (1 - p) for p in ps_)
    if not xs or v <= 0:
        return None, None
    z = (sum(xs) - sum(ps_)) / math.sqrt(v)
    return z, math.erfc(abs(z) / math.sqrt(2))


def regime(C: list, a: int) -> str:
    if a < 220:
        return "unknown"
    s0 = sum(C[a - 199:a + 1]) / 200
    s1 = sum(C[a - 219:a - 19]) / 200
    return "up" if s0 > s1 else "down"


def run_symbol(sym: str, rev_x: float) -> dict:
    bars = load(sym)
    atr = ps.wilder_atr(bars)
    piv = ps.zigzag(bars, atr)
    zones = dz.find_zones(bars, atr, piv)
    C = [b[4] for b in bars]
    status = {}
    real = []
    for z in zones:
        res = dz.evaluate(bars, atr, z, rev_x=rev_x)
        status[z.kind + ":" + res["status"]] = status.get(z.kind + ":" + res["status"], 0) + 1
        if res["status"] == "event":
            real.append((z, res))
    kept = dz.dedupe(real)
    rows = []
    for z, res in kept:
        pl = [dz.evaluate(bars, atr, dz.shifted(z, w), rev_x=rev_x) for w in (1, -1)]
        pl = [p for p in pl if p["status"] == "event"]
        rows.append({
            "kind": z.kind, "lo": round(z.lo, 4), "hi": round(z.hi, 4),
            "formed": bars[z.formed][0].isoformat(), "active": bars[z.active][0].isoformat(),
            "touch": bars[res["touch"]][0].isoformat(), "outcome": res["outcome"],
            "ret10_atr": None if res["ret10_atr"] is None else round(res["ret10_atr"], 4),
            "regime": regime(C, z.active),
            "placebo_n": len(pl),
            "placebo_success": None if not pl else sum(p["outcome"] == "reversed" for p in pl) / len(pl),
            "placebo_ret10": [None if p["ret10_atr"] is None else round(p["ret10_atr"], 4) for p in pl],
        })
    return {"symbol": sym, "bars": len(bars), "first": bars[0][0].isoformat(), "last": bars[-1][0].isoformat(),
            "pivots": len(piv), "zones": {k: sum(z.kind == k for z in zones) for k in ("R", "S")},
            "status": status, "events_before_dedupe": len(real), "rows": rows}


def summarize(rows: list) -> dict:
    out = {}
    for kind in ("R", "S"):
        ev = [r for r in rows if r["kind"] == kind]
        n = len(ev)
        k = sum(r["outcome"] == "reversed" for r in ev)
        b = sum(r["outcome"] == "broken" for r in ev)
        pl_all = [r["placebo_success"] for r in ev if r["placebo_success"] is not None]
        diffs = [(r["outcome"] == "reversed") - r["placebo_success"] for r in ev if r["placebo_success"] is not None]
        t = None
        if len(diffs) > 2 and st.pstdev(diffs) > 0:
            t = st.mean(diffs) / (st.stdev(diffs) / math.sqrt(len(diffs)))
        rets = [r["ret10_atr"] for r in ev if r["ret10_atr"] is not None]
        prets = [x for r in ev for x in r["placebo_ret10"] if x is not None]
        reg = {}
        for g in ("up", "down"):
            e = [r for r in ev if r["regime"] == g]
            reg[g] = {"n": len(e), "reversed": sum(r["outcome"] == "reversed" for r in e)}
        out[kind] = {
            "n": n, "reversed": k, "broken": b, "none": n - k - b,
            "rate": None if n == 0 else round(k / n, 4), "wilson_lo": None if n == 0 else round(wilson_lo(k, n), 4),
            "placebo_rate_mean": None if not pl_all else round(st.mean(pl_all), 4),
            "paired_n": len(diffs), "paired_mean": None if not diffs else round(st.mean(diffs), 4),
            "paired_t": None if t is None else round(t, 3),
            # sign_p 与 plug-in z 在本研究均无效（Codex 053，见 run1/run2 的 STATUS.md）：不再输出，免得被当成有效 p 消费
            "inference": "none（配对差/t 只作描述；对照与推断待重新设计）",
            "ret10_mean": None if not rets else round(st.mean(rets), 4),
            "placebo_ret10_mean": None if not prets else round(st.mean(prets), 4),
            "regime": reg,
        }
    return out


def report_md(res: dict) -> str:
    L = [f"# {RUN} / {res['run_id']}（设计期 ≤2015，探索；口径 {SPEC.name} sha256 {res['spec_sha256'][:12]}）", ""]
    for x in [None, *SENS_X]:
        key = "main" if x is None else f"rev_x={x}"
        L.append(f"## {'主口径 掉头门槛 1.5·ATR' if x is None else '敏感性 掉头门槛 ' + str(x) + '·ATR'}")
        L.append("")
        L.append("| 品种 | 类 | 事件 | 掉头 | 突破 | 都没有 | 掉头率 | Wilson 下界 | 安慰剂掉头率 | 配对差 | t（描述） | 配对 n | 10日位移(ATR) 真/假 | 上行/下行 均线 掉头 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for sym in (*PRIMARY, *REPLICA):
            s = res["results"][key][sym]["summary"]
            for kind in ("R", "S"):
                v = s[kind]
                rg = v["regime"]
                L.append(f"| {sym}{'（重复核对）' if sym in REPLICA else ''} | {kind} | {v['n']} | {v['reversed']} | {v['broken']} | {v['none']} | "
                         f"{v['rate']} | {v['wilson_lo']} | {v['placebo_rate_mean']} | {v['paired_mean']} | {v['paired_t']} | {v['paired_n']} | "
                         f"{v['ret10_mean']} / {v['placebo_ret10_mean']} | {rg['up']['reversed']}/{rg['up']['n']} · {rg['down']['reversed']}/{rg['down']['n']} |")
        L.append("")
    L.append(f"6 格（GC=F、SI=F、USO × R、S）只作描述，不做显著性判断（见 STATUS.md）。R = 向上时压力（下跌途中第一段横盘），S = 向下时支撑（起涨大阳线实体中段）。")
    L.append("安慰剂 = 同宽、同时刻启用、同来向、平移 ±2·ATR 的假区间。配对差 = 真实成功(0/1) − 其安慰剂平均成功率。")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()
    out = BASE / args.run_id
    if out.exists():
        raise SystemExit(f"{out} 已存在，拒绝覆盖")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    res = {"run": RUN, "run_id": args.run_id, "git_head": head,
           "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
           "code_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                           for p in ("undertow/analyze/directional_zones.py", "scripts/dz_explore.py")},
           "ran_at": datetime.now(timezone.utc).isoformat(), "results": {}}
    for x in [None, *SENS_X]:
        key = "main" if x is None else f"rev_x={x}"
        res["results"][key] = {}
        for sym in (*PRIMARY, *REPLICA):
            r = run_symbol(sym, dz.REV_X if x is None else x)
            r["summary"] = summarize(r["rows"])
            res["results"][key][sym] = r
    out.mkdir(parents=True)
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    (out / "report.md").write_text(report_md(res), "utf-8")
    print(report_md(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
