"""方向次要分析 dir-analysis-v1（预登记）：配对定义、无方向处理、区间传播、判定门槛、正式样本。"""
from datetime import date, timedelta

import pytest

from undertow.analyze import shadow as sh
from undertow.analyze import shadow_direction as sd
from undertow.core import market_calendar as mc

B = sh.CONFIG["primary_basis"]


_SAME = object()


def _o(v, lo=_SAME):
    """lo 缺省 = 点值；lo=None = 下界未知（残腿实值、处置未知）。"""
    lo = v if lo is _SAME else lo
    return {"pnl": {B: v if lo == v else None}, "pnl_bounds": {B: [lo, v]}, "pnl_residual0": {B: v},
            "max_risk": {B: 100.0}, "credit": {B: 20.0}, "width_usd": 100.0, "exit_mode": {B: "both_legs"},
            "status": {B: "ok"}}


def _row(day, direction, vals, inst="gold"):
    """vals: {(side, rule): 损益美元}；最大风险 100 → Y = 损益/100。"""
    legs = [{"leg_id": f"{s}-{r}", "status": "candidate", "expiry": (date.fromisoformat(day) + timedelta(days=3)).isoformat()}
            for (s, r) in vals]
    return {"session": day, "instrument": inst, "identity": {"mode": "prospective"},
            "decision": {"flow": {"call_direction": direction}}, "legs": legs,
            "outcome": {f"{s}-{r}": (_o(*v) if isinstance(v, tuple) else _o(v)) for (s, r), v in vals.items()}}


def _days(n, start=date(2026, 9, 28)):
    return [d.isoformat() for d in mc.trading_days(start, date(2026, 12, 31))][:n]


def test_frozen_analysis_identity():
    a = sd.ANALYSIS
    assert a["base_config_version"] == sh.CONFIG["version"] and a["family_size"] == 14
    assert a["alpha_one_sided"] == pytest.approx(0.05 / 14) and a["eligible_from"] == "2026-09-28"
    assert a["mapping"] == {"偏多": "P", "偏空": "C"}


def test_aligned_sides_mapping_and_none():
    assert sd.aligned_sides(_row("2026-09-28", "偏多", {})) == ("P", "C")
    assert sd.aligned_sides(_row("2026-09-28", "偏空", {})) == ("C", "P")
    assert sd.aligned_sides(_row("2026-09-28", "中性", {})) is None
    assert sd.aligned_sides({"decision": {}}) is None


def test_hdir_and_hwall_pairs():
    rows = []
    for i, d in enumerate(_days(30)):
        noise = (i % 3 - 1) * 2
        rows.append(_row(d, "偏多" if i % 2 else "偏空", {
            ("P", "B1"): 15 + noise, ("C", "B1"): 5 + noise, ("P", "A"): 18 + noise, ("C", "A"): 4 + noise}))
    rep = sd.instrument_report(rows, "gold")
    # 偏多日：顺向 P；偏空日：顺向 C。H-dir 均值 = 平均(顺−逆)
    exp_hdir = st_mean([(15 - 5) if i % 2 else (5 - 15) for i in range(30)]) / 100
    assert rep["H-dir"]["ci"]["mean"] == pytest.approx(exp_hdir)
    assert rep["H-dir"]["n_pairs"] == 30 and rep["coverage"]["direction_bull"] == 15
    assert rep["identity"] == "exploratory" and rep["H-dir"]["verdict"].startswith("探索·")
    assert rep["descriptive"]["B1_always_put"]["n_point"] == 30


def st_mean(x):
    return sum(x) / len(x)


def test_no_direction_rows_only_in_denominator():
    rows = [_row(d, None, {("P", "B1"): 10, ("C", "B1"): 0}) for d in _days(25)]
    rep = sd.instrument_report(rows, "gold")
    assert rep["coverage"]["no_direction"] == 25 and rep["H-dir"]["n_pairs"] == 0
    assert rep["H-dir"]["verdict"].endswith("证据不足（配对日期不足）")


def test_unbounded_residual_makes_hdir_undecided():
    rows = [_row(d, "偏多", {("P", "B1"): (10, None), ("C", "B1"): 0}) for d in _days(25)]
    rep = sd.instrument_report(rows, "gold")
    assert rep["H-dir"]["n_unbounded"] == 25
    assert rep["H-dir"]["verdict"].endswith("未决（残腿处置未知）")


def test_judge_thresholds():
    ok = {"status": "ok", "lo": 0.05, "hi": 0.2}
    assert sd._judge(ok, 25, 0.9).startswith("支持")
    assert sd._judge({**ok, "lo": 0.01}, 25, 0.9).startswith("统计为正")
    assert sd._judge({**ok, "lo": -0.2, "hi": -0.01}, 25, 0.9) == "不支持"
    assert sd._judge(ok, 10, 0.9).startswith("证据不足")
    assert sd._judge(ok, 25, 0.3).startswith("证据不足（可配对率")


def test_formal_sample_excludes_post_formal_and_pre_eligible():
    rows = [_row("2026-09-25", "偏多", {("P", "B1"): 10, ("C", "B1"): 0}),     # 冻结前 → 排除
            _row("2026-12-31", "偏多", {("P", "B1"): 10, ("C", "B1"): 0})]     # 到期在检验日之后 → 正式排除
    ident, rs = sd._rows_for(rows, "gold", as_of=date(2027, 1, 5))
    assert ident == "formal" and rs == []
    ident2, rs2 = sd._rows_for(rows, "gold", as_of=date(2026, 10, 1))
    assert ident2 == "exploratory" and [r["session"] for r in rs2] == ["2026-12-31"]


def test_interaction_and_direct_direction_shapes():
    rows = [_row(d, "偏多", {("P", "B1"): 10, ("C", "B1"): 0, ("P", "A"): 12, ("C", "A"): 1}) for d in _days(22)]
    rep = sd.instrument_report(rows, "gold")
    assert rep["interaction"]["n_pairs"] == 22 and rep["interaction"]["identity"] == "描述性"
    assert rep["direct_direction"]["n"] == 0 and rep["direct_direction"]["hit_rate"] is None
