"""长桥行情调用的跨进程预算（Codex 033 A4；官方 FAQ：行情接口每秒 ≤ 10 次、并发 ≤ 5）。

同一账户的 CLI 调用（quote / depth / bars / news / account）都经过这里。例外：longbridge_kline.py 在方向族冻结清单内、不得改动，
其日线调用（低频：结算、日线补齐）暂不经过预算——已知缺口，解冻或新版本时再接入：
- 并发：data/logs/.lb_slots/slot{0..N-1}.lock 上的 fcntl 非阻塞锁，最多 N_SLOTS 个同时在途；持锁进程退出内核即释放。
- 速率：data/logs/.lb_rate.json 在 flock 下记最近 1 秒的调用时刻；高优先级（模拟仓、实盘只读体检、用户查询，默认）≤ RATE_HIGH 次/秒，
  低优先级（研究采样：盘中采样、逐分钟、资金流采集、容量测量，环境变量 LB_PRIORITY=low）≤ RATE_LOW 次/秒，给高优先级留余量。
- 统计：data/logs/.lb_stats_<ET日>.json 按小时记 调用数 / 等待毫秒 / 最长等待 / 超时 / 疑似限频错误（不记任何行情内容）。
等待超时 → 抛 BudgetTimeout，调用方当作取数失败处理（不静默跳过）。只用标准库；不发任何交易请求。
参数是未校准的工程留量（低于官方上限），不是实测最优值。
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

N_SLOTS = 4
RATE_HIGH = 8
RATE_LOW = 6
TIMEOUT_S = {"high": 60.0, "low": 180.0}
ET = ZoneInfo("America/New_York")


class BudgetTimeout(RuntimeError):
    pass


def _dir() -> Path:
    d = Path(os.environ.get("LB_BUDGET_DIR") or Path(__file__).resolve().parents[2] / "data/logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def priority() -> str:
    return "low" if os.environ.get("LB_PRIORITY", "").lower() == "low" else "high"


def _stat(key: str, wait_ms: float = 0.0) -> None:
    d = _dir()
    f = d / f".lb_stats_{datetime.now(ET):%Y-%m-%d}.json"
    with open(d / ".lb_stats.lock", "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            s = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            s = {}
        h = s.setdefault(f"{datetime.now(ET):%H}", {"calls": 0, "wait_ms": 0.0, "max_wait_ms": 0.0, "timeouts": 0,
                                                     "rate_limit_errors": 0, "by_priority": {}})
        if key == "call":
            h["calls"] += 1; h["wait_ms"] = round(h["wait_ms"] + wait_ms, 1); h["max_wait_ms"] = max(h["max_wait_ms"], round(wait_ms, 1))
            h["by_priority"][priority()] = h["by_priority"].get(priority(), 0) + 1
        else:
            h[key] = h.get(key, 0) + 1
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False), "utf-8")
        os.replace(tmp, f)


def note_error(msg: str) -> None:
    """调用方在 CLI 报错时调用：疑似限频（429 / rate / limit / too many / 频率）单独计数，与无盘口等业务结果分开。"""
    m = (msg or "").lower()
    if any(k in m for k in ("429", "rate limit", "ratelimit", "too many", "频率", "限流", "out of limit")):
        try:
            _stat("rate_limit_errors")
        except OSError:
            pass


def _take_rate(prio: str, deadline: float) -> None:
    d = _dir()
    cap = RATE_LOW if prio == "low" else RATE_HIGH
    while True:
        with open(d / ".lb_rate.lock", "a+") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            f = d / ".lb_rate.json"
            try:
                ts = [t for t in json.loads(f.read_text("utf-8")) if time.time() - t < 1.0]
            except (OSError, ValueError):
                ts = []
            if len(ts) < cap:
                ts.append(time.time())
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(ts), "utf-8")
                os.replace(tmp, f)
                return
            wait = max(0.01, 1.0 - (time.time() - min(ts)))
        if time.time() + wait > deadline:
            raise BudgetTimeout("长桥调用速率预算等待超时")
        time.sleep(wait)


@contextmanager
def acquire():
    """取得一个并发槽位与一个速率令牌；离开时释放槽位。超时抛 BudgetTimeout。"""
    prio = priority()
    t0 = time.time()
    deadline = t0 + TIMEOUT_S[prio]
    slots = _dir() / ".lb_slots"
    slots.mkdir(exist_ok=True)
    fh = None
    while fh is None:
        for i in range(N_SLOTS):
            h = open(slots / f"slot{i}.lock", "a+")
            try:
                fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fh = h
                break
            except OSError:
                h.close()
        if fh is None:
            if time.time() > deadline:
                try:
                    _stat("timeouts")
                except OSError:
                    pass
                raise BudgetTimeout("长桥调用并发槽位等待超时")
            time.sleep(0.05 if prio == "high" else 0.2)
    try:
        try:
            _take_rate(prio, deadline)
        except BudgetTimeout:
            _stat("timeouts")
            raise
        try:
            _stat("call", (time.time() - t0) * 1000)
        except OSError:
            pass
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def run(cmd: list, **kw):
    """subprocess.run 的预算版：先取槽位与令牌再调 CLI；预算等待超时按 subprocess.TimeoutExpired 抛出，
    让各模块沿用已有的超时处理（转成各自的 Unavailable 错误，不静默）。CLI 非零返回时登记疑似限频错误。"""
    import subprocess
    try:
        with acquire():
            p = subprocess.run(cmd, **kw)
    except BudgetTimeout as e:
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0, output=str(e)) from e
    if getattr(p, "returncode", 0):
        note_error(f"{getattr(p, 'stderr', '')}{getattr(p, 'stdout', '')}")
    return p
