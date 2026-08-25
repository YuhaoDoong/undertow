"""事件感知 digest 的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：影响本品种的临近事件筛选、高影响事件临近置顶告警、新闻近端过滤。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.core.calendar import Event
from undertow.collect.longbridge_news import NewsItem
from undertow.analyze.newsfeed import build_news_digest


def _news(title, d):
    return NewsItem(title=title, url="", published_at=f"{d.isoformat()}T00:00:00Z",
                    published_date=d)


def test_high_impact_event_near_triggers_alert():
    """2 天后 FOMC（高影响、影响 gold）→ 置顶告警 + level 高。"""
    events = [
        Event(date=date(2026, 8, 27), name="FOMC 利率决议", category="fed",
              importance="high", instruments=("gold", "silver")),
        Event(date=date(2026, 9, 20), name="季度 OPEX", category="opex", importance="low"),
    ]
    dg = build_news_digest("gold", "黄金 Gold", [], events, today=date(2026, 8, 25))
    assert dg.alert and dg.alert_level == "高", dg
    assert "FOMC" in dg.alert and "2 天后" in dg.alert, dg.alert
    assert any(e.name == "FOMC 利率决议" for e in dg.events), dg.events
    print(f"PASS test_high_impact_event_near_triggers_alert → {dg.alert}")


def test_event_not_affecting_instrument_excluded():
    """只影响 wti 的事件不该进 gold 的 digest。"""
    events = [Event(date=date(2026, 8, 27), name="EIA 原油库存", category="data",
                    importance="high", instruments=("wti",))]
    dg = build_news_digest("gold", "黄金 Gold", [], events, today=date(2026, 8, 25))
    assert not dg.events, dg.events
    assert not dg.alert, dg.alert
    print("PASS test_event_not_affecting_instrument_excluded")


def test_far_high_event_no_alert_but_listed():
    """高影响但 >near_days（10 天后）→ 不告警但仍列入事件表。"""
    events = [Event(date=date(2026, 9, 4), name="非农就业", category="data",
                    importance="high", instruments=())]  # 空=影响全部
    dg = build_news_digest("silver", "白银 Silver", [], events, today=date(2026, 8, 25))
    assert not dg.alert, dg.alert
    assert any("非农" in e.name for e in dg.events), dg.events
    print("PASS test_far_high_event_no_alert_but_listed")


def test_news_recency_filter_and_order():
    """新闻按近端过滤（默认 7 天）并降序；旧新闻剔除。"""
    items = [_news("旧闻", date(2026, 8, 10)),        # 15 天前，剔除
             _news("次新", date(2026, 8, 22)),
             _news("最新", date(2026, 8, 24))]
    dg = build_news_digest("silver", "白银 Silver", items, [], today=date(2026, 8, 25))
    titles = [it.title for it in dg.items]
    assert titles == ["最新", "次新"], titles
    print(f"PASS test_news_recency_filter_and_order → {titles}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
