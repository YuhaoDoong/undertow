"""长桥期权/标的【逐分钟成交价】（只读）—— 给影子账的候选价差补历史盘中交易价格。

用户 2026-09-27：影子账从 9/28 才开始实时记录盘口；此前回放行（2026-09-14 起）只有收盘快照价。
长桥 `kline history --period 1m` 能取任一天的逐分钟 K 线，**但已到期合约约一周后就查不到**
（实测 9/27：9/23 到期仍可查，9/18 到期已 `quote not found`）——历史不可再生，尽早落盘。

口径边界（写进每个文件，读的人不能误会）：
- 只有**成交价**（OHLC、成交量、成交额），**没有买卖价**。零成交的分钟沿用上一价，不代表那一刻可按此价成交。
  所以它不能替代 v5 的保守入场价（卖腿 bid − 买腿 ask），只能作为「那段时间实际成交在什么价位」的补充数据。
- 时间戳为 UTC。

只读：`longbridge kline`；不引依赖。存储：data/history/option_bars/<ROOT>/<YYYY-MM-DD>.json.gz，
每个文件一天、一个标的，内含若干合约 + 标的本身。原子写 + 回读校验；损坏文件隔离保留，绝不覆盖。
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

BIN = "longbridge"
ROOT_DIR = Path("data/history/option_bars")
SCHEMA = 1
BASIS = ("longbridge kline history --period 1m：逐分钟成交价（OHLC/成交量/成交额，UTC）。"
         "无买卖价；零成交分钟沿用上一价，不代表可按此价成交。不能替代 v5 的保守入场价。")
FIELDS = ("time", "open", "high", "low", "close", "volume", "turnover")


class BarsUnavailable(RuntimeError):
    """长桥不可用 / 超时 / 返回非 JSON —— 与「合约查不到」（记为 status）区分开。"""


class BarsFileCorrupt(RuntimeError):
    pass


class BarsQuotaExhausted(BarsUnavailable):
    """长桥历史 K 线的【不同代码数】配额用尽（2026-09-27 实测：code=301607，limit:400）。
    重置周期官方文档未查到 —— 不猜，照实报告，下次运行自动重试。与网络故障分开，不能每次都当故障告警。"""


def option_symbol(root: str, expiry: str, side: str, strike: float) -> str:
    """GLD, 2026-10-09, P, 380 → GLD261009P380000.US（行权价 ×1000，取整）。"""
    d = date.fromisoformat(expiry)
    return f"{root}{d:%y%m%d}{side}{int(round(strike * 1000))}.US"


def _run(args: list[str], *, timeout: float = 30.0) -> tuple[str, object]:
    """("ok", 数据) / ("not_found", 原文) / ("invalid_symbol", 原文)。其余错误抛 BarsUnavailable。"""
    if shutil.which(BIN) is None:
        raise BarsUnavailable("未找到 longbridge CLI")
    try:
        p = subprocess.run([BIN, *args, "--format", "json"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise BarsUnavailable(f"longbridge {' '.join(args)} 超时") from e
    err = (p.stderr or "") + (p.stdout if p.returncode else "")
    if p.returncode != 0 or not p.stdout.strip().startswith(("[", "{")):
        if "301603" in err or "quote not found" in err:
            return "not_found", err.strip()[:200]
        if "301600" in err or "invalid symbol" in err:
            return "invalid_symbol", err.strip()[:200]
        if "301607" in err or "count out of limit" in err:
            raise BarsQuotaExhausted(err.strip()[:200])
        raise BarsUnavailable(err.strip()[:200] or f"rc={p.returncode}")
    try:
        return "ok", json.JSONDecoder().raw_decode(p.stdout.lstrip())[0]
    except ValueError as e:
        raise BarsUnavailable(f"返回非 JSON：{p.stdout[:120]}") from e


def parse_bars(raw) -> list[list[str]]:
    """[{time, open, …}] → [[time, open, high, low, close, volume, turnover]]（原样保留字符串，不转浮点）。"""
    if not isinstance(raw, list):
        raise BarsUnavailable(f"K 线结构异常：{type(raw).__name__}")
    out = []
    for b in raw:
        if not isinstance(b, dict) or any(k not in b for k in FIELDS):
            raise BarsUnavailable(f"K 线字段缺失：{str(b)[:80]}")
        out.append([str(b[k]) for k in FIELDS])
    return out


def fetch_day(symbol: str, day: date, *, runner=_run) -> dict:
    """某合约/标的某一天的 1 分钟 K 线。返回 {"status", "bars"?, "error"?, "fetched_at"}。"""
    st, data = runner(["kline", "history", symbol, "--period", "1m",
                       "--start", day.isoformat(), "--end", day.isoformat()])
    at = datetime.now(timezone.utc).isoformat()
    if st != "ok":
        return {"status": st, "error": data, "fetched_at": at}
    bars = parse_bars(data)
    bad = [b for b in bars if not b[0].startswith(day.isoformat())]
    if bad:
        raise BarsUnavailable(f"{symbol} {day} 返回了别的日期的 K 线：{bad[0][0]}")
    return {"status": "ok" if bars else "empty", "bars": bars, "fetched_at": at}


# ── 存储 ──────────────────────────────────────────────────────────────

def path_of(root: str, day: date, base: Path = ROOT_DIR) -> Path:
    return base / root / f"{day.isoformat()}.json.gz"


def load_day(path: Path) -> dict | None:
    """严格读取：不存在 → None；损坏/结构不对 → BarsFileCorrupt（调用方隔离，不覆盖）。"""
    if not path.exists():
        return None
    try:
        obj = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    except (OSError, EOFError, ValueError, UnicodeError) as e:
        raise BarsFileCorrupt(f"{path} 无法解析：{e}") from e
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA or not isinstance(obj.get("contracts"), dict):
        raise BarsFileCorrupt(f"{path} 结构不符（schema/contracts）")
    return obj


def quarantine(path: Path) -> Path:
    q = path.with_name(path.name + f".corrupt-{int(time.time())}")
    path.rename(q)
    return q


def save_day(path: Path, obj: dict) -> None:
    """原子写 + 回读校验（AGENTS：落盘必须原子 + 回读验证）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw); f.flush(); os.fsync(f.fileno())
        if json.loads(gzip.decompress(Path(name).read_bytes())) != obj:
            raise ValueError(f"{path} 回读校验失败；原文件未改")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def new_day(root: str, day: date) -> dict:
    return {"schema": SCHEMA, "root": root, "date": day.isoformat(), "basis": BASIS, "fields": list(FIELDS),
            "contracts": {}}
