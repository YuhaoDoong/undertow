"""长桥 K 线（OHLCV）—— 技术面的实时价格源。

为什么需要它（2026-08-27 用户实测发现）
----------------------------------------
技术面原先走 CBOE 历史日线，**实测滞后两天**：8/27 当天序列仍止于 8/25。
后果：报告里的 KDJ-J -9.6 / RSI6 21 / MACD柱 -2.23 全是 8/25 的读数，
而当天 QQQ 已经 KDJ 金叉、RSI6 拉到 55。用户在券商 App 上看到金叉，
回头看我们报告写"深度超卖"，两边对不上 —— 而报告头部的时效横幅只标了
**期权数据**的时效（8/26 交易日，正确），技术面这一层滞后却毫无提示。

三个源的实测时效（2026-08-27 ET 10:50）：
    CBOE     止于 2026-08-25   ← 原用
    Yahoo    止于 2026-08-27（日线收盘）
    长桥     8/27 日线 + 1h 更新到 ET 10:30   ← 最实时，且与用户 App 同源

⚠️ CLI 的 --format json 输出后面可能附加非 JSON 内容，必须用 raw_decode
只取第一个 JSON 值，不能直接 json.load。

周期：1m 5m 15m 30m 1h day week month year。**没有 4h**，需要时由 1h 聚合。
"""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone

from undertow.core.models import PriceSeries

BIN = "longbridge"
PERIODS = ("1m", "5m", "15m", "30m", "1h", "day", "week", "month", "year")


class KlineUnavailable(RuntimeError):
    """取不到 K 线。调用方应降级到其它价格源，**不得静默当成没有数据**。"""


def _run(sym: str, period: str, count: int, timeout: float = 40.0) -> list[dict]:
    if period not in PERIODS:
        raise ValueError(f"period 须为 {PERIODS} 之一，收到 {period!r}")
    try:
        p = subprocess.run(
            [BIN, "kline", sym, "--period", period, "--count", str(count), "--format", "json"],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise KlineUnavailable("未找到 longbridge CLI") from e
    except subprocess.TimeoutExpired as e:
        raise KlineUnavailable(f"longbridge kline {sym} 超时") from e
    if p.returncode != 0:
        raise KlineUnavailable(f"longbridge kline {sym} 失败：{(p.stderr or p.stdout)[:200]}")
    txt = (p.stdout or "").lstrip()
    if not txt:
        raise KlineUnavailable(f"longbridge kline {sym} 返回空")
    try:
        # ⚠️ 只取第一个 JSON 值：CLI 会在 JSON 之后附加统计行，json.load 会抛 Extra data
        obj, _ = json.JSONDecoder().raw_decode(txt)
    except ValueError as e:
        raise KlineUnavailable(f"longbridge kline {sym} 输出无法解析：{e}") from e
    rows = obj if isinstance(obj, list) else (obj.get("candles") or obj.get("data") or [])
    if not rows:
        raise KlineUnavailable(f"longbridge kline {sym} 无数据")
    return rows


def fetch_series(symbol: str, *, period: str = "day", count: int = 400) -> PriceSeries:
    """取 OHLCV 日线/分钟线，返回按时间升序的 PriceSeries。"""
    rows = _run(symbol, period, count)
    ds, cs, hs, ls = [], [], [], []
    for r in rows:
        t = str(r.get("time") or r.get("timestamp") or "")
        if not t:
            continue
        try:
            ds.append(date.fromisoformat(t[:10]))
            cs.append(float(r["close"])); hs.append(float(r["high"])); ls.append(float(r["low"]))
        except (KeyError, ValueError):
            continue
    if not cs:
        raise KlineUnavailable(f"{symbol} 解析后无有效 K 线")
    return PriceSeries(symbol=symbol.split(".")[0], dates=ds, closes=cs, highs=hs, lows=ls)


def fetch_bars(symbol: str, *, period: str = "1h", count: int = 200) -> list[dict]:
    """取带【完整时间戳】的 K 线（分钟/小时线用；日线用 fetch_series 即可）。

    返回 [{ts(UTC aware datetime), open, high, low, close, volume}]，按时间升序。
    """
    out = []
    for r in _run(symbol, period, count):
        t = str(r.get("time") or "")
        if not t:
            continue
        try:
            ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            out.append({"ts": ts, "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r.get("volume") or 0)})
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda x: x["ts"])
    return out


def aggregate(bars: list[dict], k: int, *, by_session: bool = True) -> list[dict]:
    """把 k 根小周期 K 线合成一根（如 4 根 1h → 1 根 4h）。长桥无 4h，只能这样聚合。

    ⚠️ **必须按交易日分组，不能对全序列盲分。**
    美股 RTH 是 6.5 小时 → 每日 7 根 1h，**不是 4 的整数倍**。若对全序列
    每 4 根一切，必然把前一日尾盘和次日开盘拼成一根：2026-08-27 实测，
    08-26 19:30 收 711.37 被拼进"08-27 15:30"那根、成为其开盘 712.31，
    横跨隔夜跳空 —— 这种 K 线在任何看盘软件里都不存在，指标自然对不上
    （聚合前 RSI6 一度算出 100）。

    by_session=True（默认）：先按交易日（UTC 日期）分桶，日内再按 k 根切；
    每日不足 k 根的**尾部残段保留为一根**（它是真实的日内最后一段），
    但**日内起始不足 k 根的整组不跨日补齐**。
    by_session=False 才走旧的全序列盲分，仅供非交易时段数据使用。
    """
    if k <= 1 or not bars:
        return list(bars)

    def _merge(g):
        return {"ts": g[-1]["ts"], "open": g[0]["open"],
                "high": max(x["high"] for x in g), "low": min(x["low"] for x in g),
                "close": g[-1]["close"], "volume": sum(x["volume"] for x in g)}

    if not by_session:
        n = len(bars)
        return [_merge(bars[i:i + k]) for i in range(n % k, n, k) if len(bars[i:i + k]) == k]

    from itertools import groupby
    out = []
    for _, grp in groupby(bars, key=lambda x: x["ts"].date()):
        day = list(grp)
        for i in range(0, len(day), k):
            g = day[i:i + k]
            if g:                      # 日内尾部残段保留：它是真实的日内最后一段
                out.append(_merge(g))
    out.sort(key=lambda x: x["ts"])
    return out
