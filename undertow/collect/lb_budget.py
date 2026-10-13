"""长桥行情调用的跨进程预算（Codex 033 A4；官方 FAQ：行情接口每秒 ≤ 10 次、并发 ≤ 5）。

同一账户的 CLI 调用（quote / depth / bars / news / account）都经过这里。例外：longbridge_kline.py 在方向族冻结清单内、不得改动，
其日线调用（低频：结算、日线补齐）暂不经过预算——已知缺口，解冻或新版本时再接入：
- 并发：data/logs/.lb_slots/slot{0..N-1}.lock 上的 fcntl 非阻塞锁，最多 N_SLOTS 个同时在途；持锁进程退出内核即释放。
  低优先级最多占 N_SLOTS-RESERVED_HIGH 个，至少 1 个只给高优先级；等速率令牌时不占槽。
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
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

N_SLOTS = 4
RESERVED_HIGH = 1            # Codex 034 §5：至少 1 个槽只给高优先级（低优先级最多占 N_SLOTS-1 个）
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


def _isolate(f: Path) -> None:
    """损坏的状态文件改名隔离保留（不静默清零）；隔离失败也不抛，调用方照样走保守退避。"""
    try:
        f.replace(f.with_name(f"{f.name}.corrupt-{datetime.now(ET):%Y%m%dT%H%M%S}-{os.getpid()}"))
    except OSError:
        pass


def _stat(key: str, wait_ms: float = 0.0) -> None:
    d = _dir()
    f = d / f".lb_stats_{datetime.now(ET):%Y-%m-%d}.json"
    with open(d / ".lb_stats.lock", "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            s = json.loads(f.read_text("utf-8"))
        except FileNotFoundError:
            s = {}
        except (OSError, ValueError):                       # 损坏：隔离保留，从新文件续记并计数（不静默清零）
            _isolate(f)
            s = {"_stats_corrupt_isolated": 1}
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


def _try_rate(prio: str) -> float:
    """在 flock 下尝试取一个速率令牌：取到返回 0；否则返回建议等待秒数（不占令牌）。
    时间戳用 time.monotonic（系统级、不随墙钟调整；重启后旧值落在未来 → 视为无效丢弃）。
    状态文件损坏 → 隔离保留、记统计、本次按满额保守退避 1 秒（1 秒窗口过后旧调用本就不再计数）。"""
    d = _dir()
    cap = RATE_LOW if prio == "low" else RATE_HIGH
    with open(d / ".lb_rate.lock", "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        f = d / ".lb_rate.json"
        now = time.monotonic()
        try:
            raw = json.loads(f.read_text("utf-8"))
            if not isinstance(raw, list):
                raise ValueError("not a list")
            ts = [t for t in raw if isinstance(t, (int, float)) and 0 <= now - t < 1.0]
        except FileNotFoundError:
            ts = []
        except (OSError, ValueError):
            _isolate(f)
            try:
                _stat("rate_state_corrupt")
            except OSError:
                pass
            print("[lb_budget] 速率状态文件损坏，已隔离保留；本次保守退避 1 秒", file=sys.stderr)
            return 1.0
        if len(ts) < cap:
            ts.append(now)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(ts), "utf-8")
            os.replace(tmp, f)
            return 0.0
        return max(0.01, 1.0 - (now - min(ts)))


def _slot_order(prio: str) -> list:
    low_n = max(1, N_SLOTS - RESERVED_HIGH)
    return list(range(low_n)) if prio == "low" else list(range(N_SLOTS - 1, -1, -1))   # 高优先级先试保留槽


@contextmanager
def acquire(timeout: float | None = None):
    """取得一个并发槽位与一个速率令牌；离开时释放槽位。超时抛 BudgetTimeout。
    等速率时【不占槽】（先放槽再睡），避免低优先级占槽等令牌把高优先级挤到超时（Codex 034 §5 探针）。
    timeout：本次预算等待的绝对上限（秒，单调时钟）；缺省按优先级 TIMEOUT_S。"""
    prio = priority()
    t0 = time.monotonic()
    deadline = t0 + (TIMEOUT_S[prio] if timeout is None else min(timeout, TIMEOUT_S[prio]))
    slots = _dir() / ".lb_slots"
    slots.mkdir(exist_ok=True)
    fh = None
    while True:
        for i in _slot_order(prio):
            h = open(slots / f"slot{i}.lock", "a+")
            try:
                fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fh = h
                break
            except OSError:
                h.close()
        if fh is None:
            if time.monotonic() > deadline:
                try:
                    _stat("timeouts")
                except OSError:
                    pass
                raise BudgetTimeout("长桥调用并发槽位等待超时")
            time.sleep(0.05 if prio == "high" else 0.2)
            continue
        wait = _try_rate(prio)
        if wait == 0:
            break
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN); fh.close(); fh = None          # 等令牌时放槽
        if time.monotonic() + wait > deadline:
            try:
                _stat("timeouts")
            except OSError:
                pass
            raise BudgetTimeout("长桥调用速率预算等待超时")
        time.sleep(wait)
    try:
        try:
            _stat("call", (time.monotonic() - t0) * 1000)
        except OSError:
            pass
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def run(cmd: list, **kw):
    """subprocess.run 的预算版：先取槽位与令牌再调 CLI。预算等待与实际调用【共享】调用方给的 timeout（绝对截止），
    不再「等满预算再另给一个完整 timeout」。等待超时或剩余时间耗尽 → 按 subprocess.TimeoutExpired 抛出，
    让各模块沿用已有的超时处理（转成各自的 Unavailable 错误，不静默）。CLI 非零返回时登记疑似限频错误。"""
    import subprocess
    total = kw.get("timeout")
    t0 = time.monotonic()
    try:
        with acquire(timeout=total):
            if total is not None:
                left = total - (time.monotonic() - t0)
                if left <= 0:
                    raise BudgetTimeout("预算等待已用尽调用方的超时")
                kw["timeout"] = left
            p = subprocess.run(cmd, **kw)
    except BudgetTimeout as e:
        raise subprocess.TimeoutExpired(cmd, total or 0, output=str(e)) from e
    if getattr(p, "returncode", 0):
        note_error(f"{getattr(p, 'stderr', '')}{getattr(p, 'stdout', '')}")
    return p
