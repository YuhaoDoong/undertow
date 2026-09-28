"""conf-v1 · A：成交密集区 × Order Block（价格层交汇），全新品种 SPY/QQQ/TLT/USO/IWM。

  python3 scripts/conf_a_backtest.py --set design
  python3 scripts/conf_a_backtest.py --set test --approved-by "<谁、何时审过定义>"   # 只运行一次

预登记：docs/prereg/2026-09-28_confluence_v1.md（草案）。检验集在 Codex 审过定义前不运行 —— 新品种的历史只能用一次。
设计集把 2016-01-01 以后的日线截掉再算。只读、纯标准库、不联网。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import smc  # noqa: E402
from undertow.analyze import volume_profile as vp  # noqa: E402

PREREG = ROOT / "docs/prereg/2026-09-28_confluence_v1.md"
OUT = ROOT / "data/backtest/conf_v1"
LOCK = OUT / "A_TEST_RUN.lock"
SPLIT, LAST = "2016-01-01", "2026-09-25"
SYMS = ("SPY", "QQQ", "TLT", "USO", "IWM")
OB_WINDOW = 500
ALPHA = 0.05 / 10
#: 冻结后的预登记 sha256（Codex 018 #12：任意非空 --approved-by 都能通过是不够的）。None = 未冻结 → 检验集拒绝运行。
#: 另：018 #2/#3 指出 A1 继承了 vp-v1 H1 的独立性/对照/触及语义问题，修订协议获审前不得冻结。
FROZEN_SHA: str | None = None


def load(sym):
    rec = json.loads(gzip.decompress((ROOT / f"data/history/inputs/monthly/2026-09/cboehist_{sym}.json.gz").read_bytes()))
    bars = [b for b in vp.clean(rec["raw"]["data"]["data"]) if b["v"] > 0]      # 成交量为 0 的节日残行整行剔除
    return bars, rec["sha256"]


def ob_zones(bars, i):
    """t−500…t−1 的有效 OB（不看未来）。"""
    w = bars[max(0, i - OB_WINDOW):i]
    return smc.order_blocks([b["o"] for b in w], [b["h"] for b in w], [b["l"] for b in w], [b["c"] for b in w],
                            size=smc.INTERNAL_LENGTH, count=smc.OB_COUNT)


def confluent(ev, zones) -> bool:
    want = smc.BULLISH if ev["side"] == "above" else smc.BEARISH
    return any(z.bias == want and min(z.lo, z.hi) <= ev["zhi"] and max(z.lo, z.hi) >= ev["zlo"] for z in zones)


def two_prop(a, b):
    na, nb = len(a), len(b)
    if not na or not nb:
        return {"n_a": na, "n_b": nb, "status": "insufficient"}
    pa, pb = sum(a) / na, sum(b) / nb
    pool = (sum(a) + sum(b)) / (na + nb)
    se = math.sqrt(pool * (1 - pool) * (1 / na + 1 / nb))
    z = (pa - pb) / se if se > 0 else float("nan")
    return {"n_a": na, "n_b": nb, "p_a": pa, "p_b": pb, "diff": pa - pb, "z": z,
            "p_one_sided": vp.norm_sf(z) if z == z else None}


def judge(r):
    if r.get("status") == "insufficient" or min(r["n_a"], r["n_b"]) < vp.MIN_EVENTS:
        return "证据不足（事件数 < 30）"
    if r["p_one_sided"] is not None and r["p_one_sided"] < ALPHA:
        return "支持" if r["diff"] >= 0.05 else "统计为正、未达经济门槛"
    return "不支持" if r["diff"] <= 0 else "未决（方向为正但未显著）"


def run(which):
    res = {"set": which, "ran_at": datetime.now(timezone.utc).isoformat(), "alpha": ALPHA,
           "prereg_sha": hashlib.sha256(PREREG.read_bytes()).hexdigest(), "results": {}}
    for sym in SYMS:
        bars, dsha = load(sym)
        bars = [b for b in bars if (b["date"] < SPLIT if which == "design" else b["date"] <= LAST)]
        lo = 0 if which == "design" else next(i for i, b in enumerate(bars) if b["date"] >= SPLIT)
        lo = max(lo, OB_WINDOW)
        sc = vp.scan(bars, lo, len(bars))
        ev = [e for e in vp.nonoverlap(sc["h1_ev"]) if e["res"] in ("rebound", "through")]
        ctrl = [e for e in sc["h1_ctrl"] if e["res"] in ("rebound", "through")]
        for e in ev:
            e["conf"] = confluent(e, ob_zones(bars, e["i"]))
        reb = lambda xs: [x["res"] == "rebound" for x in xs]
        a1 = two_prop(reb(ev), reb(ctrl))
        a2 = two_prop(reb([e for e in ev if e["conf"]]), reb([e for e in ev if not e["conf"]]))
        res["results"][sym] = {"data_sha": dsha[:16], "first": bars[lo]["date"], "last": bars[-1]["date"],
                               "A1_hvn_vs_ctrl": {**a1, "verdict": judge(a1)},
                               "A2_conf_vs_hvn_only": {**a2, "verdict": judge(a2)}}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("design", "test"), required=True)
    ap.add_argument("--approved-by")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.set == "test":
        if not a.approved_by:
            sys.exit("检验集需先由 Codex 审过定义：--approved-by 写明审阅记录。新品种历史只能用一次。")
        sha = hashlib.sha256(PREREG.read_bytes()).hexdigest()
        if FROZEN_SHA is None or sha != FROZEN_SHA:
            sys.exit(f"预登记未冻结或已改动（当前 {sha[:12]}，冻结 {FROZEN_SHA and FROZEN_SHA[:12]}）：检验集拒绝运行。")
        try:                                                  # 先原子占锁再计算：崩溃/并发不会重复消费检验集
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            sys.exit(f"A 检验集已运行过或正在运行：{LOCK.read_text().strip()}")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()} started prereg {sha[:12]} 审阅 {a.approved_by}\n")
    res = run(a.set)
    (OUT / f"A_{a.set}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), "utf-8")
    if a.set == "test":
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        with open(LOCK, "a", encoding="utf-8") as fh:
            fh.write(f"{res['ran_at']} finished HEAD {head} prereg {res['prereg_sha'][:12]} 审阅 {a.approved_by}\n")
    for sym, r in res["results"].items():
        a1, a2 = r["A1_hvn_vs_ctrl"], r["A2_conf_vs_hvn_only"]
        f = lambda x: (f"{x['p_a']:.1%}（n={x['n_a']}）vs {x['p_b']:.1%}（n={x['n_b']}），差 {x['diff']*100:+.1f}pp，"
                       f"p={x['p_one_sided']:.3f} → {x['verdict']}") if "diff" in x else str(x)
        print(f"{sym}（{r['first']}~{r['last']}）\n  A1 密集区 vs 对照：{f(a1)}\n  A2 交汇 vs 仅密集区：{f(a2)}")


if __name__ == "__main__":
    main()
