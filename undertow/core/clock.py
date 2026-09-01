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


# ═══════════════════════════════════════════════════════════════════════════
# 决策时段：一份快照到底能用来交易哪一天
# ═══════════════════════════════════════════════════════════════════════════
# ⚠️ 2026-09-01 codex P0：此前所有回测和台账都直接把【文件名日期】当作
# "D 开盘前已知"，完全丢弃了 captured_at。实测 193 份快照：
#     盘前抓 141 · 盘后抓 18 · 盘中抓 3 · 周末抓 31
# 早期（2026-06-25 ~ 07-02）全部是当晚 21:49~23:27 ET 抓的 —— 那些信息在
# 当日【收盘之后】才存在，却被当成当日开盘前可用，是不折不扣的前视。
# 另有 2026-07-21 GLD 在 09:58 ET 盘中抓取，既非开盘前、也无法确定成交价，
# 只能剔除。
MARKET_OPEN_MIN = 9 * 60 + 30      # 09:30 ET
MARKET_CLOSE_MIN = 16 * 60         # 16:00 ET

PRE, INTRADAY, POST = "pre", "intraday", "post"


def capture_phase(unix_ts: float) -> str:
    """快照抓取时刻落在美东的哪个阶段。

    周末/节假日按 POST 处理（信息已完整，但要等下一个交易日才能用）。
    """
    t = datetime.fromtimestamp(unix_ts, ET)
    if t.weekday() >= 5:
        return POST
    mins = t.hour * 60 + t.minute
    if mins < MARKET_OPEN_MIN:
        return PRE
    if mins < MARKET_CLOSE_MIN:
        return INTRADAY
    return POST


def decision_session(unix_ts: float, trading_days: list[date]) -> date | None:
    """这份快照最早能用于交易哪一天。

    · 盘前抓  → 当天（当天必须是交易日；否则顺延到下一个交易日）
    · 盘后抓  → 下一个交易日
    · 盘中抓  → **None**，直接剔除。开盘后才拿到的链既不能当开盘前信息用，
                也无法确定当天该按什么价成交；硬塞进回测就是前视。

    trading_days 必须是升序的交易日列表（用日线序列的日期即可）。
    返回 None 也可能是因为 trading_days 没有覆盖到那之后的日子。
    """
    phase = capture_phase(unix_ts)
    if phase == INTRADAY:
        return None
    d = datetime.fromtimestamp(unix_ts, ET).date()
    if phase == PRE and d in trading_days:
        return d
    for x in trading_days:              # 升序，取第一个严格晚于 d 的交易日
        if x > d:
            return x
    return None
