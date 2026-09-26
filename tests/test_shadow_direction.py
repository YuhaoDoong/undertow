"""方向次要分析 dir-analysis-v1.2（预登记，取代 v1.1/v1）：准入（D01/R01/R03）、机会表与截止（D02/R04）、
覆盖与结论范围（R02）、共同样本与界限版（D03）、判定用语与依赖指纹（D05）、配对、区间传播、正式样本、诊断。"""
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
    cur = datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc).isoformat()                  # ET 06:00
    prev = (datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc) - timedelta(days=1)).isoformat()
    return {"call_direction": direction, "source_captured_at": {"current": cur, "previous": prev},
            "available_at": cur,
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
    assert a["version"].startswith("dir-analysis-v1.2") and a["supersedes"] == "dir-analysis-v1.1-20260926"
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
    (lambda r: r["decision"]["flow"].update(available_at="2026-09-28T06:00:00"), "available_at 缺失、无时区"),
    (lambda r: r["decision"]["flow"].update(available_at="2026-09-28T09:00:00+00:00"), "不等于两份来源"),
    (lambda r: r["decision"]["flow"]["source_captured_at"].update(previous=None), "previous 的抓取时刻缺失"),
    (lambda r: r["decision"]["flow"]["source_captured_at"].update(current="2026-09-28T10:00:00"), "current 的抓取时刻缺失或无时区"),
    (lambda r: r["decision"]["flow"]["source_captured_at"].update(current="2026-09-28T11:30:00+00:00"), "不早于 recorded_at"),
    (lambda r: r["decision"].update(flow=None), "信号来源缺失"),
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


def test_r03_neutral_with_bad_source_is_identity_fail_not_abstention():
    """Codex 012 R03：中性 + ledger_code_sha 缺失 → 身份失败；中性 + 来源齐全 → 才是弃权日。"""
    r = _row("2026-09-28", "中性", {})
    assert sd.eligibility(r) == ("no_direction", "无方向（来源合格）")
    r["decision"]["flow"]["ledger_code_sha"] = None
    cat, why = sd.eligibility(r)
    assert cat == "identity_fail" and why.startswith("来源：")


def test_r01_flow_identity_rejects_missing_previous_capture_time(tmp_path):
    """Codex 012 R01：前一份快照没有抓取时刻 → available_at=None，经真实准入入口被拒。"""
    from undertow.shadow_cli import _flow_identity

    class Store:
        def __init__(self, times):
            self.times = times

        def captured_at(self, kind, sym, d):
            return self.times.get(d.isoformat())

        def path_of(self, kind, sym, d):
            p = tmp_path / f"{d.isoformat()}.json.gz"
            p.write_bytes(d.isoformat().encode())
            return p
    fr = {"date": "2026-09-28", "prev_date": "2026-09-25", "call_direction": "偏多",
          "call_code_sha": sd.ANALYSIS["call_code_sha"]}
    t_cur = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc).timestamp()
    t_prev = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc).timestamp()
    ok = _flow_identity(fr, Store({"2026-09-28": t_cur, "2026-09-25": t_prev}), "GLD", date(2026, 9, 28))
    bad = _flow_identity(fr, Store({"2026-09-28": t_cur}), "GLD", date(2026, 9, 28))
    assert ok["available_at"] == ok["source_captured_at"]["current"] and ok["source_captured_at"]["previous"]
    assert bad["available_at"] is None and bad["source_captured_at"]["previous"] is None
    for flow, want in ((ok, "ok"), (bad, "identity_fail")):
        r = _row("2026-09-28", "偏多", {("P", "B1"): 10, ("C", "B1"): 0})
        r["decision"]["flow"] = flow
        assert sd.eligibility(r)[0] == want


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


def test_r04_prefilled_outcome_cannot_cross_as_of_cutoff():
    """Codex 012 R04：退出日之前，即使数据文件里已有结果，也一律 immature，不进检验与描述。"""
    d = _days(1)[0]
    r = _row(d, "偏多", {("P", "B1"): 10, ("C", "B1"): 0, ("P", "A"): 12, ("C", "A"): 1})
    exit_day = mc.prev_trading_day(date.fromisoformat(r["legs"][0]["expiry"]))
    for as_of, want in ((exit_day - timedelta(days=1), "immature"), (exit_day, "immature"),
                        (mc.next_trading_day(exit_day), "complete")):
        ot = sd.opportunity_table([r], "gold", "H-dir", basis=B, as_of=as_of)
        assert ot["items"][0]["category"] == want, as_of
    rep = sd.instrument_report([r], "gold", as_of=exit_day)
    assert rep["common_sample"]["B1"]["n_keys"] == 0 and rep["all_opportunities"]["A_aligned"]["n"] == 0


def test_r02_collection_pending_only_until_window_closes():
    d = date(2026, 9, 29)
    before = datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)      # ET 09:00
    after = datetime(2026, 9, 29, 13, 31, tzinfo=timezone.utc)      # ET 09:31
    t0 = sd.opportunity_table([], "gold", "H-dir", basis=B, as_of=d, now=before)["table"]
    t1 = sd.opportunity_table([], "gold", "H-dir", basis=B, as_of=d, now=after)["table"]
    assert (t0["collection_pending"], t0["not_generated"]) == (1, 1)
    assert (t1["collection_pending"], t1["not_generated"]) == (0, 2)


def test_r02_sparse_collection_conditional_support_but_generalization_undecided():
    """Codex 012 R02 合成例：67 日只采到 20 日 → 条件配对率 1.0 可以判支持，但推广必须是「未决」。"""
    as_of = date(2027, 1, 5)
    days = _days(67)
    rows = [_row(d, "偏多", {("P", "B1"): 16 + (i % 2), ("C", "B1"): 0}) for i, d in enumerate(days[:20])]
    rep = sd.instrument_report(rows, "gold", as_of=as_of)
    h = rep["H-dir"]
    cov = h["opportunities"]["coverage"]
    assert h["opportunities"]["pairable_rate"] == 1.0 and h["verdict"].startswith("支持")
    assert h["verdict"].endswith("［条件样本内；全日历推广未决］")
    n_days = h["opportunities"]["days"]
    assert cov["collection"] == {"num": 20, "den": n_days, "rate": 20 / n_days}
    assert cov["end_to_end_paired"]["num"] == 20 and cov["integrity"] == "incomplete"
    assert cov["engineering_gaps"] == n_days - 20
    assert h["scope"]["conditional"].startswith("条件结论") and h["scope"]["generalization"].startswith("未决")


def test_r02_complete_coverage_generalization_statement():
    cov = sd.coverage({**dict.fromkeys(sd.CATEGORIES, 0), "complete": 5, "no_direction": 2})
    assert cov["integrity"] == "complete" and cov["direction_present"] == {"num": 5, "den": 7, "rate": 5 / 7}
    assert sd.scope(cov)["generalization"].startswith("覆盖完整")
    empty = sd.coverage(dict.fromkeys(sd.CATEGORIES, 0))
    assert empty["integrity"] == "empty" and sd.scope(empty)["generalization"] == "未决（尚无应采集日）"


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


def test_common_bounds_envelope_and_correct_relative_propagation():
    """Codex 012 第 5 项：X∈[0,10]、Y∈[−100,20] → X−Y ∈ [−20,110]；上界相减（−10）既非上界也非下界。"""
    d0, d1 = _days(2)
    rows = [_row(d0, "偏多", {("P", "B1"): (10, 0), ("C", "B1"): (20, -100)}),
            _row(d1, "偏多", {("P", "B1"): 5, ("C", "B1"): (5, None)})]
    rows[0]["outcome"]["P-B1"]["pnl_bounds"][B] = [0, 10]
    rows[0]["outcome"]["C-B1"]["pnl_bounds"][B] = [-100, 20]
    cb = sd.common_bounds(rows, B)["B1"]
    assert cb["keys"] == [d0, d1] and cb["n_unpriced"] == 0
    ab = cb["absolute"]["counter"]
    assert ab["n_lower_unknown"] == 1 and ab["mean"][0] is None and ab["mean"][1] == pytest.approx((0.20 + 0.05) / 2)
    assert ab["worst_k_mean"][0] is None
    al = cb["absolute"]["aligned"]
    assert al["mean"] == [pytest.approx((0 + 0.05) / 2), pytest.approx((0.10 + 0.05) / 2)]
    assert al["win_rate"] == [0.5, 1.0]
    rel = cb["relative"]["aligned_minus_counter"]
    # 第一日 [0−0.20, 0.10−(−1.00)] = [−0.20, 1.10]；第二日逆向下界未知 → 相对上界未知
    assert rel["mean"][0] == pytest.approx((-0.20 + (0.05 - 0.05)) / 2) and rel["mean"][1] is None
    one = sd.common_bounds(rows[:1], B)["B1"]["relative"]["aligned_minus_counter"]
    assert one["mean"] == [pytest.approx(-0.20), pytest.approx(1.10)]


def test_common_bounds_counts_unpriced_without_inventing_values():
    r = _row(_days(1)[0], "偏多", {("P", "B1"): 10, ("C", "B1"): 0})
    r["outcome"]["C-B1"] = _o(None, None, "cost_missing")
    r["outcome"]["C-B1"]["pnl_bounds"][B] = [None, None]
    cb = sd.common_bounds([r], B)["B1"]
    assert cb["n_keys"] == 0 and cb["n_unpriced"] == 1
