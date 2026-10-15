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
    res = {"status": "entered", "entry_i": 2, "entry": 100.0, "exit_i": 4, "exit_price": 102.0, "exit_reason": "x", "stop": 95.0, "days_held": 3}
    L = pp.leg(bars, [2.0] * 10, NS(direction="long"), res, 4.0)
    assert L["pnl_atr0"] == 0.5 and L["pnl_own_atr"] == 1.0 and L["mfe_atr0"] == 1.0 and L["mae_atr0"] == -0.75
    S = pp.leg(bars, [2.0] * 10, NS(direction="short"), {**res, "exit_price": 98.0}, 4.0)
    assert S["pnl_atr0"] == 0.5 and S["mfe_atr0"] == 0.75 and S["mae_atr0"] == -1.0
    assert pp.leg(bars, [], NS(direction="long"), {"status": "timeout_no_entry"}, 4.0) == {"status": "timeout_no_entry"}


def test_own_schedule_marks_overlap_only():
    it = lambda bi, ei, xi, d="long": {"break_i": bi, "direction": d, "fast": {"status": "entered", "entry_i": ei, "exit_i": xi}}
    items = [it(1, 5, 20), it(2, 10, 12), it(3, 21, 30), it(4, 11, 13, "short"), {"break_i": 5, "direction": "long", "fast": {"status": "x"}}]
    assert pp.own_schedule(items, "fast") == {1}                                # 同根不衔接：21 > 20 可入；空头独立


def test_summarize_reports_all_categories():
    mk = lambda cat, f=None, s=None, pair=None: {"method": "bar", "symbol": "GLD", "category": cat,
                                                 "fast": f or {"status": "timeout_no_entry"}, "slow": s or {"status": "invalidated_new_extreme"},
                                                 **({"pair": pair} if pair else {})}
    F = {"status": "entered", "pnl_atr0": 1.0, "mae_atr0": -0.5, "hold_atr0": 2.0, "exit_reason": "struct"}
    S = {"status": "entered", "pnl_atr0": 0.5, "mae_atr0": -0.2, "hold_atr0": 1.0}
    rows = [mk("both", F, S, {"lead_bars": 3, "entry_px_adv_atr0": 0.4, "pnl_diff_atr0": 0.5}), mk("fast_only", {**F, "pnl_atr0": -1.2}),
            mk("slow_only", None, S), mk("neither")]
    for m in ("two", "atr", "limit"):
        rows.append({**mk("neither"), "method": m})
    s = pp.summarize(rows, {"ETF": ["GLD"]})["bar|全部"]
    c = s["categories"]
    assert s["opportunities"] == 4 and [c[k]["n"] for k in ("both", "fast_only", "slow_only", "neither")] == [1, 1, 1, 1]
    assert c["fast_only"]["slow_not_entered_reason"] == {"invalidated_new_extreme": 1} and c["both"]["lead_bars"]["median"] == 3
    assert s["per_opportunity_sum_atr0"] == {"fast": -0.2, "slow": 1.0, "note": s["per_opportunity_sum_atr0"]["note"]}
