"""回放诊断（exploratory-replay-diagnostics-v1）的事件分类与因果交易队列；全部合成，不读私有标注。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import replay_diag as rd  # noqa: E402


def E(action, typ, i, as_of, seen=None, price=100.0, **kw):
    return {"action": action, "type": typ, "i": i, "as_of_i": as_of, "seen_max": as_of if seen is None else seen, "price": price, **kw}


SEG = {"id": 1, "left": 10}


def test_classify_layers_and_v1_delete_by_type_at_i():
    ev = [E("add", "HH", 5, 9), E("add", "HL", 12, 14), E("add", "入场做多", 13, 13, seen=16), E("add", "LL", 20, 18),
          E("add", "入场做多", 15, 15), E("delete", "入场做多", 15, 20), E("add", "离场", 21, 21)]
    rows = rd.classify("v1", SEG, ev)
    assert [r["cls"] for r in rows] == ["background_only", "frontier_pivot_label", "entry_backfilled", "invalid_time",
                                        "frontier_decision", "delete_frontier", "frontier_decision"]
    assert rows[4]["deleted_at_as_of"] == 20 and rows[5]["deletes"] == rows[4]["id"]


def test_trades_next_open_stop_and_delete_does_not_cancel():
    bars = [(i, 100.0, 101.0, 99.0, 100.0) for i in range(40)]
    bars[16] = (16, 100.5, 101, 99.5, 100.5)                 # 入场根：决定于 15 → 16 开盘
    bars[25] = (25, 99.0, 99.5, 96.0, 97.0)                  # 触及止损 97.5
    atr = [1.0] * 40
    ev = [E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=97.5), E("delete", "入场做多", 15, 20),
          E("add", "离场", 30, 30), E("add", "入场做空", 33, 33), E("add", "入场做空", 34, 34)]
    rows = rd.classify("v1", SEG, ev)
    t = rd.trades(rows, bars, atr)
    first = t["trades"][0]
    assert first["entry_i"] == 16 and first["entry_px"] == 100.5 and first["exit_reason"] == "stop" and first["exit_i"] == 25
    assert first["atr_units"] == -3.0 and first["deleted_later"] is True            # 删图标不撤销已可执行的入场
    assert t["flags"]["exit_without_position"] == 1 and t["flags"]["repeat_entry"] == 1
    assert t["trades"][1]["exit_reason"] == "admin_end" and t["flags"]["missing_stop"] == 1


def test_match_only_pivots_confirmed_by_label_time():
    bars = [(i, 100.0, 101.0, 99.0, 100.0) for i in range(30)]
    atr = [1.0] * 30
    rows = rd.classify("v1", SEG, [E("add", "HL", 12, 14, price=99.0)])
    alg = {"bar": [("L", 12, 16, 99.0)], "zigzag_2atr": [("L", 12, 13, 99.0)]}
    m = {x["algo"]: x for x in rd.match(rows, alg, atr)}
    assert m["bar"]["matched"] is False and m["bar"]["nearest_conf_minus_user"] == 2       # 算法晚 2 根确认，不算吻合
    assert m["zigzag_2atr"]["matched"] is True and m["zigzag_2atr"]["conf_minus_user"] == -1


def test_same_time_delete_is_cancel_and_deleted_stop_inactive():
    bars = [(i, 100.0, 101.0, 99.0, 100.0) for i in range(40)]
    atr = [1.0] * 40
    ev = [E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=99.5), E("delete", "止损", 15, 15),
          E("add", "离场", 20, 20), E("delete", "离场", 20, 20), E("add", "离场", 25, 25)]
    t = rd.trades(rd.classify("v1", SEG, ev), bars, atr)
    assert t["flags"]["cancelled_before_execution"] == 2 and t["flags"]["missing_stop"] == 1
    assert t["trades"][0]["exit_reason"] == "user_exit_next_open" and t["trades"][0]["exit_i"] == 26     # 99.5 止损已删，不触发
