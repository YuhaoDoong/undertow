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
    (bdir / ".lb_rate.json").write_text(json.dumps([time.monotonic()] * 2))
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


def test_reserved_slot_for_high_priority(bdir, monkeypatch):
    """Codex 034 §5 探针：低优先级占满能拿的槽后，高优先级仍有保留槽可用。"""
    monkeypatch.setattr(lb, "N_SLOTS", 3)
    monkeypatch.setenv("LB_PRIORITY", "low")
    held = [lb.acquire(), lb.acquire()]
    for c in held:
        c.__enter__()
    with pytest.raises(lb.BudgetTimeout):                 # 低优先级只能用 N_SLOTS-1 = 2 个
        with lb.acquire():
            pass
    monkeypatch.setenv("LB_PRIORITY", "high")
    t0 = time.monotonic()
    with lb.acquire():                                    # 高优先级立刻拿到保留槽
        pass
    assert time.monotonic() - t0 < 0.3
    for c in held:
        c.__exit__(None, None, None)


def test_waiting_for_rate_does_not_hold_slot(bdir, monkeypatch):
    """等速率令牌时先放槽：只有 1 个槽时，一个在等令牌的进程不能把它占住。"""
    monkeypatch.setattr(lb, "N_SLOTS", 1)
    monkeypatch.setattr(lb, "RATE_HIGH", 1)
    monkeypatch.setattr(lb, "TIMEOUT_S", {"high": 5, "low": 5})
    (bdir / ".lb_rate.json").write_text(json.dumps([time.monotonic()]))       # 令牌已满 ~1 秒
    slot = bdir / ".lb_slots"; slot.mkdir(exist_ok=True)
    import fcntl as _f
    code = ("import os,sys,time; os.environ['LB_BUDGET_DIR']=%r; sys.path.insert(0,%r); from undertow.collect import lb_budget as lb;"
            "lb.N_SLOTS=1; lb.RATE_HIGH=1; lb.TIMEOUT_S={'high':5,'low':5}; c=lb.acquire(); c.__enter__(); print('got', flush=True)"
            % (str(bdir), str(Path(__file__).resolve().parents[1])))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)                                        # 子进程此时在等令牌
    h = open(slot / "slot0.lock", "a+")
    _f.flock(h.fileno(), _f.LOCK_EX | _f.LOCK_NB)          # 槽是空的：等令牌的进程没占着它
    _f.flock(h.fileno(), _f.LOCK_UN); h.close()
    assert p.stdout.readline().strip() == "got"
    p.wait()


def test_run_shares_caller_deadline(bdir, monkeypatch):
    """预算等待与实际调用共享调用方 timeout：槽位全占时按调用方的 0.4 秒超时，而不是先等满 TIMEOUT_S。"""
    monkeypatch.setattr(lb, "N_SLOTS", 1)
    monkeypatch.setattr(lb, "TIMEOUT_S", {"high": 30, "low": 30})
    c = lb.acquire(); c.__enter__()
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        lb.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True, timeout=0.4)
    assert time.monotonic() - t0 < 2
    c.__exit__(None, None, None)


def test_corrupt_rate_and_stats_are_isolated_not_zeroed(bdir, monkeypatch, capsys):
    monkeypatch.setattr(lb, "TIMEOUT_S", {"high": 5, "low": 5})
    (bdir / ".lb_rate.json").write_text("{坏")
    t0 = time.monotonic()
    with lb.acquire():
        pass
    assert time.monotonic() - t0 >= 0.9                    # 保守退避 1 秒
    assert list(bdir.glob(".lb_rate.json.corrupt-*")) and "损坏" in capsys.readouterr().err
    f = bdir / f".lb_stats_{lb.datetime.now(lb.ET):%Y-%m-%d}.json"
    f.write_text("坏")
    lb._stat("call", 1.0)
    assert list(bdir.glob(f"{f.name}.corrupt-*")) and json.loads(f.read_text())["_stats_corrupt_isolated"] == 1


def _hold_lock(path):
    """子进程持锁，直到测试关闭它的 stdin（可控同步）。
    2026-10-14 曾按固定 sleep(1.0) 持锁：test_stats_lock_busy_defers_instead_of_blocking 在 ET 04:11（每日任务时段）失败于
    「没有延后文件」而耗时断言通过 —— 测试目录经 LB_BUDGET_DIR 隔离，不与定时任务共用锁；更可能是机器高负载下
    父进程在读到 'held' 之后被饿住超过 1 秒、子进程已放锁。原输出保存在 GPTcom 045 回信。改成测试显式放锁，不靠时长。"""
    code = ("import fcntl,sys; f=open(%r,'a+'); fcntl.flock(f.fileno(), fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()"
            % str(path))
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def _release(p):
    p.stdin.close(); p.wait(5)


def test_rate_lock_respects_caller_deadline_and_command_not_run(bdir):
    """Codex 036 §6 探针：真实子进程持速率锁 0.5 秒，run(timeout=0.05) 旧代码约 0.51 秒才返回。现：按截止返回、命令不执行。"""
    h = _hold_lock(bdir / ".lb_rate.lock")
    marker = bdir / "ran"
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        lb.run([sys.executable, "-c", f"open({str(marker)!r},'w')"], capture_output=True, text=True, timeout=0.05)
    assert time.monotonic() - t0 < 0.3 and not marker.exists()
    _release(h)


def test_rate_lock_wait_does_not_hold_slot(bdir, monkeypatch):
    """等速率锁时不占并发槽：唯一的槽在别的进程持速率锁期间保持空闲。"""
    monkeypatch.setattr(lb, "N_SLOTS", 1)
    h = _hold_lock(bdir / ".lb_rate.lock")
    import threading, fcntl as _f
    errs = []
    th = threading.Thread(target=lambda: errs.append(None) if lb.acquire(timeout=2).__enter__() is None else None)
    th.start(); time.sleep(0.2)
    slot = open(bdir / ".lb_slots" / "slot0.lock", "a+")
    got = 0
    for _ in range(20):                                         # 等待者只在极短的单次尝试中碰槽：多数时刻槽是空的
        try:
            _f.flock(slot.fileno(), _f.LOCK_EX | _f.LOCK_NB); got += 1; _f.flock(slot.fileno(), _f.LOCK_UN)
        except OSError:
            pass
        time.sleep(0.005)
    slot.close()
    assert got >= 15
    _release(h); th.join(3)


def test_stats_lock_busy_defers_instead_of_blocking(bdir):
    h = _hold_lock(bdir / ".lb_stats.lock")
    t0 = time.monotonic()
    lb._stat("call", 1.0)
    assert time.monotonic() - t0 < 0.5
    assert list(bdir.glob(".lb_stats_deferred_*.jsonl"))
    _release(h)


def test_all_longbridge_cli_calls_go_through_budget():
    """调用图核查（Codex 036 §4 先决条件）：除冻结的 longbridge_kline.py 外，所有调长桥 CLI 的 subprocess 都经过 lb_budget.run。"""
    import re
    root = Path(__file__).resolve().parents[1]
    bad = []
    for f in list((root / "undertow").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        if f.name in ("lb_budget.py", "longbridge_kline.py"):
            continue
        src = f.read_text("utf-8")
        for m in re.finditer(r"subprocess\.(run|Popen|check_output)\(([^)]{0,200})", src):
            if "longbridge" in m.group(2) or "BIN" in m.group(2):
                bad.append(f"{f.relative_to(root)}:{src[:m.start()].count(chr(10)) + 1}")
    assert bad == [], bad
