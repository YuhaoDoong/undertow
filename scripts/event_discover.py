"""事件捕捉的任务发现（event_watch.sh 调用；Codex 008 G05）。

原错：手工日历异常被吞成 []，feed 异常再回退到这个空列表；没有高影响事件时 SystemExit 默认成功。
两个来源同时失败 → 退出码 0、无输出 → 外层 DISCOVER_RC 检查接不到，「拿不到日历」被当成「今天没事件」。
另：事件缺时间默认 08:30；盘中/下午事件的 postopen 固定在 10:00，发生在事件【之前】却被称作事件后反应。

状态（写 --status-file，原子 JSON）：
  failed    两个来源都失败 → 退出码 1，外层告警
  partial   一个来源失败，或 feed 覆盖期不含今天 → 仍输出可用任务，外层当日告警一次
  tasks     有待捕任务
  unchanged 两源正常且此刻无任务（正常，不告警）
阶段：盘前事件 before(−30) / after(+10) / postopen（开盘 +30，=开盘观察）；
      09:30 及以后的事件 before / after / postevent（事件 +30，盘中真正的事件后反应）；
      缺时间的事件只做 postopen（开盘观察），并记为 time_unknown，不猜 08:30。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

WINDOW = 12            # 分钟：到点后 [target, target+WINDOW] 内触发（配合每 10 分钟唤醒）
OPEN = (9, 30)


def _t(now, hh, mm):
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def plan_phases(now, time_et: str | None):
    """(阶段, 目标时刻) 列表 + 该事件是否缺时间。"""
    open_t = _t(now, *OPEN)
    try:
        hh, mm = (int(x) for x in (time_et or "").strip().split(":")[:2])
    except ValueError:
        return [("postopen", open_t + dt.timedelta(minutes=30))], True
    ev = _t(now, hh, mm)
    out = [("before", ev - dt.timedelta(minutes=30)), ("after", ev + dt.timedelta(minutes=10))]
    if (hh, mm) < OPEN:
        out.append(("postopen", open_t + dt.timedelta(minutes=30)))
    else:
        out.append(("postevent", ev + dt.timedelta(minutes=30)))
    return out, False


def discover(now, today, *, load_manual, load_feed, merge, snapdir: Path) -> dict:
    sources = {}
    manual = feed = None
    try:
        manual = load_manual(); sources["manual"] = {"ok": True, "n": len(manual)}
    except Exception as e:
        sources["manual"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    try:
        feed = load_feed()
        ds = sorted(e.date for e in feed)
        cov = [ds[0].isoformat(), ds[-1].isoformat()] if ds else None
        sources["feed"] = {"ok": True, "n": len(feed), "coverage": cov,
                           "covers_today": bool(ds) and ds[0] <= today <= ds[-1]}
    except Exception as e:
        sources["feed"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    if manual is None and feed is None:
        return {"status": "failed", "tasks": [], "sources": sources, "issues": ["两个事件来源都失败"]}
    events = merge(manual or [], feed or [])
    issues = []
    partial = manual is None or feed is None or not sources["feed"].get("covers_today", False)
    if partial:
        issues.append("事件来源不完整：" + "；".join(f"{k} {'失败' if not v['ok'] else '未覆盖今天'}"
                                               for k, v in sources.items()
                                               if not v["ok"] or (k == "feed" and not v.get("covers_today"))))
    highs = [e for e in events if e.date == today and e.importance == "high"]
    buckets = {}
    for e in highs:
        phases, unknown = plan_phases(now, e.time_et)
        if unknown:
            issues.append(f"time_unknown: {e.name}（只做开盘观察，不猜 08:30）")
        for phase, target in phases:
            lag = (now - target).total_seconds() / 60.0
            if 0 <= lag <= WINDOW:                     # 到点之后才捕，不提前
                buckets.setdefault((target.strftime("%H%M"), phase), []).append(e.name)
    tasks = []
    for (hhmm, phase), names in sorted(buckets.items()):
        token = "".join(ch for ch in names[0] if ch.isalnum())[:14] or "EVENT"
        if len(names) > 1:
            token += f"+{len(names) - 1}"
        label = f"{token}-{phase}"
        if (snapdir / f"{today.isoformat()}_{label}.json").exists():
            continue                                    # 幂等
        tasks.append({"label": label, "names": " / ".join(names), "phase": phase})
    status = "partial" if partial else ("tasks" if tasks else "unchanged")
    return {"status": status, "tasks": tasks, "sources": sources, "issues": issues, "n_high_today": len(highs)}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--status-file", required=True)
    a = ap.parse_args(argv)
    sys.path.insert(0, ".")
    from zoneinfo import ZoneInfo
    from undertow.collect.asof_history import atomic_write_json
    from undertow.collect.faireconomy_cal import FairEconomyCalSource
    from undertow.core.calendar import load_events, merge
    from undertow.core.clock import market_today
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    res = discover(now, market_today(), load_manual=load_events,
                   load_feed=lambda: FairEconomyCalSource().fetch_events(use_cache=True),
                   merge=merge, snapdir=Path("data/history/events"))
    res.update({"schema": 1, "at": now.isoformat()})
    atomic_write_json(Path(a.status_file), res)
    for t in res["tasks"]:
        print(f"{t['label']}|{t['names']}|{t['phase']}")
    return 1 if res["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
