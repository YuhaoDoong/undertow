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


def _minutes(day, start_h=13, start_m=30, end_h=20, step=1, price=1.0):
    """UTC 分钟行：10 月 ET = UTC−4，13:30Z–20:00Z = 09:30–16:00 ET。"""
    t = datetime.fromisoformat(f"{day}T{start_h:02d}:{start_m:02d}:00+00:00")
    out = []
    while t <= datetime.fromisoformat(f"{day}T{end_h:02d}:00:00+00:00"):
        out.append((t, price)); t += timedelta(minutes=step)
    return out


def test_price_path_marks_missing_day_incomplete():
    t0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC); t1 = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    rows = {"2026-10-08": _minutes("2026-10-08")}
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: rows.get(d), trading_day=lambda d: True)
    assert not r["complete"] and r["missing"] == ["2026-10-09"]
    rows["2026-10-09"] = _minutes("2026-10-09", price=3.0)
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: rows.get(d), trading_day=lambda d: True)
    assert r["complete"] and r["max_gap_min"] <= 1
    assert pa.price_at("GLD", t0 - timedelta(hours=1), loader=lambda root, d: rows.get(d)) is None   # 早于当天第一根


def test_empty_day_archive_is_not_complete():
    """044 E1：两天持有、第二天存档存在但一行都没有，旧版仍 complete=True。"""
    t0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC); t1 = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    rows = {"2026-10-08": _minutes("2026-10-08"), "2026-10-09": []}
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: rows.get(d), trading_day=lambda d: True)
    assert not r["complete"] and r["missing"] == ["2026-10-09:无行"]


def test_sparse_or_truncated_path_is_not_complete():
    t0 = datetime(2026, 10, 8, 14, 0, tzinfo=UTC); t1 = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)
    sparse = {"2026-10-08": _minutes("2026-10-08", step=30)}                     # 30 分钟一根
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: sparse.get(d), trading_day=lambda d: True)
    assert not r["complete"] and r["max_gap_min"] == 30
    cut = {"2026-10-08": _minutes("2026-10-08", end_h=18)}                       # 缺末段 2 小时
    r = pa.price_path("GLD", t0, t1, loader=lambda root, d: cut.get(d), trading_day=lambda d: True)
    assert not r["complete"] and r["max_gap_min"] == 120


def test_exact_strike_is_touch_not_cross_and_zero_change_is_its_own_bucket():
    """044 E1：put 卖腿 99、终点恰 99 → 旧版叫「越过卖腿」；零涨跌旧版归「方向错」。"""
    a = pa.attribute(_t(k_sell=99.0, S0=100.0, S1=99.0, path_prices=[100.0, 99.0]))
    assert a["category"] == "方向错·收在卖腿上" and a["exit_side"] == "等于"
    assert a["crossed_intraday"] is False and a["touched_intraday"] is True
    a = pa.attribute(_t(k_sell=99.0, S0=100.0, S1=100.0, path_prices=[100.0, 100.0]))
    assert a["category"] == "未变"


def test_small_move_can_cross_and_big_move_can_stay_inside():
    """044 合成例：跌 0.2% 越过 99.9；跌 4% 仍在 95 上方。「小幅反向」与「卖腿好侧」不等价。"""
    assert pa.attribute(_t(k_sell=99.9, S0=100.0, S1=99.8, path_prices=None))["category"] == "方向错·越过卖腿"
    assert pa.attribute(_t(k_sell=95.0, S0=100.0, S1=96.0, path_prices=None))["category"] == "方向错·仍在卖腿有利一侧"


def test_incomplete_path_cannot_assert_no_cross():
    a = pa.attribute(_t(path_complete=False, path_prices=[381.5, 380.84]))
    assert a["crossed_intraday"] is None and a["mae"] is None                    # 没观察到 ≠ 没越过
    a = pa.attribute(_t(path_complete=False))                                    # 路径不完整但已观察到越过
    assert a["crossed_intraday"] is True


def test_summarize_baseline_incomplete_is_explicit():
    rows = [{**_t(), **pa.attribute(_t()), "id": "a", "pnl": 10.0, "judgment": "J1"},
            {**_t(S1=379.0), **pa.attribute(_t(S1=379.0)), "id": "b", "pnl": -5.0, "judgment": "J1"}]
    sm = pa.summarize(rows, {"a": 0.6, "b": 0.5})
    assert sm["baseline"] == {"n": 2, "expected": 1.1, "observed": 1, "at_strike": 0, "judgments": 1,
                              "same_day_no_baseline": []}                        # 不报 Σp(1−p)：同判断日不独立
    assert pa.summarize(rows, {"a": 0.6})["baseline"]["missing"] == ["b"]                # 缺基线不折零
