"""组合单成交解析：长桥对多腿单会退化，必须从资金流水重建腿级明细。

长桥 fetch_today_executions() 对组合单返回：symbol=【标的】、side="-"（方向丢失）、
每腿一行 + 一行净价，三行共用一个 order_id。照抄会写出「方向全是卖出、金额全是 0」
的垃圾记录（2026-08-26 TQQQ 76/80 首笔价差单实测）。
资金流水里才有真实 OCC 代码、Option Buy/Sell Transaction 与现金变动。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.soul.journal import capture_trades

DAY = "2026-08-26"


def _combo_exec(oid="1", t="2026-08-26T14:08:58Z"):
    """组合单的成交行：方向为 '-'、symbol 是标的。"""
    return [{"order_id": oid, "price": "0.9", "quantity": "1", "side": "-",
             "symbol": "TQQQ.US", "time": t},
            {"order_id": oid, "price": "1.68", "quantity": "1", "side": "-",
             "symbol": "TQQQ.US", "time": t},
            {"order_id": oid, "price": "0.78", "quantity": "1", "side": "-",
             "symbol": "TQQQ.US", "time": t}]


def _combo_flows(t="2026-08-26T14:08:58Z"):
    return [{"symbol": "TQQQ260918C76000.US", "balance": "-168.00",
             "flow_name": "Option Buy Transaction", "time": t},
            {"symbol": "TQQQ260918C80000.US", "balance": "78.00",
             "flow_name": "Option Sell Transaction", "time": t}]


def test_combo_rebuilt_from_cash_flow():
    """方向为 '-' 的成交行必须丢弃，改由流水重建腿级明细。"""
    ts = capture_trades(_combo_exec(), _combo_flows(), day=DAY)
    assert len(ts) == 2, [t.symbol for t in ts]
    by = {t.symbol: t for t in ts}
    long_leg = by["TQQQ260918C76000.US"]
    short_leg = by["TQQQ260918C80000.US"]
    assert long_leg.side == "buy" and abs(long_leg.amount + 168.0) < 1e-9
    assert short_leg.side == "sell" and abs(short_leg.amount - 78.0) < 1e-9
    assert abs(long_leg.price - 1.68) < 1e-9 and abs(short_leg.price - 0.78) < 1e-9
    # 净额必须等于组合单的净借记
    assert abs((long_leg.amount + short_leg.amount) + 90.0) < 1e-9
    print("PASS test_combo_rebuilt_from_cash_flow")


def test_same_minute_two_combos_marks_qty_unknown():
    """同一分钟出现两张【不同】组合单时，张数无从归属 —— 标 0(未知)，不许张冠李戴。

    流水行不带 order_id，只能按分钟对齐。旧写法用 max() 取张数，会把某一张的
    数量安到另一张头上，直接算错单价。
    """
    ex = _combo_exec(oid="A") + [
        {"order_id": "B", "price": "2.0", "quantity": "5", "side": "-",
         "symbol": "QQQ.US", "time": "2026-08-26T14:08:58Z"}]
    ts = capture_trades(ex, _combo_flows(), day=DAY)
    assert ts and all(t.qty == 0 for t in ts), [(t.symbol, t.qty) for t in ts]
    assert all(t.price == 0 for t in ts), "数量未知时不得给出单价"
    # 金额仍必须可信
    assert abs(sum(t.amount for t in ts) + 90.0) < 1e-9
    print("PASS test_same_minute_two_combos_marks_qty_unknown")


def test_single_leg_orders_untouched():
    """普通单腿单（side=Buy/Sell）照常走成交行，不受组合单逻辑影响。"""
    ex = [{"order_id": "X", "price": "1.05", "quantity": "3", "side": "Buy",
           "symbol": "SLV260918C70000.US", "time": "2026-08-26T14:27:23Z"}]
    ts = capture_trades(ex, [], day=DAY)
    assert len(ts) == 1 and ts[0].side == "buy" and ts[0].qty == 3
    print("PASS test_single_leg_orders_untouched")


def test_fee_rows_do_not_become_trades():
    """费用行只进 fee 汇总，不单独成为一笔成交。"""
    flows = _combo_flows() + [{"symbol": "TQQQ260918C76000.US", "balance": "-0.80",
                               "flow_name": "Option Buy Fee",
                               "time": "2026-08-26T14:08:58Z"}]
    ts = capture_trades(_combo_exec(), flows, day=DAY)
    assert len(ts) == 2, [t.symbol for t in ts]
    assert any(t.fee > 0 for t in ts), "费用应汇总到对应合约"
    print("PASS test_fee_rows_do_not_become_trades")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
