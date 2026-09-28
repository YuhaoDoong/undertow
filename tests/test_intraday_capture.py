"""当天逐分钟采集（用户 2026-09-28「记住数据最重要」）：解析、日期校验、收盘前不抓、盘中抓的收盘后重抓、历史补数避让。"""
import json
from datetime import date, datetime, timezone

import pytest

from undertow.collect import longbridge_bars as lbb

D = date(2026, 9, 28)
ROWS = [{"time": "2026-09-28T13:30:00Z", "price": "0.02", "volume": "126", "turnover": "286.00", "avg_price": "0"},
        {"time": "2026-09-28T13:31:00Z", "price": "0.01", "volume": "23", "turnover": "46.00", "avg_price": "0"}]


def test_fetch_intraday_today_parses_and_checks_date():
    r = lbb.fetch_intraday_today("X.US", D, runner=lambda a: ("ok", ROWS))
    assert r["status"] == "ok" and r["rows"][0] == ["2026-09-28T13:30:00Z", "0.02", "126", "286.00", "0"]
    assert lbb.fetch_intraday_today("X.US", D, runner=lambda a: ("ok", []))["status"] == "empty"
    with pytest.raises(lbb.BarsUnavailable):
        lbb.fetch_intraday_today("X.US", date(2026, 9, 29), runner=lambda a: ("ok", ROWS))   # 日期不符不静默存
    assert lbb.fetch_intraday_today("X.US", D, runner=lambda a: ("not_found", "e"))["status"] == "not_found"


def test_intraday_covered(tmp_path):
    cur = lbb.new_intraday_day("GLD", D)
    cur["contracts"] = {"A.US": {"status": "ok", "rows": []}, "B.US": {"status": "empty", "rows": []}}
    lbb.save_day(lbb.path_of("GLD", D, tmp_path), cur)
    assert lbb.intraday_covered("GLD", D, tmp_path) == {"A.US"}


def _env(tmp_path, monkeypatch, hm):
    from undertow import shadow_cli as sc

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 28, *hm, tzinfo=sc.ET)
    monkeypatch.setattr(sc, "datetime", FakeDT)
    monkeypatch.setattr(sc, "market_today", lambda: D)
    monkeypatch.setattr(lbb, "INTRADAY_DIR", tmp_path / "intra")
    monkeypatch.setattr(sc, "bars_plan", lambda rows, last_day: {("GLD", D): {"GLD.US", "GLD260930P380000.US"}})
    monkeypatch.setattr(sc, "INTRADAY_PACE_S", 0)
    calls = []

    def fake(symbol, day, runner=None):
        calls.append(symbol)
        return {"status": "ok", "rows": [["2026-09-28T13:30:00Z", "1", "1", "1", "0"]],
                "fetched_at": FakeDT.now().astimezone(timezone.utc).isoformat()}
    monkeypatch.setattr(lbb, "fetch_intraday_today", fake)

    class A:
        status_file, force = None, False
    return sc, A, calls


def test_not_before_close_unless_forced_and_forced_is_refetched(tmp_path, monkeypatch):
    sc, A, calls = _env(tmp_path, monkeypatch, (11, 0))
    assert sc.cmd_intraday(A()) == 0 and calls == []                         # 16:05 前不抓
    a = A(); a.force = True
    sc.cmd_intraday(a)
    assert len(calls) == 2
    sc2, A2, calls2 = _env(tmp_path, monkeypatch, (16, 10))
    sc2.cmd_intraday(A2())
    assert len(calls2) == 2                                                  # 盘中抓的不完整 → 收盘后重抓
    sc3, A3, calls3 = _env(tmp_path, monkeypatch, (16, 30))
    sc3.cmd_intraday(A3())
    assert calls3 == []                                                      # 收盘后已完整 → 不重抓
    got = lbb.load_day(lbb.path_of("GLD", D, tmp_path / "intra"))
    assert set(got["contracts"]) == {"GLD.US", "GLD260930P380000.US"} and got["basis"] == lbb.BASIS_INTRADAY


def test_session_hook_runs_intraday_after_close():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert src.index("intraday_capture() {") < src.index("then intraday_capture; fi")   # 定义在调用之前
    assert "(( ET_MIN >= 965 )); then intraday_capture; fi" in src and "shadow intraday" in src
