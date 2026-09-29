"""期权到期类型：Q（季度）/ M（月度）/ W（周五周度）/ D（非周五的日度）。纯计算，只按日期与交易日历判定。

用户 2026-09-29：「理论上长桥应该标注了 w，Q，还有月度期权三种类型」—— 长桥 CLI 的 `option chain` 只返回日期，
不带类型；App 上的标注是按交易所规则推出来的，这里用同一规则自己算，写死、可测：
- Q：每季度（3/6/9/12 月）最后一个交易日。ETF/指数的季度期权按此到期（9/30/2026 为周三）。
- M：每月第三个周五；该日休市 → 前一个交易日（如耶稣受难日）。Q 与 M 同日时记 Q（季度优先，另存 is_monthly）。
- W：其余的周五（及周五休市时顺延到的周四，仅当该周四本身不是 M/Q）。
- D：其余（周一至周四的日度到期）。
⚠️ 交易所的实际挂牌以链上为准；本模块只给「按规则应属哪类」，链上没有该日期的合约就不存在该到期。
"""
from __future__ import annotations

from datetime import date, timedelta

from undertow.core import market_calendar as mc


def _prev_or_same_trading(d: date) -> date | None:
    if mc.is_trading_day(d):
        return d
    return mc.prev_trading_day(d)


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def monthly_expiry(year: int, month: int) -> date | None:
    return _prev_or_same_trading(third_friday(year, month))


def quarterly_expiry(year: int, month: int) -> date | None:
    if month not in (3, 6, 9, 12):
        return None
    nxt = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return _prev_or_same_trading(nxt - timedelta(days=1))


def classify(exp: date) -> dict:
    """返回 {"type": Q/M/W/D, "is_monthly": bool, "is_quarterly": bool}。日历未覆盖 → type=None。"""
    td = mc.is_trading_day(exp)
    if td is None:
        return {"type": None, "is_monthly": None, "is_quarterly": None}
    if td is False:                                   # 休市日不可能有到期
        return {"type": None, "is_monthly": False, "is_quarterly": False, "non_trading_day": True}
    is_m = exp == monthly_expiry(exp.year, exp.month)
    is_q = exp == quarterly_expiry(exp.year, exp.month)
    if is_q:
        t = "Q"
    elif is_m:
        t = "M"
    elif exp.weekday() == 4 or (exp.weekday() == 3 and mc.is_trading_day(exp + timedelta(days=1)) is False):
        t = "W"
    else:
        t = "D"
    return {"type": t, "is_monthly": is_m, "is_quarterly": is_q}
