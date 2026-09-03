"""多周期 K 线数据层 —— 15m / 1h / 4h / 1d。

用户 2026-09-03 说明了各周期的**用途分工**：

    15m  只看开仓平仓的入场出场时机
    1h   ┐
    4h   ├ 看真正的方向和波段/趋势
    1d   ┘

这个分工是硬约束，不是建议：**15m 的读数不得用于判断方向**。
15m 上的 MACD 金叉在日线级别可能只是噪声里的一次抽动，拿它当方向信号，
等于把噪声当趋势 —— 这是我们在期权层已经栽过的坑（单信号误读资金流）。

数据来源与拼装
--------------
长桥 K 线周期里**没有 4h**，由 1h 按交易日分组聚合（见 longbridge_kline.aggregate
的注释：美股 RTH 每日 7 根 1h，不是 4 的整数倍，盲分会把隔夜跳空拼进一根）。

    15m → 直接取 '15m'
    1h  → 直接取 '1h'
    4h  → 取 '1h' 后 aggregate(4)，每交易日出 2 根（4+3）
    1d  → 直接取 'day'
"""
from __future__ import annotations

from datetime import timedelta

from undertow.collect.longbridge_kline import KlineUnavailable, aggregate, fetch_bars

TIMEFRAMES = ("15m", "1h", "4h", "1d")

#: 各周期的语义。方向判断只认 direction 的周期。
ROLE = {
    "15m": "entry",      # 入场/出场时机，**不判方向**
    "1h": "direction",
    "4h": "direction",
    "1d": "direction",
}

#: 默认取多少根。够跑 MACD(12,26,9) 需要 ≥35 根，这里留足窗口。
DEFAULT_COUNT = {"15m": 200, "1h": 250, "4h": 200, "1d": 300}

#: 每个交易日大约几根（美股 RTH 6.5h）。用于反推要取多少根小周期。
_PER_SESSION = {"15m": 26, "1h": 7, "4h": 2, "1d": 1}

#: MTF 映射：某周期的"上一级"。脚本的 mtfRes 用的是 1→5→15→30→60→240→D→W→M，
#: 我们只有这四个周期，故按用户的分工映射到相邻的上一级。1d 无更高级。
MTF_PARENT = {"15m": "1h", "1h": "4h", "4h": "1d", "1d": None}

#: 每根 K 线从 ts 到收盘要多久。长桥的 ts 是**开盘时间**，
#: 跨周期比较必须用收盘时间，否则会把尚未走完的高周期值泄露给低周期。
#:   15m/1h/1d  ts 是该根自身的开盘 → 加自身跨度
#:   4h         合成而来，ts 是**组内最后一根 1h 的开盘** → 只需再加 1 小时
#:   1d         保守加满一天：日线 ts 是 ET 00:00，收盘 ET 16:00，
#:              但夏令时会漂；加一天必不泄露，且下一个交易时段本就在次日
#:              13:30 UTC 之后，实际零延迟。
_CLOSE_OFFSET = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1),
                 "4h": timedelta(hours=1), "1d": timedelta(days=1)}

_cache: dict[tuple[str, str, int], list[dict]] = {}


def bars(symbol: str, tf: str, *, count: int | None = None,
         use_cache: bool = True) -> list[dict]:
    """取某周期的 OHLCV。返回 [{ts, open, high, low, close, volume}, ...] 升序。

    取不到抛 KlineUnavailable —— **不返回空列表**，调用方必须显式处理，
    否则"没数据"会被静默当成"没信号"。
    """
    if tf not in TIMEFRAMES:
        raise ValueError(f"周期须为 {TIMEFRAMES} 之一，收到 {tf!r}")
    n = DEFAULT_COUNT[tf] if count is None else count
    if n <= 0:
        raise ValueError(f"count 必须为正，收到 {count!r}")
    key = (symbol, tf, n)
    if use_cache and key in _cache:
        return _cache[key]

    if tf == "4h":
        # 每交易日 7 根 1h → 2 根 4h，故需约 3.5 倍的 1h；再留 20% 余量。
        need = min(int(n * 3.5 * 1.2), 1000)
        raw = fetch_bars(symbol, period="1h", count=need)
        out = aggregate(raw, 4)[-n:]
    else:
        period = {"15m": "15m", "1h": "1h", "1d": "day"}[tf]
        out = fetch_bars(symbol, period=period, count=n)

    # docstring 承诺「取不到抛异常，不返回空列表」—— 这里兑现它。
    # 上游 fetch_bars 解析后可能返回空，静默放过等于把「没数据」当「没信号」。
    if not out:
        raise KlineUnavailable(f"{symbol} {tf} 返回空序列")
    off = _CLOSE_OFFSET[tf]
    for b in out:
        b["close_ts"] = b["ts"] + off
    if use_cache:
        _cache[key] = out
    return out


def closes(symbol: str, tf: str, **kw) -> list[float]:
    return [b["close"] for b in bars(symbol, tf, **kw)]


def multi(symbol: str, tfs: tuple[str, ...] = TIMEFRAMES,
          **kw) -> dict[str, list[dict]]:
    """一次取多个周期。某个周期取数失败**不影响其它周期**，缺的那个不出现在结果里。

    调用方应检查 key 是否存在，而不是假设四个周期都在。
    """
    out = {}
    for tf in tfs:
        try:
            out[tf] = bars(symbol, tf, **kw)
        except (KlineUnavailable, ValueError):
            continue
    return out


def clear_cache() -> None:
    _cache.clear()


def describe(symbol: str, tfs: tuple[str, ...] = TIMEFRAMES) -> list[dict]:
    """每个周期的时效体检：根数、首末时间、跨度。用来确认数据真的到位了。

    2026-08-27 的教训：技术面曾静默用着滞后两天的日线，报告里却只标了期权数据
    的时效。任何新周期接进来之前，先用这个函数看一眼它的末根是什么时候。
    """
    rows = []
    for tf in tfs:
        try:
            b = bars(symbol, tf)
        except (KlineUnavailable, ValueError) as e:
            rows.append({"tf": tf, "ok": False, "err": f"{type(e).__name__}: {e}"})
            continue
        rows.append({
            "tf": tf, "ok": True, "n": len(b),
            "first": b[0]["ts"], "last": b[-1]["ts"],
            "last_close": b[-1]["close"], "role": ROLE[tf],
        })
    return rows
