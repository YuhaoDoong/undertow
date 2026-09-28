"""vp-v1 回测运行器（预登记 docs/prereg/2026-09-28_volume_profile_v1.md，已冻结于 6159826）。

  python3 scripts/vp_backtest.py --set design        # 2004–2015 设计集（可反复运行、可看）
  python3 scripts/vp_backtest.py --set test          # 2016-01-01 以后的检验集：只运行一次

守卫：
- 设计集把 2016-01-01 及以后的日线【截掉】再计算 —— 结果观察窗（最多 10 日）也不得看到检验集。
- 检验集运行前核对预登记文件 sha256 == FROZEN_SHA；运行后写锁文件 TEST_RUN.lock（时刻、代码 sha），
  再次运行需 --rerun-reason 并追加记录（不静默覆盖首次结果）。
- 数据只读存档：data/history/inputs/monthly/2026-09/cboehist_{GLD,SLV}.json.gz（带 sha256）。
只读、纯标准库、不联网。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import volume_profile as vp  # noqa: E402

PREREG = ROOT / "docs/prereg/2026-09-28_volume_profile_v1.md"
FROZEN_SHA = "dbba188efd5b14b51ce5892ecb36ef2c4bef5aa7985fb432fbc7753576efd839"
OUT = ROOT / "data/backtest/vp_v1"
LOCK = OUT / "TEST_RUN.lock"
SPLIT = "2016-01-01"
LAST = "2026-09-25"
SRC = {"GLD": ("data/history/inputs/monthly/2026-09/cboehist_GLD.json.gz", None),
       "SLV": ("data/history/inputs/monthly/2026-09/cboehist_SLV.json.gz", "2008-07-24")}   # SLV 拆股前数据错误


def _load(sym):
    path, start = SRC[sym]
    rec = json.loads(gzip.decompress((ROOT / path).read_bytes()))
    return vp.clean(rec["raw"]["data"]["data"], start=start), rec["sha256"]


def _layers(raw, flags, key):
    out = {}
    for x in raw:
        lab = flags.get(x["date"], "未知")
        out.setdefault(lab, []).append(x)
    return {k: key(v) for k, v in out.items()}


def run(which: str) -> dict:
    res = {"prereg_sha": hashlib.sha256(PREREG.read_bytes()).hexdigest(), "set": which,
           "code_sha": hashlib.sha256((ROOT / "undertow/analyze/volume_profile.py").read_bytes()).hexdigest()[:16],
           "ran_at": datetime.now(timezone.utc).isoformat(), "alpha": vp.ALPHA, "thresholds": vp.THRESH, "results": {}}
    for sym in ("GLD", "SLV"):
        bars, data_sha = _load(sym)
        if which == "design":
            bars = [b for b in bars if b["date"] < SPLIT]
            lo, hi = 0, len(bars)
        else:
            bars = [b for b in bars if b["date"] <= LAST]
            lo = next(i for i, b in enumerate(bars) if b["date"] >= SPLIT)
            hi = len(bars)
        sc = vp.scan(bars, lo, hi)
        h1, h2, h3 = vp.test_h1(sc["h1_ev"], sc["h1_ctrl"]), vp.test_h2(sc["h2"]), vp.test_h3(sc["h3"], sc["ret5"])
        flags = sc["sma200"]
        layer_h1 = _layers([x for x in vp.nonoverlap(sc["h1_ev"]) if x["res"] in ("rebound", "through")], flags,
                           lambda v: {"n": len(v), "rebound": sum(x["res"] == "rebound" for x in v) / len(v)})
        layer_h3 = _layers(vp.nonoverlap(sc["h3"]), flags,
                           lambda v: {"n": len(v), "mean_5d": sum(x["r"] for x in v) / len(v)})
        res["results"][sym] = {
            "data_sha": data_sha[:16], "first_date": bars[lo]["date"], "last_date": bars[hi - 1]["date"],
            "H1": {**h1, "verdict": vp.verdict("H1", h1), "by_regime": layer_h1},
            "H2": {**h2, "verdict": vp.verdict("H2", h2)},
            "H3": {**h3, "verdict": vp.verdict("H3", h3), "by_regime": layer_h3}}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("design", "test"), required=True)
    ap.add_argument("--rerun-reason")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.set == "test":
        sha = hashlib.sha256(PREREG.read_bytes()).hexdigest()
        if sha != FROZEN_SHA:
            sys.exit(f"预登记文件已改动（{sha[:12]} ≠ 冻结 {FROZEN_SHA[:12]}）：检验集拒绝运行。要改就发新版本。")
        if LOCK.exists() and not a.rerun_reason:
            sys.exit(f"检验集已运行过（{LOCK.read_text().strip()}）。再次运行需 --rerun-reason，并会追加记录。")
    res = run(a.set)
    name = a.set if not (a.set == "test" and LOCK.exists()) else f"test_rerun_{int(datetime.now().timestamp())}"
    (OUT / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), "utf-8")
    if a.set == "test":
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
        with open(LOCK, "a", encoding="utf-8") as fh:
            fh.write(f"{res['ran_at']} HEAD {head} code {res['code_sha']} prereg {res['prereg_sha'][:12]}"
                     + (f" 重跑原因：{a.rerun_reason}" if a.rerun_reason else "") + "\n")
    for sym, r in res["results"].items():
        print(f"\n== {sym}（{r['first_date']} ~ {r['last_date']}）")
        h1, h2, h3 = r["H1"], r["H2"], r["H3"]
        if "diff" in h1:
            print(f"H1 密集区事件 {h1['n_events']}（未决 {h1['n_undecided_events']}）先反弹 {h1['rebound_events']:.1%} "
                  f"vs 对照 {h1['n_ctrl']} 日 {h1['rebound_ctrl']:.1%}，差 {h1['diff']*100:+.1f}pp，单侧 p={h1['p_one_sided']:.4f} → {h1['verdict']}")
            print(f"   分层：{h1['by_regime']}")
        else:
            print(f"H1 {h1}")
        if "ratio" in h2:
            print(f"H2 真空 {h2['n_lvn']} 日 / 密集 {h2['n_hvn']} 日 / 其他 {h2['n_mid']}：波动比 {h2['ratio']:.3f}，"
                  f"单侧下界 {h2['lower_one_sided']:.3f} → {h2['verdict']}")
        if "mean_excess" in h3:
            print(f"H3 事件 {h3['n_events']}：5 日超额 {h3['mean_excess']*100:+.2f}%，t={h3['t']:.2f}，单侧 p={h3['p_one_sided']:.4f}，"
                  f"胜率 {h3['win_rate']:.0%} vs 基准上涨率 {h3['base_up_rate']:.0%} → {h3['verdict']}")
            print(f"   分层：{h3['by_regime']}")
        else:
            print(f"H3 {h3} → {h3.get('verdict')}")
    print(f"\n判定：单侧 α={vp.ALPHA:.4f}（6 项 Bonferroni）+ 经济门槛 {vp.THRESH}；事件数 < {vp.MIN_EVENTS} 判证据不足。")


if __name__ == "__main__":
    main()
