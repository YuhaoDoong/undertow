"""实盘试行资本与暂停（Codex 043 WP3 验收）。"""
from undertow.analyze import risk_policy as rp
from undertow.analyze import trial_capital as tc
from undertow.collect import trial_store as ts

P = rp.POLICY_B


def funded(amount=500.0):
    s = tc.new_state(trial_id="t1", planned_capital=500.0, policy_version=P["version"], created_at="2026-10-14")
    return tc.record_funding(s, amount=amount, at="2026-10-15")


def test_unfunded_trial_is_not_determinable():
    s = tc.new_state(trial_id="t1", planned_capital=500.0, policy_version=P["version"], created_at="2026-10-14")
    s, v = tc.evaluate(s, equity=500.0, equity_complete=True, asof="2026-10-15", policy=P)
    assert v["pause"] is None and "尚未到账" in v["note"]


def test_one_300_loss_latches_at_60pct_not_50():
    s = tc.record_trade(funded(), trade_id="a", net_pnl_usd=-300.0, closed_at="2026-10-16", complete=True)
    s, v = tc.evaluate(s, equity=200.0, equity_complete=True, asof="2026-10-16", policy=P)
    assert v["initial_capital_loss_frac"] == 0.6 and v["pause"]["latched"] is True


def test_two_200_stops_is_80pct():
    s = funded()
    s, v = tc.evaluate(s, equity=300.0, equity_complete=True, asof="2026-10-16", policy=P)
    assert v["pause"]["latched"] is False
    s, v = tc.evaluate(s, equity=100.0, equity_complete=True, asof="2026-10-17", policy=P)
    assert v["initial_capital_loss_frac"] == 0.8 and v["pause"]["latched"] is True


def test_four_complete_losses_latch_but_partial_close_does_not_count():
    s = funded(1000.0)
    for i, d in enumerate(("10-16", "10-17", "10-20")):
        s = tc.record_trade(s, trade_id=f"t{i}", net_pnl_usd=-10.0, closed_at=f"2026-{d}", complete=True)
    s = tc.record_trade(s, trade_id="p", net_pnl_usd=-5.0, closed_at="2026-10-21", complete=False)
    s, v = tc.evaluate(s, equity=965.0, equity_complete=True, asof="2026-10-21", policy=P)
    assert v["consecutive_losses"] == 3 and v["pause"]["latched"] is False
    s = tc.record_trade(s, trade_id="p", net_pnl_usd=-8.0, closed_at="2026-10-22", complete=True)
    s, v = tc.evaluate(s, equity=962.0, equity_complete=True, asof="2026-10-22", policy=P)
    assert v["consecutive_losses"] == 4 and v["pause"]["latched"] is True


def test_deposit_does_not_wash_loss_or_unlatch_and_resume_needs_user_words():
    s = tc.record_trade(funded(), trade_id="a", net_pnl_usd=-300.0, closed_at="2026-10-16", complete=True)
    s, _ = tc.evaluate(s, equity=200.0, equity_complete=True, asof="2026-10-16", policy=P)
    s = tc.record_funding(s, amount=300.0, at="2026-10-18")
    s, v = tc.evaluate(s, equity=500.0, equity_complete=True, asof="2026-10-18", policy=P)
    assert v["net_external_flows"] == 300.0 and v["initial_capital_loss_frac"] == 0.375 and v["pause"]["latched"] is True
    try:
        tc.resume(s, user_note="", at="2026-10-19"); assert False
    except ValueError:
        pass
    s = tc.resume(s, user_note="用户：恢复", at="2026-10-19")
    assert s["pause_latched"] is False and s["resume_log"][-1]["was_reason"]


def test_missing_valuation_freezes_new():
    s, v = tc.evaluate(funded(), equity=None, equity_complete=False, asof="2026-10-16", policy=P)
    assert v["pause"] is None
    r = rp.evaluate_budget(candidate={"underlying": "GLD", "unit_stop_loss": 100, "unit_payoff_max_loss": 100},
                           open_positions=[], capital={"equity": None}, pause=v["pause"])
    assert r["allowed_units"] == 0 and r["budget_status"] == "incomplete"


def test_restart_keeps_latch_and_corrupt_file_quarantined(tmp_path):
    f = tmp_path / "state.json"
    s = tc.record_trade(funded(), trade_id="a", net_pnl_usd=-300.0, closed_at="2026-10-16", complete=True)
    s, _ = tc.evaluate(s, equity=200.0, equity_complete=True, asof="2026-10-16", policy=P)
    ts.save(s, f)
    back, prob = ts.load(f)                                       # 重启 = 重新读盘
    assert prob is None and back["pause_latched"] is True
    f.write_text("{半截")
    back, prob = ts.load(f)
    assert back is None and "已隔离" in prob and not f.exists() and list(tmp_path.glob("state.json.corrupt-*"))
