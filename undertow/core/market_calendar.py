"""美股交易所日历（预先认证、带来源；Codex 006 C02）。

为什么需要：影子账原先从【已有日线】推「哪天是交易日」。日线缺一根，那天的应有盘口窗口
就凭空消失 —— 缺数据被当成了休市，止损与提前退出的应有观测点随之少算。
交易日必须由一份事前保存的日历独立给出，再拿日线与原始报价去对账。

规则：
- 覆盖范围外的日期一律返回 None（未知），调用方记 calendar_unknown，不猜。
- 数据只来自 SOURCE 所列官方页面，读取日期写在 SOURCE 里；换数据 = 升 VERSION。
- 提前收市日（13:00）只影响收盘窗时刻；NYSE 页面注明 2026-12-24 部分期权 13:15 收市，
  本表按标的核心收市 13:00 定窗（12:30–12:45 早于两者），不假定期权也 13:00 截止。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

VERSION = "nyse-20260101-20270331-v1"
SOURCE = {
    "url": "https://www.nyse.com/trade/hours-calendars",
    "read_at": "2026-09-26",
    "note": "NYSE Holidays & Trading Hours；2027 年只取到 3 月底（覆盖 2026-12-31 前入场的全部到期日）",
}
COVERAGE = (date(2026, 1, 1), date(2027, 3, 31))
REGULAR_CLOSE = "16:00"

HOLIDAYS = frozenset({
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
})
EARLY_CLOSES = {date(2026, 11, 27): "13:00", date(2026, 12, 24): "13:00"}


def calendar_hash() -> str:
    blob = {"v": VERSION, "src": SOURCE, "cov": [d.isoformat() for d in COVERAGE],
            "hol": sorted(d.isoformat() for d in HOLIDAYS),
            "early": {d.isoformat(): t for d, t in sorted(EARLY_CLOSES.items())}}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]


def covered(d: date) -> bool:
    return COVERAGE[0] <= d <= COVERAGE[1]


def is_trading_day(d: date) -> bool | None:
    if not covered(d):
        return None
    return d.weekday() < 5 and d not in HOLIDAYS


def close_time(d: date) -> str | None:
    """该日核心收市时刻 "HH:MM"（ET）；非交易日或未知 → None。"""
    if not is_trading_day(d):
        return None
    return EARLY_CLOSES.get(d, REGULAR_CLOSE)


def trading_days(start: date, end: date) -> list[date] | None:
    """[start, end] 内的交易日；区间任一天不在覆盖范围 → None（整体未知，不给半截）。"""
    if start > end:
        return []
    if not (covered(start) and covered(end)):
        return None
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def prev_trading_day(d: date) -> date | None:
    """严格早于 d 的最近交易日；越出覆盖 → None。"""
    x = d - timedelta(days=1)
    while covered(x):
        if is_trading_day(x):
            return x
        x -= timedelta(days=1)
    return None


def next_trading_day(d: date) -> date | None:
    x = d + timedelta(days=1)
    while covered(x):
        if is_trading_day(x):
            return x
        x += timedelta(days=1)
    return None
