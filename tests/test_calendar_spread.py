"""日历 / 对角价差子模块 —— 回归测试。

锚点是这个结构的三个符号（theta 正 / gamma 负 / vega 正）和那个被原 SOP 漏掉的
期限结构闸门。符号一旦弄反，整套核算会给出方向完全相反的建议。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze import blackscholes as bs          # noqa: E402
from undertow.analyze import calendar_spread as cal             # noqa: E402
from undertow.core.models import OptionsSnapshot, OptionContract  # noqa: E402

TODAY = date(2026, 9, 10)


def _snap(spot=100.0, near_iv=0.30, far_iv=0.25, near_dte=21, far_dte=80,
          oi=5000, strikes=(90, 95, 100, 105, 110)):
    """合成一份两到期的链。near_iv > far_iv = 逆向期限结构 = 日历顺风。"""
    cs = []
    for dte, iv in ((near_dte, near_iv), (far_dte, far_iv)):
        exp = TODAY + timedelta(days=dte)
        T = dte / 365.0
        for k in strikes:
            for kind in ("C", "P"):
                cs.append(OptionContract(
                    expiry=exp, strike=float(k), kind=kind, open_interest=oi,
                    volume=100, gamma=bs.gamma(spot, k, T, iv),
                    delta=bs.delta(spot, k, T, iv, kind), iv=iv))
    return OptionsSnapshot(instrument="test", proxy_symbol="TST", spot=spot,
                           asof="2026-09-10", contracts=cs)


def test_vega_is_per_pp_and_scales_with_sqrt_t():
    """vega 必须是【每 pp】口径，且 ∝ √T。

    教科书 vega 是每 1.0 波动率单位（=100pp）。口径混用会让日历的 vega 敞口
    差 100 倍 —— 这个结构的主敞口就是 vega，错了整个核算失去意义。
    """
    v30 = bs.vega(100, 100, 30 / 365, 0.30)
    v90 = bs.vega(100, 100, 90 / 365, 0.30)
    assert 0.05 < v30 < 0.5, f"每 pp 口径下 ATM vega 应是零点几，得到 {v30}"
    assert 1.6 < v90 / v30 < 1.85, f"vega 应 ∝ √T（√3≈1.73），得到 {v90/v30}"
    assert bs.vega(100, 100, 0, 0.3) == 0 and bs.vega(100, 100, 0.1, 0) == 0
    print("PASS test_vega_is_per_pp_and_scales_with_sqrt_t")


def test_net_greek_signs():
    """卖近月 + 买远月：Θ 正（收租）、Γ 负（怕动）、Vega 正（怕 IV 塌）。

    这三个符号是整个结构的定义。它们同时也解释了原型 SOP 那次亏损为什么会发生：
    近远月脱钩时，正 vega 和负 gamma 会同时朝不利方向兑现。
    """
    p = cal.build(_snap(), kind="C", today=TODAY)
    assert p.applicable, p.headline
    assert p.net_theta_usd > 0, f"应为正 Θ（收租），得到 {p.net_theta_usd}"
    assert p.net_gamma < 0, f"应为负 Γ，得到 {p.net_gamma}"
    assert p.net_vega_usd > 0, f"应为正 Vega，得到 {p.net_vega_usd}"
    assert p.net_debit_usd > 0, "ATM 同行权价日历应是净支出（远月更贵）"
    print("PASS test_net_greek_signs")


def test_term_structure_gate_blocks_contango():
    """近月 IV 不高于远月时必须拦下 —— 这是原 SOP 缺的那一步。

    日历多头的本质是"卖贵的近月、买便宜的远月"。倒挂时这个结构从第一天
    就在逆风，只剩负 gamma 的风险敞口。实测美股 ETF 上倒挂是常态
    （386 个样本中位 −0.57pp），所以这个闸门必须真的拦得住。
    """
    bad = cal.build(_snap(near_iv=0.25, far_iv=0.30), kind="C", today=TODAY)
    assert not bad.applicable
    assert any("期限结构逆风" in b for b in bad.blockers), bad.blockers
    assert bad.term_edge_pp < 0
    # 文案不能出现「高 -5.00pp」这种读不通的说法
    assert "高 -" not in bad.headline and "高-" not in bad.headline

    good = cal.build(_snap(near_iv=0.32, far_iv=0.25), kind="C", today=TODAY)
    assert good.applicable, good.headline
    assert good.term_edge_pp > cal.TERM_EDGE_MIN
    print("PASS test_term_structure_gate_blocks_contango")


def test_steep_term_structure_warns_instead_of_celebrating():
    """期限结构极陡是哨兵，不是喜讯 —— 近月逼空、远月不跟的教训。

    近月 IV 异常抬升通常不是白给的租金，而是市场在为近月的某件事定价。
    模块必须在这种时候出声，而不是因为"顺风"就一路绿灯。
    """
    p = cal.build(_snap(near_iv=0.45, far_iv=0.25), kind="C", today=TODAY)
    assert p.applicable
    assert p.term_edge_pp >= cal.TERM_EDGE_RICH
    assert any("🚨" in n for n in p.notes), "极陡期限结构必须有醒目警告"
    assert any("逼空" in n or "事件" in n for n in p.notes)
    print("PASS test_steep_term_structure_warns_instead_of_celebrating")


def test_daily_breakeven_move_formula():
    """负 Γ 临界：½·|Γ|·ΔS² = Θ  →  ΔS = √(2Θ/|Γ|)。

    原 SOP 里连 gamma 这个词都没有，但它是负 gamma 结构的日常失血点。
    """
    p = cal.build(_snap(), kind="C", today=TODAY)
    assert p.daily_be_move is not None and p.daily_be_move > 0
    expect = (2 * (p.net_theta_usd) / abs(p.net_gamma)) ** 0.5
    # 字段按 round(4) 落盘（展示精度），容差与之匹配，不是把公式测松
    assert abs(p.daily_be_move - expect) < 1e-3, f"{p.daily_be_move} vs {expect}"
    assert abs(p.daily_be_move_pct - p.daily_be_move / p.spot * 100) < 1e-3
    print("PASS test_daily_breakeven_move_formula")


def test_breakevens_bracket_the_sweet_spot():
    """日历的损益曲线是单峰的：甜点在卖腿行权价，上下沿各一个盈亏平衡。"""
    p = cal.build(_snap(), kind="C", today=TODAY)
    assert p.be_lo is not None and p.be_hi is not None, "两侧 BE 都该解得出"
    assert p.be_lo < p.sweet_spot < p.be_hi, f"{p.be_lo} < {p.sweet_spot} < {p.be_hi}"
    print("PASS test_breakevens_bracket_the_sweet_spot")


def test_gates_on_liquidity_and_dte_ratio():
    """近月门槛严于远月（近月要反复滚动），远近月挨太近要拦下。"""
    assert cal.NEAR_MIN_OI > cal.FAR_MIN_OI, "近月卖腿要滚动，门槛应更严"

    thin = cal.build(_snap(oi=50), kind="C", today=TODAY)
    assert not thin.applicable and any("流动性" in b for b in thin.blockers)

    close = cal.build(_snap(near_dte=30, far_dte=45), kind="C", today=TODAY)
    assert any("间距不足" in b for b in close.blockers), close.blockers
    print("PASS test_gates_on_liquidity_and_dte_ratio")


def test_registered_in_strategy_hub():
    """子模块必须真的挂进统筹层，不能只是定义了没人调用。"""
    from undertow.analyze.strategy_hub import assemble_strategies
    p = cal.build(_snap(), kind="C", today=TODAY)
    props = assemble_strategies(calendar=p)
    assert len(props) == 1 and props[0].applicable
    assert "日历" in props[0].name
    print("PASS test_registered_in_strategy_hub")


def test_render_does_not_claim_to_be_advice():
    """诚实边界：结构核算不是交易指令，且必须点出保证金口径不在本模块。"""
    md = cal.render_md(cal.build(_snap(), kind="C", today=TODAY))
    assert "非交易指令" in md
    assert "保证金口径因券商而异" in md, "跨月组合是否被识别因券商而异，必须说明"
    assert "理论中值" in md, "必须声明权利金是 BS 理论值而非可成交价"
    print("PASS test_render_does_not_claim_to_be_advice")
