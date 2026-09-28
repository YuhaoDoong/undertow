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
                   '<span class="pill">可信度 ', "按回测可信度加权", "<th>可信度</th>"):
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


# ── Codex 016 F16-02：实际输出测试（HTML 与 Markdown 同一 plan；波动率卡；情景卡） ──

def _fib_plan():
    fib = FibAnalysis(ok=True, direction="down", swing_low=90.0, swing_high=110.0, swing_low_date=date(2026, 9, 1),
                      swing_high_date=date(2026, 9, 20), leg_pct=22.0, spot=92.0, etf_spot=None, ratio=None,
                      retracements=[FibLevel(0.5, 100.0, None, "retr", "0.5", True)], extensions=[],
                      current_zone="0.236", note="")
    mk = lambda kind, rr, g: Setup(kind=kind, name="现价追（反面样板）" if kind == "chase" else "等回调到斐波 0.5（纪律做法）",
                                   direction="做空", entry=92.0, entry_label="", stop=111.0, stop_label="",
                                   target=85.0, target_label="", rr=rr, grade=g, verdict="别追：现价做空盈亏比差")
    plan = RiskRewardPlan(ok=True, direction="做空", spot=92.0, setups=[mk("chase", 0.4, "差"), mk("pullback", 1.8, "中")],
                          headline="顺下跌腿做空：……印证'别追、等回调'纪律。", note="", bias_note="近端偏空",
                          caveats=["不达标直接放弃"])
    return fib, plan


def test_same_plan_html_and_markdown_are_conditional_only():
    from undertow.report.markdown import render_fib_rr
    fib, plan = _fib_plan()
    outs = {"html": H.render_fib_rr_section(fib, plan), "md": render_fib_rr(fib, plan, "白银")}
    for kind, out in outs.items():
        assert "以现价入场" in out and "以斐波 0.5 回撤入场" in out and "未经验证" in out, kind
        for banned in ("别追", "印证", "反面样板", "纪律做法", "盈亏比闸门", "回调买", "反抽卖", "不达标直接放弃",
                       "近端偏空", "| 差 |", "| 中 |"):
            assert banned not in out, (kind, banned)


def test_vol_regime_high_iv_does_not_claim_positive_expectancy():
    from undertow.analyze.volregime import assess_vol_regime

    class R:
        name, latest, percentile_1y, chg_20d = "GVZ", 30.0, 90.0, 3.0
    closes = [100 * (1.001 ** i) * (1 + (0.002 if i % 2 else -0.002)) for i in range(60)]
    vr = assess_vol_regime(iv_reading=R(), atm_iv_pp=40.0, closes=closes)
    text = " ".join(vr.reasons + vr.caveats)
    assert vr.iv_minus_rv is not None and vr.iv_minus_rv > 2
    for banned in ("正期望空间", "买方占便宜", "，利卖方", "，利买方", "卖方留意", "买方留意"):
        assert banned not in text, banned
    out = H.render_vol_regime_section(vr)
    assert "未经验证" in out and "期权买方还是卖方" not in out and "正期望空间" not in out


def test_scenarios_have_no_action_words_and_fixed_order():
    from undertow.analyze import outlook as ol

    class GA:
        call_wall, put_wall, zero_gamma, spot, net_gex = 110.0, 90.0, 95.0, 100.0, -1.0
        def to_commodity(self, v):
            return None
    orders = []
    for sign in (-1, 0, 1):
        sc = ol._scenarios(GA(), sign)
        orders.append([s.name for s in sc])
        text = " ".join(s.path + s.trigger + s.invalidation for s in sc)
        for banned in ("别追", "高抛低吸", "接刀", "别逆势"):
            assert banned not in text, banned
    assert orders[0] == orders[1] == orders[2], "情景顺序不得随未验证的方向改变"


def test_frozen_flow_expiry_head_is_neutralized_at_render_layer():
    """flow.py 冻结不可改：渲染层替换其「全曲线共识」标题；原句两段字面量必须仍在 flow.py，否则替换已失效。"""
    fsrc = (ROOT / "undertow" / "analyze" / "flow.py").read_text("utf-8")
    assert "'<b style=\"color:#1a7f37\">✅ 各到期桶方向一致</b> —— '" in fsrc
    assert "'不是某个到期的孤立现象，是全曲线共识。'" in fsrc
    out = H.neutralize_expiry_split("<div>" + H.EXPIRY_SPLIT_LEGACY_HEAD + "</div>")
    assert "全曲线共识" not in out and "✅" not in out and "未经验证" in out
