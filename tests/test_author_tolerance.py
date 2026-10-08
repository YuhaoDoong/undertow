"""作者价位 v5 容差带（用户 2026-10-08）：带宽对称——同一宽度放宽「触及」也放宽「跌破」；日线缺根时用小时线判触及。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import author_levels_score as a  # noqa: E402


def bars(start, closes, lo_off=5, hi_off=5):
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, c, c + hi_off, c - lo_off, c)); d += timedelta(days=1)
    return out


R = {"author": "x", "posted_at": "2026-09-01T09:00:00+08:00", "claim": "support", "params": {"level": 4000.0}}
PRE = bars(date(2026, 8, 10), [4100] * 16)                      # ATR ≈ 10 → 带宽 = max(12, 2.5) = 12


def test_near_miss_counts_as_touch_and_rebound_held():
    d = PRE + bars(date(2026, 9, 1), [4030, 4011, 4040, 4060, 4070, 4080, 4090], lo_off=1)   # 最低 4010 在 4000+12 内
    r = a.score_tol(R, d, datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert r["band"] == 12.0 and r["result"] == "held" and r["touch"] == "2026-09-02"


def test_same_band_used_for_break():
    d = PRE + bars(date(2026, 9, 1), [4005, 3995, 3990, 4000, 4010, 4020], lo_off=1)          # 收 3990 未低于 4000−12
    assert a.score_tol(R, d, datetime(2026, 9, 20, tzinfo=timezone.utc))["result"] in ("ambiguous_stuck", "held")
    d = PRE + bars(date(2026, 9, 1), [4005, 3985, 3990, 4000, 4010, 4020], lo_off=1)          # 收 3985 < 3988 → 跌破
    assert a.score_tol(R, d, datetime(2026, 9, 20, tzinfo=timezone.utc))["result"] == "broken"


def test_hourly_touch_when_daily_bar_missing():
    d = PRE + bars(date(2026, 9, 1), [4050, 4060, 4070, 4080, 4090, 4100, 4100], lo_off=1)
    h = [(datetime(2026, 9, 2, 14, tzinfo=timezone.utc), 4040, 4045, 4008, 4040)]               # 日线没体现、小时线有 4008
    r = a.score_tol(R, d, datetime(2026, 9, 20, tzinfo=timezone.utc), hourly=h)
    assert r["touch"] == "2026-09-02" and r["touch_low_or_high"] == 4008


def test_far_miss_untouched_and_other_claims_not_applicable():
    d = PRE + bars(date(2026, 9, 1), [4100] * 20)
    assert a.score_tol(R, d, datetime(2026, 10, 30, tzinfo=timezone.utc))["result"] == "untouched"
    assert a.score_tol({**R, "claim": "no_support"}, d, datetime(2026, 10, 30, tzinfo=timezone.utc))["result"] == "not_applicable"
