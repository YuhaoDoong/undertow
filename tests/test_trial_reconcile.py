"""券商资金流水自动对账（trial-reconcile-v1）。全部合成输入，行形态照 2026-10-15 实测的长桥 cash-flow / executions。"""
import functools
import types

import pytest

from undertow.analyze import risk_policy as rp
from undertow.analyze import trial_capital as tc
from undertow.analyze import trial_reconcile as tr
from undertow.collect import trial_store as ts

START = "2026-10-15T14:00:00+00:00"
NOW = "2026-10-21T12:00:00+00:00"


def txn(code, side, amt, t, old=False):
    name = "Option Buy Transaction" if side == "buy" else "Option Sell Transaction"
    if old:                                                    # 6–7 月旧行：symbol 空，合约只在 description
        return {"balance": f"{amt:.2f}", "business_type": "Unknown", "currency": "USD",
                "description": f"OP/US/{code}", "flow_name": name, "symbol": "", "time": t}
    return {"balance": f"{amt:.2f}", "business_type": "Unknown", "currency": "USD", "description": code,
            "flow_name": name, "symbol": f"{code}.US", "time": t}


def fee(code, side, amt, t):
    name = "Option Buy Fee" if side == "buy" else "Option Sell Fee"
    return {"balance": f"{amt:.2f}", "business_type": "Unknown", "currency": "USD", "description": code,
            "flow_name": name, "symbol": f"{code}.US", "time": t}


L, S = "USO261030C158000", "USO261030C162000"


def bull_call(close=True, fees_close=True):
    """10/15 开 158/162C 借记 1.29（买 158 −395、卖 162 +266），10/19 平（卖 158 +500、买 162 −300）。"""
    rows = [txn(L, "buy", -395.0, "2026-10-15T14:00:05Z"), txn(S, "sell", 266.0, "2026-10-15T14:00:05Z"),
            fee(L, "buy", -2.41, "2026-10-16T10:30:00Z"), fee(S, "sell", -2.43, "2026-10-16T10:30:00Z")]
    if close:
        rows += [txn(L, "sell", 500.0, "2026-10-19T15:00:00Z"), txn(S, "buy", -300.0, "2026-10-19T15:00:00Z")]
        if fees_close:
            rows += [fee(L, "sell", -2.43, "2026-10-20T10:30:00Z"), fee(S, "buy", -2.41, "2026-10-20T10:30:00Z")]
    return rows


def ex(t, sym="USO.US", price="1.29"):
    return {"order_id": "1", "price": price, "quantity": "1", "side": "Buy", "symbol": sym, "time": t}


def run(rows, positions=(), executions=(), cov=START, now=NOW, known=None):
    return tr.reconcile(cash_flow=rows, positions=list(positions), executions=list(executions), coverage_through=cov,
                        trial_start=START, now=now, known_trade_ids=known)


def test_combo_round_trip_records_net_with_fees_and_advances():
    out = run(bull_call(), executions=[ex("2026-10-15T14:00:04Z"), ex("2026-10-19T15:00:01Z")])  # 成交与流水差 1 秒
    (g,) = out["groups"]
    assert g["state"] == "closed" and g["fees_settled"] and g["contracts"] == [L, S]
    assert g["net_pnl_usd"] == round(-395 + 266 + 500 - 300 - 2.41 - 2.43 - 2.43 - 2.41, 2)
    assert g["closed_at"] == "2026-10-19T15:00:00+00:00"
    assert out["can_advance_coverage"] and len(out["records"]) == 1 and out["records"][0]["complete"]


def test_close_fees_not_posted_yet_blocks():
    out = run(bull_call(fees_close=False))
    assert out["groups"][0]["state"] == "closed" and not out["groups"][0]["fees_settled"]
    assert out["records"] == [] and not out["can_advance_coverage"]


def test_still_held_is_open_and_not_recorded_but_coverage_can_advance():
    out = run(bull_call(close=False), positions=[f"{L}.US", f"{S}.US"])
    assert out["groups"][0]["state"] == "open" and out["records"] == [] and out["can_advance_coverage"]


def test_partial_close_still_held_is_open():
    rows = bull_call(close=False) + [txn(L, "sell", 250.0, "2026-10-19T15:00:00Z"),
                                     txn(S, "buy", -150.0, "2026-10-19T15:00:00Z")]
    out = run(rows, positions=[f"{L}.US", f"{S}.US"])            # 平了一半：仍在持仓 → 不计结果
    assert out["groups"][0]["state"] == "open" and out["records"] == []


def test_one_sided_not_expired_not_held_is_ambiguous():
    out = run(bull_call(close=False), positions=[])
    g = out["groups"][0]
    assert g["state"] == "ambiguous" and "未到期却不在持仓" in g["flags"][0]
    assert not out["can_advance_coverage"] and out["records"] == []


def test_expired_worthless_legs_close_at_expiry_and_zero_price_execution_is_expiry_event():
    now = "2026-10-31T12:00:00+00:00"                           # 10/30 到期之后
    out = run(bull_call(close=False), positions=[], now=now,
              executions=[ex("2026-10-31T09:31:28Z", sym=f"{L}.US", price="0"),
                          ex("2026-10-31T09:31:30Z", sym=f"{S}.US", price="0")])
    g = out["groups"][0]
    assert g["state"] == "closed" and sorted(g["expired_legs"]) == [L, S]
    assert g["closed_at"] == "2026-10-30T20:00:00+00:00"           # 16:00 美东（夏令时）
    assert g["net_pnl_usd"] == round(-395 + 266 - 2.41 - 2.43, 2)
    assert len(out["expiry_events"]) == 2 and out["can_advance_coverage"]


def test_legged_entry_same_underlying_expiry_overlapping_is_ambiguous():
    rows = [txn(L, "buy", -395.0, "2026-10-15T14:00:05Z"), txn(S, "sell", 266.0, "2026-10-15T14:30:00Z"),
            txn(L, "sell", 500.0, "2026-10-19T15:00:00Z"), txn(S, "buy", -300.0, "2026-10-19T15:10:00Z")]
    rows += [fee(c, sd, -2.4, "2026-10-20T10:30:00Z") for c in (L, S) for sd in ("buy", "sell")]
    out = run(rows)
    assert [g["state"] for g in out["groups"]] == ["ambiguous", "ambiguous"]
    assert not out["can_advance_coverage"] and out["records"] == []


def test_old_rows_contract_from_description():
    r = txn("GLD260701C376000", "sell", 100.0, "2026-06-30T13:43:05Z", old=True)
    assert tr.contract_of(r) == "GLD260701C376000"
    assert tr.contract_of({"symbol": "SLV.US", "description": "iShares"}) is None


def test_unknown_flow_type_blocks():
    rows = bull_call() + [{"balance": "-100.00", "currency": "USD", "description": "x", "flow_name": "Stock Buy Transaction",
                           "symbol": "USO.US", "time": "2026-10-19T16:00:00Z"}]
    out = run(rows)
    assert not out["can_advance_coverage"] and out["unknown"]
    assert out["records"]                                          # 提案仍列出，但 --apply 不会推进覆盖


def test_execution_without_cash_flow_blocks():
    out = run(bull_call(), executions=[ex("2026-10-20T15:00:00Z")])
    assert not out["can_advance_coverage"] and "找不到" in out["blockers"][0]


def test_executions_before_coverage_ignored_and_external_listed():
    rows = bull_call() + [{"balance": "650.00", "currency": "SGD", "description": "", "flow_name": "Cash Deposit",
                           "symbol": "", "time": "2026-10-16T01:00:00Z"}]
    out = run(rows, executions=[ex("2026-10-10T15:00:00Z")])
    assert out["can_advance_coverage"] and out["external"][0]["amount"] == 650.0


def test_pre_trial_txn_flags_group():
    rows = [txn(L, "buy", -395.0, "2026-10-14T14:00:05Z"), txn(L, "sell", 400.0, "2026-10-16T14:00:05Z"),
            fee(L, "buy", -2.4, "2026-10-15T10:00:00Z"), fee(L, "sell", -2.4, "2026-10-17T10:00:00Z")]
    out = run(rows)
    assert out["groups"][0]["state"] == "ambiguous" and not out["can_advance_coverage"]


def test_known_trade_marked_already_recorded():
    tid = run(bull_call())["records"][0]["trade_id"]
    out = run(bull_call(), known={tid})
    assert out["records"][0]["already_recorded"] is True


def test_missing_times_incomplete():
    out = tr.reconcile(cash_flow=[], positions=[], executions=[], coverage_through="2026-10-15", trial_start=START, now=NOW)
    assert out["status"] == "incomplete" and not out["can_advance_coverage"]


# —— CLI：--apply 登记 + 推进；不带 --apply 不写；人工登记在覆盖之后 → 拒绝自动登记（防重复计数）——

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
    monkeypatch.setattr(lb, "fetch_cash_flow", lambda start=None, end=None: bull_call())
    monkeypatch.setattr(lb, "fetch_positions", lambda: [])
    monkeypatch.setattr(lb, "fetch_executions", lambda start=None, end=None: [ex("2026-10-19T15:00:01Z")])
    monkeypatch.setattr(lb, "fetch_today_executions", lambda: [])
    monkeypatch.setattr(trial_cli, "_now", lambda: NOW)
    return trial_cli


def test_cli_dry_run_writes_nothing(cli, capsys):
    rc = cli.cmd_reconcile(types.SimpleNamespace(apply=False, note=""))
    st, _ = ts.load()
    assert rc == 0 and st["closed_trades"] == {} and st["trade_coverage"]["through"] == START
    assert "可登记 1 笔" in capsys.readouterr().out


def test_cli_apply_records_and_advances_idempotently(cli):
    a = types.SimpleNamespace(apply=True, note="用户：对账登记")
    assert cli.cmd_reconcile(a) == 0
    st, _ = ts.load()
    (tid, row), = st["closed_trades"].items()
    assert row["source"] == tr.SOURCE and row["complete"] and st["trade_coverage"]["through"] == NOW
    assert cli.cmd_reconcile(a) == 0                               # 第二次：覆盖时点已在 NOW，不重复登记
    st2, _ = ts.load()
    assert len(st2["closed_trades"]) == 1


def test_cli_refuses_when_manual_record_after_coverage(cli):
    ts.update(lambda cur: tc.record_trade(cur, trade_id="manual-1", net_pnl_usd=91.5, closed_at="2026-10-19T15:00:00+00:00",
                                          complete=True, source="人工登记", note="x"))
    assert cli.cmd_reconcile(types.SimpleNamespace(apply=True, note="用户")) == 1
    st, _ = ts.load()
    assert list(st["closed_trades"]) == ["manual-1"] and st["trade_coverage"]["through"] == START


def test_cli_apply_needs_note(cli):
    assert cli.cmd_reconcile(types.SimpleNamespace(apply=True, note=" ")) == 1


def test_cash_flow_chunks_do_not_overlap_and_keep_identical_rows():
    from undertow.trial_cli import _cash_flow_since
    calls = []
    same = txn(L, "buy", -100.0, "2026-10-15T14:00:05Z")

    class FakeLB:
        @staticmethod
        def fetch_cash_flow(start=None, end=None):
            calls.append((start, end))
            return [dict(same), dict(same)] if start == "2026-10-14" else []   # 同一秒同金额两笔真实成交
    rows = _cash_flow_since(FakeLB, START, "2026-11-30T00:00:00+00:00", chunk_days=10)
    assert len(rows) == 2                                          # 不去重
    assert calls[0][0] == "2026-10-14" and calls[-1][1] == "2026-12-01"
    for (a0, b0), (a1, _) in zip(calls, calls[1:]):
        assert a1 > b0                                             # 段与段不重叠（end 含当天）
