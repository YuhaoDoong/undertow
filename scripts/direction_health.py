"""方向台账族 D 采集健康检查（Codex 022：不读收益）。

  python3 scripts/direction_health.py                    # 全部 session
  python3 scripts/direction_health.py --session 2026-09-29

只核对采集与资格，**从不读取 outcome / scored_at / outcome_history**：
- 每条正式记录：两份快照 blob 能否从 cas 恢复、恢复出的字节 sha 前缀与记录的 *_sha 一致；
  抓取时刻 ≤ 记录时刻 < 决策截止；identity_ok / quality_ok；status 分布。
- attempts：各 attempt_status 计数；not_ready / missing 的身份问题分布（留痕是否齐）。
- levels/：H3 水平正式记录的同类检查。
只读、纯标准库、不联网。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FORBIDDEN = ("outcome", "scored_at", "outcome_history")


def _rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text("utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out.append({k: v for k, v in r.items() if k not in FORBIDDEN})     # 收益字段在这里就丢掉
    return out


def check_formal(r: dict) -> list[str]:
    from undertow.collect import cas
    from undertow.dirledger_cli import restore_row_inputs
    probs = []
    if r.get("status") != "eligible":
        return probs
    if not (r.get("identity_ok") and r.get("quality_ok") in (True, None)):
        probs.append("eligible 但身份/质量标记不齐")
    cut = datetime.fromisoformat(r["decision_cutoff"])
    rec = datetime.fromisoformat(r["recorded_at"])
    if not rec < cut:
        probs.append("记录时刻不早于截止")
    for name in ("prev", "curr"):
        ca = r.get(f"{name}_captured_at")
        if ca is None or datetime.fromisoformat(ca) > rec:
            probs.append(f"{name} 抓取时刻缺失或晚于记录")
    try:
        got = restore_row_inputs(r)
    except Exception as e:
        return probs + [f"恢复失败 {type(e).__name__}: {e}"]
    for name in ("prev", "curr"):
        if got.get(name) is None:
            probs.append(f"{name} 快照原文无法恢复")
            continue
        raw = cas.get_blob(got[name]["sha256"])
        if r.get(f"{name}_sha") and hashlib.sha256(raw).hexdigest()[:16] != r[f"{name}_sha"]:
            probs.append(f"{name} 恢复字节与记录 sha 不符")
    return probs


def main():
    from undertow.analyze import conviction as cv
    from undertow.analyze import direction_stats as dst
    from undertow.analyze import skew_reading as skr
    from undertow.dirledger_cli import DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    a = ap.parse_args()
    bad_total = 0
    for rv in (skr.RULE["version"], cv.RULE["version"]):
        base = DIR / rv
        print(f"== {rv}（正式起点 {dst.FAMILY_D_START}；早于起点的 session 标「起点前」）")
        for kind in ("prospective", "levels"):
            for p in sorted((base / kind).glob("*.jsonl")):
                rows = [r for r in _rows(p) if not a.session or r["session"] == a.session]
                for r in rows:
                    pre = "起点前 " if r["session"] < dst.FAMILY_D_START.isoformat() else ""
                    probs = check_formal(r) if kind == "prospective" else []
                    if kind == "levels" and r.get("status") == "eligible":
                        lv = r.get("level") or {}
                        if lv.get("skew25_pp") is None or lv.get("skew10_pp") is None:
                            probs.append("水平缺失却为 eligible")
                    bad_total += bool(probs)
                    print(f"  {kind:11s} {p.stem:6s} {r['session']} {pre}{r.get('status')} "
                          + ("✅" if not probs else "⚠️ " + "；".join(probs)))
        for p in sorted((base / "attempts").glob("*.jsonl")):
            rows = [r for r in _rows(p) if not a.session or r["session"] == a.session]
            st = Counter(r.get("attempt_status") for r in rows)
            why = Counter(x for r in rows if r.get("attempt_status") in ("not_ready", "missing_at_cutoff")
                          for x in (r.get("identity_problems") or ["quality"]))
            print(f"  attempts    {p.stem:6s} {dict(st)}" + (f"  未就绪原因 {dict(why)}" if why else ""))
    print(f"结论：{'有异常 ' + str(bad_total) + ' 条' if bad_total else '正式记录全部通过'}（未读取任何收益字段）")
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
