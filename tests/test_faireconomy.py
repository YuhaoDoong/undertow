"""FairEconomy 经济日历 feed：解析、过滤、与手维护锚点合并去重。"""
from datetime import date

from undertow.core.calendar import Event, derive_token, merge
from undertow.collect.faireconomy_cal import FairEconomyCalSource

_FEED = [
    {"title": "Non-Farm Employment Change", "country": "USD",
     "date": "2026-07-02T08:30:00-04:00", "impact": "High",
     "forecast": "110K", "previous": "172K"},
    {"title": "CB Consumer Confidence", "country": "USD",
     "date": "2026-06-30T10:00:00-04:00", "impact": "Medium",
     "forecast": "94.4", "previous": "93.1"},
    {"title": "FOMC Statement", "country": "USD",
     "date": "2026-07-29T14:00:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "Low impact thing", "country": "USD",
     "date": "2026-07-02T09:00:00-04:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "German Ifo", "country": "EUR",
     "date": "2026-07-02T04:00:00-04:00", "impact": "High", "forecast": "x", "previous": "y"},
]


def test_derive_token():
    assert derive_token("Non-Farm Employment Change") == "nfp"
    assert derive_token("US CPI m/m") == "cpi"
    assert derive_token("FOMC Statement") == "fomc"
    assert derive_token("Federal Funds Rate") == "fomc"
    assert derive_token("CB Consumer Confidence") == ""


def test_to_event_parses_forecast_and_category():
    ev = FairEconomyCalSource._to_event(_FEED[0], "high")
    assert ev.date == date(2026, 7, 2)
    assert ev.time_et == "08:30"
    assert ev.forecast == "110K" and ev.previous == "172K"
    assert ev.source == "ff" and ev.match == "nfp"
    assert ev.category == "data"
    assert "预测110K" in ev.consensus() and "前值172K" in ev.consensus()
    fomc = FairEconomyCalSource._to_event(_FEED[2], "high")
    assert fomc.category == "fed"  # 标题含 FOMC → 归美联储


def test_fetch_events_filters_country_and_impact(monkeypatch):
    src = FairEconomyCalSource()
    # 注入 fixture，绕过网络
    monkeypatch.setattr(src, "_fetch_one",
                        lambda key, url, use_cache: _FEED if key == "thisweek" else [])
    evs = src.fetch_events(countries=("USD",), min_impact="medium", use_cache=False)
    titles = {e.name for e in evs}
    assert "Non-Farm Employment Change" in titles
    assert "CB Consumer Confidence" in titles      # Medium 入
    assert "Low impact thing" not in titles        # Low 滤掉
    assert "German Ifo" not in titles              # 非 USD 滤掉
    assert all(e.source == "ff" for e in evs)


def test_merge_dedup_feed_overrides_manual():
    manual = [
        Event(date(2026, 7, 2), "美国 6 月非农 (NFP)", "data", "high", match="nfp"),
        Event(date(2026, 7, 14), "美国 6 月 CPI", "data", "high", match="cpi"),
        Event(date(2026, 7, 6), "CFTC COT 持仓", "cot", "medium"),  # 无 token，永不去重
    ]
    live = [
        Event(date(2026, 7, 2), "Non-Farm Employment Change", "data", "high",
              forecast="110K", source="ff", match="nfp"),
    ]
    out = merge(manual, live)
    names = [e.name for e in out]
    # NFP：手维护被 feed 覆盖（同日同 token）→ 只剩 feed 版
    assert "Non-Farm Employment Change" in names
    assert "美国 6 月非农 (NFP)" not in names
    # CPI（feed 没有）+ COT（无 token）照常保留
    assert "美国 6 月 CPI" in names
    assert "CFTC COT 持仓" in names
    assert len(out) == 3
