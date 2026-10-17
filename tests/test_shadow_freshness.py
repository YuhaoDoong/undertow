"""v5 输入新鲜度侧车（Codex 056 裁定 c）：expected 只看预存日历；周一、节假日后、正确与滞后各一例。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze import shadow as sh  # noqa: E402


def _row(session, base):
    return {"key": f"{session}|silver", "session": session, "decision": {"base_date": base}, "identity": {}}


def test_stale_by_one_trading_day():
    a = sh.freshness_audit_row(_row("2026-10-16", "2026-10-14"))
    assert a["expected_base_date"] == "2026-10-15" and a["age_trading_days"] == 1 and a["stale"] and a["affected"]


def test_monday_correct_is_friday():
    a = sh.freshness_audit_row(_row("2026-10-19", "2026-10-16"))
    assert a["age_trading_days"] == 0 and a["stale"] is False and a["affected"] == []


def test_weekend_gap_counts_trading_days_only():
    a = sh.freshness_audit_row(_row("2026-10-13", "2026-10-09"))      # 应为 10/12（周一）；10/09 落后 1 个交易日
    assert a["expected_base_date"] == "2026-10-12" and a["age_trading_days"] == 1


def test_missing_base_unknown():
    a = sh.freshness_audit_row({"key": "k", "session": "2026-10-16", "decision": {}, "identity": {}})
    assert a["stale"] is None
