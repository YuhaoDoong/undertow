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


# ═══ v3（Codex 026）：会话覆盖、按日历计窗口、结算时刻、严格边界、直接合约 tol=0 ═══
BZ = au.SPECS["BZZ26"]
GP = au.SPECS["gold_proxy"]


def full_hours(start, end, lo, hi):
    """[start, end) 内每个应有小时线时段都有数据（覆盖 100%）。"""
    return [(t, (lo + hi) / 2, hi, lo, (lo + hi) / 2) for t in au.expected_slots(start, end)]


def sess_days(start, n, close):
    from undertow.core import market_calendar as mc
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if mc.is_trading_day(d):
            out.append((d, close, close, close, close))
        d += timedelta(days=1)
    return out


def test_v3_missing_window_is_not_miss():
    """Codex 026 probe：截止已过、只有一根完整小时线、其余缺失 → v2 判 miss；v3 必须不终判。"""
    x = {"posted_at": "2026-09-29T09:00:00-04:00", "claim": "break_below",
         "params": {"level": 100, "deadline": "2026-09-30T16:00:00-04:00"}}
    h = [(datetime(2026, 9, 29, 14, tzinfo=timezone.utc), 102, 103, 101, 102)]
    asof = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert au.score(x, [], h, 0, asof)["result"] == "miss"                           # v2 原样保留（对照）
    s = au.score_v3(x, [], h, BZ, asof)
    assert s["result"] == "incomplete_window" and s["coverage"]["observed"] == 1
    hf = full_hours(at("2026-09-29", 9), at("2026-09-30", 16), 101, 103)
    assert au.score_v3(x, [], hf, BZ, asof)["result"] == "miss"                      # 覆盖足够才可判未发生


def test_v3_strict_break_vs_inclusive_touch_and_tick():
    assert au.cross3(100, au._thr(100, 1, "0.01"), True, 0, strict=True, tick="0.01") == "no"   # 恰好等于 ≠ 跌破
    assert au.cross3(99.99, au._thr(100, 1, "0.01"), True, 0, strict=True, tick="0.01") == "yes"  # 差一个报价单位
    assert au.cross3(100, au._thr(100, 1, "0.01"), True, 0, strict=False, tick="0.01") == "yes"  # 触及含等号
    assert au.cross3(93.68000000001, au._thr(93.6, 1.001, "0.01"), True, 0, strict=False, tick="0.01") == "yes"
    assert au.cross3(99.6, 100, True, 0.005, strict=True) == "maybe"                 # 代理：误差内
    assert au.cross3(99.4, 100, True, 0.005, strict=True) == "yes"


def test_v3_window_counts_calendar_sessions_not_rows():
    r = {"posted_at": "2026-09-14T09:00:00-04:00", "claim": "no_support", "params": {"level": 100}}
    d = sess_days("2026-09-14", 20, 101.0)
    asof = datetime(2026, 11, 30, tzinfo=timezone.utc)
    assert au.score_v3(r, d, [], BZ, asof)["result"] == "miss"
    gap = [x for x in d if x[0] != date(2026, 9, 22)] + [(date(2026, 10, 12), 1, 1, 1, 90.0)]  # 缺一天，第 21 天破位
    s = au.score_v3(r, gap, [], BZ, asof)
    assert s["result"] == "incomplete_window" and s["missing"] == ["2026-09-22"]       # 不把窗口拉长到第 21 天


def test_v3_same_day_settlement_before_post_is_excluded():
    """GC 结算 13:30 ET：15:00 发帖时当日结算已定价，不能算发布后的收盘。"""
    s = au.sessions_after(at("2026-09-29", 15), 2, GP)
    assert s == [date(2026, 9, 30), date(2026, 10, 1)]
    assert au.sessions_after(at("2026-09-29", 13), 1, GP) == [date(2026, 9, 29)]


def test_v3_expected_slots_skip_weekend_and_break():
    sl = au.expected_slots(at("2026-09-25", 12), at("2026-09-28", 12))
    hrs = [t.astimezone(ZoneInfo("America/New_York")).strftime("%a %H") for t in sl]
    assert len(sl) == 23 and "Fri 17" not in hrs and "Fri 18" not in hrs and hrs[5] == "Sun 18"


def test_v3_held_requires_all_follow_sessions():
    r = {"posted_at": "2026-09-14T09:00:00-04:00", "claim": "support", "params": {"level": 100}}
    h = full_hours(at("2026-09-14", 9), at("2026-09-15", 9), 99.95, 100.5)            # 9/14 首根即触及
    d = sess_days("2026-09-14", 25, 100.4)
    asof = datetime(2026, 11, 30, tzinfo=timezone.utc)
    assert au.score_v3(r, d, h, BZ, asof)["result"] == "held"
    s = au.score_v3(r, [x for x in d if x[0] != date(2026, 9, 17)], h, BZ, asof)
    assert s["result"] == "incomplete_window" and s["follow_missing"] == ["2026-09-17"]


def test_v3_trade_not_triggered_needs_coverage():
    r = {"posted_at": "2026-09-14T09:00:00-04:00", "claim": "trade_long",
         "params": {"entry": 100, "stop": 99, "target": 103, "days": 2}}
    asof = datetime(2026, 11, 30, tzinfo=timezone.utc)
    part = full_hours(at("2026-09-14", 9), at("2026-09-14", 14), 101, 102)
    assert au.score_v3(r, [], part, BZ, asof)["result"] == "incomplete_window"
    full = full_hours(at("2026-09-14", 9), at("2026-09-15", 17), 101, 102)
    assert au.score_v3(r, [], full, BZ, asof)["result"] == "not_triggered"


# ═══ v4（Codex 027）：存在性 / 全程否定 / 先后顺序分开；触及后的结算；伦敦结算时刻；节假日 ═══
BZ4 = au.SPECS_V4["BZZ26"]


def _t(s):
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def test_v4_negative_needs_every_hour_not_90_percent():
    """Codex 027 probe①：应有 10 小时、缺 1 小时 → v3 判 miss；v4 为 incomplete_window（另注已观测时段未发生）。"""
    post, end = _t("2026-09-29T09:00:00-04:00"), _t("2026-09-29T20:00:00-04:00")
    slots = au.expected_slots(post, end)
    x = {"posted_at": post.isoformat(), "claim": "break_below", "params": {"level": 100, "deadline": end.isoformat()}}
    gap = [(s, 102, 103, 101, 102) for s in slots[1:]]
    assert au.score_v3(x, [], gap, BZ, end + timedelta(hours=1))["result"] == "miss"            # v3 原样（对照）
    s = au.score_v4(x, [], gap, BZ4, end + timedelta(hours=1))
    assert s["result"] == "incomplete_window" and s["observed_no_event"] and s["n_missing"] == 1
    full = [(s_, 102, 103, 101, 102) for s_ in slots]
    assert au.score_v4(x, [], full, BZ4, end + timedelta(hours=1))["result"] == "miss"
    hit = gap[:3] + [(slots[4], 101, 101, 99, 100)]                                            # 存在性：看见穿越即命中
    assert au.score_v4(x, [], hit, BZ4, end + timedelta(hours=1))["result"] == "hit"


def test_v4_first_passage_needs_no_gap_before_decisive_bar():
    """Codex 027 probe②：09:00 正常、10:00 缺失、11:00 到目标 → v3 target_first；v4 ambiguous_order。"""
    x = {"posted_at": "2026-09-29T09:00:00-04:00", "claim": "trade_long",
         "params": {"entry": 100, "stop": 99, "target": 103, "days": 1, "entry_status": "declared_filled"}}
    h = [(_t("2026-09-29T09:00:00-04:00"), 100, 101, 99.5, 100), (_t("2026-09-29T11:00:00-04:00"), 100, 104, 100, 103)]
    asof = _t("2026-09-29T12:00:00-04:00")
    assert au.score_v3(x, [], h, BZ, asof)["result"] == "target_first"
    assert au.score_v4(x, [], h, BZ4, asof)["result"] == "ambiguous_order"
    h2 = h[:1] + [(_t("2026-09-29T10:00:00-04:00"), 100, 101, 99.5, 100)] + h[1:]
    assert au.score_v4(x, [], h2, BZ4, asof)["result"] == "target_first"


def test_v4_follow_settlements_strictly_after_touch():
    """Codex 027 probe③：15:00 发帖并触墙，当日结算（伦敦 19:30 = ET 14:30）在触墙之前 → 不能用来判 broken。"""
    x = {"posted_at": "2026-09-29T15:00:00-04:00", "claim": "support", "params": {"level": 100}}
    h = [(_t("2026-09-29T15:00:00-04:00"), 101, 101, 100, 100)]
    d = [(date(2026, 9, 29), 100, 102, 98, 99)]
    assert au.score_v3(x, d, h, BZ, _t("2026-09-29T17:00:00-04:00"))["result"] == "broken"
    s = au.score_v4(x, d, h, BZ4, _t("2026-09-29T17:00:00-04:00"))
    assert s["result"] == "pending" and s["follow_settles"][0] == "2026-09-30" and len(s["follow_settles"]) == 6
    # 正向：触及在当日结算之前 → 当日结算计入
    x2 = {"posted_at": "2026-09-29T09:00:00-04:00", "claim": "support", "params": {"level": 100}}
    h2 = [(_t("2026-09-29T09:00:00-04:00"), 101, 101, 100, 100)]
    s2 = au.score_v4(x2, d, h2, BZ4, _t("2026-09-29T17:00:00-04:00"))
    assert s2["result"] == "broken" and s2["session"] == "2026-09-29"


def test_v4_london_settlement_follows_uk_dst():
    """英国 10/25 已回冬令时、美国 11/1 才回：这一周伦敦 19:30 = ET 15:30，不是 14:30。"""
    assert au._settle_at_v4(date(2026, 9, 29), BZ4).astimezone(ET).strftime("%H:%M") == "14:30"
    assert au._settle_at_v4(date(2026, 10, 27), BZ4).astimezone(ET).strftime("%H:%M") == "15:30"
    assert au._settle_at_v4(date(2026, 9, 29), au.SPECS_V4["gold_proxy"]).astimezone(ET).strftime("%H:%M") == "13:30"


def test_v4_straddle_bar_at_post_and_holiday_guard():
    post = _t("2026-09-29T09:30:00-04:00")
    end = _t("2026-09-29T12:00:00-04:00")
    x = {"posted_at": post.isoformat(), "claim": "break_below", "params": {"level": 100, "deadline": end.isoformat()}}
    rest = [(s, 102, 103, 101, 102) for s in au.expected_slots(post, end)]
    head_cross = [(_t("2026-09-29T09:00:00-04:00"), 101, 101, 99, 100)] + rest               # 发帖跨过的那根穿越：先后不明
    assert au.score_v4(x, [], head_cross, BZ4, end)["result"] == "ambiguous"
    head_ok = [(_t("2026-09-29T09:00:00-04:00"), 102, 103, 101, 102)] + rest
    assert au.score_v4(x, [], head_ok, BZ4, end)["result"] == "miss"
    assert au.score_v4(x, [], rest, BZ4, end)["result"] == "incomplete_window"                 # 跨过的那根缺失
    # 窗口跨感恩节（11/26，NYSE 休市）：全程否定 → calendar_unverified
    p2, e2 = _t("2026-11-25T09:00:00-05:00"), _t("2026-11-27T12:00:00-05:00")
    x2 = {"posted_at": p2.isoformat(), "claim": "break_below", "params": {"level": 100, "deadline": e2.isoformat()}}
    full = [(s, 102, 103, 101, 102) for s in au.expected_slots(p2, e2)]
    s = au.score_v4(x2, [], full, BZ4, e2 + timedelta(hours=1))
    assert s["result"] == "calendar_unverified" and s["observed_result"] == "miss" and s["holidays"] == ["2026-11-26"]


def test_v4_tri_class():
    assert au.tri_class("support", "held") == "right" and au.tri_class("support", "broken") == "wrong"
    assert au.tri_class("trade_long", "not_triggered") == "undecided"
    assert au.tri_class("break_below", "incomplete_window") == "undecided"
