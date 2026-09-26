"""Codex 008 G01：账户数据契约 —— 合法空仓、坏 schema、坏数量、零净资产、NaN/Inf 必须分开。"""
import math

import pytest

from undertow.collect import longbridge_account as lb


def _stub(monkeypatch, payload):
    monkeypatch.setattr(lb, "_run", lambda args, timeout=30.0: payload)


GOOD = {"symbol": "SLV260821P57000.US", "name": "SLV P57", "quantity": "-1", "cost_price": "0.31",
        "currency": "USD", "market": "US"}


@pytest.mark.parametrize("payload", [[], {"account_type": "margin", "stock_list": [], "option_list": []}])
def test_legal_empty_is_empty(monkeypatch, payload):
    _stub(monkeypatch, payload)
    assert lb.fetch_positions() == []


@pytest.mark.parametrize("payload,frag", [
    ({"error": "rate limited"}, "未知响应形态"),
    ("oops", "响应类型"),
    ([GOOD, "x"], "非对象行"),
    ({"account_type": "m", "option_list": {"a": 1}}, "不是列表"),
    ([dict(GOOD, quantity="abc")], "不是数值"),
    ([dict(GOOD, quantity=None)], "缺失"),
    ([dict(GOOD, quantity="nan")], "非有限数"),
    ([dict(GOOD, cost_price="inf")], "非有限数"),
    ([dict(GOOD, symbol="")], "symbol 缺失"),
])
def test_bad_positions_raise_not_empty(monkeypatch, payload, frag):
    _stub(monkeypatch, payload)
    with pytest.raises(lb.AccountDataError, match=frag):
        lb.fetch_positions()


def test_account_data_error_is_caught_by_existing_degrade_paths():
    assert issubclass(lb.AccountDataError, lb.LongbridgeUnavailable)


def test_zero_quantity_row_is_a_closed_position(monkeypatch):
    _stub(monkeypatch, [dict(GOOD, quantity="0"), GOOD])
    ps = lb.fetch_positions()
    assert len(ps) == 1 and ps[0].quantity == -1 and ps[0].cost_price == pytest.approx(0.31)


def test_zero_net_assets_is_not_replaced(monkeypatch):
    _stub(monkeypatch, [{"net_assets": "0", "total_assets": "1000", "buy_power": "0",
                         "cash_infos": [{"currency": "USD", "available_cash": "0"}]}])
    a = lb.fetch_assets()
    assert a.net_assets == 0.0 and a.buy_power == 0.0 and a.cash_by_ccy == {"USD": 0.0}


@pytest.mark.parametrize("row,frag", [
    ({"total_assets": "1000", "buy_power": "5"}, "net_assets 缺失"),
    ({"net_assets": "NaN", "buy_power": "5"}, "非有限数"),
    ({"net_assets": "1", "buy_power": "x"}, "不是数值"),
    ({"net_assets": "1", "buy_power": "1", "cash_infos": [{"available_cash": "1"}]}, "无币种"),
])
def test_bad_assets_raise(monkeypatch, row, frag):
    _stub(monkeypatch, [row])
    with pytest.raises(lb.AccountDataError, match=frag):
        lb.fetch_assets()


def test_empty_assets_response_raises(monkeypatch):
    _stub(monkeypatch, [])
    with pytest.raises(lb.AccountDataError):
        lb.fetch_assets()


def test_raw_flow_non_list_raises(monkeypatch):
    _stub(monkeypatch, {"error": "x"})
    with pytest.raises(lb.AccountDataError):
        lb.fetch_cash_flow()


def test_account_command_reports_failure_not_empty(monkeypatch, capsys):
    """端到端：坏 schema 时 account 命令报「不可用」并返回非 0，不说「账户当前无持仓」。"""
    import argparse
    from undertow import cli
    _stub(monkeypatch, {"error": "bad"})
    rc = cli.cmd_account(argparse.Namespace(no_cache=True))
    out = capsys.readouterr()
    assert rc == 2 and "无持仓" not in out.out and "未知响应形态" in out.err


def test_assets_failure_becomes_visible_health_finding(monkeypatch):
    from undertow import cli

    def run(args, timeout=30.0):
        if args == ["positions"]:
            return [GOOD]
        raise lb.AccountDataError("assets：字段 net_assets 缺失")
    monkeypatch.setattr(lb, "_run", run)
    monkeypatch.setattr(cli, "_fetch_live_quotes", lambda ps: {})
    from datetime import date
    monkeypatch.setattr(cli, "_build_contexts", lambda ps, nc, live_quotes=None: ({}, date(2026, 9, 28)))   # 不联网
    b = cli._load_account_review(no_cache=True)
    assert b["assets"] is None and "net_assets" in b["assets_error"]
    assert b["health"][0].code == "capital_unknown" and b["health"][0].severity == "高"
