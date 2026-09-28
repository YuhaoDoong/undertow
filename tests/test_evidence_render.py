"""展示层证据门控（Codex 015 提交三）：各出口不再用未验证的读数下结论。"""
from datetime import date
from pathlib import Path

from undertow.analyze.fibonacci import FibAnalysis, FibLevel
from undertow.analyze.risk_reward import RiskRewardPlan, Setup
from undertow.report import html as H

ROOT = Path(__file__).resolve().parents[1]


def test_fib_section_is_conditional_scenario_not_gate():
    fib = FibAnalysis(ok=True, direction="up", swing_low=90.0, swing_high=110.0, swing_low_date=date(2026, 9, 1),
                      swing_high_date=date(2026, 9, 20), leg_pct=22.0, spot=108.0, etf_spot=None, ratio=None,
                      retracements=[FibLevel(0.5, 100.0, None, "retr", "0.5", True)], extensions=[],
                      current_zone="0.236", note="")
    mk = lambda kind, rr, g: Setup(kind=kind, name="现价追（反面样板）" if kind == "chase" else "等回调到斐波 0.5（纪律做法）",
                                   direction="做多", entry=108.0, entry_label="", stop=95.0, stop_label="",
                                   target=112.0, target_label="", rr=rr, grade=g, verdict="别追：盈亏比差")
    plan = RiskRewardPlan(ok=True, direction="做多", spot=108.0, setups=[mk("chase", 0.3, "差"), mk("pullback", 1.5, "中")],
                          headline="……印证'别追、等回调'纪律。", note="")
    out = H.render_fib_rr_section(fib, plan)
    assert "不参与结论" in out and "未经验证" in out and "以现价入场" in out
    for banned in ("印证", "别追", "反面样板", "纪律做法", "盈亏比闸门", "顺势=回调买"):
        assert banned not in out, banned


def test_tradeable_gate_is_observation():
    src = (ROOT / "undertow" / "report" / "html.py").read_text("utf-8")
    assert "今天有可交易信息" not in src and "今天没有可交易信息" not in src


def test_no_legacy_authority_wording_in_renderers_and_consult():
    src = (ROOT / "undertow" / "report" / "html.py").read_text("utf-8")
    for banned in ("推翻已校准的综合研判", "教科书组合 · ", "<h2>④ 综合研判", "· 综合研判</h1>",
                   '<span class="pill">可信度 '):
        assert banned not in src, banned
    from undertow.consult.packet import GUIDANCE, _evidence_brief
    g = " ".join(GUIDANCE)
    assert "没有通过验证的方向依据" in g and "不得作为方向结论" in g
    assert "方向研判以 instruments[].bias/near_bias/mid_bias/verdict_head 为准" not in g
    ev = _evidence_brief()
    assert ev["t1_count"] == 0 and "outlook.mid_bias" in ev["t3"]
    assert [x["claim"] for x in ev["t2"]] == ["flow.direction.gold_silver"]


def test_index_and_report_carry_evidence_line():
    src = (ROOT / "undertow" / "report" / "html.py").read_text("utf-8")
    assert src.count("模型证据：本系统暂无通过验证的方向依据") >= 2      # 索引页 + 每份研报顶部
