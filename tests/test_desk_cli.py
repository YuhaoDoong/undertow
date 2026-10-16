"""desk_cli（spread-quote / sides）与 quote 面板：假长桥/假 Yahoo，只验取数→计算→排版接线与降级。"""
import argparse
from datetime import date, datetime

import pytest

from undertow import desk_cli as dc
from undertow.collect import longbridge_quote as lq
from undertow.core.models import OptionContract


def _contracts():
    mk = lambda e, k, kind, b, a, dl: OptionContract(expiry=e, strike=k, kind=kind, open_interest=1, volume=1,
                                                      gamma=0.0, delta=dl, iv=0.4, bid=b, ask=a, bid_size=5, ask_size=5)
    e30, e21 = date(2026, 10, 29), date(2026, 10, 21)
    return [mk(e30, 160, "C", 3.25, 3.50, 0.35), mk(e30, 165, "C", 2.09, 2.24, 0.25),
            mk(e21, 148, "P", 1.27, 1.38, -0.25), mk(e21, 147, "P", 1.05, 1.13, -0.21)]


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(dc, "_et_now", lambda: datetime(2026, 10, 15, 10, 0, tzinfo=dc.ET))
    monkeypatch.setattr(dc, "latest_chain", lambda key, root: (date(2026, 10, 15), _contracts()))
    monkeypatch.setattr(dc, "etf_price", lambda root: (154.61, "常规"))
    monkeypatch.setattr(dc, "fetch_commodity", lambda s: {"CL=F": (94.62, "t"), "BZ=F": None}.get(s))
    monkeypatch.setattr(dc, "_budget_context", lambda: ({"equity": 500.00, "asof": "2026-10-15T10:00:00-04:00"},
                                                       [], {"latched": False}, ["⚠️ 负现金：USD -50.00"]))
    asked = []

    def depth(syms):
        asked.extend(syms)
        book = {"USO261029C160000.US": (3.25, 3.50), "USO261029C165000.US": (2.09, 2.24)}
        return {s: lq.Depth(symbol=s, bid=book.get(s, (None, None))[0], bid_size=5,
                            ask=book.get(s, (None, None))[1], ask_size=5) for s in syms}
    monkeypatch.setattr(lq, "fetch_depth", depth)
    return asked


def _sq(**kw):
    base = dict(underlying="USO", expiry="2026-10-29", kind="C", strikes="160/165", credit=False, qty=1,
                snapshot=False, no_account=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_spread_quote_live_depth(fakes, capsys):
    assert dc.cmd_spread_quote(_sq()) == 0
    out = capsys.readouterr().out
    assert fakes == ["USO261029C160000.US", "USO261029C165000.US"]   # 长桥格式：行权价×1000 不补零
    assert "逐腿对手价 付 1.41" in out and "中价 付 1.21" in out and "不保证整单立即成交" in out
    assert "政策 B 预算（请求 1 组）：pass" in out
    assert "政策 B 下最高可付（1 组）：付 1.46" in out
    assert "布伦特" not in out or "BZ=F unknown" in out                # 取不到的商品价标 unknown，不阻断
    assert "先平卖腿" in out and "负现金" in out


def test_spread_quote_missing_leg_not_zero(fakes, monkeypatch, capsys):
    monkeypatch.setattr(lq, "fetch_depth", lambda syms: {s: lq.Depth(symbol=s, bid=None, bid_size=0, ask=None,
                                                                     ask_size=0, error="") for s in syms})
    assert dc.cmd_spread_quote(_sq()) == 0
    out = capsys.readouterr().out
    assert "不可计算" in out and "缺盘口" in out and "$0.00" not in out


def test_spread_quote_all_depth_fail_returns_error(fakes, monkeypatch, capsys):
    def boom(syms):
        raise lq.LiveQuotesUnavailable("全部失败")
    monkeypatch.setattr(lq, "fetch_depth", boom)
    assert dc.cmd_spread_quote(_sq()) == 1


def test_spread_quote_snapshot_and_no_account(fakes, capsys):
    assert dc.cmd_spread_quote(_sq(snapshot=True, no_account=True)) == 0
    out = capsys.readouterr().out
    assert "CBOE 快照" in out and "政策 B" not in out


def test_sides_compares_same_horizon(fakes, capsys):
    a = argparse.Namespace(underlying="USO", horizon="2026-10-29", buy="160/165C", sell="148/147P", round_dte=7,
                           equity=500.00, live=False)
    assert dc.cmd_sides(a) == 0
    out = capsys.readouterr().out
    assert "买方" in out and "卖方" in out and "2026-10-21" in out
    assert "约 2 轮" in out                                       # 6 天一轮、15 天 → 2 轮
    assert "输一轮抵" in out and "价内概率粗略代理" in out
    assert "不是期望、不是回测" in out and "不按当前报价优化" in out


def test_quote_panel_sessions_commodity_levels(monkeypatch, capsys):
    from undertow import cli
    q = lq.StockQuote(symbol="USO.US", last=154.61, prev_close=153.56, freshest=154.61, freshest_kind="常规",
                      freshest_prev=153.56, sessions=(("常规", 154.61, "", 153.56),
                                                      ("盘前", 153.92, "2026-10-15T13:05:02Z", 153.56)))
    monkeypatch.setattr(lq, "fetch_stock_quotes", lambda syms: {"USO.US": q})
    monkeypatch.setattr(dc, "fetch_commodity", lambda s: {"CL=F": (94.62, "t"), "BZ=F": (105.09, "t")}.get(s))

    class A:
        symbols = ["wti"]; sessions = True; commodity = True; level = ["brent=101.50", "USO=150"]
    assert cli.cmd_quote(A()) == 0
    out = capsys.readouterr().out
    assert "盘前" in out and "10-15 09:05 ET" in out
    assert "BZ=F" in out and f"{105.09 / 154.61:.4f}" in out
    assert f"USO {101.50 / (105.09 / 154.61):,.2f}" in out
    assert f"BZ=F {150 * 105.09 / 154.61:,.2f}" in out
    assert "不是每份 ETF 的持有量" in out
    assert "参考比值" in out and "实时比值" not in out and "两腿是否同一时刻未核实" in out   # Codex 054 R1：同步未核实不叫实时


def test_quote_without_new_flags_unchanged(monkeypatch, capsys):
    """旧用法（无新参数的命名空间）输出不变、不去取商品价。"""
    from undertow import cli
    called = []
    monkeypatch.setattr(dc, "fetch_commodity", lambda s: called.append(s))
    monkeypatch.setattr(lq, "fetch_stock_quotes", lambda syms: {s: lq.StockQuote(
        symbol=s, last=1.0, prev_close=1.0, freshest=1.0, freshest_kind="常规", freshest_prev=1.0) for s in syms})

    class A:
        symbols = ["SLV"]
    assert cli.cmd_quote(A()) == 0
    assert called == [] and "各时段" not in capsys.readouterr().out


def test_account_cash_lines_flags_negative():
    from undertow.report.markdown import account_cash_lines
    from undertow.collect.longbridge_account import AccountAssets
    lines = account_cash_lines(AccountAssets(buy_power=300.00, net_assets=500.00,
                                             cash_by_ccy={"USD": -50.00, "EUR": 200.00, "HKD": 0.0}))
    assert "USD -50.00" in lines[0] and "EUR 200.00" in lines[0]
    assert len(lines) == 2 and "负现金" in lines[1] and "EUR" not in lines[1]
    assert len(account_cash_lines(AccountAssets(buy_power=1, net_assets=1, cash_by_ccy={"USD": 5.0}))) == 1


def test_sessions_parsed_from_raw_row():
    r = {"symbol": "SLV.US", "last": "50.22", "prev_close": "53.49", "timestamp": "",
         "pre_market": {"last": "51.08", "prev_close": "50.22", "timestamp": "2026-10-15T13:05:02Z"},
         "overnight": {"last": "0", "prev_close": "50.22", "timestamp": ""}}
    s = lq._sessions(r)
    assert [x[0] for x in s] == ["常规", "盘前"]                    # last ≤ 0 的时段略去


# ───── Codex 052 I3：多组定价、盘口合同、处置文案（合成 R=500，不是账户读数） ─────

def test_spread_quote_two_groups_budget_and_bound(fakes, monkeypatch, capsys):
    """两组：每组与整笔分开显示；预算按每组金额 × 2 组只乘一次；价格边界按 2 组扫描（旧版恒「不可计算」）。"""
    monkeypatch.setattr(dc, "_budget_context", lambda: ({"equity": 500.0, "asof": "2026-01-02T10:00:00-05:00"},
                                                       [], {"latched": False}, []))
    assert dc.cmd_spread_quote(_sq(qty=2)) == 0
    out = capsys.readouterr().out
    assert "每组含费最大亏损 $144.20" in out and "2 组整笔最大亏损 $288.40" in out
    assert "政策 B 预算（请求 2 组）：pass（可开 1 组）" in out          # 288.40 > 150 → 只够 1 组
    # 2 组 × (100x + 3.20) ≤ 150 → x ≤ 0.718 → 最高可付 0.71
    assert "政策 B 下最高可付（2 组）：付 0.71" in out


def test_spread_quote_crossed_depth_gives_no_price(fakes, monkeypatch, capsys):
    monkeypatch.setattr(lq, "fetch_depth", lambda syms: {s: lq.Depth(symbol=s, bid=3.6, bid_size=5, ask=3.5, ask_size=5)
                                                         for s in syms})
    assert dc.cmd_spread_quote(_sq(no_account=True)) == 0
    out = capsys.readouterr().out
    assert "盘口不可信" in out and "交叉" in out
    assert "起挂" not in out and "含费最大亏损" not in out


def test_spread_quote_leg_error_is_problem_not_price(fakes, monkeypatch, capsys):
    def depth(syms):
        return {s: lq.Depth(symbol=s, bid=None, bid_size=0, ask=None, ask_size=0, error="超时") if s.endswith("165000.US")
                else lq.Depth(symbol=s, bid=3.25, bid_size=5, ask=3.5, ask_size=5) for s in syms}
    monkeypatch.setattr(lq, "fetch_depth", depth)
    assert dc.cmd_spread_quote(_sq(no_account=True)) == 0
    out = capsys.readouterr().out
    assert "盘口不可信" in out and "超时" in out and "含费最大亏损" not in out


def test_spread_quote_snapshot_marked_stale_no_limit(fakes, capsys):
    assert dc.cmd_spread_quote(_sq(snapshot=True, no_account=True)) == 0
    out = capsys.readouterr().out
    assert "已陈旧" in out and "起挂" not in out and "每组含费最大亏损" in out
    assert "提前指派" in out and "不会被强平" not in out


def test_spread_quote_qty_must_be_positive():
    import argparse as ap
    parser = ap.ArgumentParser()
    dc.register(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["spread-quote", "USO", "2026-10-29", "C", "160/165", "--qty", "0"])
    assert parser.parse_args(["spread-quote", "USO", "2026-10-29", "C", "160/165", "--qty", "3"]).qty == 3


def test_sides_no_fee_profit_reports_no_probability(fakes, monkeypatch, capsys):
    """合成：卖腿 bid 只比买腿 ask 高 0.01 → 每轮含费最大收益为负：不报打平胜率、不给 delta 区间、不报「N 轮全赢」。"""
    e21 = date(2026, 10, 21)
    mk = lambda k, b, a, dl: OptionContract(expiry=e21, strike=k, kind="P", open_interest=1, volume=1, gamma=0.0,
                                            delta=dl, iv=0.4, bid=b, ask=a, bid_size=5, ask_size=5)
    cs = [c for c in _contracts() if c.expiry != e21] + [mk(148, 1.02, 1.02, -0.25), mk(147, 1.01, 1.01, -0.21)]
    monkeypatch.setattr(dc, "latest_chain", lambda key, root: (date(2026, 10, 15), cs))
    a = argparse.Namespace(underlying="USO", horizon="2026-10-29", buy=None, sell="148/147P", round_dte=7,
                           equity=500.0, live=False)
    assert dc.cmd_sides(a) == 0
    out = capsys.readouterr().out
    assert "−$2.20" in out and "含费无盈利可能" in out and "+$-" not in out
    assert "全赢 +" not in out and "不给概率代理" in out and "≈ 75%" not in out
