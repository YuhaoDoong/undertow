"""W03 统一重算入口：同一输入 + 同一代码 → 可复现的版本化研究产物。

Codex 验收契约（04_VALIDATION §5）要求：记录运行前后输入 hash；新 schema 新路径，旧产物保留；
每个结果带 code_version / input_manifest / run_command / generated_at / 局限。
本脚本只编排、不计算 —— 计算全在各 step 脚本里（单一实现）。

用法：python3 scripts/research_rerun.py --tag 20260926 [--only step4,step5]
产物：data/history/wall_spread/rerun_<tag>/*.json + manifest.json + stdout_*.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

STEPS = {
    "step4":         ["scripts/step4_filters.py", "--emit", "--output", "{out}/filter_test.json"],
    "step4_5pct":    ["scripts/step4_filters.py", "--buffer", "5%", "--sections", "BE"],
    "step4_sweep":   ["scripts/step4_sweep.py", "--emit", "--output", "{out}/filter_sweep.json"],
    "step5_max":     ["scripts/step5_wall_hold.py", "--emit", "--mode", "max", "--output", "{out}/wall_hold_max.json"],
    "step5_nearest": ["scripts/step5_wall_hold.py", "--emit", "--mode", "nearest", "--output", "{out}/wall_hold_nearest.json"],
    "step6":         ["scripts/step6_vrp.py", "--emit", "--output", "{out}/vrp_states.json"],
    "step7":         ["scripts/step7_atr_filter.py"],
    "step8":         ["scripts/step8_direction_x_wall.py", "--emit", "--output", "{out}/direction_x_wall.json"],
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def input_manifest() -> dict:
    """全部研究输入的 hash：日线/波动率缓存、期权快照、信号台账。"""
    groups = {
        "price_cache": sorted((ROOT / "data/cache").glob("cboehist_*.json")),
        "vol_cache": sorted((ROOT / "data/cache").glob("cboevol_*")),
        "signal_ledger": sorted((ROOT / "data/history/signals").glob("*.json")),
    }
    out = {k: {str(p.relative_to(ROOT)): sha(p) for p in v} for k, v in groups.items()}
    snaps = sorted((ROOT / "data/snapshots/options").glob("*/*.json.gz"))
    h = hashlib.sha256()
    for p in snaps:
        h.update(str(p.relative_to(ROOT)).encode()); h.update(sha(p).encode())
    out["options_snapshots"] = {"n_files": len(snaps), "combined_sha256": h.hexdigest(),
                                "last": str(snaps[-1].relative_to(ROOT)) if snaps else None}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--only", default="", help="逗号分隔的步骤名；默认全部")
    a = ap.parse_args()
    out = ROOT / "data/history/wall_spread" / f"rerun_{a.tag}"
    out.mkdir(parents=True, exist_ok=True)
    code = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "undertow", "scripts"],
                           capture_output=True, text=True, cwd=ROOT).stdout.strip()
    before = input_manifest()
    runs = {}
    steps = [s for s in STEPS if not a.only or s in a.only.split(",")]
    for name in steps:
        cmd = [PY] + [x.format(out=out) for x in STEPS[name]]
        t0 = datetime.now(timezone.utc)
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        (out / f"stdout_{name}.txt").write_text(r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""), "utf-8")
        runs[name] = {"cmd": " ".join(cmd[1:]), "rc": r.returncode, "started_at": t0.isoformat(),
                      "seconds": round((datetime.now(timezone.utc) - t0).total_seconds(), 1)}
        print(f"{name:14s} rc={r.returncode}  {runs[name]['seconds']}s")
    after = input_manifest()
    manifest = {
        "schema": 1, "tag": a.tag, "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_version": code, "code_dirty": bool(dirty), "interpreter": PY,
        "inputs_before": before, "inputs_changed_during_run": before != after,
        "runs": runs,
        "limitations": [
            "全部结果为研究模型：价格突破率、IV−RV 点差、快照盘口估价；不是可成交收益。",
            "期权快照仅约 60 个可交易日、单一样本期；个股快照仅数日。",
            "跨品种不合并；簇复现是探索佐证，不是独立重复。",
            "输入若在运行中变化（自动任务），本次结果作废需重跑（见 inputs_changed_during_run）。",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), "utf-8")
    print(f"manifest → {out/'manifest.json'}  code={code[:7]} dirty={bool(dirty)} inputs_changed={before != after}")
    return 0 if all(r["rc"] == 0 for r in runs.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
