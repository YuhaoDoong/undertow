"""桌面通知去重（用户 2026-11-06：daily 每 15 分钟重跑，把同一条告警弹十几遍）。
规矩：同一美东日同一内容只弹一次；只差运行时刻也算同一条；内容变了照弹；第二天照弹；去重出错照弹（不漏）。"""
import os
import subprocess
import sys

from undertow import notify_once as no


def _env(tmp_path, monkeypatch):
    log = tmp_path / "shown.log"
    fake = tmp_path / "fake_osascript"
    fake.write_text(f'#!/bin/sh\necho "$2" >> "{log}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("NOTIFY_ONCE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NOTIFY_ONCE_OSASCRIPT", str(fake))
    return log


def _shown(log):
    return log.read_text().splitlines() if log.exists() else []


def test_same_content_shown_once_per_day_even_if_run_time_differs(tmp_path, monkeypatch):
    log = _env(tmp_path, monkeypatch)
    assert no.push("⚠️ 前瞻台账未写入（ET 2026-11-06 01:28）", "spy:LedgerConflictError")
    assert not no.push("⚠️ 前瞻台账未写入（ET 2026-11-06 01:52）", "spy:LedgerConflictError")
    assert not no.push("⚠️ 前瞻台账未写入（ET 2026-11-06 05:40）", "spy:LedgerConflictError")
    assert len(_shown(log)) == 1


def test_changed_content_or_new_day_is_shown_again(tmp_path, monkeypatch):
    log = _env(tmp_path, monkeypatch)
    assert no.claim("t", "GLD ⚡极强看跌（17.8×）", et_date="2026-11-06")
    assert no.claim("t", "GLD ⚡极强看跌（18.0×）", et_date="2026-11-06")      # 数字变了 = 新内容
    assert not no.claim("t", "GLD ⚡极强看跌（17.8×）", et_date="2026-11-06")
    assert no.claim("t", "GLD ⚡极强看跌（17.8×）", et_date="2026-11-07")      # 第二天照弹


def test_broken_state_still_shows(tmp_path, monkeypatch):
    log = _env(tmp_path, monkeypatch)
    blocker = tmp_path / "state"
    blocker.write_text("不是目录")                                         # 状态目录建不出来
    assert no.push("⚠️ x", "y") and no.push("⚠️ x", "y")                    # 两次都弹：宁可重复不能漏
    assert len(_shown(log)) == 2
    r = subprocess.run([sys.executable, "-m", "undertow.notify_once", "a", "b"], env=dict(os.environ),
                       capture_output=True, text=True)
    assert r.returncode != 0                                                # shell 侧据此走 osascript 兜底


def test_cli_entry_dedupes(tmp_path, monkeypatch):
    log = _env(tmp_path, monkeypatch)
    for _ in range(3):
        r = subprocess.run([sys.executable, "-m", "undertow.notify_once", "🔎 资金流异常观察", "gold ⚡极强看跌", "Glass", "副标题"],
                           env=dict(os.environ), capture_output=True, text=True)
        assert r.returncode == 0
    assert len(_shown(log)) == 1


def test_daily_script_routes_popups_through_dedupe_but_keeps_failure_file():
    s = open("scripts/daily_update.sh", encoding="utf-8").read()
    body = s[s.index("alert() {"):s.index("notify() { alert")]
    assert ">> \"$f\"" in body                                              # 兜底文件照常每次追加
    assert "undertow.notify_once" in body and "|| /usr/bin/osascript" in body
    assert s.count("undertow.notify_once") == 2
