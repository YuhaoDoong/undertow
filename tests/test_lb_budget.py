"""长桥调用跨进程预算（Codex 033 A4）：并发上限、速率上限、低优先级留余量、超时转 TimeoutExpired、限频错误计数、持锁进程退出即释放。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.collect import lb_budget as lb  # noqa: E402


@pytest.fixture
def bdir(tmp_path, monkeypatch):
    monkeypatch.setenv("LB_BUDGET_DIR", str(tmp_path))
    monkeypatch.setattr(lb, "TIMEOUT_S", {"high": 0.5, "low": 0.5})
    return tmp_path


def test_concurrency_cap_and_timeout_maps_to_timeoutexpired(bdir, monkeypatch):
    monkeypatch.setattr(lb, "N_SLOTS", 2)
    ctxs = [lb.acquire(), lb.acquire()]
    for c in ctxs:
        c.__enter__()
    with pytest.raises(lb.BudgetTimeout):
        with lb.acquire():
            pass
    with pytest.raises(subprocess.TimeoutExpired):
        lb.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True, timeout=5)
    for c in ctxs:
        c.__exit__(None, None, None)
    p = lb.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True, timeout=5)
    assert p.stdout.strip() == "1"


def test_rate_cap_and_low_priority_headroom(bdir, monkeypatch):
    monkeypatch.setattr(lb, "RATE_HIGH", 3); monkeypatch.setattr(lb, "RATE_LOW", 2)
    monkeypatch.setattr(lb, "TIMEOUT_S", {"high": 5, "low": 5})
    t0 = time.time()
    for _ in range(4):                                  # 第 4 次须等到 1 秒窗口滚过
        with lb.acquire():
            pass
    assert time.time() - t0 >= 0.9
    (bdir / ".lb_rate.json").write_text(json.dumps([time.time()] * 2))
    monkeypatch.setenv("LB_PRIORITY", "low")
    monkeypatch.setattr(lb, "TIMEOUT_S", {"high": 5, "low": 0.3})
    with pytest.raises(lb.BudgetTimeout):              # 低优先级只用 2/秒，余下留给高优先级
        with lb.acquire():
            pass
    monkeypatch.setenv("LB_PRIORITY", "high")
    with lb.acquire():                                  # 高优先级仍可用
        pass


def test_stats_and_rate_limit_errors(bdir):
    lb.run([sys.executable, "-c", "import sys; sys.stderr.write('error: rate limit exceeded'); sys.exit(1)"],
           capture_output=True, text=True, timeout=5)
    s = json.loads(next(bdir.glob(".lb_stats_*.json")).read_text())
    h = next(iter(s.values()))
    assert h["calls"] == 1 and h["rate_limit_errors"] == 1 and h["by_priority"] == {"high": 1}


def test_slot_released_when_holder_process_dies(bdir, monkeypatch):
    monkeypatch.setattr(lb, "N_SLOTS", 1)
    code = ("import os,sys; os.environ['LB_BUDGET_DIR']=%r; sys.path.insert(0,%r); from undertow.collect import lb_budget as lb;"
            "lb.N_SLOTS=1; c=lb.acquire(); c.__enter__(); print('held', flush=True); import time; time.sleep(30)"
            % (str(bdir), str(Path(__file__).resolve().parents[1])))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    with pytest.raises(lb.BudgetTimeout):
        with lb.acquire():
            pass
    p.kill(); p.wait()
    with lb.acquire():                                  # 持锁进程被杀 → 内核释放
        pass
