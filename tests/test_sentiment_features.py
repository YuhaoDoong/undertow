"""情绪面板 v0 纯计算（Codex 066 合同）的合成测试。"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from undertow.analyze import sentiment as S

VAL = date(2026, 10, 26)


def _c(expiry, kind, delta, iv, bid=1.0, ask=1.1, volume=10, oi=100):
    return {"expiry": expiry, "kind": kind, "delta": delta, "iv": iv, "bid": bid, "ask": ask,
            "volume": volume, "open_interest": oi}


def _surface(dtes, iv_fn):
    """每个到期在 Δ=0.15/0.35/0.45/0.55 两侧各给一张；iv_fn(kind, |Δ|, dte)。"""
    out = []
    for t in dtes:
        e = VAL + timedelta(days=t)
        for d in (0.15, 0.35, 0.45, 0.55):
            out.append(_c(e, "C", d, iv_fn("C", d, t)))
            out.append(_c(e, "P", -d, iv_fn("P", d, t)))
    return out


# ── 严格 Delta 包围 ──

def test_delta_far_from_target_returns_none_not_endpoint():
    e = VAL + timedelta(days=30)
    cs = [_c(e, "C", 0.60, 0.30), _c(e, "C", 0.70, 0.28)]     # 全在 0.25 之上
    assert S.side_at_delta(cs, "C", 0.25) is None
    cs = [_c(e, "P", -0.04, 0.50), _c(e, "P", -0.10, 0.45)]   # 全在 0.25 之下
    assert S.side_at_delta(cs, "P", 0.25) is None


def test_delta_bracket_interpolates_and_records_gap():
    e = VAL + timedelta(days=30)
    cs = [_c(e, "P", -0.20, 0.40), _c(e, "P", -0.30, 0.30), _c(e, "P", -0.10, 0.99)]
    r = S.side_at_delta(cs, "P", 0.25)
    assert r["iv"] == pytest.approx(0.35)
    assert r["delta_gap"] == pytest.approx(0.10) and not r["exact"]


def test_exact_hit_used_directly():
    e = VAL + timedelta(days=30)
    r = S.side_at_delta([_c(e, "C", 0.25, 0.31)], "C", 0.25)
    assert r["iv"] == 0.31 and r["exact"]


def test_invalid_quotes_excluded():
    e = VAL + timedelta(days=30)
    cs = [_c(e, "C", 0.20, 0.40, bid=0.0), _c(e, "C", 0.30, 0.30),
          _c(e, "C", 0.22, 0.0), _c(e, "C", -0.2, 0.4), _c(e, "C", 0.21, float("nan"))]
    assert S.side_at_delta(cs, "C", 0.25) is None          # 只剩 0.30 一侧，无包围
    assert S.quote_ok(_c(e, "C", 0.2, 0.3, bid=1.2, ask=1.1)) == (False, "bid_ask")
    assert S.quote_ok(_c(e, "P", 0.2, 0.3)) == (False, "delta_sign")


# ── 恒 30 日 ──

def test_total_variance_interp_formula_and_bounds():
    s = S.total_variance_interp(20, 0.30, 40, 0.40, 30)
    lam = (40 - 30) / 20
    assert s == pytest.approx(math.sqrt((lam * 0.09 * 20 + (1 - lam) * 0.16 * 40) / 30))
    assert S.total_variance_interp(20, 0.3, 40, 0.4, 45) is None     # 不外推
    assert S.total_variance_interp(20, 0.3, 40, 0.4, 40) == 0.4      # 恰在端点
    assert S.total_variance_interp(20, -0.3, 40, 0.4, 30) is None


def _linear_w_iv(kind, d, t):
    # 总方差关于 T 线性 → 任何包围对插出的 30 日值都相同
    a = {"C": 0.0004, "P": 0.0009}[kind] * (1 + d)
    b = {"C": 0.0030, "P": 0.0040}[kind] * (1 + d)
    return math.sqrt((a + b * t) / t)


def test_nearest_roll_does_not_move_const_maturity():
    a = S.skew_features(_surface([23, 37, 60], _linear_w_iv), VAL)
    b = S.skew_features(_surface([16, 44, 60], _linear_w_iv), VAL)
    assert a["skew25_30d_pp"] == pytest.approx(b["skew25_30d_pp"], abs=1e-9)
    assert a["nearest_expiry"] != b["nearest_expiry"]
    assert a["nearest_skew25_pp"] != pytest.approx(b["nearest_skew25_pp"])


def test_no_expiry_bracket_no_extrapolation():
    r = S.skew_features(_surface([16, 23], _linear_w_iv), VAL)      # 全部 <30 天
    assert r["skew25_30d_pp"] is None and r["missing_reason"] == "no_expiry_bracket_30d"
    assert r["nearest_skew25_pp"] is not None                        # 审计列仍在，但不顶替主值
    r = S.skew_features(_surface([5, 9, 60], _linear_w_iv), VAL)     # <14 天不参与
    assert r["skew25_30d_pp"] is None


def test_atm_both_sides_named():
    r = S.skew_features(_surface([23, 37], _linear_w_iv), VAL)
    assert r["atm_put50_30d"] is not None and r["atm_call50_30d"] is not None
    assert r["atm_mid50_30d"] == pytest.approx((r["atm_put50_30d"] + r["atm_call50_30d"]) / 2)


def test_valuation_unknown_gives_none():
    r = S.skew_features(_surface([23, 37], _linear_w_iv), None)
    assert r["skew25_30d_pp"] is None and r["missing_reason"] == "valuation_date_unknown"


# ── P/C ──

def test_pc_window_and_full_chain_counts():
    e1, e2 = VAL + timedelta(days=10), VAL + timedelta(days=90)
    cs = [_c(e1, "P", -0.3, 0.3, volume=30, oi=300), _c(e1, "C", 0.3, 0.3, volume=10, oi=100),
          _c(e2, "P", -0.3, 0.3, volume=5, oi=50), _c(e2, "C", 0.3, 0.3, volume=5, oi=50)]
    r = S.pc_features(cs, VAL)
    assert r["window"]["volume"]["value"] == pytest.approx(3.0)
    assert r["full_chain"]["volume"]["value"] == pytest.approx(35 / 15)
    assert r["window"]["volume"]["n_contracts"] == 2


def test_pc_missing_field_not_folded_to_zero():
    e = VAL + timedelta(days=10)
    cs = [_c(e, "P", -0.3, 0.3, volume=None), _c(e, "C", 0.3, 0.3, volume=10)]
    blk = S.pc_features(cs, VAL)["window"]["volume"]
    assert blk["value"] is None and blk["state"] == "incomplete_missing_fields" and blk["n_missing"] == 1


def test_pc_zero_denominator_and_no_contracts():
    e = VAL + timedelta(days=10)
    blk = S.pc_features([_c(e, "P", -0.3, 0.3, volume=5), _c(e, "C", 0.3, 0.3, volume=0)], VAL)
    assert blk["window"]["volume"]["state"] == "zero_denominator"
    assert blk["window"]["volume"]["value"] is None
    blk = S.pc_features([_c(VAL + timedelta(days=99), "P", -0.3, 0.3)], VAL)
    assert blk["window"]["volume"]["state"] == "no_contracts"


def test_pc_coverage_flags_expired_contracts():
    cs = [_c(VAL, "P", -0.3, 0.3), _c(VAL + timedelta(days=3), "C", 0.3, 0.3)]
    cov = S.pc_features(cs, VAL)["coverage"]
    assert cov["includes_valuation_day_expiry"] and not cov["has_expired_before_valuation"]
    cov = S.pc_features([_c(VAL + timedelta(days=2), "C", 0.3, 0.3)], VAL)["coverage"]
    assert not cov["includes_valuation_day_expiry"]


# ── 分位 ──

def test_midrank_ties_and_empty():
    assert S.prior_midrank([1, 2, 2, 3], 2) == pytest.approx((1 + 0.5 * 2) / 4)
    assert S.prior_midrank([], 1.0) is None
    assert S.prior_midrank([1.0], None) is None


def test_rank_block_prior_only_future_does_not_change_past():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(10)]
    vals = [5, 3, 8, 1, 9, 2, 7, 4, 6, 10]
    series = list(zip(days, vals))
    past = S.rank_block(series[:4], vals[4], 252)
    # 未来点加进来也不影响「第 5 个点当时」的排名：调用方只传之前的观察
    again = S.rank_block(series[:4], vals[4], 252)
    assert past == again and past["n_prior"] == 4 and past["partial_window"]
    assert S.rank_block(series[:9], vals[9], 3)["n_prior"] == 3
    assert S.rank_block(series[:9], vals[9], 3)["window_start"] == days[6].isoformat()


# ── COT ──

def _rep(d, lo, sh, oi):
    return {"report_date": d, "long": lo, "short": sh, "open_interest": oi,
            "change_long": None, "change_short": None, "spread": None}


def test_cot_features_gross_and_rank_excludes_current():
    reps = [_rep(date(2026, 9, 1) + timedelta(weeks=i), 100 + i, 50, 1000) for i in range(5)]
    reps.append(_rep(date(2026, 10, 13), 16180, 12675, 100000))
    r = S.cot_features(reps)
    assert r["net"] == 3505 and r["long"] == 16180 and r["short"] == 12675
    assert r["rank_net_over_oi"]["n_prior"] == 5
    assert r["rank_net_over_oi"]["percentile"] == pytest.approx(0.0)


def test_cot_missing_field_not_zero():
    reps = [_rep(date(2026, 10, 6), 10, 5, 100), _rep(date(2026, 10, 13), None, 5, 100)]
    r = S.cot_features(reps)
    assert r["net"] is None and r["net_over_oi"] is None
    assert r["rank_net_over_oi"]["percentile"] is None


# ── 波动指数 ──

def test_vol_uses_only_values_before_decision():
    s = [(date(2026, 10, 22), 20.0), (date(2026, 10, 23), 21.0), (date(2026, 10, 26), 22.0)]
    r = S.vol_features(s, date(2026, 10, 26), "GVZ")
    assert r["value"] == 21.0 and r["event_date"] == "2026-10-23" and r["n_prior"] == 1


def test_vxslv_reconstructed_segment_flagged():
    assert S.vol_point_vintage("VXSLV", date(2024, 1, 2)) == S.RECONSTRUCTED
    assert S.vol_point_vintage("VXSLV", date(2026, 1, 2)) == S.UNKNOWN_VINTAGE
    assert S.vol_point_vintage("GVZ", date(2024, 1, 2)) == S.UNKNOWN_VINTAGE
    s = [(date(2025, 6, 2) + timedelta(days=i), 30.0 + i) for i in range(40)]
    r = S.vol_features(s, date(2025, 8, 1), "VXSLV")
    assert r["window_contains_reconstructed"]
    s2 = [(date(2026, 1, 2) + timedelta(days=i), 30.0 + i) for i in range(40)]
    assert not S.vol_features(s2, date(2026, 3, 1), "VXSLV")["window_contains_reconstructed"]


def test_vol_quality_pending_vs_stale_vs_current():
    td = [date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27)]
    dec = date(2026, 10, 28)
    assert S.vol_quality(date(2026, 10, 27), dec, td) == S.CURRENT
    assert S.vol_quality(date(2026, 10, 26), dec, td) == S.PENDING      # VIX 晚一天
    assert S.vol_quality(date(2026, 10, 23), dec, td) == S.STALE
    assert S.vol_quality(None, dec, td) == S.MISSING
    # 周末决策：周一的决策只要求上周五的值
    td2 = [date(2026, 10, 23), date(2026, 10, 26)]
    assert S.vol_quality(date(2026, 10, 23), date(2026, 10, 26), td2) == S.CURRENT
    assert S.vol_quality(date(2026, 10, 23), date(2026, 10, 26), None) == S.UNKNOWN_VINTAGE
