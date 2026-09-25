"""统一时钟：以【美东时间】为基准。

为什么：盯的是美国市场（COMEX/NYMEX/CBOE/CFTC），交易日按美东 (America/New_York) 算。
用户在新加坡 (SGT, UTC+8)，本机 date.today() 是 SGT 日期，会比美东快约半天到一天
（如 SGT 周六上午 = 美东周五晚），导致快照按 SGT 日期落盘、与真实交易日错位。
本模块把"今天/某时刻属于哪个交易日"统一锚定到美东。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
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


# ═══════════════════════════════════════════════════════════════════════════
# 数据源「停更」判据
# ═══════════════════════════════════════════════════════════════════════════
# 2026-09-24：CBOE 接口卡在 2026-09-22T15:59:59 超过 34 小时。管线每个时点都
# 判「与上一交易日逐行相同」→ 一份没落盘、研报缺两天、**零告警**，因为
# 「今天还没结算」（正常）与「源停止更新」（致命）共用同一个表象。
#
# 判据只能数【交易日】不能数日历日：周五收盘的 OI 要等周一凌晨才结算，
# 周末数日历日必然误报（见 AGENTS.md「周末拿不到周五的 OI」）。
#
# ⚠️ 本函数是 cli.cmd_snapshot 的降级开关与 scripts/daily_update.sh 的告警
# **共用的唯一实现**。两处各写一遍必然漂移（AGENTS.md：同一个量不许算两遍）。
STALE_SESSIONS = 2      # 跨过这么多个交易日仍无新 OI → 判定源停更


def sessions_between(last: date, today: date) -> int:
    """从 last（不含）到 today（含）之间有几个交易日。

    正常状态是 **1**：今天凌晨拿到的是上一交易日收盘结算的 OI。
    ≥2 意味着中间整整跳过了一个交易日 —— 那一天的 OI 已经永久丢失。

    近似：只排除周末，不排除美股节假日（config/calendar.json 只有宏观事件，
    没有交易所休市表）。节假日会让计数偏大 1，方向是【更容易告警】，
    对「宁可多叫一次也不能漏」的用途是可接受的偏保守。
    """
    n, d = 0, last
    while d < today:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n
