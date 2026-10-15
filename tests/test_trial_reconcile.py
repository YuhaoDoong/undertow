"""券商资金流水 + 成交自动对账（trial-reconcile-v2，Codex 048 A/B/C）。全部合成输入；行形态照 2026-10-15 实测长桥 CLI：
组合单成交 = 标的代码的一行净价 + 各腿价各一行（side 为整单方向），流水 = 每腿一行带符号金额（无张数），费用按（合约, 方向）合并、
结算日入账，到期作废 = 0 价合约代码成交行。"""
import functools
import types

import pytest

from undertow.analyze import risk_policy as rp
from undertow.analyze import trial_capital as tc
from undertow.analyze import trial_reconcile as tr
from undertow.collect import trial_store as ts

START = "2026-10-15T14:00:00+00:00"
NOW = "2026-10-21T12:00:00+00:00"
READS = []                                                     # 「已提供读数且无问题」
L, S = "USO261030C158000", "USO261030C162000"
X = "SLV261030P50000"


def cash(code, side, amt, t, ccy="USD", old=False):
    name = "Option Buy Transaction" if side == "buy" else "Option Sell Transaction"
    if old:
        return {"balance": f"{amt:.2f}", "currency": ccy, "description": f"OP/US/{code}", "flow_name": name, "symbol": "", "time": t}
    return {"balance": f"{amt:.2f}", "currency": ccy, "description": code, "flow_name": name, "symbol": f"{code}.US", "time": t}


def fee(code, side, amt, t, ccy="USD"):
    name = "Option Buy Fee" if side == "buy" else "Option Sell Fee"
    return {"balance": f"{amt:.2f}", "currency": ccy, "description": code, "flow_name": name, "symbol": f"{code}.US", "time": t}


def ex(order, sym, side, qty, px, t):
    return {"order_id": order, "price": f"{px}", "quantity": f"{qty}", "side": side.capitalize(), "symbol": sym, "time": t}


def single(order, code, side, qty, px, t):
    """单腿单：成交行 symbol = 合约代码；流水一行。"""
    sign = -1 if side == "buy" else 1
    return [cash(code, side, sign * px * qty * 100, t)], [ex(order, f"{code}.US", side, qty, px, t)]


def combo(order, legs, qty, t, order_side, root="USO"):
    """组合单：legs = [(合约, 腿方向, 腿价)]；成交 = 净价一行 + 各腿价各一行（symbol 为标的、side 为整单方向）；流水每腿一行。"""
    net = abs(sum((1 if sd == "sell" else -1) * px for _, sd, px in legs))
    exs = [ex(order, f"{root}.US", order_side, qty, round(net, 4), t)] + [ex(order, f"{root}.US", order_side, qty, px, t) for _, _, px in legs]
    flows = [cash(c, sd, (-1 if sd == "buy" else 1) * px * qty * 100, t) for c, sd, px in legs]
    return flows, exs


def bull_call(close=True, fees_close=True):
    """10/15 开 USO 158/162C ×1（买 158@3.95、卖 162@2.66），10/19 平（卖 158@5.00、买 162@3.00）。"""
    f1, e1 = combo("o1", [(L, "buy", 3.95), (S, "sell", 2.66)], 1, "2026-10-15T14:00:05Z", "buy")
    flows, exs = f1 + [fee(L, "buy", -2.41, "2026-10-16T10:30:00Z"), fee(S, "sell", -2.43, "2026-10-16T10:30:00Z")], e1
    if close:
        f2, e2 = combo("o2", [(L, "sell", 5.00), (S, "buy", 3.00)], 1, "2026-10-19T15:00:01Z", "sell")
        flows, exs = flows + f2, exs + e2
        if fees_close:
            flows += [fee(L, "sell", -2.43, "2026-10-20T10:30:00Z"), fee(S, "buy", -2.41, "2026-10-20T10:30:00Z")]
    return flows, exs


def run(flows, exs, positions=None, cov=START, now=NOW, known=None, reads=READS):
    return tr.reconcile(cash_flow=flows, positions=positions or {}, executions=exs, coverage_through=cov,
                        trial_start=START, now=now, known_trades=known, reads=reads)


# —— 基本路径 ——

def test_combo_round_trip_records_net_with_fees():
    out = run(*bull_call())
    (g,) = out["groups"]
    assert g["state"] == "closed" and g["contracts"] == [L, S] and g["segments"] == 2
    assert g["net_pnl_usd"] == round(-395 + 266 + 500 - 300 - 2.41 - 2.43 - 2.43 - 2.41, 2)
    assert out["can_advance_coverage"] and len(out["records"]) == 1


def test_close_fees_missing_blocks_and_no_record():
    out = run(*bull_call(fees_close=False))
    assert out["groups"][0]["fees_settled"] is False and not out["can_advance_coverage"] and out["records"] == []


def test_still_held_is_open_and_position_must_match_broker():
    flows, exs = bull_call(close=False)
    ok = run(flows, exs, positions={f"{L}.US": 1, f"{S}.US": -1})
    assert ok["groups"][0]["state"] == "open" and ok["records"] == [] and ok["can_advance_coverage"]
    bad = run(flows, exs, positions={})                         # 推算仍持有、券商说没有 → 全局阻断
    assert not bad["can_advance_coverage"] and any("持仓不符" in b for b in bad["blockers"])


# —— Codex 048 验收清单 ——

def test_same_contract_four_round_trips_on_four_days_are_four_trades_not_one():
    flows, exs = [], []
    for k, day in enumerate(("15", "16", "19", "20")):
        f, e = single(f"b{k}", X, "buy", 1, 1.00, f"2026-10-{day}T15:00:00Z")
        f2, e2 = single(f"s{k}", X, "sell", 1, 0.92, f"2026-10-{day}T16:00:00Z")
        nxt = f"2026-10-{int(day) + 1}T10:00:00Z"
        flows += f + f2 + [fee(X, "buy", -2.0, nxt), fee(X, "sell", -2.0, nxt)]
        exs += e + e2
    out = run(flows, exs, now="2026-10-22T12:00:00+00:00")
    assert [g["net_pnl_usd"] for g in out["groups"]] == [-12.0] * 4
    assert len({g["trade_id"] for g in out["groups"]}) == 4 and len(out["records"]) == 4
    first = out["records"][0]
    known = {first["trade_id"]: {"net_pnl_usd": -12.0, "closed_at": first["closed_at"], "source": tr.SOURCE}}
    out2 = run(flows, exs, now="2026-10-22T12:00:00+00:00", known=known)
    assert [r["already_recorded"] for r in out2["records"]] == [True, False, False, False]   # 后三笔照样是新亏损


def test_same_contract_round_trips_same_day_merged_fees_are_ambiguous():
    flows, exs = [], []
    for k in range(2):
        f, e = single(f"b{k}", X, "buy", 1, 1.00, f"2026-10-15T15:{2 * k}0:00Z")       # 先后两轮：15:00/15:10、15:20/15:30
        f2, e2 = single(f"s{k}", X, "sell", 1, 0.92, f"2026-10-15T15:{2 * k + 1}0:00Z")
        flows += f + f2
        exs += e + e2
    flows += [fee(X, "buy", -4.0, "2026-10-16T10:00:00Z"), fee(X, "sell", -4.0, "2026-10-16T10:00:00Z")]
    out = run(flows, exs)
    assert len(out["groups"]) == 2 and all(g["state"] == "ambiguous" for g in out["groups"])
    assert not out["can_advance_coverage"] and out["records"] == []


def test_two_underlyings_same_second_not_linked():
    f1, e1 = single("a", X, "buy", 1, 1.00, "2026-10-15T15:00:00Z")
    f2, e2 = single("b", L, "buy", 1, 3.95, "2026-10-15T15:00:00Z")
    out = run(f1 + f2, e1 + e2, positions={f"{X}.US": 1, f"{L}.US": 1})
    assert len(out["groups"]) == 2 and all(len(g["contracts"]) == 1 for g in out["groups"])


def test_other_execution_not_explained_by_synth_cash_flow():
    f1, _ = single("a", X, "buy", 1, 1.00, "2026-10-15T15:00:00Z")
    other = [ex("z", "OTHER.US", "buy", 99, 1.0, "2026-10-15T15:00:01Z")]
    out = run(f1, other, positions={f"{X}.US": 1})
    assert not out["can_advance_coverage"]
    assert any("找不到价×量相符" in b for b in out["blockers"]) and any("OTHER.US" in b for b in out["blockers"])


def test_one_flow_cannot_explain_two_executions():
    f1, e1 = single("a", X, "buy", 1, 1.00, "2026-10-15T15:00:00Z")
    _, e2 = single("b", X, "buy", 1, 1.00, "2026-10-15T15:00:00Z")      # 另一订单同秒同价，但流水只有一行
    out = run(f1, e1 + e2, positions={f"{X}.US": 1})
    assert not out["can_advance_coverage"]


def test_one_flow_aggregates_partial_fills_of_same_order():
    f = [cash(X, "buy", -300.0, "2026-10-15T15:00:01Z")]                 # 3 张 × 1.00 合并成一行流水
    e = [ex("a", f"{X}.US", "buy", 1, 1.00, "2026-10-15T15:00:00Z") for _ in range(3)]
    out = run(f, e, positions={f"{X}.US": 3})
    assert out["can_advance_coverage"] and out["groups"][0]["state"] == "open"


def test_same_underlying_same_second_two_orders_overlapping_is_ambiguous():
    f1, e1 = combo("o1", [(L, "buy", 3.95), (S, "sell", 2.66)], 1, "2026-10-15T14:00:05Z", "buy")
    f2, e2 = combo("o9", [("USO261030C150000", "buy", 7.35), ("USO261030C155000", "sell", 4.50)], 1, "2026-10-15T14:00:05Z", "buy")
    pos = {f"{L}.US": 1, f"{S}.US": -1, "USO261030C150000.US": 1, "USO261030C155000.US": -1}
    out = run(f1 + f2, e1 + e2, positions=pos)
    assert len(out["groups"]) == 2 and all(g["state"] == "ambiguous" for g in out["groups"])   # 可能分腿/分批：不当确定
    assert not out["can_advance_coverage"]


def test_partial_close_held_remaining_is_open():
    f1, e1 = combo("o1", [(L, "buy", 3.95), (S, "sell", 2.66)], 2, "2026-10-15T14:00:05Z", "buy")
    f2, e2 = combo("o2", [(L, "sell", 5.00), (S, "buy", 3.00)], 1, "2026-10-19T15:00:01Z", "sell")
    out = run(f1 + f2, e1 + e2, positions={f"{L}.US": 1, f"{S}.US": -1})
    assert out["groups"][0]["state"] == "open" and out["records"] == [] and out["can_advance_coverage"]


def test_reused_leg_roll_is_ambiguous():
    S2 = "USO261030C165000"
    f1, e1 = combo("o1", [(L, "buy", 3.95), (S, "sell", 2.66)], 1, "2026-10-15T14:00:05Z", "buy")
    f2, e2 = combo("roll", [(S, "buy", 3.00), (S2, "sell", 2.10)], 1, "2026-10-19T15:00:01Z", "buy")
    out = run(f1 + f2, e1 + e2, positions={f"{L}.US": 1, f"{S2}.US": -1})
    assert out["groups"][0]["state"] == "ambiguous" and "换腿" in out["groups"][0]["flags"][0]
    assert not out["can_advance_coverage"]


def test_zero_price_before_expiry_is_unknown():
    f1, e1 = single("a", X, "buy", 1, 1.00, "2026-10-15T15:00:00Z")
    z = [ex("z", f"{X}.US", "sell", 1, 0, "2026-10-20T09:31:00Z")]      # 10/30 到期，10/20 的 0 价
    out = run(f1 + [fee(X, "buy", -2.0, "2026-10-16T10:00:00Z")], e1 + z, now="2026-10-22T12:00:00+00:00")
    assert not out["can_advance_coverage"] and any("到期前的 0 价" in b for b in out["blockers"])


def test_expiry_after_close_closes_position_and_qty_must_match():
    f1, e1 = single("a", X, "buy", 2, 1.00, "2026-10-15T15:00:00Z")
    base = f1 + [fee(X, "buy", -2.0, "2026-10-16T10:00:00Z")]
    good = run(base, e1 + [ex("z", f"{X}.US", "sell", 2, 0, "2026-10-31T09:31:00Z")], now="2026-10-31T12:00:00+00:00")
    assert good["can_advance_coverage"] and good["groups"][0]["net_pnl_usd"] == -202.0 and len(good["expiry_events"]) == 1
    bad = run(base, e1 + [ex("z", f"{X}.US", "sell", 1, 0, "2026-10-31T09:31:00Z")], now="2026-10-31T12:00:00+00:00",
              positions={f"{X}.US": 0})
    assert not bad["can_advance_coverage"] and any("到期处理无法证实" in b for b in bad["blockers"])


def test_orphan_and_positive_fees_block():
    flows, exs = bull_call()
    out = run(flows + [fee(S, "buy", -1.00, "2026-10-21T10:00:00Z")], exs)      # 迟到补费：配不上新成交日
    assert not out["can_advance_coverage"] and any("配不上任何成交日" in b for b in out["blockers"])
    out2 = run(flows + [fee(L, "sell", 0.50, "2026-10-21T10:00:00Z")], exs)
    assert not out2["can_advance_coverage"] and any("正数费用" in b for b in out2["blockers"])


def test_recorded_result_changed_is_conflict_not_skipped():
    flows, exs = bull_call()
    rec = run(flows, exs)["records"][0]
    known = {rec["trade_id"]: {"net_pnl_usd": rec["net_pnl_usd"] + 1.0, "closed_at": rec["closed_at"], "source": tr.SOURCE}}
    out = run(flows, exs, known=known)
    assert not out["can_advance_coverage"] and any("结果变了" in b for b in out["blockers"])
    gone = run(flows, exs, known={"lb2|旧id": {"net_pnl_usd": -1.0, "closed_at": NOW, "source": tr.SOURCE}})
    assert any("找不到" in b for b in gone["conflicts"])


@pytest.mark.parametrize("bad", ["SGD", "nan", "inf"])
def test_non_usd_or_non_finite_amounts_block(bad):
    flows, exs = bull_call()
    r = dict(flows[0])
    if bad == "SGD":
        r["currency"] = "SGD"
    else:
        r["balance"] = bad
    out = run([r] + flows[1:], exs)
    assert not out["can_advance_coverage"] and out["records"] == []


def test_old_rows_contract_from_description_and_read_failures():
    assert tr.contract_of(cash("GLD260701C376000", "sell", 100.0, "2026-06-30T13:43:05Z", old=True)) == "GLD260701C376000"
    assert tr.contract_of({"symbol": "SLV.US", "description": "iShares"}) is None
    flows, exs = bull_call()
    assert not run(flows, exs, reads=None)["can_advance_coverage"]
    assert not run(flows, exs, reads=[{"what": "cash-flow", "start": "a", "end": "b", "n": 3, "ok": False}])["can_advance_coverage"]
    assert not run(flows, exs, reads=[{"what": "executions", "start": "a", "end": "b", "n": 150, "ok": True}])["can_advance_coverage"]


def test_unknown_flow_type_blocks_after_coverage_but_old_issue_is_history():
    flows, exs = bull_call()
    stock = {"balance": "-100.00", "currency": "USD", "description": "x", "flow_name": "Stock Buy Transaction",
             "symbol": "USO.US", "time": "2026-10-19T16:00:00Z"}
    out = run(flows + [stock], exs)
    assert not out["can_advance_coverage"]
    old = run(flows + [stock], exs, cov="2026-10-20T00:00:00+00:00")   # 覆盖时点之后再看：这条早于覆盖、不涉及在场合约
    assert old["history_issues"] and old["can_advance_coverage"]


def test_pre_trial_trades_on_same_contract_block():
    f0, _ = single("pre", L, "buy", 1, 3.0, "2026-10-14T15:00:00Z")
    flows, exs = bull_call()
    out = run(f0 + flows, exs)
    assert not out["can_advance_coverage"] and any("试行到账之前" in b for b in out["blockers"])


def test_missing_times_incomplete():
    out = tr.reconcile(cash_flow=[], positions={}, executions=[], coverage_through="2026-10-15", trial_start=START, now=NOW)
    assert out["status"] == "incomplete" and not out["can_advance_coverage"]


# —— CLI：读取、--apply 锁内重核 ——

@pytest.fixture
def cli(tmp_path, monkeypatch):
    from undertow import trial_cli
    from undertow.collect import longbridge_account as lb
    f = tmp_path / "state.json"
    monkeypatch.setattr(ts, "load", functools.partial(ts.load, path=f))
    monkeypatch.setattr(ts, "update", functools.partial(ts.update, path=f))
    ts.update(lambda cur: tc.record_funding(tc.new_state(trial_id="t", planned_capital=500.0, policy_version=rp.VERSION_B,
                                                         created_at="2026-10-15T13:00:00+00:00"),
                                            amount=500.0, at=START), create=True)
    flows, exs = bull_call()
    monkeypatch.setattr(lb, "fetch_cash_flow", lambda start=None, end=None: flows if start == "2026-10-14" else [])
    monkeypatch.setattr(lb, "fetch_positions", lambda: [])
    monkeypatch.setattr(lb, "fetch_executions", lambda start=None, end=None: exs if start == "2026-10-14" else [])
    monkeypatch.setattr(lb, "fetch_today_executions", lambda: [])
    monkeypatch.setattr(trial_cli, "_now", lambda: NOW)
    return trial_cli


def A(apply=True, note="用户：对账登记"):
    return types.SimpleNamespace(apply=apply, note=note)


def test_cli_dry_run_writes_nothing(cli, capsys):
    assert cli.cmd_reconcile(A(apply=False, note="")) == 0
    st, _ = ts.load()
    assert st["closed_trades"] == {} and st["trade_coverage"]["through"] == START
    assert "可登记 1 笔" in capsys.readouterr().out


def test_cli_apply_records_and_advances_idempotently(cli):
    assert cli.cmd_reconcile(A()) == 0
    st, _ = ts.load()
    (tid, row), = st["closed_trades"].items()
    assert row["source"] == tr.SOURCE and st["trade_coverage"]["through"] == NOW
    assert cli.cmd_reconcile(A()) == 0
    assert len(ts.load()[0]["closed_trades"]) == 1


def test_cli_concurrent_manual_registration_inside_window_is_refused(cli, monkeypatch):
    orig = ts.update
    calls = {"n": 0}

    def racing_update(fn, *a, **k):
        if calls["n"] == 0:                                    # 锁外算完提案之后、写入之前，另一进程人工登记了同一笔
            calls["n"] += 1
            orig(lambda cur: tc.record_trade(cur, trade_id="manual-1", net_pnl_usd=91.5,
                                             closed_at="2026-10-19T15:00:01+00:00", complete=True, source="人工登记", note="x"))
        return orig(fn, *a, **k)
    monkeypatch.setattr(ts, "update", racing_update)
    assert cli.cmd_reconcile(A()) == 1
    st, _ = ts.load()
    assert list(st["closed_trades"]) == ["manual-1"] and st["trade_coverage"]["through"] == START


def test_cli_manual_record_already_present_refuses(cli):
    ts.update(lambda cur: tc.record_trade(cur, trade_id="manual-1", net_pnl_usd=91.5, closed_at="2026-10-19T15:00:01+00:00",
                                          complete=True, source="人工登记", note="x"))
    assert cli.cmd_reconcile(A()) == 1
    st, _ = ts.load()
    assert list(st["closed_trades"]) == ["manual-1"] and st["trade_coverage"]["through"] == START


def test_cli_blockers_refuse_apply(cli, monkeypatch):
    from undertow.collect import longbridge_account as lb
    monkeypatch.setattr(lb, "fetch_positions", lambda: [types.SimpleNamespace(symbol=f"{L}.US", quantity=1.0)])
    assert cli.cmd_reconcile(A()) == 1
    st, _ = ts.load()
    assert st["closed_trades"] == {} and st["trade_coverage"]["through"] == START


def test_cli_apply_needs_note(cli):
    assert cli.cmd_reconcile(A(note=" ")) == 1


def test_cash_flow_chunks_do_not_overlap_and_keep_identical_rows():
    from undertow.trial_cli import _cash_flow_since
    calls, reads = [], []
    same = cash(X, "buy", -100.0, "2026-10-15T14:00:05Z")

    class FakeLB:
        @staticmethod
        def fetch_cash_flow(start=None, end=None):
            calls.append((start, end))
            return [dict(same), dict(same)] if start == "2026-10-14" else []
    rows = _cash_flow_since(FakeLB, START, "2026-11-30T00:00:00+00:00", chunk_days=10, reads=reads)
    assert len(rows) == 2 and len(reads) == len(calls)
    assert calls[0][0] == "2026-10-14" and calls[-1][1] == "2026-12-01"
    for (a0, b0), (a1, _) in zip(calls, calls[1:]):
        assert a1 > b0


def test_today_executions_overlap_handling():
    from undertow.trial_cli import _executions_since
    r1 = ex("a", f"{X}.US", "buy", 1, 1.0, "2026-10-20T15:00:00Z")
    r2 = ex("b", f"{X}.US", "buy", 1, 1.0, "2026-10-21T11:00:00Z")

    def fake(hist, today):
        return types.SimpleNamespace(fetch_executions=lambda start=None, end=None: hist if start == "2026-10-14" else [],
                                     fetch_today_executions=lambda: today)
    reads = []
    assert _executions_since(fake([r1], [r2]), START, NOW, reads=reads) == [r1, r2] and reads[-1]["ok"]
    reads = []
    assert _executions_since(fake([r1, r2], [r2]), START, NOW, reads=reads) == [r1, r2] and reads[-1]["ok"]   # 整体重叠：不重复计
    reads = []
    _executions_since(fake([r1, r2], [r2, r1, dict(r1, order_id="c")]), START, NOW, reads=reads)
    assert reads[-1]["ok"] is False                                                                   # 部分重叠：读数不完整
