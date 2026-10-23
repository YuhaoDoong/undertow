"""Codex 064 T 补修：借记提示的版本分派、部分减仓降级、越界报价、tick 文案。全部合成数据。"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from undertow import riskwatch_cli as rc
from undertow.analyze import debit_advisory as da
from undertow.analyze import risk_watch as rw
from undertow.analyze import spread_econ as se

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 23, 11, 0, tzinfo=ET)
V1, V2 = "debit-roi100-review-v1", "debit-roi100-review-v2-20261023"


def _paper(**kw):
    p = dict(side="C", k_sell=105.0, k_buy=100.0, entry_credit=-1.2, qty=1, fee_round_trip=3.2, structure="debit",
             advisory_rule=V1, underlying="QQQ.US", expiry="2026-11-20", state="entered",
             sell="QQQ261120C105000.US", buy="QQQ261120C100000.US")
    p.update(kw)
    return p


def _patrol(tmp_path, p, now=NOW, buy_bid=2.66):
    """从统一巡检入口注入临时模拟账（同 064 探针的注入方式）：可平仓净价 = 买腿 bid − 卖腿 ask。"""
    from scripts import paper_trades as pt
    journal = Path(tmp_path) / "journal.json"
    journal.write_text(json.dumps({"theses": [{"id": "synthetic", "execution": "模拟", "paper": p}]}))
    quotes = {p["sell"]: {"ask": 0.2, "bid": 0.1, "ask_size": 10, "bid_size": 10},
              p["buy"]: {"bid": buy_bid, "ask": buy_bid + 0.1, "ask_size": 10, "bid_size": 10}}
    deps = dict(positions=lambda: [], assets=lambda: SimpleNamespace(cash_by_ccy={"USD": 0.0}),
                quotes=lambda syms: quotes, spots=lambda roots: {"QQQ": 102.0}, ref_prices=lambda plans, spots: {},
                trial=lambda: {"state": "ok", "paused": False, "consecutive_unknown": False},
                book=lambda: {"overall": "pass", "failed": []},
                paper=lambda: SimpleNamespace(JOURNAL=journal, close_value=pt.close_value),
                plans=lambda: ({}, None), state=rw.new_state("2026-10-23"))
    res = rc.collect_and_evaluate(now, deps=deps)
    tp = [i for i in res["items"] if i.key.startswith("take_profit|paper|")]
    cov = [c for c in res["coverage"] if c["key"] == "paper|synthetic"][0]["rules"]["take_profit"]
    return tp, cov, res


# —— T1：统一巡检尊重模拟仓记录挂的规则与生效时刻 ——
def test_patrol_v1_record_stays_v1(tmp_path):
    """挂 v1、付 1.20、宽 5、往返费 3.20、可平仓 2.46：v1 不到线（2.464），旧巡检却按 v2 提醒。"""
    tp, cov, _ = _patrol(tmp_path, _paper())
    assert tp == [] and cov["status"] == "checked"


def test_patrol_v2_before_and_after_effective(tmp_path):
    p = _paper(advisory_rule=V2, advisory_effective_at="2026-10-26T11:00:00-04:00")
    tp, cov, _ = _patrol(tmp_path, p)
    assert tp == [] and cov["status"] == "not_applicable" and "尚未生效" in cov["why"]
    tp, _, _ = _patrol(tmp_path, p, now=datetime(2026, 10, 26, 11, 0, tzinfo=ET))
    assert len(tp) == 1 and tp[0].numbers["paper_advisory"]["rule"] == V2
    assert tp[0].numbers["paper_advisory"]["pnl_usd"] == 122.8 and tp[0].numbers["paper_advisory"]["risk_cost_usd"] == 121.6


def test_patrol_v1_triggers_on_v1_boundary(tmp_path):
    tp, _, _ = _patrol(tmp_path, _paper(), buy_bid=2.70)            # 可平仓 2.50 > 2.464
    assert len(tp) == 1 and "含费风险成本" in tp[0].title and V1 in tp[0].title


def test_patrol_no_rule_and_bad_rule(tmp_path):
    tp, cov, _ = _patrol(tmp_path, _paper(advisory_rule=None), buy_bid=3.0)
    assert tp == [] and cov["status"] == "not_applicable"
    tp, cov, _ = _patrol(tmp_path, _paper(advisory_rule="no-such-rule"), buy_bid=3.0)
    assert tp == [] and cov["status"] == "missing" and cov["input"] == "advisory_rule"


def test_paper_entry_and_patrol_share_one_function():
    from scripts import paper_trades as pt
    p = _paper(advisory_rule=V2)
    assert pt.advisory_view(p, -2.46, None, NOW) == da.advisory_view(p, -2.46, None, NOW)
    assert pt.ADVISORY_RULES is da.ADVISORY_RULES and pt.ADVISORY_CURRENT["debit"] == se.DEBIT_ROI_REVIEW["version"]


def test_analyze_does_not_import_scripts():
    import ast
    src = (Path(__file__).resolve().parents[1] / "undertow" / "analyze" / "debit_advisory.py").read_text("utf-8")
    mods = [n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)] + \
           [a.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names]
    assert mods and not any((m or "").split(".")[0] == "scripts" for m in mods)


# —— T2：部分减仓 / 入场组数未登记 → 不冒充整轮 ROI ——
def _live(**kw):
    base = dict(key="live|QQQ|2026-11-20|C|105/100", scope="live", label="QQQ 100/105C", underlying="QQQ",
                expiry="2026-11-20", side="C", k_sell=105.0, k_buy=100.0, entry_credit=-1.2, qty=1, fee_round_trip=3.2,
                value=-2.46, spot=102.0, tdays_to_expiry=20, cost_source="plan")
    base.update(kw)
    return rw.SpreadView(**base)


def _tp(v, plan):
    return [i for i in rw.evaluate_spread(v, plan, today="2026-10-23", now_hm="11:00", close_hm="16:00")
            if i.key.startswith("take_profit")]


def test_reduced_qty_allocates_entry_fee_and_downgrades():
    """原两组、剩一组：原整笔入场费 3.20 不整笔套给剩余组（旧版投入 123.20 不触发），按比例 1.60 → 121.60，标整轮未核实。"""
    r = rw.debit_review(_live(), {"entry_fees_usd": 3.2, "qty_entered": 2})
    assert r["invest_usd"] == 121.6 and r["entry_fee_source"] == "allocated" and "整轮 ROI 未核实" in r["scope_note"]
    t = _tp(_live(), {"entry_fees_usd": 3.2, "qty_entered": 2})
    assert t and "整轮 ROI 未核实" in t[0].title and "比例分摊" in t[0].title and "ROI 1" not in t[0].title


def test_qty_entered_unknown_not_defaulted_to_current():
    r = rw.debit_review(_live(), {"entry_fees_usd": 1.6})
    assert r["scope_note"] and "入场组数未登记" in r["scope_note"] and r["entry_fee_source"] == "actual"
    t = _tp(_live(), {"entry_fees_usd": 1.6})
    assert t and "整轮 ROI 未核实" in t[0].title


def test_qty_entered_matches_full_round_title():
    t = _tp(_live(), {"entry_fees_usd": 1.6, "qty_entered": 1})
    assert t and "ROI 101%" in t[0].title and t[0].numbers["debit_roi_review"]["scope_note"] is None


def test_validate_plans_qty_entered():
    ok = {"schema": rw.PLAN_SCHEMA, "plans": {"k": {"qty_entered": 2}}}
    assert rw.validate_plans(ok) is None
    for bad in (0, -1, 1.5, True, "2"):
        assert "qty_entered" in rw.validate_plans({"schema": rw.PLAN_SCHEMA, "plans": {"k": {"qty_entered": bad}}})


# —— T3：越界报价 ——
def test_out_of_range_quote_isolated():
    r = se.debit_roi_review(side="C", k_sell=102.0, k_buy=100.0, cost=1.1, qty=1, entry_fees=1.6, exit_fees=1.6, value_exec=3.0)
    assert r["status"] == "quote_out_of_range" and r["triggered"] is False
    assert r["remaining_gross_usd"] is None and r["profit_usd"] is None


def test_zero_and_negative_close_are_legal_readings():
    z = se.debit_roi_review(side="C", k_sell=105.0, k_buy=100.0, cost=1.2, qty=1, entry_fees=1.6, exit_fees=1.6, value_exec=0.0)
    n = se.debit_roi_review(side="C", k_sell=105.0, k_buy=100.0, cost=1.2, qty=1, entry_fees=1.6, exit_fees=1.6, value_exec=-0.05)
    m = se.debit_roi_review(side="C", k_sell=105.0, k_buy=100.0, cost=1.2, qty=1, entry_fees=1.6, exit_fees=1.6, value_exec=None)
    assert z["status"] == "below" and z["negative_close"] is False and z["giveback_gross_usd"] == 0.0
    assert n["status"] == "below" and n["negative_close"] is True and n["close_net_usd"] == -6.6
    assert m["status"] == "no_quote"


def test_patrol_out_of_range_live_and_paper(tmp_path):
    its = rw.evaluate_spread(_live(value=-6.0), {"qty_entered": 1}, today="2026-10-23", now_hm="11:00", close_hm="16:00")
    assert not [i for i in its if i.key.startswith("take_profit")]
    assert [i for i in its if i.key.startswith("quote_anomaly")]
    tp, cov, res = _patrol(tmp_path, _paper(), buy_bid=5.4)                 # 可平仓 5.20 > 宽 5
    assert tp == [] and cov["input"] == "quote_quality"
    assert [i for i in res["items"] if i.key.startswith("quote_anomaly|paper|")]


# —— T4：tick 文案 ——
def test_tick_text_not_called_placeable():
    t = _tp(_live(), {"entry_fees_usd": 1.6, "qty_entered": 1})
    assert "可挂" not in t[0].detail and "参考价" in t[0].detail and "未核实" in t[0].detail
