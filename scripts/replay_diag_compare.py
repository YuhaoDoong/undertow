"""回放诊断两次 run 的逐笔对照（Codex 047 A：引擎 r2 修复前后的实际影响）。

  python3 scripts/replay_diag_compare.py --old run3 --new run4

读私有 data/soul/structure_labels/diag-v1/<run>/private_detail.json；逐笔按 (版本, 段, 决定根, 方向) 对齐。
对照只写进新 run 的私有目录（compare_<old>.json），只含计数与变化分布 —— 不含逐点判断与备注，可以给 Codex。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.replay_diag import OUT, _dist  # noqa: E402


def _key(t: dict) -> tuple:
    return (t["seg"], t["decided_at"], t["dir"])


def _dkey(d: dict) -> tuple | None:
    if "decided_at" in d:
        return (d.get("seg"), d["decided_at"], d["dir"])
    return None


def compare(old: dict, new: dict) -> dict:
    res = {}
    for v in ("v1", "v2"):
        o = {_key(t): t for t in old[v]["trades"]}
        n = {_key(t): t for t in new[v]["trades"]}
        both = sorted(set(o) & set(n))
        ch_exit = [k for k in both if (o[k]["exit_i"], o[k]["exit_px"], o[k]["exit_reason"]) != (n[k]["exit_i"], n[k]["exit_px"], n[k]["exit_reason"])]
        d_atr = [n[k]["atr_units"] - o[k]["atr_units"] for k in ch_exit if n[k]["atr_units"] is not None and o[k]["atr_units"] is not None]
        od = [x for x in old[v]["delays"]]
        nd = {_dkey(x): x for x in new[v]["delays"] if _dkey(x)}
        # 旧延迟行没有决定根：按交易顺序对齐（旧 delays 与旧 trades 一一对应、同序）
        od_keyed = {_key(t): d for t, d in zip(old[v]["trades"], od)}
        dk = sorted(set(od_keyed) & set(nd))
        def comp(k, f):
            a, b = od_keyed[k].get(f), nd[k].get(f)
            return None if a is None or b is None else b - a
        res[v] = {"trades_old": len(o), "trades_new": len(n), "added": len(set(n) - set(o)), "removed": len(set(o) - set(n)),
                  "same_trade_exit_changed": len(ch_exit),
                  "exit_reason_changes": sorted({f"{o[k]['exit_reason']}→{n[k]['exit_reason']}" for k in ch_exit}),
                  "atr_units_change": _dist(d_atr),
                  "delay_rows_compared": len(dk),
                  "delay_ref_point_changed": sum(1 for k in dk if (od_keyed[k].get("extreme_to_exec") is None) != (nd[k]["ref"] is None)
                                                 or (nd[k]["ref"] and od_keyed[k].get("extreme_to_exec") is not None
                                                     and nd[k]["entry_i"] - nd[k]["ref"]["point_i"] != od_keyed[k]["extreme_to_exec"])),
                  "delay_ref_lost": sum(1 for k in dk if od_keyed[k].get("ref") != "none" and nd[k]["ref"] is None),
                  "extreme_to_exec_change": _dist([comp(k, "extreme_to_exec") for k in dk]),
                  "extreme_to_label_change": _dist([comp(k, "extreme_to_label") for k in dk]),
                  "label_to_decision_change": _dist([comp(k, "label_to_decision") for k in dk])}
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    a = ap.parse_args()
    dst = OUT / a.new / f"compare_{a.old}.json"
    if dst.exists():
        raise SystemExit(f"{dst} 已存在：不覆盖")
    old = json.loads((OUT / a.old / "private_detail.json").read_text("utf-8"))
    new = json.loads((OUT / a.new / "private_detail.json").read_text("utf-8"))
    r = {"old": a.old, "new": a.new, "by_version": compare(old, new),
         "note": "逐笔按 (段, 决定根, 方向) 对齐；只含计数与变化分布"}
    dst.write_text(json.dumps(r, ensure_ascii=False, indent=1), "utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
