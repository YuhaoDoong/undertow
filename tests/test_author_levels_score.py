"""外部作者价位计分 v2（Codex 025-3 反例）：未来截止 pending、未触发入场不算赢、只用完整 K 线、
「整段守住」语义并单记 rebound_first/later_broken、tol 各分支一致、窗口为空、跨截止那根。纯合成数据。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import author_levels_score as au  # noqa: E402

ET = ZoneInfo("America/New_York")


def at(d, h=10, m=0):
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET)


def days(start, closes, lows=None, highs=None):
    """交易日（跳过周末）日线。"""
    out, d = [], date.fromisoformat(start)
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, c, (highs or closes)[i], (lows or closes)[i], c))
        d += timedelta(days=1)
    return out


def hours(start, lows_highs):
    t0 = at(start, 11).astimezone(timezone.utc)
    return [(t0 + timedelta(hours=i), (lo + hi) / 2, hi, lo, (lo + hi) / 2) for i, (lo, hi) in enumerate(lows_highs)]


def test_future_deadline_is_pending():
    """Codex probe：9/29 只有一根线、截止 10/10 → 旧版判 miss。"""
    r = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "break_below",
         "params": {"level": 100, "deadline": at("2026-10-10", 16).isoformat()}}
    s = au.score(r, [(date(2026, 9, 29), 102, 103, 101, 102)], hours("2026-09-29", [(101, 103)]), .005, at("2026-09-30", 9))
    assert s["result"] == "pending"


def test_target_without_entry_is_not_a_win():
    """Codex probe：价格一直在 101 以上、entry=100，旧版判 target_first。"""
    r = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "trade_long",
         "params": {"entry": 100, "stop": 99, "target": 103, "days": 5}}
    d = days("2026-09-29", [102] * 5)
    h = hours("2026-09-29", [(101, 104)] * 30)
    s = au.score(r, d, h, .0, at("2026-10-07", 9))
    assert s["result"] == "not_triggered" and s["entry_status_assumed"]
    s2 = au.score(r, d, h, .0, at("2026-09-30", 9))
    assert s2["result"] == "pending"                                       # 窗口未满 → pending 而非 not_triggered


def test_trade_long_after_trigger_and_same_bar_ambiguous():
    r = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "trade_long",
         "params": {"entry": 100, "stop": 98, "target": 104, "days": 5}}
    d = days("2026-09-29", [101] * 5)
    s = au.score(r, d, hours("2026-09-29", [(101, 102), (99.5, 101), (99, 101), (101, 105)]), 0, at("2026-10-07", 9))
    assert s["result"] == "target_first" and "triggered_at" in s
    s = au.score(r, d, hours("2026-09-29", [(101, 102), (97, 101)]), 0, at("2026-10-07", 9))
    assert s["result"] == "ambiguous"                                      # 触发那根同时穿止损：先后不明
    r["params"]["entry_status"] = "declared_filled"
    s = au.score(r, d, hours("2026-09-29", [(101, 102), (97, 101)]), 0, at("2026-10-07", 9))
    assert s["result"] == "stop_first" and not s["entry_status_assumed"]


def test_incomplete_bars_are_not_used():
    r = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "no_support", "params": {"level": 100}}
    d = days("2026-09-29", [99])                                          # 9/29 当日 K 线，17:00 才收
    assert au.score(r, d, hours("2026-09-29", [(98, 100)]), 0, at("2026-09-29", 15))["result"] == "pending"
    assert au.score(r, d, hours("2026-09-29", [(98, 100)]), 0, at("2026-09-29", 18))["result"] == "hit"
    h = hours("2026-09-29", [(98, 100)])                                   # 11:00 起的小时线 12:00 才完整
    r2 = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "support", "params": {"level": 99}}
    assert au.score(r2, d, h, 0, at("2026-09-29", 11, 30))["result"] == "pending"


def test_support_whole_window_semantics_rebound_then_break():
    """先反弹 ≥ L×1.01 再跌破：旧版取先发生的 → held；v2 整段守住才算 held。"""
    r = {"posted_at": at("2026-09-28", 9).isoformat(), "claim": "support", "params": {"level": 100}}
    d = days("2026-09-28", [100.2, 101.5, 101.2, 99.0, 100.5, 100.5, 100.5] + [100.5] * 20)
    h = hours("2026-09-28", [(99.9, 100.5)])
    s = au.score(r, d, h, 0, at("2026-11-30", 9))
    assert s["result"] == "broken" and s["rebound_first"] and s["later_broken"]
    d2 = days("2026-09-28", [100.2, 101.5] + [100.2] * 25)
    s2 = au.score(r, d2, h, 0, at("2026-11-30", 9))
    assert s2["result"] == "held" and s2["rebound_first"] and not s2["later_broken"]


def test_tol_applies_to_every_branch():
    r = {"posted_at": at("2026-09-28", 9).isoformat(), "claim": "no_support", "params": {"level": 100}}
    d = days("2026-09-28", [99.3] + [101] * 25)                            # 99.3 < 99.5，但在 0.5% 代理误差内
    assert au.score(r, d, hours("2026-09-28", [(99, 101)]), .005, at("2026-11-30", 9))["result"] == "ambiguous"
    assert au.score(r, d, hours("2026-09-28", [(99, 101)]), 0, at("2026-11-30", 9))["result"] == "hit"
    rr = {"posted_at": at("2026-09-28", 9).isoformat(), "claim": "range", "params": {"lo": 100, "hi": 110, "days": 3}}
    assert au.score(rr, days("2026-09-28", [105, 99.3, 105]), [], .005, at("2026-11-30", 9))["result"] == "ambiguous"
    assert au.score(rr, days("2026-09-28", [105, 98, 105]), [], .005, at("2026-11-30", 9))["result"] == "broken"
    rt = {"posted_at": at("2026-09-28", 9).isoformat(), "claim": "trade_long",
          "params": {"entry": 100, "stop": 99, "target": 103, "days": 3, "entry_status": "declared_filled"}}
    s = au.score(rt, days("2026-09-28", [101] * 3), hours("2026-09-28", [(100, 101), (99.2, 101)]), .005, at("2026-11-30", 9))
    assert s["result"] == "ambiguous"                                      # 99.2 离止损 99 在 0.5% 内


def test_empty_window_and_deadline_straddle():
    r = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "support", "params": {"level": 100}}
    assert au.score(r, [], [], .005, at("2026-09-29", 9, 30))["result"] == "pending"
    r2 = {"posted_at": at("2026-09-29", 9).isoformat(), "claim": "break_below",
          "params": {"level": 100, "deadline": at("2026-09-29", 11, 30).isoformat()}}
    h = hours("2026-09-29", [(99, 101)])                                   # 11:00–12:00 跨截止 11:30
    s = au.score(r2, [], h, 0, at("2026-10-05", 9))
    assert s["result"] == "ambiguous"                                      # 跨截止那根不能判 hit


def test_status_at_post_already_through():
    r = {"posted_at": at("2026-09-29", 13).isoformat(), "claim": "support", "params": {"level": 100}}
    h = hours("2026-09-29", [(98, 99), (98, 99), (97, 99)])                # 11:00、12:00 两根在发布前已低于 100
    s = au.score(r, days("2026-09-29", [98] * 25), h, 0, at("2026-11-30", 9))
    assert s["status_at_post"] == "already_through"
