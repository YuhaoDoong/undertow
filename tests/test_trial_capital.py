"""实盘试行资本与暂停（Codex 043 WP3 验收；Codex 044 包 B/C 反例回归）。全部合成输入，持久化只用 tmp_path。"""
import threading

import pytest

from undertow.analyze import risk_policy as rp
from undertow.analyze import trial_capital as tc
from undertow.collect import trial_store as ts

P = rp.POLICY_B


def T(day, hh=12):
    return f"2026-{day}T{hh:02d}:00:00+00:00"


def funded(amount=500.0):
    # 到账 10-14 00:00：此前用例在 10-14 白天做核查，旧版早于到账（10-15）的观察也照算；现在观察早于到账 → 不可判定
    s = tc.new_state(trial_id="t1", planned_capital=500.0, policy_version=P["version"], created_at=T("10-13"))
    return tc.record_funding(s, amount=amount, at=T("10-14", 0))


def ev(s, equity, at, fill=None):
    return tc.evaluate(s, equity=equity, equity_complete=True, asof=at, policy=P, latest_fill_at=fill)


def test_unfunded_trial_is_not_determinable():
    s = tc.new_state(trial_id="t1", planned_capital=500.0, policy_version=P["version"], created_at=T("10-14"))
    s, v = ev(s, 500.0, T("10-15"))
    assert v["pause"] is None and "尚未到账" in v["note"]


def test_one_300_loss_latches_at_60pct_not_50():
    s = tc.record_trade(funded(), trade_id="a", net_pnl_usd=-300.0, closed_at=T("10-16"), complete=True)
    s, v = ev(s, 200.0, T("10-16", 13))
    assert v["initial_capital_loss_frac"] == 0.6 and v["pause"]["latched"] is True


def test_two_200_stops_is_80pct():
    s, v = ev(funded(), 300.0, T("10-16"))
    assert v["pause"]["latched"] is False
    s, v = ev(s, 100.0, T("10-17"))
    assert v["initial_capital_loss_frac"] == 0.8 and v["pause"]["latched"] is True


def test_four_complete_losses_latch_but_partial_close_does_not_count():
    s = funded(1000.0)
    for i, d in enumerate(("10-16", "10-17", "10-20")):
        s = tc.record_trade(s, trade_id=f"t{i}", net_pnl_usd=-10.0, closed_at=T(d), complete=True)
    s = tc.record_trade(s, trade_id="p", net_pnl_usd=-5.0, closed_at=T("10-21"), complete=False)
    s, v = ev(s, 965.0, T("10-21", 13))
    assert v["consecutive_losses"] == 3 and v["pause"]["latched"] is False
    s = tc.record_trade(s, trade_id="p", net_pnl_usd=-8.0, closed_at=T("10-22"), complete=True)   # 部分平仓随后完结
    s, v = ev(s, 962.0, T("10-22", 13))
    assert v["consecutive_losses"] == 4 and v["pause"]["latched"] is True
    assert v["initial_capital_loss_frac"] < 0.5                           # 回撤闸门补不上这一条：靠连续亏损锁住


def test_duplicate_import_idempotent_conflict_rejected():
    s = tc.record_trade(funded(), trade_id="a", net_pnl_usd=-10.0, closed_at=T("10-16"), complete=True)
    assert tc.record_trade(s, trade_id="a", net_pnl_usd=-10.0, closed_at=T("10-16"), complete=True) == s
    with pytest.raises(ValueError, match="revise"):
        tc.record_trade(s, trade_id="a", net_pnl_usd=-12.0, closed_at=T("10-16"), complete=True)
    s2 = tc.record_trade(s, trade_id="a", net_pnl_usd=-12.0, closed_at=T("10-16"), complete=True, revise=True)
    assert s2["closed_trades"]["a"]["revisions"][0]["net_pnl_usd"] == -10.0


def test_no_result_source_is_unknown_not_zero():
    """044 P1-3：没有核查券商成交 → 连续亏损不可判定（旧版空字典 = 确定 0 笔）。"""
    _, v = tc.evaluate(funded(), equity=500.0, equity_complete=True, asof=T("10-16"), policy=P)   # 未传成交核查
    assert v["consecutive_losses"] is None and v["pause"] is None
    _, v = ev(funded(), 500.0, T("10-16"), fill=T("10-15", 15))          # 覆盖时点之后有成交、未登记
    assert v["consecutive_losses"] is None and v["pause"] is None and "之后还有成交" in v["consecutive_losses_note"]
    _, v = ev(funded(), 500.0, T("10-16"), fill=None)                    # 已核查：到账后没有任何成交 → 确定 0
    assert v["consecutive_losses"] == 0 and v["pause"]["latched"] is False
    s = tc.verify_trades(funded(), through=T("10-16", 1), verified_at=T("10-16", 1), source="人工核对", note="对过流水")
    _, v = ev(s, 500.0, T("10-16"), fill=T("10-15", 15))                 # 核对时点覆盖了那笔成交
    assert v["consecutive_losses"] == 0
    with pytest.raises(ValueError):
        tc.verify_trades(s, through=T("10-15"), verified_at=T("10-16"), source="x", note="回退")


def test_deposit_before_first_check_does_not_dilute_initial_loss():
    """044 P1-4：起始 500、完整净亏 300、追加 300、权益 500、首次核查 → 起始亏损仍 60% 并锁（旧版 37.5%、不锁）。"""
    s = tc.record_trade(funded(), trade_id="t1", net_pnl_usd=-300, closed_at=T("10-14", 1), complete=True)
    s = tc.record_funding(s, amount=300, at=T("10-14", 2))
    s, v = ev(s, 500.0, T("10-14", 3))
    assert v["initial_capital_loss_frac"] == 0.6 and v["pause"]["latched"] is True
    assert v["cumulative_invested_loss_frac"] == 0.375                   # 累计投入口径只报告


def test_latched_then_deposit_stays_latched_and_withdrawal_is_not_loss():
    s, _ = ev(funded(), 200.0, T("10-16"))
    s = tc.record_funding(s, amount=300, at=T("10-17"))
    s, v = ev(s, 500.0, T("10-18"))
    assert v["pause"]["latched"] is True
    s = tc.record_funding(funded(), amount=-100, at=T("10-17"))          # 转出 100，权益 400：不是交易亏损
    s, v = ev(s, 400.0, T("10-18"))
    assert v["initial_capital_loss_frac"] == 0.0 and v["pause"]["latched"] is False


def test_resume_needs_user_words_and_warns_it_will_relock():
    s, _ = ev(funded(), 200.0, T("10-16"))
    with pytest.raises(ValueError):
        tc.resume(s, user_note="", at=T("10-19"))
    s = tc.resume(s, user_note="用户：恢复", at=T("10-19"))
    assert s["pause_latched"] is False and s["resume_log"][-1]["was_reason"]
    assert "立即重新暂停" in tc.relock_warning(s, P)
    _, v = ev(s, 200.0, T("10-20"))
    assert v["pause"]["latched"] is True                                 # 沿用原基准：说明与行为一致


def test_missing_valuation_freezes_new():
    s, v = tc.evaluate(funded(), equity=None, equity_complete=False, asof=T("10-16"), policy=P, latest_fill_at=None)
    assert v["pause"] is None
    r = rp.evaluate_budget(candidate={"underlying": "GLD", "unit_stop_loss": 100, "unit_payoff_max_loss": 100},
                           open_positions=[], capital={"equity": None}, pause=v["pause"])
    assert r["allowed_units"] == 0 and r["budget_status"] == "incomplete"


def test_stale_observation_does_not_overwrite_history():
    s, _ = ev(funded(), 480.0, T("10-16", 15))
    s2, v = ev(s, 500.0, T("10-16", 14))
    assert v["stale_observation"] and s2["equity_history"] == s["equity_history"]


def test_codex_045_stale_equity_does_not_subtract_future_deposit():
    """045 P2 反例：起始 500，02:00 追加 500，03:00 权益 1000；之后 01:00 的旧读数 500 返回。
    旧版：500 − 500（02:00 才到的入金）→ 起始亏损 100%、锁住；其实没有亏损。"""
    s = tc.record_funding(funded(), amount=500, at=T("10-14", 2))
    s, v = ev(s, 1000.0, T("10-14", 3))
    assert v["initial_capital_loss_frac"] == 0.0 and v["pause"]["latched"] is False
    s2, v = ev(s, 500.0, T("10-14", 1))
    assert v["stale_observation"] and not s2["pause_latched"] and v["pause"] is None   # 只审计：本次不可判定、冻结新增
    assert v["initial_capital_loss_frac"] == 0.0 and v["net_external_flows"] == 0 and v["flows_after_asof"] == 1
    assert s2["equity_history"] == s["equity_history"]


def test_observation_without_newer_history_uses_flows_asof():
    """首次观察早于后来登记的入金/转出：只扣观察时点之前已发生的现金流。"""
    s = tc.record_funding(funded(), amount=500, at=T("10-14", 2))       # 入金登记了，但观察在入金之前
    s, v = ev(s, 500.0, T("10-14", 1))
    assert v["initial_capital_loss_frac"] == 0.0 and v["pause"]["latched"] is False
    s = tc.record_funding(funded(), amount=-200, at=T("10-14", 2))      # 转出登记了，观察在转出之前
    s, v = ev(s, 500.0, T("10-14", 1))
    assert v["initial_capital_loss_frac"] == 0.0 and v["net_external_flows"] == 0
    s, v = ev(s, 300.0, T("10-14", 3))                                   # 转出之后：权益 300 + 转出 200 = 500
    assert v["initial_capital_loss_frac"] == 0.0 and v["pause"]["latched"] is False


def test_observation_before_funding_not_determinable():
    s, v = ev(funded(), 100.0, T("10-13", 23))
    assert v["pause"] is None and "早于到账" in v["note"] and not s["pause_latched"] and s["equity_history"] == []


def test_stale_observation_keeps_real_latch():
    s, v = ev(funded(), 200.0, T("10-14", 5))                           # 真亏 60% → 锁
    assert s["pause_latched"]
    s2, v = ev(s, 500.0, T("10-14", 4))                                  # 旧读数（更高权益）不能解除
    assert s2["pause_latched"] and v["pause"]["latched"] is True and v["stale_observation"]


@pytest.mark.parametrize("mut", [
    lambda s: s.pop("funded_capital"),
    lambda s: s.update(funded_capital=float("nan")),
    lambda s: s.update(funded_capital="500"),
    lambda s: s.update(external_flows=[{"at": T("10-16"), "amount": None}]),
    lambda s: s.update(closed_trades={"a": {"closed_at": "2026-10-16", "complete": True, "net_pnl_usd": -1.0}}),
    lambda s: s.update(pause_latched=True),                              # 已暂停却无原因/时间
    lambda s: s.update(rev=-1),
    lambda s: s["trade_coverage"].pop("through"),
])
def test_deep_validation_catches_semantic_damage(mut):
    """044 P2-7：删掉 funded_capital 旧版 validate 放行、evaluate 才 KeyError。"""
    s = {**funded(), "rev": 0, "trade_coverage": dict(funded()["trade_coverage"])}
    mut(s)
    assert tc.validate(s) is not None


# —— 持久化：事务与不变量（044 包 B）——

def test_restart_keeps_latch_and_corrupt_file_quarantined(tmp_path):
    f = tmp_path / "state.json"
    ts.update(lambda _: funded(), f, create=True)
    ts.update(lambda cur: ev(cur, 200.0, T("10-16"))[0], f)
    back, prob = ts.load(f)                                               # 重启 = 重新读盘
    assert prob is None and back["pause_latched"] is True and back["rev"] == 1
    f.write_text("{半截")
    back, prob = ts.load(f)
    assert back is None and "已隔离" in prob and not f.exists() and list(tmp_path.glob("state.json.corrupt-*"))


def test_semantic_damage_on_disk_is_quarantined_not_overwritten(tmp_path):
    import json
    f = tmp_path / "state.json"
    st = ts.update(lambda _: funded(), f, create=True)
    bad = dict(st); bad.pop("funded_capital")
    f.write_text(json.dumps(bad))
    with pytest.raises(ts.TrialStateError, match="已隔离"):
        ts.update(lambda cur: ev(cur, 500.0, T("10-16"))[0], f)
    assert not f.exists() and list(tmp_path.glob("state.json.corrupt-*"))


def test_init_does_not_overwrite(tmp_path):
    f = tmp_path / "state.json"
    ts.update(lambda _: funded(), f, create=True)
    with pytest.raises(ts.TrialStateError, match="已存在"):
        ts.update(lambda _: funded(), f, create=True)


def test_codex_interleaving_stale_reader_cannot_clear_latch(tmp_path):
    """044 P1-2 反例：A/B 都读到未暂停的旧版本；A 以权益 200 写入暂停；B 用旧快照（权益 500）后写。
    旧版磁盘 latch true→false。现在 B 的计算在锁内对最新状态做，latch 保持 true。"""
    f = tmp_path / "state.json"
    ts.update(lambda _: funded(), f, create=True)
    a_snap, _ = ts.load(f)
    b_snap, _ = ts.load(f)
    assert a_snap == b_snap and not b_snap["pause_latched"]
    ts.update(lambda cur: ev(cur, 200.0, T("10-14", 10))[0], f)
    ts.update(lambda cur: ev(cur, 500.0, T("10-14", 9))[0], f)           # B：较早的观察、较高的权益
    back, _ = ts.load(f)
    assert back["pause_latched"] is True
    with pytest.raises(ts.TrialStateError, match="resume"):              # 直接写回旧快照也被不变量拒绝
        ts.update(lambda cur: {**b_snap, "rev": cur["rev"]}, f)
    assert ts.load(f)[0]["pause_latched"] is True


def test_threads_with_barrier_inside_lock(tmp_path):
    """真并发：A 在锁内停住时 B 发起写入；B 必须等 A 写完，再看到 A 的暂停。"""
    f = tmp_path / "state.json"
    ts.update(lambda _: funded(), f, create=True)
    inside, release = threading.Event(), threading.Event()

    def a_fn(cur):
        inside.set(); release.wait(5)
        return ev(cur, 200.0, T("10-14", 10))[0]
    seen = {}

    def b_fn(cur):
        seen["latched_when_b_ran"] = cur["pause_latched"]
        return ev(cur, 500.0, T("10-14", 11))[0]
    ta = threading.Thread(target=lambda: ts.update(a_fn, f)); ta.start()
    inside.wait(5)
    tb = threading.Thread(target=lambda: ts.update(b_fn, f)); tb.start()
    release.set(); ta.join(5); tb.join(5)
    assert seen["latched_when_b_ran"] is True and ts.load(f)[0]["pause_latched"] is True and ts.load(f)[0]["rev"] == 2


def test_concurrent_fund_trade_and_observation_do_not_lose_events(tmp_path):
    f = tmp_path / "state.json"
    ts.update(lambda _: funded(1000.0), f, create=True)
    jobs = [lambda cur, i=i: tc.record_trade(cur, trade_id=f"t{i}", net_pnl_usd=-1.0, closed_at=T("10-16", i), complete=True)
            for i in range(6)]
    jobs += [lambda cur, i=i: tc.record_funding(cur, amount=10.0, at=T("10-17", i)) for i in range(4)]
    jobs += [lambda cur, i=i: ev(cur, 990.0, T("10-18", i))[0] for i in range(4)]
    th = [threading.Thread(target=lambda j=j: ts.update(j, f)) for j in jobs]
    [t.start() for t in th]; [t.join(10) for t in th]
    back, _ = ts.load(f)
    assert len(back["closed_trades"]) == 6 and len(back["external_flows"]) == 4 and len(back["equity_history"]) >= 1
    assert back["rev"] == 6 + 4 + len(back["equity_history"])          # 每个事件一次写入；过时观察不写


def test_pause_view_end_to_end_four_losses_via_cli_entry(tmp_path, monkeypatch):
    """044 包 C 验收：经正式入口（trial trade / verify-trades 用的同一函数）登记四笔完整净亏 → pause_view 与候选预算都看到并暂停。"""
    from undertow import trial_cli
    f = tmp_path / "state.json"
    monkeypatch.setattr(ts, "STATE", f)
    monkeypatch.setattr(ts.update, "__defaults__", (f,) + ts.update.__defaults__[1:])
    monkeypatch.setattr(ts.load, "__defaults__", (f,))
    ts.update(lambda _: funded(1000.0), create=True)
    for i in range(4):
        ts.update(lambda cur, i=i: tc.record_trade(cur, trade_id=f"t{i}", net_pnl_usd=-10.0, closed_at=T("10-16", i),
                                                   complete=True, source="人工登记", note="合成"))
    ts.update(lambda cur: tc.verify_trades(cur, through=T("10-16", 20), verified_at=T("10-16", 20), source="人工核对", note="合成"))
    ts.update(lambda cur: tc.record_trade(cur, trade_id="t0", net_pnl_usd=-10.0, closed_at=T("10-16", 0),
                                          complete=True, source="人工登记", note="合成"))   # 重复导入不增笔数
    pause, note = trial_cli.pause_view(960.0, equity_complete=True, asof=T("10-17"), latest_fill_at=None)
    assert pause["latched"] is True and "连续 4 笔" in pause["reason"]
    r = rp.evaluate_budget(candidate={"underlying": "GLD", "unit_stop_loss": 10, "unit_payoff_max_loss": 10, "qty_requested": 1},
                           open_positions=[], capital={"equity": 960.0}, pause=pause)
    assert r["allowed_units"] == 0 and r["budget_status"] == "fail"
    pause, note = trial_cli.pause_view(960.0, equity_complete=True, asof=T("10-18"), latest_fill_at=...)
    assert pause["latched"] is True                                       # 已锁：成交未核查也不解除


def test_store_slow_response_after_deposit_no_false_latch(tmp_path):
    """045 P2 经正式存储事务：入金与新观察先落盘，慢返回的旧读数在锁内对最新状态计算 → 不锁、不进历史。"""
    f = tmp_path / "state.json"
    ts.update(lambda _: funded(), f, create=True)
    ts.update(lambda cur: tc.record_funding(cur, amount=500, at=T("10-14", 2)), f)
    ts.update(lambda cur: ev(cur, 1000.0, T("10-14", 3))[0], f)
    ts.update(lambda cur: ev(cur, 500.0, T("10-14", 1))[0], f)
    back, _ = ts.load(f)
    assert back["pause_latched"] is False and [h["asof"] for h in back["equity_history"]] == [T("10-14", 3)]
