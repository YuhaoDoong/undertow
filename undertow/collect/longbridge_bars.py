"""长桥期权/标的【逐分钟成交价】（只读）—— 给影子账的候选价差补历史盘中交易价格。

用户 2026-09-27：影子账从 9/28 才开始实时记录盘口；此前回放行（2026-09-14 起）只有收盘快照价。
长桥 `kline history --period 1m` 能取任一天的逐分钟 K 线，但已到期较久的合约会查不到
（2026-09-27 查询：9/23 到期仍可查，9/18 到期返回 `quote not found`）。这只是该接口当时的返回，
不等于永久不可得；尽早落盘，并保留查询时刻与原始返回，可用 `shadow bars --retry-missing` 重查。

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


# ── 当日逐分钟（`longbridge intraday`，今天）──────────────────────────────
# 2026-09-28 实测：`intraday <期权> --date <历史日>` 对美股返回空（GLD.US 9/24 也空）→ 只能取【当天】；
# 当天查询没有报历史 K 线配额错误（配额已用尽时照常返回）。长桥历史 K 线配额按自然月计（官方文档：
# 月初补满、不结转；400/月档；同月重复查同一代码只算一次）→ 每天收盘后用它存当天的逐分钟，给历史配额省量。
# 口径：每分钟一个成交价 + 成交量/成交额（无 OHLC、无买卖价），与 kline 分开存。
INTRADAY_DIR = Path("data/history/option_intraday")
INTRADAY_FIELDS = ("time", "price", "volume", "turnover", "avg_price")
# 口径（官方 intraday 文档 + 2026-09-28 实测）：time = 分钟【起点】（UTC）；price = 该分钟【收盘价】，无成交的分钟
# 沿用上一价、volume=0；turnover 为美元成交额 —— 期权含合约乘数（IWM 期权：286.00 / 126 张 ≈ 2.27 = 0.0227×100），
# 正股不含（GLD.US：27107700.944 / 71726 ≈ 377.9）；期权的 avg_price 返回 "0"，含义未核实，不得当作 VWAP。
# 分钟 VWAP 代理 = turnover / (volume × 乘数)，乘数须逐合约核实，不默认固定。
BASIS_INTRADAY = ("longbridge intraday（当天）：每分钟收盘价（time=分钟起点，UTC）、成交量、成交额；无 OHLC、无买卖价。"
                  "volume=0 的分钟为沿用上一价，不是成交证据。收盘后抓取，补充而非替代 v5 保守入场价。")
#: 终态规则（Codex 024-1，事前写死）：收盘后（ET 16:05 起）同一代码
#:   full_session → 完成；partial_session / 解析异常 → 继续重试；
#:   empty 连续 EMPTY_TERMINAL 次（相邻计入的两次间隔 ≥ RETRY_MIN_SPACING_S，代码强制）→ empty_confirmed（终态，原始响应保留）；
#:   not_found / invalid_symbol 连续 GONE_TERMINAL 次 → 终态。
EMPTY_TERMINAL = 3
GONE_TERMINAL = 2
RETRY_MIN_SPACING_S = 240         # 终态计数的最小间隔（约一次 5 分钟唤醒；设计值）
FULL_SESSION_MIN_FRAC = 0.95       # 分钟行覆盖 ≥ 95% 的应有交易分钟，且首末分钟在时段两端（设计值，未校准）


def parse_intraday(raw, day: date) -> list[list[str]]:
    if not isinstance(raw, list):
        raise BarsUnavailable(f"intraday 结构异常：{type(raw).__name__}")
    out = []
    for b in raw:
        if not isinstance(b, dict) or any(k not in b for k in INTRADAY_FIELDS):
            raise BarsUnavailable(f"intraday 字段缺失：{str(b)[:80]}")
        out.append([str(b[k]) for k in INTRADAY_FIELDS])
    bad = [r for r in out if not r[0].startswith(day.isoformat())]
    if bad:
        raise BarsUnavailable(f"intraday 返回了别的日期：{bad[0][0]}（期望 {day}）")
    return out


def session_minutes(day: date) -> tuple[datetime, datetime, int] | None:
    """该交易日常规时段的首、末分钟起点（UTC）与应有分钟数（半日市按日历收市时刻）。"""
    from zoneinfo import ZoneInfo
    from undertow.core import market_calendar as mc
    ct = mc.close_time(day)
    if ct is None:
        return None
    et = ZoneInfo("America/New_York")
    hh, mm = map(int, ct.split(":"))
    first = datetime(day.year, day.month, day.day, 9, 30, tzinfo=et).astimezone(timezone.utc)
    close = datetime(day.year, day.month, day.day, hh, mm, tzinfo=et).astimezone(timezone.utc)
    n = int((close - first).total_seconds() // 60)
    return first, close, n


def intraday_quality(rows: list, day: date) -> dict:
    """质量标签（Codex 024-2 / 025-4）：不要求每分钟有成交，但要求字段齐、数值有限、时区为 UTC、落在整分钟栅格、
    排序无重复、时段覆盖。full_session 是【数据工程容忍规则】（覆盖 ≥ 95%、首尾在时段两端），不等于每分钟齐全 ——
    缺失分钟数 missing_minutes 与时段外行数 outside_session 一并记下。时段按标的常规时段（含半日市）。"""
    import math
    from datetime import timedelta
    if not rows:
        return {"label": "empty", "n": 0}
    if any(len(r) != len(INTRADAY_FIELDS) for r in rows):
        return {"label": "invalid", "why": "字段数不符", "n": len(rows)}
    try:
        ts = [datetime.fromisoformat(r[0].replace("Z", "+00:00")) for r in rows]
        px = [float(r[1]) for r in rows]
        vol = [float(r[2]) for r in rows]
        tov = [float(r[3]) for r in rows]
    except (ValueError, IndexError) as e:
        return {"label": "invalid", "why": f"{type(e).__name__}: {e}"[:120], "n": len(rows)}
    if not all(math.isfinite(x) for x in px + vol + tov):
        return {"label": "invalid", "why": "价格/成交量/成交额含 NaN 或无穷", "n": len(rows)}
    if any(t.tzinfo is None or t.utcoffset() != timedelta(0) for t in ts):
        return {"label": "invalid", "why": "时间戳缺时区或非 UTC", "n": len(rows)}
    if any(t.second or t.microsecond for t in ts):
        return {"label": "invalid", "why": "时间戳不在整分钟栅格", "n": len(rows)}
    if ts != sorted(ts) or len(set(ts)) != len(ts):
        return {"label": "invalid", "why": "时间未排序或有重复", "n": len(rows)}
    if any(p <= 0 for p in px) or any(v < 0 for v in vol) or any(x < 0 for x in tov):
        return {"label": "invalid", "why": "价格非正或成交量/成交额为负", "n": len(rows)}
    sm = session_minutes(day)
    if sm is None:
        return {"label": "invalid", "why": "非交易日", "n": len(rows)}
    first, close, n_exp = sm
    in_sess = [t for t in ts if first <= t < close]
    frac = len(in_sess) / n_exp if n_exp else 0.0
    ok = frac >= FULL_SESSION_MIN_FRAC and ts[0] <= first + timedelta(minutes=2) and \
        ts[-1] >= close - timedelta(minutes=3)
    return {"label": "full_session" if ok else "partial_session", "n": len(rows), "coverage": round(frac, 4),
            "expected_minutes": n_exp, "missing_minutes": n_exp - len(in_sess),
            "outside_session": len(ts) - len(in_sess),
            "first": ts[0].isoformat(), "last": ts[-1].isoformat(), "traded_minutes": sum(v > 0 for v in vol)}


def fetch_intraday_today(symbol: str, day: date, *, runner=_run) -> dict:
    """当天逐分钟。day 必须是今天（ET）—— 调用方保证；返回行的日期不符即报错，不静默存错日。"""
    st, data = runner(["intraday", symbol])
    at = datetime.now(timezone.utc).isoformat()
    if st != "ok":
        return {"status": st, "error": data, "fetched_at": at}
    rows = parse_intraday(data, day)
    return {"status": "ok" if rows else "empty", "rows": rows, "fetched_at": at,
            "quality": intraday_quality(rows, day)}


def merge_attempt(prev: dict | None, res: dict) -> dict:
    """保留每次尝试（时刻、状态、行数、质量标签）；已有 full_session 的数据不会被更差的一次覆盖。"""
    hist = list((prev or {}).get("attempts") or [])
    hist.append({"at": res.get("fetched_at"), "status": res["status"],
                 "n": len(res.get("rows") or []), "label": (res.get("quality") or {}).get("label"),
                 "error": (str(res.get("error"))[:160] if res.get("error") else None)})
    keep_old = (prev or {}).get("quality", {}).get("label") == "full_session" and \
        (res.get("quality") or {}).get("label") != "full_session"
    base = dict(prev) if keep_old else dict(res)
    base["attempts"] = hist
    base["state"] = symbol_state(base)
    return base


def symbol_state(v: dict | None) -> str:
    """单个代码的终态判定：complete / empty_confirmed / gone_confirmed / pending。"""
    if not v:
        return "pending"
    if (v.get("quality") or {}).get("label") == "full_session":
        return "complete"
    att = v.get("attempts") or []

    def run_of(stats):
        """末尾连续同类尝试数；与上一次计入的尝试相隔不足 RETRY_MIN_SPACING_S 的不计（人工连按不能立刻终态）。"""
        n, last = 0, None
        for a in reversed(att):
            if a["status"] not in stats:
                break
            try:
                t = datetime.fromisoformat(str(a.get("at")))
            except ValueError:
                continue
            if last is None or (last - t).total_seconds() >= RETRY_MIN_SPACING_S:
                n += 1
                last = t
        return n
    if run_of(("empty",)) >= EMPTY_TERMINAL:
        return "empty_confirmed"
    if run_of(("not_found", "invalid_symbol")) >= GONE_TERMINAL:
        return "gone_confirmed"
    return "pending"


def new_intraday_day(root: str, day: date) -> dict:
    return {"schema": SCHEMA, "root": root, "date": day.isoformat(), "basis": BASIS_INTRADAY,
            "fields": list(INTRADAY_FIELDS), "contracts": {}}


def intraday_covered(root: str, day: date, base: Path | None = None, *, need: str = "close") -> set:
    """按研究所需字段判覆盖（Codex 024-2）：need="close" → 质量为 full_session 的代码；
    need="ohlc" → 永远为空集（逐分钟只有分钟收盘，不能替代 OHLC 的路径/止损/极值研究）。
    坏文件 → 空集合（由抓取命令负责隔离）。"""
    if need != "close":
        return set()
    base = base or INTRADAY_DIR            # 运行时取（默认参数在定义时求值，替换模块常量会失效）
    try:
        cur = load_day(path_of(root, day, base))
    except BarsFileCorrupt:
        return set()
    return {s for s, v in ((cur or {}).get("contracts") or {}).items()
            if (v.get("quality") or {}).get("label") == "full_session"}
