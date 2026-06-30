"""事件日历：加载、倒计时、窗口/品种过滤、排序。"""
import json
from datetime import date

from undertow.core.calendar import Event, load_events, upcoming


def _sample():
    return [
        Event(date(2026, 7, 2), "NFP", "data", "high", "08:30", ("gold", "wti"), "假期提前"),
        Event(date(2026, 7, 14), "CPI", "data", "high", "08:30", (), ""),  # 空=全局
        Event(date(2026, 7, 29), "FOMC", "fed", "high", "14:00", (), ""),
        Event(date(2026, 6, 20), "过去事件", "other", "low", "", (), ""),
    ]


def test_days_until_and_tminus():
    e = Event(date(2026, 7, 2), "NFP", "data", "high")
    assert e.days_until(date(2026, 6, 29)) == 3
    assert e.tminus(date(2026, 6, 29)) == "T-3"
    assert e.tminus(date(2026, 7, 2)) == "今天"


def test_affects_global_and_specific():
    nfp = Event(date(2026, 7, 2), "NFP", "data", "high", instruments=("gold", "wti"))
    cpi = Event(date(2026, 7, 14), "CPI", "data", "high", instruments=())
    assert nfp.affects("gold") and not nfp.affects("silver")
    assert cpi.affects("silver")  # 空 instruments = 全局，对谁都算


def test_upcoming_window_and_instrument_filter():
    evs = _sample()
    today = date(2026, 6, 29)
    # 21 天窗口：NFP(7/2)+CPI(7/14) 入，FOMC(7/29) 出，过去事件出
    within = upcoming(evs, today=today, within_days=21)
    names = [e.name for e in within]
    assert names == ["NFP", "CPI"]
    # 限定 silver：NFP 只影响 gold/wti → 仅剩全局的 CPI
    silver = upcoming(evs, today=today, within_days=21, instrument="silver")
    assert [e.name for e in silver] == ["CPI"]
    # 放宽到 40 天纳入 FOMC
    wide = upcoming(evs, today=today, within_days=40)
    assert "FOMC" in [e.name for e in wide]


def test_load_events_sorted(tmp_path):
    p = tmp_path / "calendar.json"
    p.write_text(json.dumps({"events": [
        {"date": "2026-07-29", "name": "FOMC", "category": "fed", "importance": "high"},
        {"date": "2026-07-02", "name": "NFP", "category": "data", "importance": "high"},
    ]}), encoding="utf-8")
    evs = load_events(p)
    assert [e.name for e in evs] == ["NFP", "FOMC"]  # 升序


def test_load_events_missing_file(tmp_path):
    assert load_events(tmp_path / "nope.json") == []


def test_real_calendar_file_parses():
    """仓库内的真实 config/calendar.json 必须可解析且非空（防手改坏）。"""
    evs = load_events()
    assert len(evs) >= 1
    assert all(isinstance(e.date, date) for e in evs)
