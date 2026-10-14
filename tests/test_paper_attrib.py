"""模拟仓「方向还是墙」归因（scripts/paper_attrib.py，用户 2026-10-14）。"""
from datetime import date, datetime, timedelta, timezone

from scripts import paper_attrib as pa

UTC = timezone.utc


def test_direction_by_structure():
    assert pa.direction("P", "credit") == 1 and pa.direction("C", "credit") == -1
    assert pa.direction("C", "debit") == 1 and pa.direction("P", "debit") == -1
    assert pa.direction(None, "credit") == 0


def _t(**kw):
    base = {"dirn": 1, "structure": "credit", "k_sell": 380.0, "S0": 381.5, "S1": 380.84, "path_prices": [381.5, 379.75, 380.84]}
    return {**base, **kw}


def test_wrong_direction_but_stayed_inside_short_strike():
    """9/29 GLD 380P：正股 −0.18%（方向错），盘中到过 379.75，收 380.84 仍在卖腿上方。"""
    a = pa.attribute(_t())
    assert a["category"] == "方向错·仍在卖腿有利一侧" and a["crossed_intraday"] is True and a["exit_ok"] is True
    assert round(a["cushion"], 4) == round(1.5 / 381.5, 4) and a["mae"] < 0


def test_direction_right_even_if_short_strike_itm_at_entry():
    """9/30 GLD 380C：入场卖腿已价内（距离为负），跌到 379.55 → 方向对。"""
    a = pa.attribute(_t(dirn=-1, S0=382.81, S1=379.55, path_prices=[382.81, 385.0, 379.55]))
    assert a["category"] == "方向对" and a["cushion"] < 0 and a["crossed_intraday"] is True


def test_wrong_direction_crossed_and_missing_prices():
    a = pa.attribute(_t(dirn=-1, k_sell=55.0, S0=54.95, S1=55.13, path_prices=None))
    assert a["category"] == "方向错·越过卖腿" and a["mae"] is None and a["crossed_intraday"] is None
    assert pa.attribute(_t(S0=None))["category"] is None                  # 缺价 → 不归类，不估
    assert pa.attribute(_t(dirn=0))["category"] is None
    assert pa.attribute(_t(structure="debit", dirn=1, S1=379.0))["category"] == "方向错"


def test_baseline_uses_only_prior_closes_and_counts_trading_days():
    d0 = date(2026, 1, 1)
    closes = [(d0 + timedelta(days=i), 100.0 + (i % 2)) for i in range(400)]       # 交替 100/101
    entry, exit_ = closes[300][0], closes[301][0]
    p = pa.baseline_p(closes, entry, exit_, +1, 0.005)       # h=1：一半 +1%、一半 −0.99%（越过 0.5% 垫子）
    assert abs(p - 0.5) < 0.01
    future = closes[:301] + [(closes[301][0], 0.01)] + closes[302:]                  # 改动入场日之后的价格不影响基线
    assert pa.baseline_p(future, entry, exit_, +1, 0.005) == p
    assert pa.baseline_p(closes, entry, entry, +1, 0.005) is None                     # h=0
    assert pa.baseline_p(closes[:40], closes[30][0], closes[31][0], +1, 0.005) is None  # 样本不足


def test_price_path_marks_missing_day_incomplete():
    t0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC); t1 = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    rows = {"2026-10-08": [(t0, 1.0), (t0 + timedelta(hours=1), 2.0)]}
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: rows.get(d), trading_day=lambda d: True)
    assert not r["complete"] and r["missing"] == ["2026-10-09"]
    rows["2026-10-09"] = [(t1 - timedelta(hours=1), 3.0)]
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: rows.get(d), trading_day=lambda d: True)
    assert r["complete"] and [p for _, p in r["rows"]] == [1.0, 2.0, 3.0]
    assert pa.price_at("GLD", t0 - timedelta(minutes=1), loader=lambda root, d: rows.get(d)) is None   # 早于当天第一根


def test_summarize_baseline_incomplete_is_explicit():
    rows = [{**_t(), **pa.attribute(_t()), "id": "a", "pnl": 10.0, "judgment": "J1"},
            {**_t(S1=379.0), **pa.attribute(_t(S1=379.0)), "id": "b", "pnl": -5.0, "judgment": "J1"}]
    sm = pa.summarize(rows, {"a": 0.6, "b": 0.5})
    assert sm["baseline"] == {"n": 2, "expected": 1.1, "observed": 1, "var": 0.49, "judgments": 1}
    assert pa.summarize(rows, {"a": 0.6})["baseline"]["missing"] == ["b"]                # 缺基线不折零
