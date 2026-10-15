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


# ---- Codex 047 A：事件时序（回看删除 / 止损按最新有效事件 / 入场根跳空）----

def B(n=40):
    return [(i, 100.0, 101.0, 99.0, 100.0) for i in range(n)]


def T(ev, bars=None):
    return rd.trades(rd.classify("v1", SEG, ev), bars or B(), [1.0] * 40)


def test_rewind_delete_after_advancing_does_not_cancel_executable_entry():
    """决定 15、已推进到 25 再回到 15 删除：16 开盘已可执行，因果层保留；删除只进最终解释层。"""
    for typ, d in (("入场做多", "long"), ("入场做空", "short")):
        t = T([E("add", typ, 15, 15), E("delete", typ, 15, 15, seen=25)])
        assert len(t["trades"]) == 1 and t["trades"][0]["entry_i"] == 16 and t["trades"][0]["dir"] == d
        assert "cancelled_before_execution" not in t["flags"] and t["flags"]["rewind_delete_kept_executable"] == 1
        fl = t["trades"][0]["final_layer"]
        assert fl["entry_deleted_after_executable"] and fl["delete_seen_max"] == 25 and fl["delete_rewind"] is True


def test_same_asof_delete_but_actually_later_is_not_cancel():
    """删除的回看光标与决定同根（15），但实际已见到 18：仍算已可执行。未推进就删才算执行前取消。"""
    t = T([E("add", "入场做多", 15, 15), E("delete", "入场做多", 15, 15, seen=18), E("add", "离场", 20, 20)])
    assert len(t["trades"]) == 1 and t["trades"][0]["exit_i"] == 21
    t2 = T([E("add", "入场做多", 15, 15), E("delete", "入场做多", 15, 15)])
    assert t2["trades"] == [] and t2["flags"]["cancelled_before_execution"] == 1


def test_exit_deleted_after_executable_still_exits():
    t = T([E("add", "入场做多", 15, 15), E("add", "离场", 20, 20), E("delete", "离场", 20, 22)])
    assert t["trades"][0]["exit_i"] == 21 and t["trades"][0]["exit_reason"] == "user_exit_next_open"


def test_deleted_stop_stops_applying_after_delete_boundary():
    """97 止损在 20 删除，25 才跌到 96：删除后不再生效（047 §4 反例）；先触发后删除则照常止损。"""
    b = B(); b[25] = (25, 100.0, 101.0, 96.0, 100.0)
    ev = [E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=97.0), E("delete", "止损", 15, 20), E("add", "离场", 30, 30)]
    t = T(ev, b)["trades"][0]
    assert t["exit_reason"] == "user_exit_next_open" and t["exit_i"] == 31 and t["stop_deleted_in_position"]
    assert t["stop_ever_set"] and t["stop"] is None and [x["stop"] for x in t["stop_path"]] == [97.0, None]
    b2 = B(); b2[18] = (18, 100.0, 101.0, 96.0, 100.0)
    t2 = T(ev, b2)["trades"][0]
    assert t2["exit_reason"] == "stop" and t2["exit_i"] == 18 and t2["exit_px"] == 97.0
    b3 = B(); b3[20] = (20, 100.0, 101.0, 96.0, 100.0)                     # 删除所在根本身仍受旧止损保护
    assert T(ev, b3)["trades"][0]["exit_i"] == 20


def test_stop_update_uses_latest_and_falls_back_when_latest_deleted():
    b = B(); b[25] = (25, 100.0, 101.0, 97.0, 100.0)
    ev = [E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=95.0), E("add", "止损", 20, 20, price=98.5), E("add", "离场", 30, 30)]
    t = T(ev, b)["trades"][0]
    assert t["exit_reason"] == "stop" and t["exit_i"] == 25 and t["exit_px"] == 98.5 and t["stop_changes"] == 2
    ev2 = ev[:3] + [E("delete", "止损", 20, 22)] + ev[3:]                   # 最新的 98.5 删掉 → 回落到仍有效的 95
    t2 = T(ev2, b)["trades"][0]
    assert t2["exit_i"] == 31 and t2["stop"] == 95.0 and t2["stop_deleted_in_position"]
    ev3 = ev[:3] + [E("add", "止损", 22, 22, price=96.0)] + ev[3:]          # 两次更新：最后一条 96 生效
    assert T(ev3, b)["trades"][0]["exit_i"] == 31


def test_short_stop_mirror_and_update():
    b = B(); b[25] = (25, 100.0, 103.0, 99.0, 100.0)
    ev = [E("add", "入场做空", 15, 15), E("add", "止损", 15, 15, price=105.0), E("add", "止损", 20, 20, price=102.0), E("add", "离场", 30, 30)]
    t = T(ev, b)["trades"][0]
    assert t["dir"] == "short" and t["exit_reason"] == "stop" and t["exit_px"] == 102.0 and t["atr_units"] == -2.0


def test_entry_bar_open_through_stop_never_fills_at_unreached_price():
    """入场根开盘 90 已在 95 止损之下：按开盘离场（filled_through_stop，0 ATR），不能 95 出、+5 ATR。空头镜像。"""
    b = B(); b[16] = (16, 90.0, 92.0, 88.0, 90.0)
    t = T([E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=95.0)], b)["trades"][0]
    assert t["exit_reason"] == "filled_through_stop" and t["exit_px"] == 90.0 and t["atr_units"] == 0.0 and t["exit_i"] == 16
    b2 = B(); b2[16] = (16, 110.0, 112.0, 108.0, 110.0)
    t2 = T([E("add", "入场做空", 15, 15), E("add", "止损", 15, 15, price=105.0)], b2)["trades"][0]
    assert t2["exit_reason"] == "filled_through_stop" and t2["exit_px"] == 110.0 and t2["atr_units"] == 0.0
    b3 = B(); b3[16] = (16, 100.0, 101.0, 94.0, 95.0)                      # 开盘在止损之上、盘中触及：按止损价
    t3 = T([E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=95.0)], b3)["trades"][0]
    assert t3["exit_reason"] == "stop" and t3["exit_px"] == 95.0 and t3["exit_i"] == 16


def test_later_bar_gap_through_stop_exits_at_open():
    b = B(); b[20] = (20, 94.0, 95.0, 93.0, 94.0)
    t = T([E("add", "入场做多", 15, 15), E("add", "止损", 15, 15, price=97.0)], b)["trades"][0]
    assert t["exit_reason"] == "gap_stop" and t["exit_px"] == 94.0


# ---- Codex 047 B：延迟参照可审计 ----

def test_delay_ref_uses_point_alive_at_decision_and_lists_deleted_separately():
    rs = rd.classify("v1", SEG, [E("add", "HL", 10, 11, price=99.0), E("add", "HL", 14, 15, price=99.0), E("delete", "HL", 14, 16)])
    d = rd.delays(rs, [{"dir": "long", "decided_at": 20, "entry_i": 21}], {"bar": [("L", 14, 15, 99.0), ("L", 10, 12, 99.0)],
                                                                         "two": [], "atr": [], "zigzag_2atr": []})[0]
    assert d["ref"]["point_i"] == 10 and d["ref"]["seq"] == 0 and d["ref"]["deleted_before_decision"] is False
    assert d["ref_changes_if_deleted_included"] and d["ref_incl_deleted"]["point_i"] == 14 and d["ref_incl_deleted"]["deleted_before_decision"]
    assert d["extreme_to_exec"] == 11 and d["extreme_to_label"] == 1 and d["label_to_decision"] == 9
    assert d["bar_final_conf_minus_user_label"] == 1 and d["bar_confirmed_by_label_time"] is False     # 算法在用户标注之后确认：如实为正
    assert d["zigzag_2atr_final_conf_minus_user_label"] is None and "启发式" in d["selection_rule"]


def test_delay_ref_none_when_no_alive_point_and_deleted_later_kept():
    rs = rd.classify("v1", SEG, [E("add", "HL", 14, 15), E("delete", "HL", 14, 16)])
    d = rd.delays(rs, [{"dir": "long", "decided_at": 20, "entry_i": 21}], {n: [] for n in ("bar", "two", "atr", "zigzag_2atr")})[0]
    assert d["ref"] is None and "extreme_to_exec" not in d and d["n_candidates_incl_deleted"] == 1
    rs2 = rd.classify("v1", SEG, [E("add", "HL", 14, 15), E("delete", "HL", 14, 25)])
    d2 = rd.delays(rs2, [{"dir": "long", "decided_at": 20, "entry_i": 21}], {n: [] for n in ("bar", "two", "atr", "zigzag_2atr")})[0]
    assert d2["ref"]["point_i"] == 14 and d2["ref"]["deleted_later"] and not d2["ref_changes_if_deleted_included"]


def test_delay_multiple_labels_same_bar_and_wrong_side_ignored():
    rs = rd.classify("v1", SEG, [E("add", "HL", 14, 15), E("add", "LL", 14, 16), E("add", "LH", 17, 18)])
    d = rd.delays(rs, [{"dir": "long", "decided_at": 20, "entry_i": 21}], {n: [] for n in ("bar", "two", "atr", "zigzag_2atr")})[0]
    assert d["ref"]["point_i"] == 14 and d["ref"]["label_asof"] == 16 and d["same_bar_labels"] == 2


def test_trade_carries_decided_seq_for_delay_ordering():
    """同一边界内：先删点再决定 → 不用该点；先决定再删 → 用该点（标为 deleted_later）。"""
    ev = [E("add", "HL", 14, 15), E("delete", "HL", 14, 20), E("add", "入场做多", 20, 20)]
    rs = rd.classify("v1", SEG, ev)
    t = rd.trades(rs, B(), [1.0] * 40)["trades"]
    d = rd.delays(rs, t, {n: [] for n in ("bar", "two", "atr", "zigzag_2atr")})[0]
    assert d["ref"] is None
    ev2 = [E("add", "HL", 14, 15), E("add", "入场做多", 20, 20), E("delete", "HL", 14, 20)]
    rs2 = rd.classify("v1", SEG, ev2)
    d2 = rd.delays(rs2, rd.trades(rs2, B(), [1.0] * 40)["trades"], {n: [] for n in ("bar", "two", "atr", "zigzag_2atr")})[0]
    assert d2["ref"]["point_i"] == 14 and d2["ref"]["deleted_later"]


def test_compare_aligns_trades_and_reports_counts_only():
    from scripts import replay_diag_compare as rc
    tr = lambda seg, d, ei, px, why, au: {"seg": seg, "decided_at": d, "dir": "long", "exit_i": ei, "exit_px": px, "exit_reason": why,
                                          "atr_units": au, "entry_i": d + 1}
    old = {"v1": {"trades": [tr(1, 15, 25, 97.0, "stop", -3.0), tr(1, 30, 35, 100.0, "admin_end", 0.0)],
                  "delays": [{"ref": "unique", "extreme_to_exec": 7, "extreme_to_label": 1, "label_to_decision": 5},
                             {"ref": "none"}]},
           "v2": {"trades": [], "delays": []}}
    new = {"v1": {"trades": [tr(1, 15, 31, 100.0, "user_exit_next_open", 0.0), tr(1, 30, 35, 100.0, "admin_end", 0.0),
                             tr(2, 5, 9, 100.0, "admin_end", 0.0)],
                  "delays": [{"seg": 1, "decided_at": 15, "dir": "long", "entry_i": 16, "ref": {"point_i": 10}, "extreme_to_exec": 6,
                              "extreme_to_label": 1, "label_to_decision": 4},
                             {"seg": 1, "decided_at": 30, "dir": "long", "entry_i": 31, "ref": None}]},
           "v2": {"trades": [], "delays": []}}
    r = rc.compare(old, new)["v1"]
    assert r["added"] == 1 and r["removed"] == 0 and r["same_trade_exit_changed"] == 1
    assert r["exit_reason_changes"] == ["stop→user_exit_next_open"] and r["atr_units_change"]["median"] == 3.0
    assert r["delay_rows_compared"] == 2 and r["delay_ref_point_changed"] == 1 and r["extreme_to_exec_change"]["median"] == -1


def test_same_bar_exit_executes_before_new_entry_both_directions():
    """Codex 048 E：已有多仓，同一决定根先标新空、再标离场 → 按契约 ①离场 ②入场：先平多、再开空，不记 repeat_entry。"""
    for first, second, d1, d2 in (("入场做多", "入场做空", "long", "short"), ("入场做空", "入场做多", "short", "long")):
        t = T([E("add", first, 15, 15), E("add", second, 20, 20), E("add", "离场", 20, 20)])
        assert [(x["dir"], x["entry_i"]) for x in t["trades"]] == [(d1, 16), (d2, 21)]
        assert t["trades"][0]["exit_i"] == 21 and t["trades"][0]["exit_reason"] == "user_exit_next_open"
        assert "repeat_entry" not in t["flags"] and t["flags"]["same_bar_exit_reordered_before_entry"] == 1
    t = T([E("add", "入场做多", 15, 15), E("add", "离场", 20, 20), E("add", "入场做空", 20, 20)])   # 已按契约顺序：不计重排
    assert len(t["trades"]) == 2 and "same_bar_exit_reordered_before_entry" not in t["flags"]
    t = T([E("add", "入场做多", 15, 15), E("add", "入场做多", 20, 20)])                          # 无离场的重复入场照旧
    assert len(t["trades"]) == 1 and t["flags"]["repeat_entry"] == 1
