"""盘中持仓风险巡检（risk-watch-v1）：每条规则正反例、去重与升级、缺价不折零、计划校验、CLI（假长桥）、调度守卫。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from undertow.analyze import risk_watch as rw

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def uso(**kw):
    """合成例：USO 160/165C 借记一组，付 1.20。value = 卖腿 ask − 买腿 bid（借记为负）。"""
    base = dict(key="live|USO|2026-11-20|C|165/160", scope="live", label="USO 160/165C", underlying="USO",
                expiry="2026-11-20", side="C", k_sell=165.0, k_buy=160.0, entry_credit=-1.20, qty=1,
                fee_round_trip=3.2, value=-1.30, spot=154.6, tdays_to_expiry=11)
    base.update(kw)
    return rw.SpreadView(**base)


def credit(**kw):
    base = dict(key="paper|x", scope="paper", label="GLD 385/380C", underlying="GLD", expiry="2026-10-16", side="C",
                k_sell=380.0, k_buy=385.0, entry_credit=2.45, qty=1, fee_round_trip=3.2, value=1.0, spot=370.0,
                tdays_to_expiry=5, stop_value=4.9)
    base.update(kw)
    return rw.SpreadView(**base)


def keys(items):
    return {i.key.split("|")[0] for i in items}


def ev(v, plan=None, hm="11:00", refs=None, close="16:00"):
    return rw.evaluate_spread(v, plan, today="2026-10-15", now_hm=hm, ref_prices=refs, close_hm=close)


def cov(v, plan=None, hm="11:00", refs=None, close="16:00"):
    return rw.assess_spread(v, plan, today="2026-10-15", now_hm=hm, ref_prices=refs, close_hm=close)


# —— 正常情况：什么都不报 ——
def test_normal_is_silent():
    assert ev(uso()) == []
    assert ev(credit()) == []


# —— 1. 止盈 ——
def test_debit_take_profit_at_100pct_of_paid():
    e = rw.econ(uso())
    L = e["entry_net_payoff_max_loss"]                   # 128 + 3.2
    need = -(1.20 + (L + 3.2) / 100)                     # 含费盈利 = 付出（往返费 3.2 一并扣）→ 价差价值
    assert "take_profit" in keys(ev(uso(value=need - 0.01)))
    assert "take_profit" not in keys(ev(uso(value=need + 0.02)))


def test_debit_take_profit_ignored_when_valuation_assumed():
    """买腿 bid 缺失按 0 计的估值不能触发止盈。"""
    assert "take_profit" not in keys(ev(uso(value=-4.0, value_assumption="buy_bid_missing")))


def test_plan_take_profit_value():
    items = ev(uso(value=-2.60), plan={"take_profit_value": 2.40})
    assert "take_profit" in keys(items) and sum(i.key.startswith("take_profit") for i in items) == 1
    assert "take_profit" not in keys(ev(uso(value=-2.30), plan={"take_profit_value": 2.40}))


def test_credit_take_profit_policy_b_90pct():
    v = credit(value=0.10)                               # 2.45 收、0.10 平：盈利 (2.35*100-3.2)=231.8；最大 245-3.2=241.8 → 96%
    assert "take_profit" in keys(ev(v))
    assert "take_profit" not in keys(ev(credit(value=0.60)))
    assert rw.RISK_WATCH_META["credit_take_profit_frac"] == 0.90


# —— 4. 止损/减损 ——
def test_debit_plan_stop_and_credit_paper_stop():
    assert "stop" in keys(ev(uso(value=-0.40), plan={"stop_value": 0.50}))
    assert "stop" not in keys(ev(uso(value=-0.60), plan={"stop_value": 0.50}))
    assert "stop" in keys(ev(credit(value=5.0, spot=None)))
    assert "stop" not in keys(ev(credit(value=4.0)))
    # 估值假设也照报（只会让估值更差）
    assert "stop" in keys(ev(uso(value=-0.40, value_assumption="buy_bid_missing"), plan={"stop_value": 0.5}))


# —— 2. 失效位 ——
INV = {"invalidation": {"ref": "BZ=F", "op": "<", "level": 101.50, "basis": "close", "underlying_equiv": 149.0}}


def test_invalidation_intraday_warn_then_urgent_near_close():
    a = [i for i in ev(uso(), INV, hm="11:00", refs={"BZ=F": 101.0}) if i.key.startswith("invalidation")]
    assert a and a[0].severity == "warn"
    b = [i for i in ev(uso(), INV, hm="15:40", refs={"BZ=F": 101.0}) if i.key.startswith("invalidation")]
    assert b and b[0].severity == "urgent"
    assert "临收盘预警" in b[0].title and "尚未收盘确认" in b[0].title     # 15:40 不是收盘确认（Codex 052）
    assert not [i for i in ev(uso(), INV, refs={"BZ=F": 104.0}) if i.key.startswith("invalidation")]


def test_invalidation_falls_back_to_underlying_equiv_when_ref_missing():
    a = [i for i in ev(uso(spot=148.5), INV, refs={"BZ=F": None}) if i.key.startswith("invalidation")]
    assert a and "等价位" in a[0].detail
    assert not [i for i in ev(uso(spot=150.0), INV, refs={}) if i.key.startswith("invalidation")]
    assert not [i for i in ev(uso(spot=None), INV, refs={}) if i.key.startswith("invalidation")]   # 都没有 → 不猜


# —— 3. 卖腿 / 到期 ——
def test_credit_short_itm_urgent_and_near_warn():
    a = ev(credit(spot=381.0, value=1.5))
    assert any(i.key.startswith("short_itm") and i.severity == "urgent" for i in a)
    b = ev(credit(spot=377.0, value=1.5))
    assert any(i.key.startswith("short_near") for i in b) and not any(i.key.startswith("short_itm") for i in b)


def test_debit_short_itm_only_matters_near_expiry():
    assert "short_itm" not in keys(ev(uso(spot=166.0, value=-4.5)))
    assert "expiry_pin" not in keys(ev(uso(spot=166.0, value=-4.5)))
    a = ev(uso(spot=165.5, value=-4.6, tdays_to_expiry=1))
    pin = [i for i in a if i.key.startswith("expiry_pin")]
    assert pin and "先买回卖腿" in pin[0].detail          # 用户 2026-10-15：组合单可分腿平，先平卖腿
    assert "接近最大收益" in pin[0].detail and "不是亏损" in pin[0].detail   # 借记价差卖腿价内不能说成风险方向
    c = [i for i in ev(credit(spot=381.0, value=1.5, tdays_to_expiry=1)) if i.key.startswith("expiry_pin")]
    assert c and "接近最大收益" not in c[0].detail


def test_expiry_far_or_calendar_unknown_no_pin():
    assert "expiry_pin" not in keys(ev(credit(spot=379.0, tdays_to_expiry=3)))
    assert "expiry_pin" not in keys(ev(credit(spot=379.0, tdays_to_expiry=None)))


# —— 缺价：不折零，连续 N 次才报 ——
def test_missing_quote_not_zero_and_streak_threshold():
    v1 = uso(value=None, missing_streak=1)
    assert ev(v1) == []                                  # 首次缺失：开盘常态，不报；也不触发止损/止盈
    v2 = uso(value=None, missing_streak=2)
    assert keys(ev(v2)) == {"quote_missing"}
    assert "stop" not in keys(ev(uso(value=None, missing_streak=1), plan={"stop_value": 5.0}))


def test_bump_missing_counts_and_resets():
    s = rw.new_state("2026-10-15")
    assert rw.bump_missing(s, "k", True) == 1 and rw.bump_missing(s, "k", True) == 2
    assert rw.bump_missing(s, "k", False) == 0 and "k" not in s["missing_streak"]


# —— 5. 复核日 ——
def test_review_date():
    assert "review" in keys(ev(uso(), {"review_date": "2026-10-15"}))
    assert "review" not in keys(ev(uso(), {"review_date": "2026-11-13"}))


# —— 6. 账户 ——
def test_account_rules():
    a = rw.evaluate_account(cash_by_ccy={"USD": -50.00, "EUR": 200.00}, trial={"state": "absent"},
                            book={"overall": "pass", "failed": []})
    assert {i.key for i in a} == {"cash_negative|USD", "trial_absent"}
    b = rw.evaluate_account(cash_by_ccy={"USD": 10.0}, trial={"state": "ok", "paused": True, "consecutive_unknown": True},
                            book={"overall": "fail", "failed": ["全场合计 fail"]})
    assert {i.key: i.severity for i in b} == {"trial_paused": "urgent", "trial_consecutive_unknown": "info",
                                              "book_fail": "urgent"}
    c = rw.evaluate_account(cash_by_ccy=None, trial=None, book=None)
    assert {i.key for i in c} == {"account_unknown|cash", "trial_unknown", "book_unknown"}   # 读不到 ≠ 通过
    assert rw.evaluate_account(cash_by_ccy={"USD": 0.0}, trial={"state": "ok", "paused": False,
                                                                "consecutive_unknown": False},
                               book={"overall": "pass"}) == []


# —— 去重与升级（入箱口径）——
def _rows(today, *items):
    return [{"alert_id": rw.alert_id(today, i), "date": today, "key": i.key, "severity": i.severity} for i in items]


def test_dedup_same_day_and_escalation_and_rollover():
    w = rw.RiskItem("invalidation|k", "warn", "live", "t")
    u = rw.RiskItem("invalidation|k", "urgent", "live", "t")
    assert rw.select_new([w], {}) == [w]
    rows = _rows("2026-10-15", w)
    assert rw.select_new([w], rw.enqueued_map(rows, "2026-10-15")) == []      # 同日同 key 不重复入箱
    assert rw.select_new([u], rw.enqueued_map(rows, "2026-10-15")) == [u]     # 升级再入箱
    rows += _rows("2026-10-15", u)
    assert rw.select_new([w], rw.enqueued_map(rows, "2026-10-15")) == []      # 降级不入箱
    assert rw.select_new([w], rw.enqueued_map(rows, "2026-10-16")) == [w]     # 次日再入箱一次
    assert rw.select_new([w, u], {}) == [u]                                   # 同轮同 key 只取最高严重度
    assert rw.alert_id("2026-10-15", w) != rw.alert_id("2026-10-15", u)


def test_state_roll_keeps_streak_and_migrates_v1():
    s = rw.new_state("2026-10-15")
    s["missing_streak"]["m"] = 3
    s2 = rw.roll_state(s, "2026-10-16")
    assert s2 == {"schema": rw.STATE_SCHEMA, "date": "2026-10-16", "missing_streak": {"m": 3}}
    v1 = {"schema": "risk-watch-state-v1", "date": "2026-10-15", "sent": {"k": "warn"}, "missing_streak": {"m": 1}}
    assert rw.roll_state(v1, "2026-10-15") == {"schema": rw.STATE_SCHEMA, "date": "2026-10-15", "missing_streak": {"m": 1}}
    assert rw.roll_state({"junk": 1}, "2026-10-16") == rw.new_state("2026-10-16")


def test_digest_has_no_numbers():
    i = rw.RiskItem("take_profit|live|USO", "warn", "live", "盈利 $300", numbers={"pnl_usd": 300})
    d = rw.digest([rw.alert_id("2026-10-15", i)])
    assert len(d) == 12 and "300" not in d


# —— 计划 schema ——
def test_validate_plans():
    ok = {"schema": rw.PLAN_SCHEMA, "plans": {"k": {"cost_per_share": 1.20, "take_profit_value": 2.40,
                                                     "review_date": "2026-11-13", **INV}}}
    assert rw.validate_plans(ok) is None
    for bad in ({"schema": "x", "plans": {}},
                {"schema": rw.PLAN_SCHEMA, "plans": {"k": {"take_profit_value": -1}}},
                {"schema": rw.PLAN_SCHEMA, "plans": {"k": {"take_profit_value": float("nan")}}},
                {"schema": rw.PLAN_SCHEMA, "plans": {"k": {"invalidation": {"ref": "BZ=F", "op": "<="}}}},
                {"schema": rw.PLAN_SCHEMA, "plans": {"k": {"review_date": "10/23"}}}):
        assert rw.validate_plans(bad)


def test_econ_invalid_is_reported_not_silent():
    a = ev(uso(entry_credit=1.20))                        # 借记结构却写成收钱 → 经济量不成立
    assert keys(a) == {"econ_invalid"}


# —— Codex 052 I2：临收盘窗口按交易日历 ——
def test_close_window_follows_calendar_half_day():
    from undertow.core import market_calendar as mc
    assert mc.close_time(datetime(2026, 11, 27).date()) == "13:00"          # 预存日历里的半日市
    inv = lambda hm, close: [i for i in ev(uso(), INV, hm=hm, refs={"BZ=F": 101.0}, close=close)
                             if i.key.startswith("invalidation")][0]
    assert inv("12:45", "13:00").severity == "urgent" and "13:00 收市" in inv("12:45", "13:00").title
    assert inv("12:00", "13:00").severity == "warn"
    assert inv("12:45", "16:00").severity == "warn"                          # 常规日 12:45 仍是盘中
    assert inv("15:30", "16:00").severity == "urgent"
    u = inv("15:50", None)                                                   # 收市时刻未知：不升级、写明
    assert u.severity == "warn" and "收市时刻未知" in u.title


def test_cli_half_day_uses_calendar_close(cli):
    pos = lambda: [_pos("SYN261218C105000.US", -1, 1.0), _pos("SYN261218C100000.US", 1, 2.0)]
    q = {"SYN261218C105000.US": {"bid": 1.0, "ask": 1.1, "error": ""}, "SYN261218C100000.US": {"bid": 2.0, "ask": 2.1, "error": ""}}
    plans = {"live|SYN|2026-12-18|C|105/100": {"invalidation": {"ref": "SYNTH.REF", "op": "<", "level": 50, "basis": "close"}}}
    deps = _deps(q, positions=pos, plans=lambda: (plans, None), spots=lambda roots: {"SYN": 98.0},
                 ref_prices=lambda plans, spots: {"SYNTH.REF": 49.0})
    for hm, sev in (((12, 45), "urgent"), ((12, 0), "warn")):
        r = cli.collect_and_evaluate(datetime(2026, 11, 27, *hm, tzinfo=ET), deps=deps)   # 半日市（13:00 收市）
        inv = [i for i in r["items"] if i.key.startswith("invalidation")]
        assert inv and inv[0].severity == sev, (hm, inv)
    r = cli.collect_and_evaluate(datetime(2026, 11, 25, 12, 45, tzinfo=ET), deps=deps)        # 常规日同一时刻
    assert [i.severity for i in r["items"] if i.key.startswith("invalidation")] == ["warn"]


# —— Codex 052 I2：卖腿价内 ≠ 已亏损；四结构镜像；借记理论限损 ≠ 无处置风险 ——
def syn(side, debit, **kw):
    """合成价差：卖腿 100，宽 5；四种结构由 side/debit 决定。"""
    k_buy = (95.0 if side == "C" else 105.0) if debit else (105.0 if side == "C" else 95.0)
    base = dict(key=f"paper|syn-{side}-{debit}", scope="paper", label=f"SYN {side} {'借记' if debit else '贷记'}",
                underlying="SYN", expiry="2026-11-20", side=side, k_sell=100.0, k_buy=k_buy,
                entry_credit=-2.0 if debit else 1.0, qty=1, fee_round_trip=3.2, value=-2.0 if debit else 1.0,
                spot=90.0 if side == "C" else 110.0, tdays_to_expiry=10)
    base.update(kw)
    return rw.SpreadView(**base)


def test_credit_short_itm_but_profitable_not_called_loss():
    """Codex 052 合成：put 贷记净收 1、卖腿 100、标的 99.9、平仓成本 0.2 → 含费浮盈 +76.80，旧版却称「进入亏损区」。"""
    v = syn("P", False, spot=99.9, value=0.2)
    assert rw.pnl_usd(v) == 76.8
    it = [i for i in ev(v) if i.key.startswith("short_itm")]
    assert it and "亏损区" not in it[0].title and "不等于已亏损" in it[0].title
    assert "+76.80" in it[0].detail and "盈亏平衡" in it[0].detail and "指派" in it[0].detail


def test_four_structures_short_leg_mirror():
    for side in ("C", "P"):
        beyond = 100.5 if side == "C" else 99.5            # 越过卖腿 100
        far = 90.0 if side == "C" else 110.0
        assert "short_itm" in keys(ev(syn(side, False, spot=beyond)))
        assert "short_itm" not in keys(ev(syn(side, False, spot=far)))
        assert "short_itm" not in keys(ev(syn(side, True, spot=beyond, value=-4.5)))   # 借记卖腿价内 = 接近最大收益
        assert ev(syn(side, True)) == [] and ev(syn(side, False)) == []
        c = cov(syn(side, True))[1]["rules"]
        assert c["short_leg"]["status"] == "not_applicable" and c["expiry"]["status"] == "checked"


def test_debit_stop_text_keeps_assignment_risk():
    it = [i for i in ev(uso(value=-0.40), plan={"stop_value": 0.50}) if i.key.startswith("stop")][0]
    assert "不会被强平" not in it.detail and "提前指派" in it.detail and "理论最大亏损" in it.detail


# —— Codex 052 I2：费用与成本 ——
def test_zero_fee_kept_and_bad_fee_rejected():
    v0 = syn("C", True, fee_round_trip=0.0)
    assert ev(v0) == [] and rw.econ(v0)["entry_net_payoff_max_loss"] == 200.0          # 0 费合法：最大亏损 = 付出
    for bad in (None, float("nan"), float("inf"), -1.0):
        items, c = cov(syn("C", True, fee_round_trip=bad))
        assert "econ_invalid" in keys(items) and c["rules"]["fee"]["status"] == "missing"
        assert rw.core_gaps(c)                                                         # 不完整，不是没事
    for bad in (-1.0, float("nan")):
        assert rw.validate_plans({"schema": rw.PLAN_SCHEMA, "plans": {"k": {"fee_round_trip": bad}}})
    assert rw.validate_plans({"schema": rw.PLAN_SCHEMA, "plans": {"k": {"fee_round_trip": 0.0, "cost_side": "credit"}}}) is None
    assert rw.validate_plans({"schema": rw.PLAN_SCHEMA, "plans": {"k": {"cost_side": "付"}}})


def test_cost_sign_from_structure_and_sources(cli):
    deb = {"side": "C", "k_sell": 105.0, "k_buy": 100.0, "broker_entry_credit": -2.1}
    cre = {"side": "P", "k_sell": 100.0, "k_buy": 95.0, "broker_entry_credit": 0.9}
    assert cli.live_entry_credit(deb, {"cost_per_share": 2.0}) == (-2.0, "plan")
    assert cli.live_entry_credit(cre, {"cost_per_share": 1.0}) == (1.0, "plan")        # 贷记核定净收不再变成付出
    assert cli.live_entry_credit(cre, {"cost_per_share": 1.0, "cost_side": "debit"}) == (None, "plan_conflict")
    assert cli.live_entry_credit(cre, {}) == (0.9, "broker_estimate")
    assert cli.live_entry_credit({**cre, "broker_entry_credit": None}, {}) == (None, "unknown")
    live, _ = cli.group_verticals([_pos("SYN261120P100000.US", -1, None), _pos("SYN261120P095000.US", 1, 0.5)])
    assert live[0]["broker_entry_credit"] is None                                      # 缺成本价不折零、不崩


def test_broker_estimate_take_profit_labelled_estimate():
    v = uso(value=-4.0, cost_source="broker_estimate")
    tp = [i for i in ev(v) if i.key.startswith("take_profit")][0]
    assert "估算" in tp.title and "未核定" in tp.detail
    tp2 = [i for i in ev(uso(value=-4.0, cost_source="plan")) if i.key.startswith("take_profit")][0]
    assert "估算" not in tp2.title


# —— CLI：假长桥 ——
@pytest.fixture(autouse=True)
def _private_paths_isolated(tmp_path, monkeypatch):
    """本文件所有测试的私有路径都改到临时目录：绝不读写真实 data/account/（Codex 052：计划默认参数曾在定义时绑定）。"""
    from undertow import riskwatch_cli as rc
    monkeypatch.setattr(rc, "RW_DIR", tmp_path / "rw")
    monkeypatch.setattr(rc, "PLANS", tmp_path / "plans.json")
    monkeypatch.setattr(rc, "_paper", lambda: FakePaper)       # 默认依赖也不读真实模拟仓 journal（测试另行覆盖）


@pytest.fixture()
def cli(tmp_path):
    from undertow import riskwatch_cli as rc
    assert rc.RW_DIR == tmp_path / "rw"
    return rc


def test_load_plans_default_follows_patched_path(cli, tmp_path):
    """load_plans() 不带参数时读的是【当前】PLANS（被测试 patch 的临时文件），不是定义时绑定的私有路径。"""
    assert cli.load_plans() == ({}, None)                 # 临时路径不存在 → 没计划
    cli.PLANS.write_text(json.dumps({"schema": rw.PLAN_SCHEMA, "plans": {"k": {"note": "合成"}}}), "utf-8")
    assert cli.load_plans()[0] == {"k": {"note": "合成"}}


def _pos(sym, q, c):
    return SimpleNamespace(symbol=sym, quantity=q, cost_price=c)


def test_group_verticals(cli):
    live, odd = cli.group_verticals([_pos("USO261120C165000.US", -1, 2.20), _pos("USO261120C160000.US", 1, 3.40),
                                     _pos("SLV.US", 10, 50.0), _pos("GLD261016P370000.US", 1, 1.0)])
    assert [v["key"] for v in live] == ["live|USO|2026-11-20|C|165/160"]
    assert live[0]["broker_entry_credit"] == -1.20 and live[0]["qty"] == 1
    assert sorted(odd) == ["GLD261016P370000.US", "SLV.US"]


class FakePaper:
    JOURNAL = Path("/nonexistent/journal.json")

    @staticmethod
    def close_value(q, p):
        s, b = q.get(p["sell"]), q.get(p["buy"])
        if not s or s.get("ask") is None:
            return None
        bb = b.get("bid") if b else None
        return (round(s["ask"] - (bb or 0.0), 4), None if bb else "buy_bid_missing")

    @staticmethod
    def fallback_append(path, line):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _deps(quotes, **kw):
    d = {"positions": lambda: [_pos("USO261120C165000.US", -1, 2.20), _pos("USO261120C160000.US", 1, 3.40)],
         "assets": lambda: SimpleNamespace(cash_by_ccy={"USD": 5.0}),
         "quotes": lambda syms: quotes, "spots": lambda roots: {"USO": 154.6}, "ref_prices": lambda plans, spots: {},
         "trial": lambda: {"state": "ok", "paused": False, "consecutive_unknown": False},
         "book": lambda: {"overall": "pass", "failed": []}, "paper": lambda: FakePaper, "plans": lambda: ({}, None)}
    d.update(kw)
    return d


NOW = datetime(2026, 10, 15, 11, 0, tzinfo=ET)


def test_cli_normal_round_is_silent(cli):
    q = {"USO261120C165000.US": {"bid": 2.2, "ask": 2.3, "error": ""},
         "USO261120C160000.US": {"bid": 3.5, "ask": 3.7, "error": ""}}
    r = cli.collect_and_evaluate(NOW, deps=_deps(q))
    assert r["items"] == [] and r["problems"] == [] and r["counts"]["live"] == 1


def test_cli_take_profit_and_negative_cash(cli):
    q = {"USO261120C165000.US": {"bid": 4.0, "ask": 4.1, "error": ""},
         "USO261120C160000.US": {"bid": 6.9, "ask": 7.1, "error": ""}}
    r = cli.collect_and_evaluate(NOW, deps=_deps(q, assets=lambda: SimpleNamespace(cash_by_ccy={"USD": -50.00})))
    assert {i.key.split("|")[0] for i in r["items"]} == {"take_profit", "cash_negative"}


def test_cli_missing_quotes_streak_then_alert(cli):
    r1 = cli.collect_and_evaluate(NOW, deps=_deps({}))
    assert r1["items"] == [] and r1["problems"] == ["全部盘口取不到"]      # 取数失败 = 本轮不完整（rc=1），不是「没事」
    r2 = cli.collect_and_evaluate(NOW, deps=_deps({}, state=r1["state"]))
    assert [i.key.split("|")[0] for i in r2["items"]] == ["quote_missing"]


OKQ = {"USO261120C165000.US": {"bid": 2.2, "ask": 2.3, "error": ""},
       "USO261120C160000.US": {"bid": 3.5, "ask": 3.7, "error": ""}}
SYN_PLAN = {"live|USO|2026-11-20|C|165/160": {"invalidation": {"ref": "SYNTH.REF", "op": "<", "level": 100, "basis": "close"}}}


def test_cli_missing_spot_and_ref_is_incomplete_then_quiet_after_recovery(cli, tmp_path, monkeypatch):
    """Codex 052 例：价差两腿报价正常，但标的价与计划参照价都缺、没有等价位 —— 旧版 problems=[]、items=[] → complete/无需提醒。"""
    deps = _deps(OKQ, spots=lambda roots: {}, plans=lambda: (SYN_PLAN, None))
    r1 = cli.collect_and_evaluate(NOW, deps=deps)
    assert r1["problems"] == [] and r1["items"] == []                      # 第一轮：不弹提醒（去噪）……
    assert r1["incomplete"] and any("invalidation" in x for x in r1["incomplete"])   # ……但明确不完整
    rules = r1["coverage"][0]["rules"]
    assert rules["invalidation"]["status"] == "missing" and rules["invalidation"]["input"] == "ref"
    r2 = cli.collect_and_evaluate(NOW, deps={**deps, "state": r1["state"]})
    assert [i.key.split("|")[0] for i in r2["items"]] == ["coverage_incomplete"]     # 连续 2 轮 → 提醒
    assert "invalidation" in r2["items"][0].detail
    ok = {**deps, "state": r2["state"], "spots": lambda roots: {"USO": 154.6},
          "ref_prices": lambda plans, spots: {"SYNTH.REF": 104.0}}
    r3 = cli.collect_and_evaluate(NOW, deps=ok)
    assert r3["items"] == [] and r3["incomplete"] == [] and "cov|live|USO|2026-11-20|C|165/160" not in r3["state"]["missing_streak"]
    # 显式等价位兜底：有 underlying_equiv + 标的价 → checked 且标来源
    eq = {"live|USO|2026-11-20|C|165/160": {"invalidation": {**SYN_PLAN["live|USO|2026-11-20|C|165/160"]["invalidation"],
                                                              "underlying_equiv": 149.0}}}
    r4 = cli.collect_and_evaluate(NOW, deps={**deps, "spots": lambda roots: {"USO": 154.6}, "plans": lambda: (eq, None)})
    assert r4["incomplete"] == [] and r4["coverage"][0]["rules"]["invalidation"]["source"] == "underlying_equiv"
    # 经 cmd_run：不完整 → overall=partial、rc=1（调度不写成功哨兵）
    orig = cli.collect_and_evaluate
    monkeypatch.setattr(cli, "collect_and_evaluate", lambda: orig(NOW, deps=deps))
    st = tmp_path / "st.json"
    assert cli.cmd_run(SimpleNamespace(result_file=None, status_file=str(st), run_id="r")) == 1
    assert json.loads(st.read_text())["overall"] == "partial"


def test_cli_credit_spot_missing_short_leg_incomplete(cli):
    pos = lambda: [_pos("SYN261120P100000.US", -1, 1.5), _pos("SYN261120P095000.US", 1, 0.5)]
    q = {"SYN261120P100000.US": {"bid": 0.5, "ask": 0.6, "error": ""}, "SYN261120P095000.US": {"bid": 0.1, "ask": 0.2, "error": ""}}
    r = cli.collect_and_evaluate(NOW, deps=_deps(q, positions=pos, spots=lambda roots: {}))
    rules = r["coverage"][0]["rules"]
    assert rules["short_leg"]["status"] == "missing" and rules["cost"]["source"] == "broker_estimate"
    assert any("short_leg" in x for x in r["incomplete"])
    r = cli.collect_and_evaluate(NOW, deps=_deps(q, positions=pos, spots=lambda roots: {"SYN": 110.0}))
    assert r["incomplete"] == [] and r["items"] == []


def test_cli_bad_quotes_degrade_with_real_close_value(cli):
    """真实 close_value（模拟仓同一口径）：交叉 / 出错 / 非有限 → 不可估值，不出止盈止损；连续 2 次才报盘口缺失。"""
    import importlib.util as iu
    spec = iu.spec_from_file_location("paper_trades_t", ROOT / "scripts/paper_trades.py")
    m = iu.module_from_spec(spec); spec.loader.exec_module(m)
    paper = SimpleNamespace(JOURNAL=Path("/nonexistent/journal.json"), close_value=m.close_value)
    for bad_sell in ({"bid": 2.4, "ask": 2.3, "error": ""}, {"bid": 2.2, "ask": 2.3, "error": "timeout"},
                     {"bid": 2.2, "ask": float("nan"), "error": ""}):
        q = {**OKQ, "USO261120C165000.US": bad_sell}
        r1 = cli.collect_and_evaluate(NOW, deps=_deps(q, paper=lambda: paper))
        assert r1["items"] == [] and r1["coverage"][0]["rules"]["take_profit"]["input"] == "quote"
        assert r1["incomplete"] == []                                       # 首次缺价：去噪
        r2 = cli.collect_and_evaluate(NOW, deps=_deps(q, paper=lambda: paper, state=r1["state"]))
        assert [i.key.split("|")[0] for i in r2["items"]] == ["quote_missing"] and r2["incomplete"]


def test_cli_plan_zero_fee_reaches_view(cli):
    plans = {"live|USO|2026-11-20|C|165/160": {"cost_per_share": 1.0, "fee_round_trip": 0.0}}
    r = cli.collect_and_evaluate(NOW, deps=_deps(OKQ, plans=lambda: (plans, None)))
    assert r["coverage"][0]["rules"]["fee"]["status"] == "checked" and r["incomplete"] == []
    plans = {"live|USO|2026-11-20|C|165/160": {"cost_per_share": 1.0, "cost_side": "credit"}}
    r = cli.collect_and_evaluate(NOW, deps=_deps(OKQ, plans=lambda: (plans, None)))
    assert "plan_cost_conflict" in keys(r["items"])


def test_cli_positions_unreadable_is_problem_and_book_unknown(cli):
    def boom():
        raise RuntimeError("down")
    r = cli.collect_and_evaluate(NOW, deps=_deps({}, positions=boom))
    assert "实盘持仓读不到：RuntimeError" in r["problems"]
    assert "book_unknown" in {i.key for i in r["items"]}


def test_cli_plan_cost_overrides_broker_and_invalid_plans_reported(cli):
    q = {"USO261120C165000.US": {"bid": 2.2, "ask": 2.3, "error": ""},
         "USO261120C160000.US": {"bid": 3.5, "ask": 3.7, "error": ""}}
    plans = {"live|USO|2026-11-20|C|165/160": {"cost_per_share": 1.20, "review_date": "2026-10-15"}}
    r = cli.collect_and_evaluate(NOW, deps=_deps(q, plans=lambda: (plans, None)))
    assert [i.key.split("|")[0] for i in r["items"]] == ["review"]
    r = cli.collect_and_evaluate(NOW, deps=_deps(q, plans=lambda: ({}, "坏")))
    assert "plans_invalid" in {i.key for i in r["items"]}


def _jl(path):
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()] if path.exists() else []


def test_cli_run_deliver_ack_roundtrip(cli, tmp_path, monkeypatch):
    q = {"USO261120C165000.US": {"bid": 4.0, "ask": 4.1, "error": ""},
         "USO261120C160000.US": {"bid": 6.9, "ask": 7.1, "error": ""}}
    mod = cli
    orig = mod.collect_and_evaluate
    monkeypatch.setattr(mod, "collect_and_evaluate", lambda: orig(NOW, deps=_deps(q)))
    monkeypatch.setattr(mod, "_paper", lambda: FakePaper)
    res, st = tmp_path / "res.json", tmp_path / "st.json"
    args = SimpleNamespace(result_file=str(res), status_file=str(st), run_id="r1")
    assert mod.cmd_run(args) == 0
    out = json.loads(res.read_text())
    assert out["status"] == "alert" and [i["key"].split("|")[0] for i in out["pending"]] == ["take_profit"]
    assert out["new_ids"] == [out["pending"][0]["alert_id"]]
    assert json.loads(st.read_text())["overall"] == "complete"
    title, body = mod.notify_text(out)
    assert "持仓提醒 1 条" in title and "≥100%" in body
    assert mod.cmd_deliver(SimpleNamespace(result=str(res))) == 0
    assert "take_profit" in (tmp_path / "rw" / "inbox_2026-10-15.log").read_text()
    assert mod.cmd_deliver(SimpleNamespace(result=str(res))) == 0          # 旧结果重投：不重复写收件箱、不重复确认
    assert (tmp_path / "rw" / "inbox_2026-10-15.log").read_text().count("take_profit|") == 1
    assert len(_jl(tmp_path / "rw" / "acks.jsonl")) == 1
    assert mod.cmd_run(args) == 0                         # 已送达：同日不再报
    assert json.loads(res.read_text())["status"] == "ok"
    assert len(_jl(tmp_path / "rw" / "outbox.jsonl")) == 1                 # 重复巡检不重复入箱


def _fake_round(mod, monkeypatch, items, today="2026-10-15"):
    monkeypatch.setattr(mod, "collect_and_evaluate", lambda: {
        "items": list(items), "problems": [], "state": mod.load_state(today), "today": today, "hm": "11:00",
        "counts": {"live": 1, "paper": 0, "legs": 2, "quoted": 2}})


def test_cli_deliver_failure_does_not_ack_and_pending_survives_condition_gone(cli, tmp_path, monkeypatch):
    mod = cli

    class Broken(FakePaper):
        @staticmethod
        def fallback_append(path, line):
            raise OSError("disk")
    monkeypatch.setattr(mod, "_paper", lambda: Broken)
    res = tmp_path / "res.json"
    args = SimpleNamespace(result_file=str(res), status_file=None, run_id=None)
    _fake_round(mod, monkeypatch, [rw.RiskItem("short_itm|x", "urgent", "live", "合成告警")])
    assert mod.cmd_run(args) == 0
    assert mod.cmd_deliver(SimpleNamespace(result=str(res))) == 1          # 收件箱写失败 → 不确认
    assert _jl(tmp_path / "rw" / "acks.jsonl") == []
    _fake_round(mod, monkeypatch, [])                                      # 下一轮条件消失
    assert mod.cmd_run(args) == 0
    out = json.loads(res.read_text())
    assert out["status"] == "alert" and [i["key"] for i in out["pending"]] == ["short_itm|x"] and out["new_ids"] == []
    monkeypatch.setattr(mod, "_paper", lambda: FakePaper)
    assert mod.cmd_deliver(SimpleNamespace(result=str(res))) == 0         # 恢复后补发
    assert "合成告警" in (tmp_path / "rw" / "inbox_2026-10-15.log").read_text()
    assert mod.cmd_run(args) == 0 and json.loads(res.read_text())["status"] == "ok"
    bad = tmp_path / "bad.json"; bad.write_text("{")
    assert mod.cmd_deliver(SimpleNamespace(result=str(bad))) == 1
    old = tmp_path / "old.json"                                            # v1 结果（new 字段）不再被当成合格结果
    old.write_text(json.dumps({"schema": "risk-watch-result-v1", "date": "2026-10-15", "status": "alert",
                               "new": [{"key": "k", "severity": "warn", "title": "t"}]}))
    assert mod.cmd_deliver(SimpleNamespace(result=str(old))) == 1


def test_cli_pending_crosses_day_and_daily_dedup_resets(cli, tmp_path, monkeypatch):
    mod = cli
    monkeypatch.setattr(mod, "_paper", lambda: FakePaper)
    res = tmp_path / "res.json"
    args = SimpleNamespace(result_file=str(res), status_file=None, run_id=None)
    it = rw.RiskItem("cash_negative|USD", "warn", "account", "合成")
    _fake_round(mod, monkeypatch, [it], today="2026-10-15")
    assert mod.cmd_run(args) == 0                                          # 当天没投递
    _fake_round(mod, monkeypatch, [it], today="2026-10-16")
    assert mod.cmd_run(args) == 0
    out = json.loads(res.read_text())
    assert sorted(i["date"] for i in out["pending"]) == ["2026-10-15", "2026-10-16"]   # 昨天未送达的照样待投递；今天再入箱一次
    assert mod.cmd_deliver(SimpleNamespace(result=str(res))) == 0
    assert len(_jl(tmp_path / "rw" / "acks.jsonl")) == 2


def test_cli_run_busy_when_locked_and_deliver_waits(cli, tmp_path, monkeypatch):
    mod = cli
    _fake_round(mod, monkeypatch, [])
    with mod._rw_lock(0) as got:
        assert got
        st = tmp_path / "st.json"
        assert mod.cmd_run(SimpleNamespace(result_file=None, status_file=str(st), run_id="r")) == mod.BUSY_RC
        assert not st.exists()                                             # busy 不写状态文件（run_bound 判 busy）
    assert mod.cmd_run(SimpleNamespace(result_file=None, status_file=str(st), run_id="r")) == 0


def test_cli_corrupt_outbox_isolated_not_overwritten(cli, tmp_path, monkeypatch):
    mod = cli
    (tmp_path / "rw").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rw" / "outbox.jsonl").write_text('{"alert_id": "a"}\n{坏\n', "utf-8")
    res = tmp_path / "res.json"
    _fake_round(mod, monkeypatch, [rw.RiskItem("k", "warn", "live", "合成")])
    assert mod.cmd_run(SimpleNamespace(result_file=str(res), status_file=None, run_id=None)) == 1   # problem → 不完整
    iso = list((tmp_path / "rw").glob("outbox.jsonl.corrupt-*"))
    assert len(iso) == 1 and "{坏" in iso[0].read_text("utf-8")            # 原件隔离保留
    keys_ = {i["key"] for i in json.loads(res.read_text())["pending"]}
    assert keys_ == {"k", "riskwatch_outbox_corrupt"}


def test_plan_set_and_show(cli, capsys):
    a = SimpleNamespace(key="live|USO|2026-11-20|C|165/160", cost=1.20, take_profit_value=2.40, stop_value=None,
                        fee_round_trip=None, inv_ref="BZ=F", inv_op="<", inv_level=101.50, inv_equiv=149.0,
                        inv_basis="close", inv_note="布伦特收盘跌破外部位点", review_date="2026-11-13",
                        review_note=None, note="1 组")
    assert cli.cmd_plan_set(a) == 0
    plans, prob = cli.load_plans(cli.PLANS)
    assert prob is None and plans[a.key]["take_profit_value"] == 2.40 and plans[a.key]["invalidation"]["level"] == 101.50
    bad = SimpleNamespace(**{**a.__dict__, "review_date": "10/23"})
    assert cli.cmd_plan_set(bad) == 1                     # 不合格不写
    assert cli.load_plans(cli.PLANS)[0][a.key]["review_date"] == "2026-11-13"
    cli.PLANS.write_text("{")
    assert cli.load_plans(cli.PLANS)[1]                   # 损坏 → problem，不当作没计划


# —— 调度守卫 ——
def test_session_hook_riskwatch_wiring():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert src.index("riskwatch_bucket() {") < src.index("riskwatch_bucket \"$_RW_LO\" \"$_RW_HI\"")
    body = src[src.index("riskwatch_bucket() {"):src.index("# ⑯ end")]
    assert 'run_bound "riskwatch" "riskwatch run" riskwatch run --result-file' in body
    assert "riskwatch deliver" in body and "riskwatch notify-text" in body
    assert "FAILURE_${ET_DATE}.txt" in body and "printf 'x' >> \"$FAILF\"" in body
    traps = [ln for ln in src.splitlines() if re.match(r"\s*trap .*'\s+EXIT\b", ln)]
    assert len(traps) == 1                                # 只许一个 EXIT trap
    # 先于影子账长任务：⑯ 在 while 循环（④⑤⑥⑦）之前调用
    assert src.index("riskwatch_bucket \"$_RW_LO\" \"$_RW_HI\"") < src.index("while read -r _W _LO _HI; do")


# —— 调度：riskwatch_bucket 沙盒端到端（Codex 052 I1）——
# 真实 shell 函数（从 session_hooks.sh 原样摘出）+ 真实 `undertow.cli riskwatch run/notify-text/deliver`，
# 只替换两样：巡检取数（夹具文件给出本轮告警，代替长桥/计划）与 notify（写文件代替弹窗）。私有目录全部在临时目录。
import os  # noqa: E402
import subprocess  # noqa: E402

_HARNESS = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["RW_ROOT"])
from undertow import riskwatch_cli as rc
from undertow.analyze import risk_watch as rw
_REAL = rc.collect_and_evaluate
base = Path(os.environ["RW_SANDBOX"])
rc.RW_DIR = base / "rw"
rc.PLANS = base / "plans.json"
argv = sys.argv[1:]
assert argv[:2] == ["-m", "undertow.cli"], argv
argv = argv[2:]
fx = json.loads((base / "fixture.json").read_text("utf-8"))
def fake_collect(now=None, *, deps=None):
    today = fx.get("today", "2026-10-15")
    return {"items": [rw.RiskItem(**i) for i in fx.get("items", [])], "problems": list(fx.get("problems", [])),
            "incomplete": list(fx.get("incomplete", [])), "state": rc.load_state(today), "today": today,
            "hm": fx.get("hm", "11:00"), "counts": {"live": 1, "paper": 0, "legs": 2, "quoted": 2}}
rc.collect_and_evaluate = fake_collect
if fx.get("real"):   # 真实 collect_and_evaluate + 真实 load_plans（读沙盒 plans.json）；只把长桥/模拟仓换成合成持仓与盘口
    from datetime import datetime
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    class _P:
        JOURNAL = base / "no_journal.json"
        @staticmethod
        def close_value(q, p):
            return (round(q[p["sell"]]["ask"] - q[p["buy"]]["bid"], 4), None)
    _q = {"USO261120C165000.US": {"bid": 2.2, "ask": 2.3, "error": ""},
          "USO261120C160000.US": {"bid": 3.5, "ask": 3.7, "error": ""}}
    _deps = {"positions": lambda: [SimpleNamespace(symbol="USO261120C165000.US", quantity=-1, cost_price=2.20),
                                   SimpleNamespace(symbol="USO261120C160000.US", quantity=1, cost_price=3.40)],
             "assets": lambda: SimpleNamespace(cash_by_ccy={"USD": 500.0}),
             "quotes": lambda syms: _q, "spots": lambda roots: {"USO": 154.6}, "ref_prices": lambda plans, spots: {},
             "trial": lambda: {"state": "ok", "paused": False, "consecutive_unknown": False},
             "book": lambda: {"overall": "pass", "failed": []}, "paper": lambda: _P}
    rc.collect_and_evaluate = lambda now=None, *, deps=None: _REAL(datetime(2026, 10, 15, 11, 0, tzinfo=ZoneInfo("America/New_York")), deps=_deps)
with open(base / "calls.log", "a") as f:
    f.write(argv[1] + "\n")
from undertow.cli import main
code = main(argv)
if argv[1] == "run" and fx.get("break_result"):
    rf = Path(argv[argv.index("--result-file") + 1])
    if fx["break_result"] == "missing":
        rf.unlink()
    else:
        rf.write_text("{corrupt", "utf-8")
sys.exit(code)
"""


def _rw_hook_funcs():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    out = []
    for name in ("clip", "bound_overall", "run_bound", "ov_text", "riskwatch_bucket"):
        m = re.search(rf"^{name}\(\) {{.*?^}}\n", src, re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


URG = {"key": "short_itm|live|SYN|2026-11-20|P|100/95", "severity": "urgent", "scope": "live", "title": "合成告警甲",
       "detail": "合成"}


class Sandbox:
    def __init__(self, tmp_path):
        self.d = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "harness.py").write_text(_HARNESS, "utf-8")
        fake = tmp_path / "fakepy"
        fake.write_text(f"""#!/bin/zsh
if [[ "$1" == "-m" ]]; then exec python3 "{tmp_path}/harness.py" "$@"; fi
exec python3 "$@"
""")
        fake.chmod(0o755)
        (tmp_path / "data" / "reports").mkdir(parents=True, exist_ok=True)
        self.fake = fake

    def inbox_broken(self, broken: bool):
        """收件箱路径占成目录 = 真实的落盘失败（open 抛 IsADirectoryError），不 patch 代码。"""
        ib = self.d / "rw" / "inbox_2026-10-15.log"
        if broken:
            ib.mkdir(parents=True, exist_ok=True)
        elif ib.is_dir():
            ib.rmdir()

    def wake(self, et_min, items=(), **fx):
        (self.d / "fixture.json").write_text(json.dumps({"items": list(items), **fx}, ensure_ascii=False), "utf-8")
        script = _rw_hook_funcs() + f"""
hb() {{ print -r -- "$1" >> "{self.d}/hb.log"; }}
notify() {{ print -r -- "$1|$2" >> "{self.d}/notify.log"; }}
cd "{self.d}"; LOG_DIR="{self.d}/logs"; mkdir -p "$LOG_DIR"; ET_DATE=2026-10-15; ET_MIN={et_min}; PY="{self.fake}"
riskwatch_bucket 585 945
"""
        env = {**os.environ, "RW_ROOT": str(ROOT), "RW_SANDBOX": str(self.d)}
        subprocess.run(["zsh", "-c", script], check=True, env=env)

    def rd(self, n):
        f = self.d / n
        return f.read_text("utf-8") if f.exists() and f.is_file() else ""

    def calls(self):
        return self.rd("calls.log").split()

    def ok(self, b):
        return (self.d / "logs" / f".riskwatch_2026-10-15_b{b}.ok").exists()

    def acks(self):
        return _jl(self.d / "rw" / "acks.jsonl")

    def outbox(self):
        return _jl(self.d / "rw" / "outbox.jsonl")



def test_bucket_outside_window_does_nothing(tmp_path):
    sb = Sandbox(tmp_path)
    for m in (584, 960):                                  # 09:44 之前、16:00 起
        sb.wake(m, [URG])
    assert sb.calls() == [] and sb.rd("notify.log") == ""


def test_bucket_ok_is_silent(tmp_path):
    sb = Sandbox(tmp_path)
    sb.wake(945 + 10)                                     # 15:55 → 最后一个桶（15:45）
    assert sb.calls() == ["run", "notify-text"] and sb.rd("notify.log") == ""
    assert sb.ok(12)


def test_bucket_deliver_fails_twice_retries_then_acks_once(tmp_path):
    """同桶连续两次投递失败都重试；恢复后只确认一次；之后同桶不再跑、下一桶同条件不再提醒。"""
    sb = Sandbox(tmp_path)
    sb.inbox_broken(True)
    sb.wake(680, [URG]); sb.wake(690, [URG])              # 11:20、11:30 → 同一桶 b3（11:15–11:44）
    assert sb.calls() == ["run", "notify-text", "deliver"] * 2
    assert not sb.ok(3) and sb.acks() == [] and len(sb.outbox()) == 1   # 重复巡检不重复入箱
    assert sb.rd("notify.log").count("合成告警甲") == 2                 # 弹窗成功也不算送达
    assert sb.rd("hb.log").count("送达未确认") == 2
    sb.inbox_broken(False)
    sb.wake(700, [URG])
    assert sb.ok(3) and len(sb.acks()) == 1
    assert sb.rd("rw/inbox_2026-10-15.log").count("合成告警甲") == 1
    n = len(sb.calls())
    sb.wake(702, [URG])                                   # 同桶已成功：不再跑
    assert len(sb.calls()) == n
    sb.wake(710, [URG])                                   # 下一桶 b4，条件仍在：已送达 → 安静
    assert sb.calls()[n:] == ["run", "notify-text"] and sb.ok(4)
    assert sb.rd("notify.log").count("合成告警甲") == 3 and len(sb.acks()) == 1
    assert "USO" not in sb.rd("hb.log") and "合成告警甲" not in sb.rd("hb.log")   # 公开心跳不含仓位/告警原文


def test_bucket_condition_gone_still_delivers(tmp_path):
    sb = Sandbox(tmp_path)
    sb.inbox_broken(True)
    sb.wake(680, [URG])
    assert not sb.ok(3)
    sb.inbox_broken(False)
    sb.wake(690, [])                                      # 第二轮条件消失
    assert sb.calls()[-3:] == ["run", "notify-text", "deliver"]
    assert "合成告警甲" in sb.rd("rw/inbox_2026-10-15.log") and len(sb.acks()) == 1 and sb.ok(3)


def test_bucket_notify_ok_but_inbox_write_fails_not_acked(tmp_path):
    sb = Sandbox(tmp_path)
    sb.inbox_broken(True)
    sb.wake(680, [URG])
    assert "合成告警甲" in sb.rd("notify.log")
    assert sb.acks() == [] and not sb.ok(3)
    assert (tmp_path / "logs" / ".riskwatch_fail_2026-10-15").read_text() == "x"


def test_bucket_run_rc0_but_result_missing_or_corrupt_is_crashed(tmp_path):
    for brk in ("missing", "corrupt"):
        sb = Sandbox(tmp_path / brk)
        sb.wake(680, [URG], break_result=brk)
        assert sb.calls() == ["run", "notify-text"]       # run 本身 rc=0 complete
        assert not sb.ok(3) and "result=crashed" in sb.rd("hb.log")
        sb.wake(690, [URG])                               # 下次唤醒重试：告警仍在发件箱，照样投递
        assert sb.ok(3) and len(sb.acks()) == 1


def test_bucket_partial_run_still_dispatches_but_no_sentinel(tmp_path):
    sb = Sandbox(tmp_path)
    sb.wake(680, [URG], incomplete=["合成：缺标的价"])
    assert sb.calls() == ["run", "notify-text", "deliver"] and len(sb.acks()) == 1
    assert not sb.ok(3) and "overall=partial" in sb.rd("hb.log")


def test_bucket_failures_retry_then_fallback_once(tmp_path):
    sb = Sandbox(tmp_path)
    for m in (680, 685, 690, 695):
        sb.wake(m, [], problems=["合成：持仓读不到"])
    assert not sb.ok(3)
    fail = sb.rd("data/reports/FAILURE_2026-10-15.txt")
    assert fail.count("持仓风险巡检当天 3 次未完成") == 1 and "SYN" not in fail and "合成" not in fail   # 公开兜底不含仓位
    assert sb.rd("notify.log").count("持仓风险巡检连续未完成") == 1


# —— 计划文件损坏（Codex 054 R1）：损坏 ≠ 没有计划 ——

def test_cli_corrupt_plans_is_problem_and_plan_rules_undecided(cli):
    q = {"USO261120C165000.US": {"bid": 2.2, "ask": 2.3, "error": ""},
         "USO261120C160000.US": {"bid": 3.5, "ask": 3.7, "error": ""}}
    ok = cli.collect_and_evaluate(NOW, deps=_deps(q))                       # 没有计划文件：合法，计划规则不适用
    assert ok["problems"] == [] and ok["incomplete"] == []
    assert {k: ok["coverage"][0]["rules"][k]["status"] for k in ("stop", "invalidation", "review")} == \
        {"stop": "not_applicable", "invalidation": "not_applicable", "review": "not_applicable"}
    bad = cli.collect_and_evaluate(NOW, deps=_deps(q, plans=lambda: ({}, "合成：计划 schema 不合格")))
    assert "plans_invalid" in {i.key for i in bad["items"]} and bad["problems"]
    rules = bad["coverage"][0]["rules"]
    assert all(rules[k]["status"] == "missing" and rules[k]["input"] == "plan" for k in ("stop", "invalidation", "review"))
    assert rules["expiry"]["status"] == "checked" and rules["take_profit"]["status"] == "checked"   # 不依赖计划的照常检查
    assert any(x.startswith("实盘") and "计划" in x for x in bad["incomplete"])


def test_cli_run_corrupt_plans_partial_rc1_then_recovers(cli, tmp_path, monkeypatch):
    """唯一故障 = 计划文件损坏，走真实 load_plans → collect → cmd_run：partial/rc1、提醒照样入箱；修好后同样输入 complete/rc0，发件箱只追加。"""
    q = {"USO261120C165000.US": {"bid": 2.2, "ask": 2.3, "error": ""},
         "USO261120C160000.US": {"bid": 3.5, "ask": 3.7, "error": ""}}
    d = _deps(q)
    d.pop("plans")
    orig = cli.collect_and_evaluate
    monkeypatch.setattr(cli, "collect_and_evaluate", lambda: orig(NOW, deps=d))
    res, st = tmp_path / "res.json", tmp_path / "st.json"
    args = SimpleNamespace(result_file=str(res), status_file=str(st), run_id="r1")
    (tmp_path / "plans.json").write_text("{corrupt", "utf-8")
    assert cli.cmd_run(args) == 1
    assert json.loads(st.read_text())["overall"] == "partial"
    out = json.loads(res.read_text())
    assert out["problems"] and [i["key"] for i in out["pending"]] == ["plans_invalid"]
    n0 = _jl(tmp_path / "rw" / "outbox.jsonl")
    (tmp_path / "plans.json").write_text(json.dumps({"schema": "position-plans-v1", "plans": {}}), "utf-8")
    assert cli.load_plans() == ({}, None)
    assert cli.cmd_run(args) == 0
    assert json.loads(st.read_text())["overall"] == "complete"
    assert _jl(tmp_path / "rw" / "outbox.jsonl")[:len(n0)] == n0                # 失败那轮的提醒没被抹掉
    assert [i["key"] for i in json.loads(res.read_text())["pending"]] == ["plans_invalid"]   # 未送达的仍待投递


def test_bucket_corrupt_plans_dispatches_but_no_sentinel_then_recovers(tmp_path):
    """真实 riskwatch_bucket + 真实 cmd_run/collect/load_plans，唯一故障是沙盒 plans.json 损坏。"""
    sb = Sandbox(tmp_path)
    (tmp_path / "plans.json").write_text("{corrupt", "utf-8")
    sb.wake(680, real=True)
    assert sb.calls() == ["run", "notify-text", "deliver"]
    assert [r["key"] for r in sb.outbox()] == ["plans_invalid"] and len(sb.acks()) == 1
    assert not sb.ok(3) and "overall=partial" in sb.rd("hb.log")
    (tmp_path / "plans.json").write_text(json.dumps({"schema": "position-plans-v1", "plans": {}}), "utf-8")
    sb.wake(681, real=True)                               # 同一个桶，修好后重试
    assert sb.ok(3) and sb.calls()[-2:] == ["run", "notify-text"]
    assert [r["key"] for r in sb.outbox()] == ["plans_invalid"] and len(sb.acks()) == 1
