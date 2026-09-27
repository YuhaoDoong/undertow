"""长桥逐分钟 K 线采集（只读）：代码格式、解析、查不到的状态、日期校验、原子存储与损坏隔离、补抓计划。"""
from datetime import date

import pytest

from undertow.collect import longbridge_bars as lbb


def test_option_symbol_format():
    assert lbb.option_symbol("GLD", "2026-10-09", "P", 380) == "GLD261009P380000.US"
    assert lbb.option_symbol("SLV", "2026-09-28", "C", 58.5) == "SLV260928C58500.US"


BAR = {"time": "2026-09-21T13:30:00Z", "open": "1.35", "high": "1.35", "low": "1.33", "close": "1.33",
       "volume": "6", "turnover": "800.00"}


def test_fetch_day_ok_and_statuses():
    ok = lbb.fetch_day("X.US", date(2026, 9, 21), runner=lambda a: ("ok", [BAR]))
    assert ok["status"] == "ok" and ok["bars"] == [["2026-09-21T13:30:00Z", "1.35", "1.35", "1.33", "1.33", "6", "800.00"]]
    gone = lbb.fetch_day("X.US", date(2026, 9, 21), runner=lambda a: ("not_found", "code=301603"))
    assert gone["status"] == "not_found" and "bars" not in gone
    assert lbb.fetch_day("X.US", date(2026, 9, 21), runner=lambda a: ("ok", []))["status"] == "empty"


def test_fetch_day_rejects_other_dates_and_bad_fields():
    with pytest.raises(lbb.BarsUnavailable):
        lbb.fetch_day("X.US", date(2026, 9, 22), runner=lambda a: ("ok", [BAR]))
    with pytest.raises(lbb.BarsUnavailable):
        lbb.fetch_day("X.US", date(2026, 9, 21), runner=lambda a: ("ok", [{"time": "2026-09-21T13:30:00Z"}]))


def test_save_load_roundtrip_and_corrupt_quarantine(tmp_path):
    p = lbb.path_of("GLD", date(2026, 9, 21), base=tmp_path)
    obj = lbb.new_day("GLD", date(2026, 9, 21))
    obj["contracts"]["GLD.US"] = {"status": "ok", "bars": [["t", "1", "1", "1", "1", "0", "0"]], "fetched_at": "x"}
    lbb.save_day(p, obj)
    assert lbb.load_day(p) == obj and "无买卖价" in lbb.load_day(p)["basis"]
    p.write_bytes(b"not gzip")
    with pytest.raises(lbb.BarsFileCorrupt):
        lbb.load_day(p)
    q = lbb.quarantine(p)
    assert q.exists() and not p.exists() and q.read_bytes() == b"not gzip"


def test_bars_plan_covers_both_legs_through_expiry_capped():
    from undertow.shadow_cli import bars_plan
    row = {"symbol": "GLD", "session": "2026-09-22", "legs": [
        {"status": "candidate", "side": "P", "sell": 380, "buy": 379, "expiry": "2026-09-25"},
        {"status": "no_candidate", "side": "C", "sell": 400, "buy": 401, "expiry": "2026-09-25"}]}
    plan = bars_plan([row], last_day=date(2026, 9, 24))
    assert sorted(d.isoformat() for _, d in plan) == ["2026-09-22", "2026-09-23", "2026-09-24"]
    assert plan[("GLD", date(2026, 9, 23))] == {"GLD.US", "GLD260925P380000.US", "GLD260925P379000.US"}


def test_quota_error_is_distinct_from_not_found(monkeypatch):
    class P:
        returncode = 1
        stdout = ""
        stderr = "Error: WebSocket error (status=7, code=301607): history candlestick symbol count out of limit"
    monkeypatch.setattr(lbb.shutil, "which", lambda b: "/bin/longbridge")
    monkeypatch.setattr(lbb.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(lbb.BarsQuotaExhausted):
        lbb._run(["kline"])
    P.stderr = "Error: WebSocket error (status=7, code=301603): quote not found"
    assert lbb._run(["kline"])[0] == "not_found"


def test_bars_plan_scope_filters_instrument_and_rule():
    from undertow.shadow_cli import bars_plan
    rows = [{"symbol": "GLD", "instrument": "gold", "session": "2026-09-22", "legs": [
                {"status": "candidate", "rule": "A", "side": "P", "sell": 380, "buy": 379, "expiry": "2026-09-23"},
                {"status": "candidate", "rule": "B2", "side": "P", "sell": 370, "buy": 369, "expiry": "2026-09-23"}]},
            {"symbol": "NVDA", "instrument": "nvda", "session": "2026-09-22", "legs": [
                {"status": "candidate", "rule": "A", "side": "C", "sell": 230, "buy": 231, "expiry": "2026-09-23"}]}]
    plan = bars_plan(rows, last_day=date(2026, 9, 22), insts={"gold"}, rules=("A", "B1"))
    assert list(plan) == [("GLD", date(2026, 9, 22))]
    assert plan[("GLD", date(2026, 9, 22))] == {"GLD.US", "GLD260923P380000.US", "GLD260923P379000.US"}
