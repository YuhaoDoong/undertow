"""收盘决策与快照估价的时间顺序回归；不调用网络或真实台账。"""
from datetime import date
from types import SimpleNamespace

import pytest

from undertow.analyze.spread_exit import evaluate_exit
from undertow.analyze.wall_spread import FEE_PER_TRADE, close_cost

D0, D1, D2, EXP = (date(2026, 9, x) for x in (8, 9, 10, 11))


def snapshot(sell_bid=.4, sell_ask=.6, buy_bid=.1, buy_ask=.2):
    return SimpleNamespace(contracts=[
        SimpleNamespace(kind="C", strike=105, expiry=EXP, bid=sell_bid, ask=sell_ask),
        SimpleNamespace(kind="C", strike=107, expiry=EXP, bid=buy_bid, ask=buy_ask),
    ])


def run(closes, snapshots, rule="break"):
    return evaluate_exit(kind="C", sell=105, buy=107, expiry=EXP,
                         entry_day=D0, credit=31.25, initial_wall=105,
                         snapshots=snapshots, closes=closes, rule=rule)


def test_close_trigger_cannot_use_same_days_preopen_quote():
    snaps = {D0: snapshot(), D1: snapshot(), D2: snapshot(1.5, 1.7, .1, .2)}
    got = run({D0: 100, D1: 110, D2: 110, EXP: 110}, snaps)
    assert got["trigger_day"] == D1
    assert got["exit_day"] == D2
    later = snaps[D2].contracts
    assert got["pnl"] == pytest.approx(31.25 - close_cost(*later) - FEE_PER_TRADE)
    assert got["execution_verified"] is False


def test_entry_day_close_is_checked_and_roundtrip_fee_not_doubled():
    snaps = {D0: snapshot(), D1: snapshot()}
    got = run({D0: 110, D1: 100, EXP: 100}, snaps)
    assert (got["trigger_day"], got["exit_day"]) == (D0, D1)
    assert got["pnl"] == pytest.approx(31.25 - close_cost(*snaps[D1].contracts) - FEE_PER_TRADE)


def test_close_on_missing_snapshot_day_still_triggers_and_persists():
    got = run({D0: 100, D1: 110, D2: 100, EXP: 100}, {D0: snapshot(), D2: snapshot()})
    assert (got["trigger_day"], got["exit_day"]) == (D1, D2)


def test_unpriced_exit_is_unknown_not_substituted_with_expiry_profit():
    got = run({D0: 100, D1: 110, EXP: 100}, {D0: snapshot(), D1: snapshot()})
    assert got["status"] == "unpriced_exit"
    assert got["pnl"] is None and got["exit_day"] is None


def test_hold_and_small_breach_can_still_profit():
    got = run({D0: 100, EXP: 105.1}, {D0: snapshot()}, rule="hold")
    assert got["pnl"] == pytest.approx(31.25 - 10 - FEE_PER_TRADE)
    assert got["status"] == "expiry_model"


def test_zero_settlement_is_real_data_for_put():
    got = evaluate_exit(kind="P", sell=105, buy=103, expiry=EXP,
                        entry_day=D0, credit=31.25, initial_wall=105,
                        snapshots={}, closes={D0: 110, EXP: 0}, rule="hold")
    assert got["pnl"] == pytest.approx(31.25 - 200 - FEE_PER_TRADE)


def test_malformed_quote_does_not_create_exit_fill():
    got = run({D0: 110, EXP: 100}, {D0: snapshot(), D1: snapshot(sell_bid=2, sell_ask=1)})
    assert got["pnl"] is None and got["status"] == "unpriced_exit"


def test_expiry_quote_after_previous_close_trigger_is_usable_only_as_model():
    got = run({D0: 100, D2: 110, EXP: 100}, {D0: snapshot(), EXP: snapshot()})
    assert got["exit_day"] == EXP and got["trigger_day"] == D2
    assert got["status"] == "snapshot_model" and not got["execution_verified"]
