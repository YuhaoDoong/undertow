"""COT 报告的实际可用时刻（as-of 身份；Codex 008 G08）。纯函数，无 I/O。

原近似：entry = report_date（周二）+ 3 天 → 周五收盘定基。正常周五 15:30 ET 公布、周五收盘定基不算前视；
问题在假日/停摆周：例如 2026-11-10 的报告因 11-11 退伍军人节推到 11-16（周一）公布，
旧规则却在 11-13（周五）收盘就用上了它 —— 早于公开时刻。

规则（已用 CFTC 官方 2026 发布日程核对：6 个非周五发布日全部符合，读取 2026-09-26，
https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm ，页面称 3:30 p.m. ET、
「Federal holidays may delay release by one or two days」）：
  - 常规：as-of 周二之后的周五 15:30 ET；
  - 该周周三至周五有联邦假日 → 顺延到周五之后第一个非假日工作日（周一假日不影响当周周五）；
  - 政府停摆期间与其后补发期：没有逐周官方发布记录 → 返回 None（未知），调用方必须排除，不猜。
停摆窗口依据公开报道的停摆起止日，补发尾巴保守取停摆结束后 8 周；逐周真实发布日期未取得，属未核实。
"""
from __future__ import annotations

from datetime import date, timedelta

RELEASE_TIME_ET = "15:30"
BACKLOG_TAIL = timedelta(weeks=8)
# (停摆开始, 停摆结束, 补发期末：此日及之前 as-of 的报告发布时刻未知)
#   2025：官方 2026 日程显示 as-of 2025-12-30 的报告按常规假日规则于 2026-01-05 发布 → 补发期不晚于 12-29；
#   2013、2018-19：未取得逐周官方记录，保守取停摆结束后 8 周（未核实）。
SHUTDOWNS = [
    (date(2013, 10, 1), date(2013, 10, 16), date(2013, 10, 16) + BACKLOG_TAIL),
    (date(2018, 12, 22), date(2019, 1, 25), date(2019, 1, 25) + BACKLOG_TAIL),
    (date(2025, 10, 1), date(2025, 11, 12), date(2025, 12, 29)),
]


def _nth_weekday(y, m, weekday, n):
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y, m, weekday):
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else (d + timedelta(days=1) if d.weekday() == 6 else d)


def federal_holidays(y: int) -> set[date]:
    """美国联邦假日（含周末顺延的补假）。六月节自 2021 年起。"""
    h = {_observed(date(y, 1, 1)), _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3),
         _last_weekday(y, 5, 0), _observed(date(y, 7, 4)), _nth_weekday(y, 9, 0, 1),
         _nth_weekday(y, 10, 0, 2), _observed(date(y, 11, 11)), _nth_weekday(y, 11, 3, 4),
         _observed(date(y, 12, 25))}
    if y >= 2021:
        h.add(_observed(date(y, 6, 19)))
    h.add(_observed(date(y + 1, 1, 1)))          # 次年元旦落在本年 12-31 的补假
    return h


def _is_holiday(d: date) -> bool:
    return d in federal_holidays(d.year)


def available_date(report_date: date) -> tuple[date | None, str]:
    """(可用日期, 依据)。可用 = 该日 15:30 ET 公布，当日收盘可定基。None = 未知（停摆等）。"""
    for a, b, tail in SHUTDOWNS:
        if a - timedelta(days=7) <= report_date <= tail:
            return None, f"停摆 {a}~{b} 及补发期（至 {tail}）：发布时刻未知"
    fri = report_date + timedelta(days=((4 - report_date.weekday()) % 7) or 7)   # as-of 之后第一个周五
    week = [fri - timedelta(days=k) for k in (2, 1, 0)]          # 周三、周四、周五
    if not any(_is_holiday(d) for d in week):
        return fri, "常规周五"
    d = fri + timedelta(days=1)
    while d.weekday() >= 5 or _is_holiday(d):
        d += timedelta(days=1)
    return d, "联邦假日顺延"
