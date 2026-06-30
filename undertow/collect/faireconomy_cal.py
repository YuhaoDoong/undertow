"""经济日历实时源 —— FairEconomy 公开 JSON feed（ForexFactory 数据方）。

为什么用它、为什么合法：用户要"能定期发布、带预测和影响"的事件源。直接爬
ForexFactory/Investing 的网页有 Cloudflare 反爬 + ToS 禁止，撞本项目"不绕过任何
反爬/ToS"的底线（同拒爬 CME）。而 `nfs.faireconomy.media/ff_calendar_*.json` 是
ForexFactory 数据方**公开发布的 JSON feed**（给日历挂件消费用），不在反爬后面、
正常请求即 200——属于"消费公开 feed"（类似订阅 RSS），不是破解网页。

提供：title / country / date(带美东时区) / impact(High|Medium|Low) / forecast / previous
（发布后回填 actual）。**只稳定覆盖本周**（nextweek 临近周末才填充，月度/日度 feed 不存在），
所以远期锚点（FOMC 全年/COT/OPEX）仍由手维护 config/calendar.json 兜，二者在
core.calendar.merge() 合并去重（同日同 token 以本 feed 为准，因其带预测值）。

数据用途：FairEconomy/ForexFactory 日历数据按其条款仅供个人/非商业使用——本项目
为个人研判工具，符合；报告中如实标注来源 (FF)。礼貌使用：带缓存、不频繁拉取。
"""
from __future__ import annotations

from datetime import datetime

from undertow.collect.base import http_get_text, DataSourceError
from undertow.collect.cache import FileCache
from undertow.core.calendar import Event, derive_token
from undertow.core.clock import ET

FEEDS = {
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}
_IMPACT = {"High": "high", "Medium": "medium", "Low": "low"}
_FED_HINTS = ("fomc", "federal funds", "fed chair", "fed monetary", "powell",
              "interest rate", "beige book", "fed ")


class FairEconomyCalSource:
    name = "faireconomy"
    CACHE_TTL = 4 * 3600  # 日历日内变化小，4h 缓存够用且礼貌

    def __init__(self, cache: FileCache | None = None) -> None:
        self.cache = cache or FileCache()

    def _fetch_one(self, key: str, url: str, use_cache: bool) -> list[dict]:
        import json
        text = self.cache.get(f"ffcal_{key}", self.CACHE_TTL if use_cache else 0) if use_cache else None
        if text is None:
            text = http_get_text(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            self.cache.set(f"ffcal_{key}", text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def fetch_events(self, *, countries: tuple[str, ...] = ("USD",),
                     min_impact: str = "medium", use_cache: bool = True) -> list[Event]:
        """拉本周+下周 feed，过滤指定币种与影响级别，转成 Event（美东日历）。

        失败/空 feed 优雅降级为 []（上层退回手维护锚点），不抛断流程。
        """
        rank = {"low": 1, "medium": 2, "high": 3}
        floor = rank.get(min_impact, 2)
        raw: list[dict] = []
        for key, url in FEEDS.items():
            try:
                raw += self._fetch_one(key, url, use_cache)
            except DataSourceError:
                continue  # 单个 feed 失败不影响另一个
        out: list[Event] = []
        for r in raw:
            if r.get("country") not in countries:
                continue
            imp = _IMPACT.get(r.get("impact", ""), "")
            if not imp or rank[imp] < floor:
                continue
            ev = self._to_event(r, imp)
            if ev is not None:
                out.append(ev)
        return out

    @staticmethod
    def _to_event(r: dict, importance: str) -> Event | None:
        ds = r.get("date", "")
        try:
            dt = datetime.fromisoformat(ds).astimezone(ET)
        except (ValueError, TypeError):
            return None
        title = (r.get("title") or "").strip()
        if not title:
            return None
        low = title.lower()
        category = "fed" if any(h in low for h in _FED_HINTS) else "data"
        # feed 时间若为 00:00（全天/待定）则不显示具体时刻
        tm = dt.strftime("%H:%M")
        return Event(
            date=dt.date(),
            name=title,
            category=category,
            importance=importance,
            time_et="" if tm == "00:00" else tm,
            instruments=(),  # 美元宏观事件=全局影响
            note="",
            forecast=(r.get("forecast") or "").strip(),
            previous=(r.get("previous") or "").strip(),
            actual=(r.get("actual") or "").strip(),
            source="ff",
            match=derive_token(title),
        )
