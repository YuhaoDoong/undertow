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


def test_buyer_edge_thin_flagged():
    """借方结构走【买方边际】：现价 63 却买 61/60 看跌价差 → 盈亏平衡价远在下方，边际深负。"""
    pos = [_Pos("SLV260918P61000.US", "买 61P", 1, 1.00),
           _Pos("SLV260918P60000.US", "卖 60P", -1, 0.40)]   # 净付 0.60 的熊市看跌价差
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 24))
    hf = run_healthcheck(pr, None)
    thin = [f for f in hf if f.code == "BUYER_EDGE_THIN"]
    assert thin, _codes(hf)
    assert "盈亏平衡" in thin[0].detail and "边际" in thin[0].detail, thin[0].detail
    assert "SELLER_EDGE_THIN" not in _codes(hf), "借方结构不该走卖方边际闸门"
    print(f"PASS test_buyer_edge_thin_flagged → {thin[0].detail}")


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
