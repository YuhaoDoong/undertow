"""模拟仓状态机：入场窗口、权利金门槛、盯市止损、到期结算（只读报价、只写私有日志，从不下单）。"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import paper_trades as pt  # noqa: E402

ET = ZoneInfo("America/New_York")


def _p():
    return {"underlying": "GLD.US", "expiry": "2026-09-30", "side": "P", "sell": "S", "buy": "B", "k_sell": 375.0,
            "k_buy": 373.0, "qty": 1, "entry_date": "2026-09-29", "entry_window_et": ["10:00", "10:20"],
            "min_credit_ratio": 0.15, "stop_mult": 2.0, "fee_round_trip": 3.2, "mark_slots_et": ["09:40", "16:35"],
            "state": "planned", "events": []}


def q(sb, sa, bb, ba):
    return lambda syms: {"S": {"bid": sb, "ask": sa, "error": None}, "B": {"bid": bb, "ask": ba, "error": None}}


def at(d, h, m):
    return datetime(2026, 9, d, h, m, tzinfo=ET)


def test_entry_window_and_credit_rule():
    p = _p()
    assert pt.step(p, at(29, 9, 55), depth=q(1.4, 1.5, 0.9, 1.0)) is None               # 窗口前
    assert pt.step(p, at(29, 10, 5), depth=q(1.44, 1.6, 0.95, 1.03)) == "enter"
    assert p["entry_credit"] == 0.41 and p["stop_value"] == 0.82 and p["max_loss_usd"] == 162.2
    assert abs(p["breakeven"] - (375 - 0.41 + 0.032)) < 1e-9
    low = _p()
    assert pt.step(low, at(29, 10, 5), depth=q(1.0, 1.1, 0.8, 0.9)) == "skip" and "credit_low" in low["skip_reason"]
    miss = _p()
    assert pt.step(miss, at(29, 10, 5), depth=q(None, None, 0.8, 0.9)) == "retry"
    assert pt.step(miss, at(29, 10, 21), depth=q(None, None, 0.8, 0.9)) == "skip"


def test_mark_once_per_slot_and_stop():
    p = _p(); pt.step(p, at(29, 10, 5), depth=q(1.44, 1.6, 0.95, 1.03))
    assert pt.step(p, at(29, 16, 36), depth=q(0.5, 0.6, 0.2, 0.3)) == "mark"
    assert pt.step(p, at(29, 16, 41), depth=q(0.5, 0.6, 0.2, 0.3)) is None               # 同一时点只盯一次
    assert pt.step(p, at(30, 9, 41), depth=q(1.5, 1.7, 0.7, 0.8)) == "stop"              # 1.7 − 0.7 = 1.0 ≥ 0.82
    assert p["state"] == "closed_stop" and p["pnl_usd"] == round((0.41 - 1.0) * 100 - 3.2, 2)


def test_settle_at_expiry_intrinsic():
    for s, val in ((376.0, 0.0), (374.0, 1.0), (370.0, 2.0)):
        p = _p(); pt.step(p, at(29, 10, 5), depth=q(1.44, 1.6, 0.95, 1.03))
        assert pt.step(p, at(30, 16, 21), close_price=lambda u: s) == "settle"
        assert p["settle_value"] == val and p["pnl_usd"] == round((0.41 - val) * 100 - 3.2, 2)


def test_never_places_orders():
    src = (Path(__file__).resolve().parents[1] / "scripts/paper_trades.py").read_text("utf-8")
    for bad in ("order buy", "order sell", "order cancel", "order replace", '"order"', "submit_order"):
        assert bad not in src


def test_session_hook_runs_paper_tick_each_trading_wake():
    src = (Path(__file__).resolve().parents[1] / "scripts/session_hooks.sh").read_text("utf-8")
    assert src.index("paper_tick() {") < src.index("    paper_tick\n")
    assert "scripts/paper_trades.py tick" in src
