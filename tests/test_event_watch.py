"""Codex 008 G05 / G10：事件发现的失败分支与只发布本次产物。全部在临时目录/临时 Git 仓库里演练。"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import event_discover as ed      # noqa: E402

ET = ZoneInfo("America/New_York")
TODAY = dt.date(2026, 9, 28)


def _ev(name, t, day=TODAY, imp="high"):
    return NS(name=name, time_et=t, date=day, importance=imp)


def _boom():
    raise RuntimeError("source down")


def _run(now, manual, feed, tmp_path):
    return ed.discover(now, TODAY, load_manual=manual, load_feed=feed, merge=lambda a, b: list(a) + list(b),
                       snapdir=tmp_path)


def test_both_sources_fail_is_failed(tmp_path):
    r = _run(dt.datetime(2026, 9, 28, 8, 45, tzinfo=ET), _boom, _boom, tmp_path)
    assert r["status"] == "failed" and not r["sources"]["manual"]["ok"] and not r["sources"]["feed"]["ok"]


def test_one_source_fails_is_partial_but_still_captures(tmp_path):
    r = _run(dt.datetime(2026, 9, 28, 8, 45, tzinfo=ET), _boom, lambda: [_ev("CPI", "08:30")], tmp_path)
    assert r["status"] == "partial" and [t["label"] for t in r["tasks"]] == ["CPI-after"]


def test_feed_not_covering_today_is_partial(tmp_path):
    r = _run(dt.datetime(2026, 9, 28, 8, 45, tzinfo=ET), lambda: [],
             lambda: [_ev("X", "08:30", day=dt.date(2026, 9, 20))], tmp_path)
    assert r["status"] == "partial"


def test_normal_no_event_is_unchanged(tmp_path):
    r = _run(dt.datetime(2026, 9, 28, 8, 45, tzinfo=ET), lambda: [], lambda: [_ev("Low", "08:30", imp="low")],
             tmp_path)
    assert r["status"] == "unchanged" and r["tasks"] == []


def test_missing_time_is_not_0830(tmp_path):
    r = _run(dt.datetime(2026, 9, 28, 8, 45, tzinfo=ET), lambda: [], lambda: [_ev("Speech", "")], tmp_path)
    assert r["tasks"] == [] and any("time_unknown" in i for i in r["issues"]), "旧版会按 08:30 在此刻捕 after"
    r2 = _run(dt.datetime(2026, 9, 28, 10, 5, tzinfo=ET), lambda: [], lambda: [_ev("Speech", "")], tmp_path)
    assert [t["phase"] for t in r2["tasks"]] == ["postopen"]


def test_afternoon_event_gets_postevent_not_1000(tmp_path):
    ph, unk = ed.plan_phases(dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET), "14:00")
    names = dict(ph)
    assert not unk and "postopen" not in names and names["postevent"].strftime("%H:%M") == "14:30"
    ph2, _ = ed.plan_phases(dt.datetime(2026, 9, 28, 9, 0, tzinfo=ET), "08:30")
    assert dict(ph2)["postopen"].strftime("%H:%M") == "10:00"


# —— 端到端：真实 event_watch.sh 在临时仓库里，两个来源都失败 ——
FAKE = {
    "undertow/__init__.py": "",
    "undertow/core/__init__.py": "",
    "undertow/core/calendar.py": "def load_events():\n    raise RuntimeError('manual down')\n"
                                 "def merge(a, b):\n    return list(a) + list(b)\n",
    "undertow/core/clock.py": "import datetime\ndef market_today():\n    return datetime.date(2026, 9, 28)\n",
    "undertow/collect/__init__.py": "",
    "undertow/collect/faireconomy_cal.py": "class FairEconomyCalSource:\n    def fetch_events(self, use_cache=True):\n"
                                           "        raise RuntimeError('feed down')\n",
}


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def _repo(tmp_path):
    for rel, body in FAKE.items():
        f = tmp_path / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(body)
    shutil.copy(ROOT / "undertow/collect/asof_history.py", tmp_path / "undertow/collect/asof_history.py")
    (tmp_path / "scripts").mkdir()
    for n in ("event_watch.sh", "lib_publish.sh", "event_discover.py"):
        shutil.copy(ROOT / "scripts" / n, tmp_path / "scripts" / n)
    (tmp_path / "data/logs").mkdir(parents=True)
    _git(tmp_path, "init", "-q"); _git(tmp_path, "config", "user.email", "t@t"); _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("x"); _git(tmp_path, "add", "README"); _git(tmp_path, "commit", "-qm", "init")
    notif = tmp_path / "notify.log"
    fake = tmp_path / "fake_osascript"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {notif}\n"); fake.chmod(0o755)
    return notif, fake


@pytest.mark.skipif(shutil.which("zsh") is None, reason="需要 zsh")
def test_event_watch_both_sources_down_end_to_end(tmp_path):
    notif, fake = _repo(tmp_path)
    env = {**os.environ, "PYTHON": sys.executable, "OSASCRIPT": str(fake)}
    r = subprocess.run(["zsh", "scripts/event_watch.sh"], cwd=tmp_path, env=env, capture_output=True, text=True)
    st = json.loads((tmp_path / "data/logs/.status_event_watch.json").read_text())
    assert r.returncode == 1 and st["status"] == "failed"
    assert list((tmp_path / "data/logs").glob(".event_fail_*")), "当日告警标记"
    assert "两个来源都失败" in notif.read_text(), "用户可见告警（通知）"
    assert "不是「没有事件」" in (tmp_path / "data/history/events/watch.log").read_text()


def _pub(tmp_path, env=None):
    cmd = 'source scripts/lib_publish.sh; publish_dir "msg" data/history/events; echo "RC=$?"'
    return subprocess.run(["zsh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "PUBLISH_NO_PUSH": "1", **(env or {})})


@pytest.mark.skipif(shutil.which("zsh") is None, reason="需要 zsh")
def test_publish_does_not_sweep_in_foreign_staged_files(tmp_path):
    _repo(tmp_path)
    (tmp_path / "other.py").write_text("someone else's work"); _git(tmp_path, "add", "other.py")
    ev = tmp_path / "data/history/events"; ev.mkdir(parents=True, exist_ok=True)
    (ev / "2026-09-28_CPI-after.json").write_text("{}")
    r = _pub(tmp_path)
    assert "RC=3" in r.stdout and "PUBLISH_BLOCKED" in r.stdout
    assert _git(tmp_path, "diff", "--cached", "--name-only").stdout.split() == ["other.py"], "他人暂存原样保留"
    assert _git(tmp_path, "log", "--oneline").stdout.count("\n") == 1, "什么都没提交"
    _git(tmp_path, "reset", "-q", "other.py")                   # 他人自己处理完暂存区后
    r2 = _pub(tmp_path)
    assert "RC=0" in r2.stdout
    files = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert files == ["data/history/events/2026-09-28_CPI-after.json"], files
    assert (tmp_path / "other.py").exists() and "other.py" in _git(tmp_path, "status", "--porcelain").stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="需要 zsh")
def test_publish_dirs_multi_and_daily_wiring(tmp_path):
    _repo(tmp_path)
    for rel in ("data/snapshots/a.json", "data/history/b.json", "outside.txt"):
        f = tmp_path / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text("1")
    cmd = 'source scripts/lib_publish.sh; publish_dirs "m" data/snapshots data/history data/reports; echo "RC=$?"'
    r = subprocess.run(["zsh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True,
                       env={**os.environ, "PUBLISH_NO_PUSH": "1"})
    assert "RC=0" in r.stdout
    files = sorted(_git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout.split())
    assert files == ["data/history/b.json", "data/snapshots/a.json"], "目录外的 outside.txt 不得被卷入"
    src = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    tail = src[src.index("source scripts/lib_publish.sh"):]
    assert "publish_dirs" in tail and "\ngit commit" not in tail and "\ngit add" not in tail
