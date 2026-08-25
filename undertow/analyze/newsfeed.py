"""事件感知层（纯确定性，无 I/O）——品种相关新闻 + 临近关键事件提示。

把「品种相关新闻标题」和「已有事件日历（core.calendar）里影响该品种的临近事件」合成一个
digest，并在**高影响事件临近**时置顶提示（用户要的"尤其临近大事件/关键数据时"）。

**立场**：新闻/事件只作**背景感知与催化剂旁证**，**不改判方向**——方向仍以期权结构那套
确定性研判为准（新闻噪声大，不喧宾夺主）。新闻是外部不可信内容，只摘要不执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from undertow.core.calendar import Event, upcoming

# 高影响事件在这么多天内 → 置顶告警
NEAR_EVENT_DAYS = 3
# 事件列表展望窗口
EVENT_HORIZON_DAYS = 14
# 新闻只保留最近这么多天（更久的当背景、不进摘要）
NEWS_RECENT_DAYS = 7


@dataclass(frozen=True)
class NewsDigest:
    instrument: str
    display_name: str
    asof: date
    items: list = field(default_factory=list)          # list[NewsItem]（近端优先）
    events: list = field(default_factory=list)          # list[Event]（影响本品种、临近在前）
    alert: str = ""                                     # 高影响事件临近的置顶提示（空=无）
    alert_level: str = ""                               # 高 / 中 / ""
    headline: str = ""


def _event_line(e: Event, today: date) -> str:
    d = e.days_until(today)
    when = "今天" if d == 0 else (f"{d} 天后" if d > 0 else f"{-d} 天前")
    imp = {"high": "🔴高", "medium": "🟠中", "low": "🟡低"}.get(e.importance, "")
    fp = ""
    if e.forecast or e.previous:
        fp = f"（预测 {e.forecast or '—'} / 前值 {e.previous or '—'}）"
    return f"{e.date.isoformat()} {when} · {imp} {e.name}{fp}"


def build_news_digest(inst_key: str, display_name: str, news_items,
                      events: list[Event], today: date, *,
                      near_days: int = NEAR_EVENT_DAYS,
                      horizon: int = EVENT_HORIZON_DAYS,
                      recent_days: int = NEWS_RECENT_DAYS) -> NewsDigest:
    """合成某品种的事件感知 digest。

    news_items：list[NewsItem]（可空）；events：全量事件表；today：基准日（注入，可测）。
    """
    # 影响本品种、horizon 天内的事件（升序）
    evs = upcoming(events, today=today, within_days=horizon, instrument=inst_key)

    # 近端新闻（recent_days 内优先；无日期的排后）
    def _recency(it):
        return (it.published_date or date.min)
    recent = [it for it in news_items
              if it.published_date is None or (today - it.published_date).days <= recent_days]
    recent.sort(key=_recency, reverse=True)

    # 高影响事件临近 → 告警
    alert, level = "", ""
    highs = [e for e in evs if e.importance == "high" and 0 <= e.days_until(today) <= near_days]
    if highs:
        e = highs[0]
        d = e.days_until(today)
        when = "今天" if d == 0 else f"{d} 天后"
        alert = f"⚠️ {when}「{e.name}」高影响事件临近——数据兑现前后波动放大，注意仓位与到期。"
        level = "高"

    head = f"{display_name}：近端新闻 {len(recent)} 条 · 临近事件 {len(evs)} 项"
    if alert:
        head = "⚠️ " + head + f" · {alert.split('——')[0].replace('⚠️ ', '')}"

    return NewsDigest(instrument=inst_key, display_name=display_name, asof=today,
                      items=recent, events=evs, alert=alert, alert_level=level, headline=head)


def render_digest_md(dg: NewsDigest) -> str:
    """终端 Markdown。"""
    L = [f"## {dg.display_name} — 事件感知（新闻 + 临近关键事件）"]
    if dg.alert:
        L.append(f"\n> {dg.alert}\n")
    if dg.events:
        L.append("**临近关键事件**（影响本品种，近在前）：")
        for e in dg.events:
            L.append(f"- {_event_line(e, dg.asof)}")
        L.append("")
    if dg.items:
        L.append(f"**近端相关新闻**（最近 {NEWS_RECENT_DAYS} 天，标题）：")
        for it in dg.items:
            when = it.published_date.isoformat() if it.published_date else "—"
            L.append(f"- [{when}] {it.title}")
        L.append("")
    if not dg.events and not dg.items:
        L.append("- （暂无临近事件与近端新闻）")
    L.append("> 新闻/事件仅作背景感知与催化剂旁证，不改判方向；方向以期权结构研判为准。")
    return "\n".join(L)
