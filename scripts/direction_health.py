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


def check_formal(r: dict, *, post_start: bool, level: bool = False) -> list[str]:
    """单条正式记录的采集核查（不读收益）。level=True：H3 水平台账，只有当日一份快照。"""
    from undertow.collect import cas
    from undertow.dirledger_cli import restore_row_inputs
    probs = []
    if r.get("status") != "eligible":
        return probs
    if not r.get("identity_ok"):
        probs.append("eligible 但 identity_ok 不为真")
    if post_start and r.get("quality_ok") is not True:
        probs.append("起点后 quality_ok 必须为 True")              # Codex 023：None 只允许出现在起点前的旧行
    cut = datetime.fromisoformat(r["decision_cutoff"])
    rec = datetime.fromisoformat(r["recorded_at"])
    if not rec < cut:
        probs.append("记录时刻不早于截止")
    names = ("curr",) if level else ("prev", "curr")
    for name in names:
        ca = r.get(f"{name}_captured_at")
        if ca is None or datetime.fromisoformat(ca) > rec:
            probs.append(f"{name} 抓取时刻缺失或晚于记录")
    try:
        got = restore_row_inputs(r)
    except Exception as e:
        return probs + [f"恢复失败 {type(e).__name__}: {e}"]
    for name in names:
        if got.get(name) is None:
            probs.append(f"{name} 快照原文无法恢复")
            continue
        raw = cas.get_blob(got[name]["sha256"])
        if r.get(f"{name}_sha") and hashlib.sha256(raw).hexdigest()[:16] != r[f"{name}_sha"]:
            probs.append(f"{name} 恢复字节与记录 sha 不符")
    if level:
        lv = r.get("level") or {}
        if lv.get("skew25_pp") is None or lv.get("skew10_pp") is None:
            probs.append("水平缺失却为 eligible")
    return probs


def session_status(rows_by_inst: dict, expected: tuple, session: str, *, start: str, now: datetime,
                   cutoff: datetime, post_start: bool, level: bool = False) -> tuple[str, dict]:
    """某 (规则版本, 交易日, 台账) 的状态：not_started / pending / complete / partial / missing。
    每个应有品种：ok / bad(原因) / missing_at_cutoff / absent。空目录绝不算通过（Codex 023）。"""
    if session < start:
        return "not_started", {}
    per = {}
    for inst in expected:
        r = rows_by_inst.get(inst)
        if r is None:
            per[inst] = "absent"
        elif r.get("status") == "missing_at_cutoff":
            per[inst] = "missing_at_cutoff"
        elif r.get("status") == "eligible":
            probs = check_formal(r, post_start=post_start, level=level)
            per[inst] = "ok" if not probs else "bad：" + "；".join(probs)
        else:
            per[inst] = f"other:{r.get('status')}"
    n_ok = sum(v == "ok" for v in per.values())
    if n_ok == len(expected):
        return "complete", per
    if now < cutoff:
        return "pending", per                                     # 截止前未齐：待定，不是缺失
    return ("partial" if n_ok else "missing"), per


def main():
    from datetime import date, timezone
    from undertow.analyze import conviction as cv
    from undertow.analyze import direction_stats as dst
    from undertow.analyze import skew_reading as skr
    from undertow.core import market_calendar as mc
    from undertow.core.clock import market_today
    from undertow.dirledger_cli import CONVICTION_INSTRUMENTS, DIR, open_time
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="只查某个交易日（默认：正式起点至今天的全部交易日）")
    ap.add_argument("--root", help="台账根目录（测试用）")
    a = ap.parse_args()
    base_dir = Path(a.root) if a.root else DIR
    now = datetime.now(timezone.utc)
    start = dst.FAMILY_D_START.isoformat()
    if a.session:
        sessions = [a.session]
    else:
        end = market_today()
        sessions = [d.isoformat() for d in (mc.trading_days(dst.FAMILY_D_START, end) or [])] if end >= dst.FAMILY_D_START else []
        if not sessions:
            print(f"正式起点 {start} 尚未到（今天 {end}）：not_started —— 不是「通过」。")
            return 0
    books = [(skr.RULE["version"], skr.RULE["instruments"], ("prospective",)),
             (cv.RULE["version"], CONVICTION_INSTRUMENTS, ("prospective", "levels"))]
    tally = Counter()
    for rv, expected, kinds in books:
        print(f"== {rv}（正式起点 {start}；应有品种 {len(expected)}）")
        for kind in kinds:
            by_inst = {}
            for p in sorted((base_dir / rv / kind).glob("*.jsonl")):
                for r in _rows(p):
                    by_inst.setdefault(r["session"], {})[p.stem] = r
            for sess in sessions:
                d = date.fromisoformat(sess)
                st, per = session_status(by_inst.get(sess, {}), tuple(expected), sess, start=start, now=now,
                                         cutoff=open_time(d), post_start=sess >= start, level=(kind == "levels"))
                tally[st] += 1
                n = Counter("ok" if v == "ok" else ("bad" if v.startswith("bad") else v) for v in per.values())
                print(f"  {kind:11s} {sess} {st:11s} 应有 {len(expected)}、合格 {n.get('ok', 0)}、异常 {n.get('bad', 0)}、"
                      f"截止缺失 {n.get('missing_at_cutoff', 0)}、未见 {n.get('absent', 0)}")
                for inst, v in per.items():
                    if v != "ok":
                        print(f"      {inst}: {v}")
        for p in sorted((base_dir / rv / "attempts").glob("*.jsonl")):
            rows = [r for r in _rows(p) if r["session"] in sessions]
            if not rows:
                continue
            st = Counter(r.get("attempt_status") for r in rows)
            why = Counter(x for r in rows if r.get("attempt_status") in ("not_ready", "missing_at_cutoff")
                          for x in (r.get("identity_problems") or ["quality"]))
            print(f"  attempts    {p.stem:6s} {dict(st)}" + (f"  未就绪原因 {dict(why)}" if why else ""))
    bad = tally["partial"] + tally["missing"]
    print(f"汇总：{dict(tally)}（未读取任何收益字段）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
