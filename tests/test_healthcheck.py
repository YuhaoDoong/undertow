"""持仓体检的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：复现本次对话暴露的坑——近到期被指派×资金不足、盈亏比过低、窄价差近到期。
"""
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.portfolio import review_portfolio, InstrumentContext, AccountCapital
from undertow.analyze.healthcheck import run_healthcheck


@dataclass
class _Pos:
    symbol: str
    name: str
    quantity: float
    cost_price: float


def _ctx(spot, *, bias="偏多"):
    return InstrumentContext(
        etf_symbol="SLV", display_name="白银 Silver (COMEX)", spot=spot,
        call_wall=70.0, put_wall=55.0, zero_gamma=None,
        bias=bias, near_bias="中性", mid_bias="偏多",
        verdict_head="不做空 · 长线拿住", proxy_quality="good", greeks=None)


def _codes(findings):
    return {f.code for f in findings}


def test_assign_capital_gap_high_severity():
    """近到期贴价短 put + 购买力远不够接货 → ASSIGN_CAPITAL_GAP，严重度高。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 260826 60 Put", 4, 0.27)]
    cap = AccountCapital(buy_power=6.0, net_assets=630.0, cash_usd=6.0)
    pr = review_portfolio(pos, {"SLV": _ctx(61.5)}, asof=date(2026, 8, 24), capital=cap)
    hf = run_healthcheck(pr, cap)
    gap = [f for f in hf if f.code == "ASSIGN_CAPITAL_GAP"]
    assert gap, _codes(hf)
    assert gap[0].severity == "高", gap[0]
    assert "接货" in gap[0].detail and "购买力" in gap[0].detail, gap[0].detail
    assert hf[0].severity == "高", "高危应排最前"
    print(f"PASS test_assign_capital_gap_high_severity → {gap[0].title}")


def _ctx_delta(spot, short_delta):
    """构造带 greeks 的上下文：让短腿有指定 delta（用于胜率边际测试）。"""
    def greeks(kind, strike, expiry):
        return (-short_delta if kind == "P" else short_delta, 0.40)
    return InstrumentContext(
        etf_symbol="SLV", display_name="白银 Silver (COMEX)", spot=spot,
        call_wall=70.0, put_wall=55.0, zero_gamma=None,
        bias="偏多", near_bias="中性", mid_bias="偏多",
        verdict_head="", proxy_quality="good", greeks=greeks)


def test_seller_edge_thin_flagged():
    """收权金结构：隐含胜率(1−0.35=65%) 略高于盈亏平衡(62%) 但边际仅 3pp < 10pp → 命中。"""
    pos = [_Pos("SLV260918P60000.US", "60P", -1, 1.76),
           _Pos("SLV260918P59000.US", "59P", 1, 1.38)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(61.2, 0.35)}, asof=date(2026, 8, 25))
    hf = run_healthcheck(pr, None)
    thin = [f for f in hf if f.code == "SELLER_EDGE_THIN"]
    assert thin, _codes(hf)
    assert "隐含胜率" in thin[0].detail and "边际" in thin[0].detail, thin[0].detail
    assert "POOR_RR" not in _codes(hf), "卖方结构不应再被 R:R<1 硬卡"
    print(f"PASS test_seller_edge_thin_flagged → {thin[0].detail}")


def test_seller_edge_sufficient_no_flag():
    """短腿卖得够远（delta 0.12 → 隐含 88% vs 需 62%，边际 26pp）→ 不告警。"""
    pos = [_Pos("SLV260918P60000.US", "60P", -1, 1.76),
           _Pos("SLV260918P59000.US", "59P", 1, 1.38)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(61.2, 0.12)}, asof=date(2026, 8, 25))
    hf = run_healthcheck(pr, None)
    assert "SELLER_EDGE_THIN" not in _codes(hf), [f.detail for f in hf]
    print("PASS test_seller_edge_sufficient_no_flag")


def test_bearish_debit_spread_uses_primary_buyer_gates():
    """看跌借方价差必须走【主闸门】（净Δ + 每日损耗），不是到期概率兜底。

    2026-08-27 codex review 指出：buyer_carry 旧写法 `nd <= 0 → return None`，
    使看跌借方价差与多头 put 的净Δ本就为负、直接被排除在买方框架之外——
    整套买方闸门只覆盖了看涨结构。改用 |净Δ| 后，看跌侧同样受闸门约束。
    BUYER_EDGE_THIN 是 `carry is None` 时的兜底，主闸门可算时不应再出现。
    """
    pos = [_Pos("SLV260918P61000.US", "买 61P", 1, 1.00),
           _Pos("SLV260918P60000.US", "卖 60P", -1, 0.40)]   # 净付 0.60 的熊市看跌价差
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 24))
    hf = run_healthcheck(pr, None)
    codes = _codes(hf)
    assert "LOW_NET_DELTA" in codes, f"看跌侧应受净Δ闸门约束: {codes}"
    low = [f for f in hf if f.code == "LOW_NET_DELTA"][0]
    assert "跌" in low.title, f"方向措辞应随净Δ符号变化: {low.title}"
    assert "SELLER_EDGE_THIN" not in codes, "借方结构不该走卖方边际闸门"
    print(f"PASS test_bearish_debit_spread_uses_primary_buyer_gates → {low.title}")


def test_buyer_carry_handles_both_directions():
    """buyer_carry 对看涨/看跌结构都要能算出「每日打平所需波动」。"""
    from undertow.analyze.healthcheck import buyer_carry
    bear = [_Pos("SLV260918P61000.US", "买 61P", 1, 1.00),
            _Pos("SLV260918P60000.US", "卖 60P", -1, 0.40)]
    pr = review_portfolio(bear, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 24))
    combo = pr.groups[0].combos[0]
    carry = buyer_carry(combo, 63.0)
    assert carry is not None, "看跌结构不该返回 None"
    _th, nd, need = carry
    assert nd < 0, "看跌结构净Δ应为负（符号保留用于展示方向）"
    assert need is not None and need > 0, "打平所需波动应为正（幅度问题，与方向无关）"
    print(f"PASS test_buyer_carry_handles_both_directions → 净Δ{nd:+.3f} 打平{need:.2f}%/日")


def test_buyer_edge_sufficient_no_flag():
    """买得价内、赔率合理的借方结构 → 边际充足，不告警。"""
    pos = [_Pos("SLV260918C58000.US", "买 58C", 1, 4.20),
           _Pos("SLV260918C64000.US", "卖 64C", -1, 1.20)]   # 付 3.0、宽 6 的牛市看涨价差
    pr = review_portfolio(pos, {"SLV": _ctx(62.0)}, asof=date(2026, 8, 24))
    hf = run_healthcheck(pr, None)
    assert "BUYER_EDGE_THIN" not in _codes(hf), [f.detail for f in hf]
    print("PASS test_buyer_edge_sufficient_no_flag")


def test_tight_near_spread_gamma_flag():
    """$1 宽 + 近到期 → TIGHT_NEAR（gamma 风险）。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 60 Put", 4, 0.27)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 24))
    hf = run_healthcheck(pr, None)
    assert "TIGHT_NEAR" in _codes(hf), _codes(hf)
    print("PASS test_tight_near_spread_gamma_flag")


def test_healthy_far_wide_spread_no_high_severity():
    """远到期、够宽、现价远离行权 → 不该有"高"级预警。"""
    pos = [_Pos("SLV261218P55000.US", "SLV 55 Put", -2, 1.5),
           _Pos("SLV261218P50000.US", "SLV 50 Put", 2, 0.6)]
    cap = AccountCapital(buy_power=5000.0, net_assets=20000.0, cash_usd=5000.0)
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 24), capital=cap)
    hf = run_healthcheck(pr, cap)
    assert not any(f.severity == "高" for f in hf), [f.title for f in hf]
    print("PASS test_healthy_far_wide_spread_no_high_severity")


def test_single_long_lottery_flagged():
    """单腿深度价外买 call：回本需 >1σ 且 delta<0.3 → SINGLE_LONG_THIN（复刻 70C）。"""
    pos = [_Pos("SLV260918C70000.US", "SLV 70 Call", 3, 1.05)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(61.2, 0.15)}, asof=date(2026, 8, 25))
    hf = run_healthcheck(pr, None)
    thin = [f for f in hf if f.code == "SINGLE_LONG_THIN"]
    assert thin, _codes(hf)
    assert "回本" in thin[0].detail and ("σ" in thin[0].detail or "delta" in thin[0].detail)
    print(f"PASS test_single_long_lottery_flagged → {thin[0].detail}")


def test_single_long_efficient_no_flag():
    """价内买 call（delta 0.6、回本近）→ 不告警。"""
    pos = [_Pos("SLV260918C58000.US", "SLV 58 Call", 1, 4.20)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(61.2, 0.60)}, asof=date(2026, 8, 25))
    hf = run_healthcheck(pr, None)
    assert "SINGLE_LONG_THIN" not in _codes(hf), [f.detail for f in hf]
    print("PASS test_single_long_efficient_no_flag")


def test_negative_ev_after_fees():
    """二值估算 +$3.0 但手续费 $3.20 → 扣费后为负，判【中】危（复刻 60/59 实例）。

    ⚠️ 严重性从「高」降为「中」：这个数不是期望值，是最坏情形的二值近似——
    把价差当成「要么最大盈、要么最大亏」，忽略两个行权价之间的连续 payoff，
    且用短腿 delta 近似胜率。两处近似都偏保守，系统性低估真实期望，
    用它做高危否决会错杀本可接受的结构。（codex review 2026-08-27）
    """
    pos = [_Pos("SLV260918P60000.US", "60P", -1, 1.76),
           _Pos("SLV260918P59000.US", "59P", 1, 1.38)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(61.2, 0.35)}, asof=date(2026, 8, 25))
    hf = run_healthcheck(pr, None)
    neg = [f for f in hf if f.code == "NEGATIVE_EV_AFTER_FEES"]
    assert neg and neg[0].severity == "中", _codes(hf)
    assert "手续费" in neg[0].detail, neg[0].detail
    # 文案必须自带「这不是期望值」的警示，否则读者会当成真 EV
    assert "不可当期望值" in neg[0].detail or "非真期望值" in neg[0].title, neg[0].detail
    print(f"PASS test_negative_ev_after_fees → {neg[0].detail[:70]}")


def test_after_fee_ev_math():
    """扣费后期望值公式：毛期望=p×最大盈−(1−p)×最大亏；费=腿×张×费率×2。"""
    from undertow.analyze.healthcheck import after_fee_ev, FEE_PER_CONTRACT
    pos = [_Pos("SLV260918P60000.US", "60P", -2, 1.76),
           _Pos("SLV260918P59000.US", "59P", 2, 1.38)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(61.2, 0.20)}, asof=date(2026, 8, 25))
    c = pr.groups[0].combos[0]
    gross, fees, net, frac = after_fee_ev(c, spot=61.2)
    assert abs(fees - 2 * 2 * FEE_PER_CONTRACT * 2) < 1e-9, fees   # 2腿×2张×费率×2
    assert abs(gross - (0.80 * c.max_profit - 0.20 * c.max_loss)) < 1e-6
    assert abs(net - (gross - fees)) < 1e-9
    print(f"PASS test_after_fee_ev_math → 毛{gross:+.1f} 费{fees:.2f} 净{net:+.1f}")


def test_fees_ok_when_edge_thick():
    """边际够厚时费用占比小，不告警。"""
    pos = [_Pos("SLV260918P55000.US", "55P", -1, 1.20),
           _Pos("SLV260918P50000.US", "50P", 1, 0.30)]
    pr = review_portfolio(pos, {"SLV": _ctx_delta(62.0, 0.08)}, asof=date(2026, 8, 25))
    hf = run_healthcheck(pr, None)
    assert "NEGATIVE_EV_AFTER_FEES" not in _codes(hf), [f.detail for f in hf]
    print("PASS test_fees_ok_when_edge_thick")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
