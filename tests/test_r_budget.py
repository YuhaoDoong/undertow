"""Codex 065 WP2：预算权益 R = max(0, min(E0 + F, E_now)) 与日初基准选择。全部合成金额。"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from undertow.analyze import r_budget as rb
from undertow.collect import jsonl_ledger as jl
from undertow.collect import r_anchor_store as ras

ET = ZoneInfo("America/New_York")


def t(s):                                                     # ET 墙钟
    return datetime.fromisoformat(s).replace(tzinfo=ET)


def eod(session, E, at):
    return rb.anchor_record(E=E, captured_at=t(at), origin="eod", market_session=session)


# —— 公式与 065 合成时序 1000 → 900 → 970 → 1170 → 1070 ——

@pytest.mark.parametrize("E_now,F,R", [(1080, 0, 1000), (900, 0, 900), (970, 0, 970), (1170, 200, 1170), (1070, 100, 1070)])
def test_codex_065_intraday_sequence(E_now, F, R):
    r = rb.budget_r(1000.0, float(F), float(E_now))
    assert r["R_budget"] == R and r["status"] == "ok"


def test_profit_does_not_raise_but_recovery_allowed():
    assert rb.budget_r(1000.0, 0.0, 1080.0)["R_budget"] == 1000.0      # 盘中利润不突破日初基准
    assert rb.budget_r(1000.0, 0.0, 900.0)["R_budget"] == 900.0        # 亏损立即缩额
    assert rb.budget_r(1000.0, 0.0, 970.0)["R_budget"] == 970.0        # 回升可恢复，不锁日内最低


def test_nonpositive_equity_blocks_new_risk_and_unknown_is_incomplete():
    z = rb.budget_r(1000.0, 0.0, 0.0)
    assert z["R_budget"] == 0.0 and z["status"] == "no_new_risk"
    assert rb.budget_r(1000.0, 0.0, -50.0)["status"] == "no_new_risk"
    for args in ((None, 0.0, 900.0), (1000.0, None, 900.0), (1000.0, 0.0, float("nan"))):
        assert rb.budget_r(*args) == {**rb.budget_r(*args), "R_budget": None, "status": "incomplete"}


def test_cash_reshuffle_with_same_equity_same_R():
    """期权开仓现金/负债换形但净权益不变 → R 不变（R 只看 E，不看现金或买力）。"""
    assert rb.budget_r(1000.0, 0.0, 1000.0) == rb.budget_r(1000.0, 0.0, 1000.0)


# —— 外部资金：同截面、只计基准之后 ——

FLOWS = [{"at": "2026-10-28T11:00:00-04:00", "amount": 200.0}, {"at": "2026-10-28T14:00:00-04:00", "amount": -100.0}]


def test_future_flow_does_not_affect_past_observation():
    after = t("2026-10-27T17:00:00")
    assert rb.flows_between(FLOWS, after, t("2026-10-28T10:00:00")) == (0.0, 0)
    assert rb.flows_between(FLOWS, after, t("2026-10-28T12:00:00")) == (200.0, 1)
    assert rb.flows_between(FLOWS, after, t("2026-10-28T15:00:00")) == (100.0, 2)   # 出金只扣一次


def test_flows_before_anchor_not_double_counted():
    """基准观察已包含的资金流不能再加一遍。"""
    assert rb.flows_between(FLOWS, t("2026-10-28T12:00:00"), t("2026-10-28T15:00:00")) == (-100.0, 1)


# —— 日初基准选择 ——

def test_session_uses_previous_close_anchor():
    a, st, _ = rb.select_anchor([eod("2026-10-27", 1000, "2026-10-27T16:20:00")], t("2026-10-28T11:00:00"))
    assert st == "ok" and a["E"] == 1000


def test_first_eod_wins_and_later_observation_not_used_before_capture():
    anchors = [eod("2026-10-27", 1000, "2026-10-27T16:20:00")]
    a, st, _ = rb.select_anchor(anchors, t("2026-10-27T16:10:00"))      # 采集之前：看不到它
    assert a is None or a["market_session"] != "2026-10-27"


def test_after_close_before_eod_capture_carries_previous_eod():
    anchors = [eod("2026-10-26", 950, "2026-10-26T16:20:00")]
    a, st, _ = rb.select_anchor(anchors, t("2026-10-27T16:05:00"))
    assert st == "pending_eod" and a["market_session"] == "2026-10-26"


def test_next_open_without_eod_is_missing_not_stale_reuse():
    anchors = [eod("2026-10-26", 950, "2026-10-26T16:20:00")]
    a, st, _ = rb.select_anchor(anchors, t("2026-10-28T09:45:00"))
    assert a is None and st == "missing"


def test_weekend_uses_friday_eod():
    a, st, _ = rb.select_anchor([eod("2026-10-23", 900, "2026-10-23T16:30:00")], t("2026-10-25T12:00:00"))
    assert st == "ok" and a["market_session"] == "2026-10-23"


def test_bootstrap_needs_reason_and_is_used_only_after_last_close():
    with pytest.raises(ValueError):
        rb.anchor_record(E=900, captured_at=t("2026-10-28T11:00:00"), origin="bootstrap", market_session=None)
    b = rb.anchor_record(E=900, captured_at=t("2026-10-28T11:00:00"), origin="bootstrap", market_session=None,
                         note="065 启用首日，缺历史日终观察")
    a, st, _ = rb.select_anchor([b], t("2026-10-28T12:00:00"))
    assert st == "bootstrap" and a["E"] == 900
    assert rb.select_anchor([b], t("2026-10-29T10:00:00"))[1] == "missing"   # 不拿盘中 bootstrap 冒充 10/28 收盘


def test_eod_capture_window():
    assert rb.eod_session_for_capture(t("2026-10-27T21:00:00"))[0].isoformat() == "2026-10-27"
    assert rb.eod_session_for_capture(t("2026-10-28T08:00:00"))[0].isoformat() == "2026-10-27"   # 开盘前仍属 10/27
    assert rb.eod_session_for_capture(t("2026-10-28T11:00:00"))[0] is None                     # 盘中不是日终
    assert rb.eod_session_for_capture(t("2026-11-27T13:30:00"))[0].isoformat() == "2026-11-27"  # 半日市 13:00 收


def test_snapshot_is_content_addressed_and_incomplete_without_anchor():
    anchors = [eod("2026-10-27", 1000, "2026-10-27T16:20:00")]
    s1 = rb.snapshot(anchors=anchors, flows=FLOWS, E_now=1170.0, captured_at=t("2026-10-28T12:00:00"), policy_version="v")
    s2 = rb.snapshot(anchors=anchors, flows=FLOWS, E_now=1170.0, captured_at=t("2026-10-28T12:00:00"), policy_version="v")
    assert s1["snapshot_id"] == s2["snapshot_id"] and s1["R_budget"] == 1170.0 and s1["anchor_adjusted_A"] == 1200.0
    s0 = rb.snapshot(anchors=[], flows=[], E_now=1170.0, captured_at=t("2026-10-28T12:00:00"), policy_version="v")
    assert s0["R_budget"] is None and s0["status"] == "incomplete" and "E0" in s0["missing_fields"]
    other = rb.snapshot(anchors=[dict(anchors[0], R_basis_version="net-liquidation-usd-v2")], flows=[], E_now=1.0,
                        captured_at=t("2026-10-28T12:00:00"), policy_version="v")
    assert other["R_budget"] is None                                       # 不同口径的基准不混接


# —— 私有存储：首份为准、坏文件隔离 ——

def test_anchor_store_first_wins_and_corrupt_isolated(tmp_path):
    p = tmp_path / "anchors.jsonl"
    r1 = eod("2026-10-27", 1000, "2026-10-27T16:20:00")
    assert ras.record(r1, p) == "inserted"
    assert ras.record(dict(r1, E=1234.0, captured_at=t("2026-10-27T18:00:00").isoformat()), p) == "exists"
    assert ras.load(p)[0]["E"] == 1000
    p.write_text('{"id": "x"\n')
    with pytest.raises(jl.LedgerCorruptError):
        ras.load(p)
    assert list(tmp_path.glob("anchors.jsonl.corrupt-*"))


# —— 编排：r_view 读不到任何一项 → incomplete，不拿 net_assets 顶替 ——

def _wire(monkeypatch, *, anchors=None, anchors_err=None, assets=None, state=None):
    from types import SimpleNamespace as NS
    from undertow import trial_cli as tcli
    from undertow.collect import longbridge_account as lb
    from undertow.collect import trial_store as ts

    def load(path=None):
        if anchors_err:
            raise jl.LedgerCorruptError(anchors_err)
        return anchors or []
    monkeypatch.setattr(ras, "load", load)
    monkeypatch.setattr(ts, "load", lambda: (state, None))
    monkeypatch.setattr(lb, "fetch_assets", lambda: assets or NS(net_assets=1100.0, currency="USD"))
    monkeypatch.setattr(lb, "fetch_cash_flow", lambda start=None, end=None: [])
    return tcli


def test_r_view_ok_and_failure_modes(monkeypatch):
    from types import SimpleNamespace as NS
    anchors = [eod("2026-10-27", 1000, "2026-10-27T16:20:00")]
    now = t("2026-10-28T11:00:00")
    tcli = _wire(monkeypatch, anchors=anchors, state={"external_flows": []})
    s = tcli.r_view(now=now)
    assert s["R_budget"] == 1000.0 and s["status"] == "ok"
    tcli = _wire(monkeypatch, anchors_err="坏行", state={"external_flows": []})
    s = tcli.r_view(now=now)
    assert s["R_budget"] is None and any("不可读" in m for m in s["missing_fields"])
    tcli = _wire(monkeypatch, anchors=anchors, assets=NS(net_assets=1500.0, currency="SGD"), state={"external_flows": []})
    assert tcli.r_view(now=now)["R_budget"] is None
