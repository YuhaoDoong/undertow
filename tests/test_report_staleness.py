"""2026-09-28：日线滞后导致研报日期退一天、墙位台账对已过交易日重算触发冲突告警。"""
from datetime import date

from undertow import cli


def test_data_source_day_uses_calendar_when_prices_lag(capsys):
    px = [date(2026, 9, 23), date(2026, 9, 24)]                     # 缺 9/25
    assert cli._data_source_day("2026-09-28", "2026-09-25", px, "spy") == "2026-09-25"
    assert "落后于快照日前一交易日" in capsys.readouterr().err
    px_ok = px + [date(2026, 9, 25)]
    assert cli._data_source_day("2026-09-28", "2026-09-25", px_ok, "spy") == "2026-09-25"


def test_cboe_history_refetches_stale_cache_once(tmp_path, monkeypatch):
    from undertow.collect import cboe_history as ch
    from undertow.collect.cache import FileCache
    from undertow.core.config import load_config
    cache = FileCache(root=tmp_path)
    old = {"data": [{"date": "2026-09-24", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}
    new = {"data": old["data"] + [{"date": "2026-09-25", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]}
    cache.set("cboehist_SPY", old)
    calls = []
    monkeypatch.setattr(ch, "http_get_json", lambda url: calls.append(url) or new)
    monkeypatch.setattr(ch, "_expected_last_bar", lambda: date(2026, 9, 25))
    monkeypatch.setattr(ch, "_STALE_REFETCHED", set()); monkeypatch.setattr(ch, "_STALE_WARNED", set())
    src = ch.CboeHistorySource(cache=cache)
    assert src.fetch_series(load_config().get("spy")).dates[-1] == date(2026, 9, 25) and len(calls) == 1
    assert src.fetch_series(load_config().get("spy")).dates[-1] == date(2026, 9, 25) and len(calls) == 1


def test_wall_ledger_skips_past_sessions_without_alert():
    src = (cli.__file__ and open(cli.__file__, encoding="utf-8").read())
    i = src.index("raise _SkipLedger()")
    assert src.index("except _SkipLedger:") > i and src.index("except _SkipLedger:") < src.index("价差台账落盘失败")
    assert "_ws_sess_s < market_today().isoformat()" in src and "not replay" in src[i - 400:i]
