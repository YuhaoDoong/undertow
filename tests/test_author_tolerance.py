"""作者价位 v5.1 容差带（用户 2026-10-08；Codex 034 §2）：带宽对称；时序/会话/完整覆盖全部沿用 v4 框架。
全部为合成价格，不含任何作者内容。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import author_levels_score as a  # noqa: E402

ET = ZoneInfo("America/New_York")
SPEC = {"settle": (13, 30), "settle_tz": "America/New_York", "tick": None, "tol": 0.0, "basis": "合成", "settle_src": "合成"}
POST = "2026-10-01T10:00:00-04:00"
R = {"author": "x", "posted_at": POST, "claim": "support", "params": {"level": 100.0}}


def daily(start, n, close=105.0, low=None, lows=None):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            lo = (lows or {}).get(d, low if low is not None else close - 0.5)
            out.append((d, close, close + 0.5, lo, close))
        d += timedelta(days=1)
    return out


PRE = daily(date(2026, 9, 1), 20)                          # ATR ≈ 1 → 带宽 = max(0.3, 0.25) = 0.3


def hourly(start: datetime, end: datetime, price=105.0, overrides=None):
    """[start, end) 内每个应有小时都给一根完整小时线；overrides: {ET 时刻: (o,h,l,c)}。"""
    out = []
    for t in a.expected_slots(start, end):
        o = (overrides or {}).get(t.astimezone(ET).replace(tzinfo=None))
        out.append((t, *(o or (price, price + 0.2, price - 0.2, price))))
    return out


def test_post_day_unformed_close_is_not_known_at_post():
    """反例 1：发帖 ET 10/01 10:00、asof 11:00，当天日线 close=99 尚未形成 → 不能判 already_through。"""
    d = PRE + daily(date(2026, 10, 1), 1, close=99.0, low=98.5)
    post = datetime.fromisoformat(POST)
    h = hourly(post - timedelta(hours=10), post + timedelta(hours=1))
    r = a.score_tol(R, d, h, SPEC, post + timedelta(hours=1))
    assert r["result"] == "pending" and r["status_at_post"] == "approach"


def test_settle_before_touch_is_not_follow_evidence():
    """反例 2：小时线 ET 15:00 才触及，当天 13:30 的结算在触及之前 → 不能拿来判跌破。"""
    post = datetime.fromisoformat(POST)
    d = PRE + daily(date(2026, 10, 1), 1, close=99.0, low=99.0) + daily(date(2026, 10, 2), 10, close=101.0)
    end = datetime(2026, 10, 20, tzinfo=ET)
    h = hourly(post, end, overrides={datetime(2026, 10, 1, 15): (100.5, 100.6, 99.9, 100.2)})
    r = a.score_tol(R, d, h, SPEC, end)
    assert r["touch_at"].startswith("2026-10-01 19:00") and r["result"] != "broken"
    assert "2026-10-01" not in r["follow_settles"]


def test_sparse_hourly_cannot_prove_untouched():
    """反例 3：20 日里日线有一天低点 99（在 100±0.3 内），小时线只有另一天的一根 → 不能判 untouched。"""
    d = PRE + daily(date(2026, 10, 1), 25, lows={date(2026, 10, 6): 99.0})
    h = [(datetime(2026, 10, 2, 14, tzinfo=timezone.utc), 105, 105.2, 104.8, 105)]
    r = a.score_tol(R, d, h, SPEC, datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert r["result"] in ("incomplete_window", "source_conflict")


def test_full_hourly_but_daily_touch_is_source_conflict():
    d = PRE + daily(date(2026, 10, 1), 25, lows={date(2026, 10, 6): 99.9})
    post = datetime.fromisoformat(POST)
    end = datetime(2026, 11, 10, tzinfo=ET)
    r = a.score_tol(R, d, hourly(post - timedelta(hours=1), end), SPEC, end)
    assert r["result"] == "source_conflict" and r["daily_touch"] == "2026-10-06"


def test_band_touch_rebound_and_symmetric_break():
    post = datetime.fromisoformat(POST)
    end = datetime(2026, 11, 10, tzinfo=ET)
    over = {datetime(2026, 10, 2, 10): (100.5, 100.6, 100.25, 100.5)}           # 最低 100.25：v4 不算触及（需 ≤100.1），v5 在带内
    held = a.score_tol(R, PRE + daily(date(2026, 10, 1), 25, close=101.0), hourly(post, end, 101.0, over), SPEC, end)
    assert held["result"] == "held" and held["touch_kind"] == "overlap" and held["band"] == 0.3
    v4 = a.score_v4(R, PRE + daily(date(2026, 10, 1), 25, close=101.0), hourly(post, end, 101.0, over), SPEC, end)
    assert v4["result"] == "untouched"                                           # v4 口径不变
    lows = {date(2026, 10, 5): 99.5}
    brk = PRE + daily(date(2026, 10, 1), 2, close=101.0) + [(date(2026, 10, 5), 100, 100.2, 99.5, 99.6)] \
        + daily(date(2026, 10, 6), 20, close=101.0)
    r = a.score_tol(R, brk, hourly(post, end, 101.0, over), SPEC, end)
    assert r["result"] == "broken"                                               # 收 99.6 < 100−0.3


def test_gap_through_is_labelled_and_metadata_is_retrospective():
    post = datetime.fromisoformat(POST)
    end = datetime(2026, 11, 10, tzinfo=ET)
    over = {datetime(2026, 10, 2, 10): (99.0, 99.5, 98.8, 99.2)}                 # 整根在 99.7 之下：跳空穿越
    r = a.score_tol(R, PRE + daily(date(2026, 10, 1), 25, close=101.0), hourly(post, end, 101.0, over), SPEC, end)
    assert r["touch_kind"] == "gap_through"
    assert r["evaluation_mode"] == "retrospective_replay" and r["rule_decided_at"] == a.V5_RULE_DECIDED_AT
    assert r["case_seen_before_rule"] is False and r["status"].startswith("sensitivity_unfrozen")
    assert a.score_tol({**R, "claim": "no_support"}, PRE, [], SPEC, end)["result"] == "not_applicable"


def test_missing_atr_is_explicit():
    post = datetime.fromisoformat(POST)
    r = a.score_tol(R, daily(date(2026, 9, 25), 4), hourly(post, post + timedelta(hours=2)), SPEC, post + timedelta(hours=2))
    assert r["atr14"] is None and r["band_basis"].startswith("pct_only")


def test_silver_specs_exist_for_v4_and_v5():
    """2026-10-08：白银价位此前全是 no_price_source；XAG（现货口径，SI=F 近似、容差更宽）与 SI（期货）分开。"""
    for k in ("silver_fut", "silver_proxy"):
        assert k in a.SPECS and k in a.SPECS_V4 and a.SPECS_V4[k]["settle_tz"] == "America/New_York"
    assert a.SPECS["silver_proxy"]["tol"] > a.SPECS["silver_fut"]["tol"]
