"""方向次要分析 dir-analysis-v1.1（预登记，取代 v1）：准入（D01）、机会表（D02）、共同样本（D03）、
判定用语与依赖指纹（D05）、配对定义、区间传播、正式样本、描述性诊断。"""
from datetime import date, datetime, timedelta, timezone

import pytest

from undertow.analyze import shadow as sh
from undertow.analyze import shadow_direction as sd
from undertow.core import market_calendar as mc

B = sh.CONFIG["primary_basis"]
AS_OF = date(2026, 12, 20)          # 探索期、样本都已成熟

_SAME = object()


def _o(v, lo=_SAME, status="ok"):
    """lo 缺省 = 点值；lo=None = 下界未知（残腿实值、处置未知）。"""
    lo = v if lo is _SAME else lo
    return {"pnl": {B: v if lo == v else None}, "pnl_bounds": {B: [lo, v]}, "pnl_residual0": {B: v},
            "max_risk": {B: 100.0}, "credit": {B: 20.0}, "width_usd": 100.0, "exit_mode": {B: "both_legs"},
            "status": {B: status}}


def _flow(direction, day):
    d = date.fromisoformat(day)
    return {"call_direction": direction,
            "available_at": datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc).isoformat(),  # ET 06:00
            "snapshot_sha": "a" * 16, "prev_snapshot_sha": "b" * 16, "ledger_row_sha": "c" * 16,
            "ledger_code_sha": sd.ANALYSIS["call_code_sha"], "mapping": sd.ANALYSIS["mapping_version"]}


def _row(day, direction, vals, inst="gold", **over):
    """vals: {(side, rule): 损益美元 | (上界, 下界)}；最大风险 100 → Y = 损益/100。"""
    d = date.fromisoformat(day)
    exp = mc.next_trading_day(mc.next_trading_day(mc.next_trading_day(d)))
    legs = [{"leg_id": f"{s}-{r}", "side": s, "rule": r, "status": "candidate", "expiry": exp.isoformat()}
            for (s, r) in vals]
    row = {"session": day, "instrument": inst, "identity": {"mode": "prospective", "status": "certified"},
           "recorded_at": datetime(d.year, d.month, d.day, 11, 0, tzinfo=timezone.utc).isoformat(),  # ET 07:00
           "decision": {"flow": _flow(direction, day), "base_close": 100.0, "atr14": 2.0}, "legs": legs,
           "outcome": {f"{s}-{r}": (_o(*v) if isinstance(v, tuple) else _o(v)) for (s, r), v in vals.items()}}
    row.update(over)
    return row


def _days(n, start=date(2026, 9, 28)):
    return [d.isoformat() for d in mc.trading_days(start, date(2026, 12, 31))][:n]


def st_mean(x):
    return sum(x) / len(x)


def test_frozen_analysis_identity():
    a = sd.ANALYSIS
    assert a["version"].startswith("dir-analysis-v1.1") and a["supersedes"] == "dir-analysis-v1-20260926"
    assert a["base_config_version"] == sh.CONFIG["version"] and a["family_size"] == 14
    assert a["alpha_one_sided"] == pytest.approx(0.05 / 14) and a["eligible_from"] == "2026-09-28"
    assert a["mapping"] == {"偏多": "P", "偏空": "C"} and a["mapping_version"] == "dir-map-v1"


def test_frozen_call_code_sha_matches_current_algorithm():
    from undertow.analyze import signal_ledger as sl
    assert sl.call_code_sha() == sd.ANALYSIS["call_code_sha"]


def test_aligned_sides_mapping_and_none():
    assert sd.aligned_sides(_row("2026-09-28", "偏多", {})) == ("P", "C")
    assert sd.aligned_sides(_row("2026-09-28", "偏空", {})) == ("C", "P")
    assert sd.aligned_sides(_row("2026-09-28", "中性", {})) is None
    assert sd.aligned_sides({"decision": {}}) is None


# ── D01 准入 ──

def _flow_over(day, **kw):
    r = _row(day, "偏多", {("P", "B1"): 10, ("C", "B1"): 0})
    r["decision"]["flow"].update(kw)
    return r


@pytest.mark.parametrize("mut,reason", [
    (lambda r: r["decision"]["flow"].update(available_at=None), "available_at 缺失"),
    (lambda r: r["decision"]["flow"].update(available_at="2026-09-28T06:00:00"), "available_at 缺失或无时区"),
    (lambda r: r["decision"]["flow"].update(available_at="2026-09-28T11:30:00+00:00"), "不早于 recorded_at"),
    (lambda r: r["decision"]["flow"].update(snapshot_sha=None), "snapshot_sha 缺失"),
    (lambda r: r["decision"]["flow"].update(prev_snapshot_sha=""), "prev_snapshot_sha 缺失"),
    (lambda r: r["decision"]["flow"].update(ledger_row_sha=None), "ledger_row_sha 缺失"),
    (lambda r: r["decision"]["flow"].update(mapping="dir-map-v0"), "映射版本"),
    (lambda r: r["decision"]["flow"].update(ledger_code_sha=None), "方向算法指纹"),
    (lambda r: r["decision"]["flow"].update(ledger_code_sha="deadbeef"), "方向算法指纹"),
    (lambda r: r.update(recorded_at="2026-09-28T13:31:00+00:00"), "09:30"),        # ET 09:31
    (lambda r: r.update(recorded_at="2026-09-28T07:00:00"), "无时区"),
    (lambda r: r.update(identity={"mode": "prospective", "status": "uncertified"}), "未认证"),
    (lambda r: r.update(identity={"mode": "replay", "status": "certified"}), "非前瞻"),
])
def test_d01_identity_failures_are_counted_not_dropped(mut, reason):
    r = _row("2026-09-28", "偏多", {("P", "B1"): 10, ("C", "B1"): 0})
    assert sd.eligibility(r) == ("ok", "")
    mut(r)
    cat, why = sd.eligibility(r)
    assert cat == "identity_fail" and reason in why
    ot = sd.opportunity_table([r], "gold", "H-dir", basis=B, as_of=date(2026, 9, 28))
    assert ot["table"]["identity_fail"] == 1 and sum(ot["reasons"].values()) == 1


def test_d01_uncertified_row_is_identity_fail_even_without_direction():
    r = _row("2026-09-28", None, {}, identity={"mode": "prospective", "status": "uncertified"})
    assert sd.eligibility(r)[0] == "identity_fail"
    assert sd.eligibility(_row("2026-09-28", None, {}))[0] == "no_direction"


# ── D02 机会表 ──

def test_d02_calendar_opportunity_table_every_day_once():
    days = _days(10)
    rows = [_row(days[0], "偏多", {("P", "B1"): 10, ("C", "B1"): 0}),                  # complete
            _row(days[1], None, {("P", "B1"): 10, ("C", "B1"): 0}),                    # no_direction
            _row(days[2], "偏多", {("P", "B1"): 10}),                                   # no_candidate（缺 C-B1）
            _row(days[3], "偏多", {("P", "B1"): (10, None), ("C", "B1"): 0}),          # unbounded
            _row(days[4], "偏多", {("P", "B1"): 10, ("C", "B1"): 0}, outcome={}),      # 成熟但缺结果
            _row(days[5], "偏多", {("P", "B1"): 10, ("C", "B1"): 0},
                 outcome={"P-B1": _o(None, None, "entry_missing"), "C-B1": _o(0)}),     # entry_missing
            ]
    ot = sd.opportunity_table(rows, "gold", "H-dir", basis=B, as_of=AS_OF)
    t = ot["table"]
    assert ot["days"] == len(mc.trading_days(date(2026, 9, 28), AS_OF))
    assert sum(t.values()) == ot["days"]
    assert (t["complete"], t["no_direction"], t["no_candidate"], t["unbounded"],
            t["matured_missing_result"], t["entry_missing"]) == (1, 1, 1, 1, 1, 1)
    assert t["not_generated"] == ot["days"] - 6
    # 可配对率 = (完整+无界)/(成熟缺结果+入场缺失+未定价+无界+完整) = 2/4
    assert ot["pairable_rate"] == pytest.approx(0.5)


def test_d02_missing_result_before_exit_is_immature():
    d = _days(1)[0]
    r = _row(d, "偏多", {("P", "B1"): 10, ("C", "B1"): 0}, outcome={})
    ot = sd.opportunity_table([r], "gold", "H-dir", basis=B, as_of=date.fromisoformat(d))
    assert ot["table"]["immature"] == 1 and ot["pairable_rate"] is None


def test_d02_pairable_rate_below_threshold_is_insufficient():
    rows = [_row(d, "偏多", {("P", "B1"): 10, ("C", "B1"): 0}, outcome={} if i % 3 else None)
            for i, d in enumerate(_days(30))]
    for r in rows:
        if r["outcome"] is None:
            r["outcome"] = {"P-B1": _o(12), "C-B1": _o(0)}
    rep = sd.instrument_report(rows, "gold", as_of=AS_OF)
    assert rep["H-dir"]["opportunities"]["pairable_rate"] < 0.5
    assert rep["H-dir"]["verdict"].endswith("证据不足（可配对率低于设计门槛）")


# ── 配对与判定 ──

def test_hdir_and_hwall_pairs():
    rows = []
    for i, d in enumerate(_days(30)):
        noise = (i % 3 - 1) * 2
        rows.append(_row(d, "偏多" if i % 2 else "偏空", {
            ("P", "B1"): 15 + noise, ("C", "B1"): 5 + noise, ("P", "A"): 18 + noise, ("C", "A"): 4 + noise}))
    rep = sd.instrument_report(rows, "gold", as_of=AS_OF)
    exp_hdir = st_mean([(15 - 5) if i % 2 else (5 - 15) for i in range(30)]) / 100
    assert rep["H-dir"]["ci"]["mean"] == pytest.approx(exp_hdir)
    assert rep["H-dir"]["n_pairs"] == 30 and rep["H-dir"]["opportunities"]["table"]["complete"] == 30
    assert rep["identity"] == "exploratory" and rep["H-dir"]["verdict"].startswith("探索·")
    exp_wall = st_mean([(18 - 15) if i % 2 else (4 - 5) for i in range(30)]) / 100
    assert rep["H-wall|dir"]["ci"]["mean"] == pytest.approx(exp_wall)


def test_no_direction_rows_only_in_denominator():
    rows = [_row(d, None, {("P", "B1"): 10, ("C", "B1"): 0}) for d in _days(25)]
    rep = sd.instrument_report(rows, "gold", as_of=AS_OF)
    assert rep["H-dir"]["opportunities"]["table"]["no_direction"] == 25 and rep["H-dir"]["n_pairs"] == 0
    assert rep["H-dir"]["verdict"].endswith("证据不足（尚无已成熟、可评估的机会）")


def test_unbounded_residual_makes_hdir_undecided():
    rows = [_row(d, "偏多", {("P", "B1"): (10, None), ("C", "B1"): 0}) for d in _days(25)]
    rep = sd.instrument_report(rows, "gold", as_of=AS_OF)
    assert rep["H-dir"]["n_unbounded"] == 25
    assert rep["H-dir"]["verdict"].endswith("未决（残腿处置未知）")


def test_d05_judge_wording_separates_unproven_from_excluded():
    d = sd.ANALYSIS["economic_delta"]
    j = lambda lo, hi, n=25, rate=0.9: sd.judge({"status": "ok", "lo": lo, "hi": hi}, n, rate)
    assert j(d + 0.01, 0.2).startswith("支持")
    assert j(0.01, 0.2) == "统计为正，尚未证实超过经济门槛"
    assert j(0.005, d - 0.01) == "统计为正，但排除实用增量（上界 < 经济门槛）"
    assert j(-0.05, d - 0.01) == "排除实用增量（校正后上界 < 经济门槛）"
    assert j(-0.2, -0.01).startswith("不支持")
    assert j(-0.05, 0.2) == "未决"
    assert j(0.05, 0.2, n=10).startswith("证据不足（配对日期")
    assert j(0.05, 0.2, rate=0.3).startswith("证据不足（可配对率")
    assert j(0.05, 0.2, rate=None) == "证据不足（尚无已成熟、可评估的机会）"


def test_formal_sample_window_and_beyond_formal_date():
    rows = [_row("2026-09-25", "偏多", {("P", "B1"): 10, ("C", "B1"): 0}),     # 纳入起点之前 → 不在日历窗口
            _row("2026-12-30", "偏多", {("P", "B1"): 10, ("C", "B1"): 0})]     # 到期在检验日之后
    ot = sd.opportunity_table(rows, "gold", "H-dir", basis=B, as_of=date(2027, 1, 5))
    assert ot["days"] == len(mc.trading_days(date(2026, 9, 28), date(2026, 12, 31)))
    assert ot["table"]["beyond_formal_date"] == 1 and ot["table"]["complete"] == 0
    ot2 = sd.opportunity_table(rows, "gold", "H-dir", basis=B, as_of=date(2026, 12, 30))
    assert ot2["table"]["beyond_formal_date"] == 0


# ── D03 共同样本与描述 ──

def test_d03_common_sample_uses_same_keys():
    days = _days(6)
    rows = [_row(days[0], "偏多", {("P", "B1"): 10, ("C", "B1"): -20}),
            _row(days[1], "偏空", {("P", "B1"): -30, ("C", "B1"): 5}),
            _row(days[2], "偏多", {("P", "B1"): 10, ("C", "B1"): (5, None)}),   # C 无点值 → 不进共同键
            _row(days[3], "偏多", {("P", "B1"): 10})]                            # 缺 C → 不进
    cs = sd.instrument_report(rows, "gold", as_of=AS_OF)["common_sample"]["B1"]
    assert cs["keys"] == days[:2] and cs["n_keys"] == 2
    for k in ("aligned", "counter", "always_put", "always_call"):
        assert cs[k]["n"] == 2
    assert cs["aligned"]["mean"] == pytest.approx((0.10 + 0.05) / 2)
    assert cs["always_put"]["mean"] == pytest.approx((0.10 - 0.30) / 2)


def test_interaction_and_all_opportunities_shapes():
    rows = [_row(d, "偏多", {("P", "B1"): 10, ("C", "B1"): 0, ("P", "A"): 12, ("C", "A"): 1}) for d in _days(22)]
    rep = sd.instrument_report(rows, "gold", as_of=AS_OF)
    assert rep["interaction"]["n_pairs"] == 22 and rep["interaction"]["identity"] == "描述性"
    assert rep["all_opportunities"]["A_aligned"]["n"] == 22
    assert rep["direct_direction"]["n"] == 0 and rep["direct_direction"]["hit_rate"] is None


def test_diagnostics_aligned_to_used_attempt(monkeypatch):
    d = _days(1)[0]
    r = _row(d, "偏空", {("C", "B1"): 10, ("P", "B1"): 0})
    leg = r["legs"][0]
    x = mc.prev_trading_day(date.fromisoformat(leg["expiry"])).isoformat()
    r["windows"] = {f"{d}|open": {"attempts": [{"started_at": "t0a", "underlying": {"freshest": 999.0}},
                                               {"started_at": "t0b", "underlying": {"freshest": 99.0}}]},
                    f"{x}|close": {"attempts": [{"started_at": "t1", "underlying": {"freshest": 96.0}}]}}
    used = {f"{d}|open": "t0b", f"{x}|close": "t1"}
    monkeypatch.setattr(sd.sh, "window_leg", lambda row, lg, wk, p: {"status": "valid", "at": used[wk]})
    dg = sd.diagnostics([r])
    # 偏空 d=−1：pre = −(99−100)/2 = +0.5；post = −(96−99)/2 = +1.5（第一次尝试 999 不是被采用的那次）
    assert dg["pre_move_ATR"]["mean"] == pytest.approx(0.5) and dg["post_move_ATR"]["mean"] == pytest.approx(1.5)
    assert dg["version"] == "dir-diagnostics-v1" and "未知" in dg["split_close_to_available_to_entry"]
    dd = sd.direct_direction([r])
    assert dd == {**dd, "n": 1, "hit_rate": 1.0}


def test_diagnostics_unknown_when_no_valid_entry(monkeypatch):
    r = _row(_days(1)[0], "偏多", {("P", "B1"): 10, ("C", "B1"): 0})
    monkeypatch.setattr(sd.sh, "window_leg", lambda *a: {"status": "missing"})
    dg = sd.diagnostics([r])
    assert dg["n_unknown"] == 1 and dg["pre_move_ATR"]["n"] == 0


def test_dependency_fingerprints_cover_declared_functions():
    fp = sd.dependency_fingerprints()
    assert set(fp) >= {"shadow._vals", "shadow._norm", "shadow.interval_ci", "shadow.block_bootstrap",
                       "shadow.formal_identity", "shadow.window_leg", "core.market_calendar", "shadow.config_hash"}
    assert fp["shadow.config_hash"] == "9e60a71ac3cec0f3"
