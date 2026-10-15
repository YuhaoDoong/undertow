"""ps-entry-pair-v1（Codex 047 D）：同机会慢/快入场成对对照的分类、路径与占位说明；全部合成。"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import ps_entry_pair as pp  # noqa: E402


def test_slow_trade_keeps_episode_status_when_no_signal():
    assert pp.slow_trade([], [], [], NS(status="invalidated_close_beyond_extreme"), 10) == {"status": "invalidated_close_beyond_extreme"}
    ep = NS(status="signal", signal_at=10, direction="long", extreme=NS(price=90.0))
    assert pp.slow_trade([], [1.0] * 20, [], ep, 10)["status"] == "no_entry_bar"


def test_leg_uses_breakout_atr_and_path_both_directions():
    bars = [(i, 100.0, 101.0, 99.0, 100.0) for i in range(10)]
    bars[3] = (3, 100.0, 104.0, 97.0, 103.0)
    res = {"status": "entered", "entry_i": 2, "entry": 100.0, "exit_i": 4, "exit_price": 102.0, "exit_reason": "time_exit",
           "stop": 95.0, "days_held": 3}
    L = pp.leg(bars, [2.0] * 10, NS(direction="long"), res, 4.0)
    assert L["pnl_atr0"] == 0.5 and L["pnl_own_atr"] == 1.0 and L["mfe_atr0"] == 1.0 and L["mae_atr0"] == -0.75 and L["path_exact"]
    S = pp.leg(bars, [2.0] * 10, NS(direction="short"), {**res, "exit_price": 98.0}, 4.0)
    assert S["pnl_atr0"] == 0.5 and S["mfe_atr0"] == 0.75 and S["mae_atr0"] == -1.0
    assert pp.leg(bars, [], NS(direction="long"), {"status": "timeout_no_entry"}, 4.0) == {"status": "timeout_no_entry"}


def _res(ei, xi, exit_px, why, entry=100.0):
    return {"status": "entered", "entry_i": ei, "entry": entry, "exit_i": xi, "exit_price": exit_px, "exit_reason": why}


def test_open_exit_excludes_rest_of_exit_bar_codex_048():
    """Codex 048 原反例：100 入、次根 105 开盘离场、该根其后最高 1000 最低 1 → 只算到开盘：MAE −1 / MFE +5。"""
    b = [(0, 100.0, 101.0, 99.0, 100.0), (1, 105.0, 1000.0, 1.0, 105.0)]
    for why in ("struct_exit", "gap_stop", "struct_exit+gap_stop", "breakeven_gap_stop", "struct_exit+breakeven_gap_stop"):
        p = pp.path(b, NS(direction="long"), _res(0, 1, 105.0, why), 1.0)
        assert (p["mae_atr0"], p["mfe_atr0"], p["path_exact"]) == (-1.0, 5.0, True), why
    q = pp.path([(0, 100.0, 101.0, 99.0, 100.0), (1, 95.0, 1000.0, 1.0, 95.0)], NS(direction="short"), _res(0, 1, 95.0, "struct_exit"), 1.0)
    assert (q["mae_atr0"], q["mfe_atr0"]) == (-1.0, 5.0)                                      # 做空镜像


def test_intrabar_stop_gives_bounds_not_guess():
    """盘中止损根：先到高点还是先到止损不可知 → MFE 给界；止损外的低点在出场之后，不进 MAE。"""
    b = [(0, 100.0, 102.0, 99.0, 101.0), (1, 101.0, 110.0, 80.0, 85.0)]
    p = pp.path(b, NS(direction="long"), _res(0, 1, 95.0, "stop"), 1.0)
    assert p["mae_atr0"] == -5.0 and p["mfe_atr0"] is None and (p["mfe_atr0_lo"], p["mfe_atr0_hi"]) == (2.0, 10.0)
    assert not p["path_exact"]
    assert pp.path(b, NS(direction="long"), _res(0, 1, 95.0, "stop_both_touched"), 1.0)["mae_atr0"] == -5.0


def test_limit_intrabar_entry_favorable_before_fill_is_only_possible():
    """limit 盘中成交（成交价 ≠ 开盘）：入场根高点可能在成交前 → 只进上界；低点在成交之后（先过挂单价）→ 确定。"""
    b = [(0, 105.0, 120.0, 98.0, 103.0), (1, 103.0, 104.0, 101.0, 102.0), (2, 102.0, 103.0, 101.0, 102.0)]
    p = pp.path(b, NS(direction="long"), _res(0, 2, 102.0, "struct_exit"), 1.0, intrabar_entry=True)
    assert p["mae_atr0"] == -2.0 and (p["mfe_atr0_lo"], p["mfe_atr0_hi"]) == (4.0, 20.0) and p["mfe_atr0"] is None
    o = pp.path(b, NS(direction="long"), _res(0, 2, 102.0, "struct_exit", entry=105.0), 1.0)      # 开盘成交：整根都在持有内
    assert o["path_exact"] and o["mfe_atr0"] == 15.0


def test_filled_through_stop_and_unknown_reason():
    b = [(0, 90.0, 120.0, 80.0, 100.0)]
    p = pp.path(b, NS(direction="long"), {"status": "filled_through_stop", "entry_i": 0, "entry": 90.0, "exit_i": 0,
                                         "exit_price": 90.0, "exit_reason": "filled_through_stop"}, 1.0)
    assert (p["mae_atr0"], p["mfe_atr0"]) == (0.0, 0.0)
    u = pp.path([(0, 100.0, 101.0, 99.0, 100.0), (1, 100.0, 130.0, 70.0, 100.0)], NS(direction="long"), _res(0, 1, 100.0, "???"), 1.0)
    assert not u["path_exact"] and (u["mae_atr0_lo"], u["mae_atr0_hi"], u["mfe_atr0_lo"], u["mfe_atr0_hi"]) == (-30.0, -1.0, 1.0, 30.0)


def test_own_schedule_marks_overlap_only():
    it = lambda bi, ei, xi, d="long": {"break_i": bi, "direction": d, "fast": {"status": "entered", "entry_i": ei, "exit_i": xi}}
    items = [it(1, 5, 20), it(2, 10, 12), it(3, 21, 30), it(4, 11, 13, "short"), {"break_i": 5, "direction": "long", "fast": {"status": "x"}}]
    assert pp.own_schedule(items, "fast") == {1}                                # 同根不衔接：21 > 20 可入；空头独立


def test_summarize_reports_all_categories():
    mk = lambda cat, f=None, s=None, pair=None: {"method": "bar", "symbol": "GLD", "category": cat,
                                                 "fast": f or {"status": "timeout_no_entry"}, "slow": s or {"status": "invalidated_new_extreme"},
                                                 **({"pair": pair} if pair else {})}
    pv = lambda a: {"mae_atr0": a, "mae_atr0_lo": a - 1, "mae_atr0_hi": a, "mfe_atr0": None, "mfe_atr0_lo": 1.0, "mfe_atr0_hi": 2.0,
                    "path_exact": False}
    F = {"status": "entered", "pnl_atr0": 1.0, "hold_atr0": 2.0, "exit_reason": "struct", **pv(-0.5)}
    S = {"status": "entered", "pnl_atr0": 0.5, "hold_atr0": 1.0, **pv(-0.2)}
    rows = [mk("both", F, S, {"lead_bars": 3, "entry_px_adv_atr0": 0.4, "pnl_diff_atr0": 0.5}), mk("fast_only", {**F, "pnl_atr0": -1.2}),
            mk("slow_only", None, S), mk("neither")]
    for m in ("two", "atr", "limit"):
        rows.append({**mk("neither"), "method": m})
    s = pp.summarize(rows, {"ETF": ["GLD"]})["bar|全部"]
    c = s["categories"]
    assert s["opportunities"] == 4 and [c[k]["n"] for k in ("both", "fast_only", "slow_only", "neither")] == [1, 1, 1, 1]
    assert c["fast_only"]["slow_not_entered_reason"] == {"invalidated_new_extreme": 1} and c["both"]["lead_bars"]["median"] == 3
    assert s["per_opportunity_sum_atr0"] == {"fast": -0.2, "slow": 1.0, "note": s["per_opportunity_sum_atr0"]["note"]}
    assert c["both"]["fast_path_n"] == 1 and c["both"]["fast_path_exact_n"] == 0 and c["both"]["fast_mae_atr0_lo"]["n"] == 1
