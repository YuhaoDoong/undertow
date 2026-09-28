"""vp-v1 纯函数：清洗、分摊、区识别、首达判定、t 分布、不重叠、判定分档、运行器守卫。"""
import math

import pytest

from undertow.analyze import volume_profile as vp


def _bar(d, o, h, l, c, v=100.0):
    return {"date": d, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_clean_repairs_ohlc_and_start():
    rows = [{"date": "2008-07-23", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"date": "2014-02-04", "open": 120.38, "high": 121.09, "low": 120.39, "close": 120.99, "volume": 7}]
    out = vp.clean(rows, start="2008-07-24")
    assert len(out) == 1 and out[0]["l"] == 120.38 and out[0]["h"] == 121.09


def test_profile_spreads_volume_by_overlap():
    bars = [_bar(str(i), 10, 12, 10, 11, 100.0) for i in range(vp.LOOKBACK)] + [_bar("x", 11, 11, 11, 11)]
    hist = vp.profile(bars, vp.LOOKBACK, 1.0)
    assert set(hist) == {10, 11}                                  # 第 12 箱与 [10,12] 重叠长度为 0，不计
    assert hist[10] == pytest.approx(50 * vp.LOOKBACK) and hist[11] == pytest.approx(50 * vp.LOOKBACK)


def test_hvn_zones_contiguous_runs():
    hist = {1: 1.0, 2: 10.0, 3: 10.0, 4: 1.0, 7: 10.0, 8: 1.0}
    assert vp.hvn_zones(hist, 2.0) == [(4.0, 8.0), (14.0, 16.0)]
    assert vp.bin_class(hist, 5.0, 2.0) == "HVN" and vp.bin_class(hist, 11.0, 2.0) == "LVN"


def test_first_passage():
    short = [_bar("0", 100, 100, 100, 100)] + [_bar(str(i), 0, 0, 0, c) for i, c in enumerate([100.5, 99.8], 1)]
    assert vp.first_passage(short, 0, 1.0, up_is_rebound=True) == "immature"      # 数据不足 10 日且未触发
    bars = [_bar("0", 100, 100, 100, 100)] + [_bar(str(i), 0, 0, 0, c) for i, c in enumerate([100.5, 99.8, 101.2], 1)]
    assert vp.first_passage(bars, 0, 1.0, up_is_rebound=True) == "rebound"        # 已触发即判定，不必等满 10 日
    bars = [_bar("0", 100, 100, 100, 100)] + [_bar(str(i), 0, 0, 0, 100.0) for i in range(1, 12)]
    bars[3] = _bar("3", 0, 0, 0, 101.2)
    assert vp.first_passage(bars, 0, 1.0, up_is_rebound=False) == "through"
    assert vp.first_passage(bars, 0, 5.0, up_is_rebound=True) == "undecided"


def test_t_sf_known_values():
    assert vp.t_sf(2.0, 10) == pytest.approx(0.036694, abs=1e-6)
    assert vp.t_sf(1.0, 5) == pytest.approx(0.181609, abs=1e-6)
    assert vp.t_sf(1.96, 1e6) == pytest.approx(vp.norm_sf(1.96), abs=1e-5)


def test_nonoverlap_and_verdicts():
    ev = [{"i": i} for i in (0, 2, 5, 9, 10)]
    assert [e["i"] for e in vp.nonoverlap(ev)] == [0, 5, 10]
    assert vp.verdict("H3", {"n_events": 10, "mean_excess": 0.01, "p_one_sided": 0.001}).startswith("证据不足")
    assert vp.verdict("H1", {"n_events": 50, "diff": 0.06, "p_one_sided": 0.001}) == "支持"
    assert vp.verdict("H1", {"n_events": 50, "diff": 0.02, "p_one_sided": 0.001}) == "统计为正、未达经济门槛"
    assert vp.verdict("H1", {"n_events": 50, "diff": -0.02, "p_one_sided": 0.9}) == "不支持"
    assert vp.verdict("H2", {"n_lvn": 50, "n_hvn": 50, "ratio": 1.2, "lower_one_sided": 1.05}) == "支持"


def test_runner_guards_frozen_prereg():
    from scripts import vp_backtest as r
    import hashlib
    assert hashlib.sha256(r.PREREG.read_bytes()).hexdigest() == r.FROZEN_SHA, "vp-v1 预登记冻结后被改动"
    assert r.SPLIT == "2016-01-01" and r.SRC["SLV"][1] == "2008-07-24"
