"""conviction（开发期）：三层编码、未知≠0、纯净度未知不计入 F、H1 判定。"""
from datetime import date
from types import SimpleNamespace as NS

from undertow.analyze import conviction as cv


def _fa(d25=-0.8, datm=0.5, spot=100.0, vol=True):
    cur = NS(skew25_pp=-1.0, skew10_pp=-1.5, atm_iv_pp=20.0, expiry=date(2026, 10, 30), days_out=32)
    prv = NS(skew25_pp=-0.2, skew10_pp=-1.0, atm_iv_pp=19.5, expiry=date(2026, 10, 30), days_out=33)
    vs = NS(d_skew25_pp=d25, d_atm_pp=datm, curr=cur, prev=prv) if vol else None
    return NS(vol=vs, spot=spot)


def _leg(kind, strike, d_oi, delta, adj, purity=0.8, counts=True):
    return NS(kind=kind, strike=strike, d_oi=d_oi, delta=delta, delta_adj_pp=adj, purity=purity, counts=counts)


def _read(legs):
    return NS(ok=True, legs=legs)


def test_two_layers_agree_bullish_and_three_layer_flag():
    legs = [_leg("C", 101, 500, 0.4, +0.8), _leg("P", 99, 300, -0.4, -0.5), _leg("P", 98, 50, -0.3, +0.6)]
    f = cv.layers(_fa(), _read(legs), 100.0, 101.0)
    assert (f["S"], f["F"], f["V"], f["H1"], f["three_layer"]) == (1, 1, 1, 1, True)


def test_v_opposite_blocks_h1_and_v_zero_allows():
    legs = [_leg("C", 101, 500, 0.4, +0.8)]
    assert cv.layers(_fa(), _read(legs), 100.0, 99.0)["H1"] == 0                 # IV 升但价格跌 → V=−1 反向
    f = cv.layers(_fa(datm=-0.4), _read(legs), 100.0, 99.0)
    assert f["V"] == 0 and f["H1"] == 1 and f["three_layer"] is False            # IV 回落 → V=0，两层成立


def test_unknown_is_not_zero():
    legs = [_leg("C", 101, 500, 0.4, +0.8)]
    assert cv.layers(_fa(vol=False), _read(legs), 100.0, 101.0)["H1"] is None     # 曲面缺失
    assert cv.layers(_fa(), None, 100.0, 101.0)["F"] is None                     # 逐腿缺失
    assert cv.layers(_fa(), _read(legs), None, 101.0)["V"] is None               # 价格缺失


def test_unknown_purity_and_far_legs_excluded_and_counted():
    legs = [_leg("C", 101, 500, 0.4, +0.8, purity=None), _leg("P", 120, 900, -0.1, +1.0),
            _leg("P", 99, 100, -0.4, +0.7), _leg("C", 100, 100, 0.5, +0.3, counts=False)]
    f = cv.layers(_fa(d25=0.6), _read(legs), 100.0, 99.0)
    assert f["f_unknown_purity_legs"] == 1 and f["f_legs"] == 1 and f["F"] == -1   # 只剩 99P 买方
    assert f["S"] == -1 and f["H1"] == -1


def test_daily_script_records_conviction_after_skew_ledger():
    from pathlib import Path
    du = (Path(__file__).resolve().parents[1] / "scripts" / "daily_update.sh").read_text("utf-8")
    assert du.index("dirledger record") < du.index("dirledger conviction-record") < du.index("publish_dirs \"每日自动更新")


def _series(vals25, vals10=None, exps=None):
    rows = []
    for i, v in enumerate(vals25):
        rows.append({"session": f"d{i:02d}", "skew25_pp": v, "skew10_pp": (vals10[i] if vals10 else v),
                     "expiry": (exps[i] if exps else "E1")})
    return rows


def test_flip_requires_prior_run_and_merges_wings():
    rows = _series([-1.0] * 10 + [0.3, 0.4])
    out = cv.flip_states(rows)
    assert [e["status"] for e in out["events"]] == ["event"] and out["events"][0]["direction"] == -1
    assert set(out["events"][0]["wings"]) == {"skew25_pp", "skew10_pp"} and out["events"][0]["session"] == "d10"
    short = cv.flip_states(_series([-1.0] * 5 + [0.3]))
    assert short["events"] == [] and short["states"]["d05"]["skew25_pp"] == "change_short_run"


def test_roll_day_change_is_not_a_flip_and_missing_resets():
    exps = ["E1"] * 10 + ["E2"]
    out = cv.flip_states(_series([-1.0] * 10 + [0.5], exps=exps))
    assert out["events"] == [] and out["states"]["d10"]["skew25_pp"] == "roll_switch"
    rows = _series([-1.0] * 10 + [None, None] + [0.5])
    rows[10]["missing"] = rows[11]["missing"] = True
    out = cv.flip_states(rows)
    assert out["events"] == []                                                   # 连续缺 2 天 → 持续段清零


def test_opposite_wing_flips_are_conflict_and_zero_does_not_count():
    v25 = [-1.0] * 10 + [0.5]
    v10 = [1.0] * 10 + [-0.5]
    out = cv.flip_states(_series(v25, v10))
    assert [e["status"] for e in out["events"]] == ["conflict"]
    z = cv.flip_states(_series([-1.0] * 10 + [0.0]))
    assert z["events"] == [] and z["states"]["d10"]["skew25_pp"] == "zero"
