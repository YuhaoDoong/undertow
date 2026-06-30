"""事件日历 —— 关键宏观/市场节点登记与"事件雷达"。

为什么：持仓与期权结构的研判在【催化剂】前后最有价值——临近 FOMC/CPI/非农时
应主动调低置信、警惕事件跳空与到期前 Gamma 失真。把已知节点集中登记，每次更新
数据自动提示"距 XX 还有 N 天"，避免裸奔进数据。

日期一律美东 (ET) 口径，与 clock.market_today 对齐（用户在新加坡，本机日期会偏快）。
数据来源见 config/calendar.json 的 _sources；本模块只做读取/过滤/排序，不取数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from undertow.core.clock import market_today
from undertow.core.config import PROJECT_ROOT

CALENDAR_PATH = PROJECT_ROOT / "config" / "calendar.json"

CATEGORY_LABEL = {
    "fed": "美联储", "data": "数据", "cot": "持仓", "opex": "期权到期", "other": "其它",
}
SEVERITY_MARK = {"high": "🔴", "medium": "🟡", "low": "⚪"}
_IMPORTANCE_RANK = {"high": 3, "medium": 2, "low": 1}


# 用于「手维护锚点 ↔ 实时 feed」去重的归一化 token（feed 英文标题 → token）。
# 只为有手维护锚点的事件设 token（其余 feed 事件 token="" 不参与去重）。
_TOKEN_RULES = (
    ("nfp", ("non-farm", "nonfarm", "non farm employment")),
    ("cpi", ("cpi", "consumer price")),
    ("fomc", ("fomc", "federal funds", "interest rate decision")),
    ("ppi", ("ppi", "producer price")),
)


def derive_token(title: str) -> str:
    t = title.lower()
    for tok, kws in _TOKEN_RULES:
        if any(k in t for k in kws):
            return tok
    return ""


@dataclass
class Event:
    date: date
    name: str
    category: str = "other"          # fed / data / cot / opex / other
    importance: str = "medium"       # high / medium / low
    time_et: str = ""
    instruments: tuple[str, ...] = field(default_factory=tuple)  # 空=影响全部
    note: str = ""
    forecast: str = ""               # 市场预测/共识（feed 提供）
    previous: str = ""               # 前值
    actual: str = ""                 # 实际值（发布后 feed 回填）
    source: str = "manual"           # manual=手维护锚点 / ff=ForexFactory/FairEconomy feed
    match: str = ""                  # 去重 token（手维护可显式给；feed 自动派生）

    def days_until(self, today: date) -> int:
        return (self.date - today).days

    def affects(self, instrument: str | None) -> bool:
        """该事件是否影响某品种（事件未指定品种=全局影响，对谁都算）。"""
        return (not self.instruments) or (instrument in self.instruments) if instrument else True

    @property
    def mark(self) -> str:
        return SEVERITY_MARK.get(self.importance, "⚪")

    def tminus(self, today: date) -> str:
        d = self.days_until(today)
        return "今天" if d == 0 else (f"T-{d}" if d > 0 else f"T+{-d}")

    def consensus(self) -> str:
        """预测/前值/实际的紧凑串，无数据返回空。"""
        bits = []
        if self.actual:
            bits.append(f"实际{self.actual}")
        if self.forecast:
            bits.append(f"预测{self.forecast}")
        if self.previous:
            bits.append(f"前值{self.previous}")
        return " ".join(bits)


def load_events(path: Path | None = None) -> list[Event]:
    """读取事件表，按日期升序。文件缺失返回空列表（功能优雅降级）。"""
    p = path or CALENDAR_PATH
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: list[Event] = []
    for e in raw.get("events", []):
        out.append(Event(
            date=date.fromisoformat(e["date"]),
            name=e["name"],
            category=e.get("category", "other"),
            importance=e.get("importance", "medium"),
            time_et=e.get("time_et", ""),
            instruments=tuple(e.get("instruments", ())),
            note=e.get("note", ""),
            source="manual",
            match=e.get("match", ""),
        ))
    out.sort(key=lambda x: (x.date, -_IMPORTANCE_RANK.get(x.importance, 0)))
    return out


def merge(manual: list[Event], live: list[Event]) -> list[Event]:
    """合并手维护锚点与实时 feed：同(日期, token)以 feed 为准（它带预测值/精确时间）。

    feed 全部保留；手维护事件若与某 feed 事件落在同一天且 token 相同则丢弃（被 feed 覆盖）。
    手维护的 COT/OPEX 等 feed 没有的事件 token 为空，永不被去重，照常保留。
    """
    feed_keys = {(e.date, e.match) for e in live if e.match}
    kept = [m for m in manual if not (m.match and (m.date, m.match) in feed_keys)]
    out = live + kept
    out.sort(key=lambda x: (x.date, -_IMPORTANCE_RANK.get(x.importance, 0)))
    return out


def upcoming(events: list[Event], *, today: date | None = None,
             within_days: int = 21, instrument: str | None = None,
             include_today: bool = True) -> list[Event]:
    """未来 within_days 天内、（可选）影响某品种的事件，按日期升序。"""
    today = today or market_today()
    lo = 0 if include_today else 1
    return [e for e in events
            if lo <= e.days_until(today) <= within_days and e.affects(instrument)]
