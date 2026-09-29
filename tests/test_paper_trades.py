"""模拟仓状态机 v2（Codex 025-1/2）：规格与报价校验、第一份合格报价入场、错过窗口终态、常规时段内才执行止损、
到期日收盘结算与补结、手续费口径统一。只读报价、只写私有日志，从不下单。"""
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import paper_trades as pt  # noqa: E402

ET = ZoneInfo("America/New_York")


def _p(**kw):
    p = {"underlying": "GLD.US", "expiry": "2026-09-30", "side": "P", "sell": "S", "buy": "B", "k_sell": 375.0,
         "k_buy": 373.0, "qty": 1, "entry_date": "2026-09-29", "entry_window_et": ["10:00", "10:20"],
         "min_credit_ratio": 0.15, "stop_mult": 2.0, "fee_round_trip": 3.2, "mark_slots_et": ["09:45", "15:45"],
         "state": "planned", "events": []}
    p.update(kw)
    return p


def q(sb, sa, bb, ba):
    return lambda syms: {"S": {"bid": sb, "ask": sa, "error": None}, "B": {"bid": bb, "ask": ba, "error": None}}


def at(d, h, m):
    return datetime(2026, 9, d, h, m, tzinfo=ET)


def test_economics_single_source():
    e = pt.economics("P", 375, 373, 0.41, 1, 3.2)
    assert e["max_loss_usd"] == 162.2 and e["max_gain_usd"] == 37.8 and e["breakeven"] == pytest.approx(374.622)
    e2 = pt.economics("P", 375, 373, 0.41, 2, 3.2)                      # 费用为每组，整单 × qty
    assert e2["max_loss_usd"] == 324.4 and e2["fee_total_usd"] == 6.4
    assert pt.economics("C", 400, 402, 0.3, 1, 3.2)["breakeven"] == pytest.approx(400.268)


def test_entry_first_qualifying_quote_and_rejects():
    p = _p()
    assert pt.step(p, at(29, 9, 55), depth=q(1.4, 1.5, 0.9, 1.0)) is None
    assert pt.step(p, at(29, 10, 1), depth=q(1.0, 1.1, 0.8, 0.9)) == "retry"            # 门槛不够：窗口内继续
    assert pt.step(p, at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03)) == "enter"
    assert p["entry_credit"] == 0.41 and p["economics"]["max_loss_usd"] == 162.2 and p["stop_value"] == 0.82


def test_credit_at_or_above_width_is_anomaly_not_entry():
    """Codex 025 probe：宽度 1、权利金 1.9 以前被接受、最大亏损为负。"""
    p = _p(k_sell=100.0, k_buy=99.0)
    assert pt.step(p, at(29, 10, 1), depth=q(2.0, 2.1, 0.05, 0.1)) == "retry" and p["state"] == "planned"
    assert "quote_anomaly" in p["last_reject"]
    assert pt.step(p, at(29, 10, 21), depth=q(2.0, 2.1, 0.05, 0.1)) == "skipped" and "quote_anomaly" in p["skip_reason"]


@pytest.mark.parametrize("bad", [{"k_sell": float("nan")}, {"qty": 0}, {"qty": 1.5}, {"k_buy": 376.0},
                                 {"side": "X"}, {"k_buy": 375.0}])
def test_invalid_spec(bad):
    p = _p(**bad)
    assert pt.step(p, at(29, 10, 1), depth=q(1.4, 1.5, 0.9, 1.0)) == "invalid_spec" and p["state"] == "invalid_spec"


def test_invalid_quotes_nan_and_crossed():
    p = _p()
    assert pt.step(p, at(29, 10, 1), depth=q(float("nan"), 1.5, 0.9, 1.0)) == "retry"
    assert pt.step(p, at(29, 10, 2), depth=q(1.6, 1.5, 0.9, 1.0)) == "retry"            # bid > ask
    assert p["state"] == "planned"


def test_missed_entry_day_is_terminal():
    """Codex 025 probe：错过入场日到次日仍 planned。"""
    p = _p()
    assert pt.step(p, at(30, 11, 0), depth=q(1.4, 1.5, 0.9, 1.0)) == "missed" and "missed_window" in p["skip_reason"]


def _entered():
    p = _p(); pt.step(p, at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03)); return p


def test_stop_only_inside_rth_and_marks_are_sparse():
    p = _entered()
    p["mark_slots_et"] = ["09:45", "16:35"]
    assert pt.step(p, at(29, 16, 36), depth=q(1.5, 1.7, 0.7, 0.8)) == "mark"            # 16:35 盘后：只估值
    assert p["state"] == "entered" and p["events"][-1]["action"] == "mark_offhours"
    assert pt.step(p, at(30, 9, 46), depth=q(1.5, 1.7, 0.7, 0.8)) == "stop"             # 常规时段内触发
    assert p["pnl_usd"] == round((0.41 - 1.0) * 100 - 3.2, 2)


@pytest.mark.parametrize("close,val", [(376.0, 0.0), (374.0, 1.0), (370.0, 2.0)])
def test_settle_uses_expiry_day_close(close, val):
    p = _entered()
    sc = lambda u, d: {"close": close, "source": "t", "bar_date": d.isoformat()} if d == date(2026, 9, 30) else None
    assert pt.step(p, at(30, 16, 21), session_close=sc) == "settle"
    assert p["settle_value"] == val and p["pnl_usd"] == round((0.41 - val) * 100 - 3.2, 2)
    assert "不模拟" in p["settlement_model"]


def test_settlement_pending_then_recovered_next_day():
    """Codex 025 probe：错过到期日到次日仍 entered。现在：取不到 → pending；次日补结用到期日那根。"""
    p = _entered()
    assert pt.step(p, at(30, 16, 21), session_close=lambda u, d: None) == "settlement_pending"
    assert pt.step(p, at(30, 16, 26), session_close=lambda u, d: None) is None           # 不重复刷事件
    seen = []
    sc = lambda u, d: seen.append(d) or {"close": 376.0, "source": "t", "bar_date": d.isoformat()}
    assert pt.step(p, datetime(2026, 10, 1, 10, 0, tzinfo=ET), session_close=sc) == "settle"
    assert seen == [date(2026, 9, 30)] and p["state"] == "settled"


def test_never_places_orders():
    src = (Path(__file__).resolve().parents[1] / "scripts/paper_trades.py").read_text("utf-8")
    for bad in ("order buy", "order sell", "order cancel", "order replace", '"order"', "submit_order"):
        assert bad not in src


def test_session_hook_runs_paper_tick_each_trading_wake():
    src = (Path(__file__).resolve().parents[1] / "scripts/session_hooks.sh").read_text("utf-8")
    assert src.index("paper_tick() {") < src.index("    paper_tick\n")
    assert "scripts/paper_trades.py tick" in src
