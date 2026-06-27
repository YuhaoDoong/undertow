"""统一时钟：以【美东时间】为基准。

为什么：盯的是美国市场（COMEX/NYMEX/CBOE/CFTC），交易日按美东 (America/New_York) 算。
用户在新加坡 (SGT, UTC+8)，本机 date.today() 是 SGT 日期，会比美东快约半天到一天
（如 SGT 周六上午 = 美东周五晚），导致快照按 SGT 日期落盘、与真实交易日错位。
本模块把"今天/某时刻属于哪个交易日"统一锚定到美东。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def market_today() -> date:
    """当前的美东日期（≈最新交易日；周末/盘后为最近的日历日）。"""
    return datetime.now(ET).date()


def market_date(unix_ts: float) -> date:
    """某 unix 时间戳对应的美东日期（用于把历史落盘按美东日归位）。"""
    return datetime.fromtimestamp(unix_ts, ET).date()
