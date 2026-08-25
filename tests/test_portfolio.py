"""实盘持仓理论评价的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：复现真实 SLV 车轮结构的解析、单腿评价、价差识别与被行权风险旗标。
"""
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.portfolio import (
    parse_symbol, review_portfolio, InstrumentContext, AccountCapital)


@dataclass
class _Pos:
    symbol: str
    name: str
    quantity: float
    cost_price: float


def _ctx(spot, *, bias="偏多", put_wall=58.0, call_wall=66.0):
    # greeks=None → 回退 BS；测试只关心方向/结构，不苛求精确定价
    return InstrumentContext(
        etf_symbol="SLV", display_name="白银 Silver (COMEX)", spot=spot,
        call_wall=call_wall, put_wall=put_wall, zero_gamma=None,
        bias=bias, near_bias="中性", mid_bias="偏多",
        verdict_head="不做空 · 长线拿住", proxy_quality="good", greeks=None)


def test_parse_option_and_stock():
    p = parse_symbol("SLV260826P61000.US")
    assert p.underlying == "SLV" and p.kind == "P", p
    assert p.strike == 61.0 and p.expiry == date(2026, 8, 26), p
    c = parse_symbol("SLV260918C70000.US")
    assert c.kind == "C" and c.strike == 70.0 and c.expiry == date(2026, 9, 18), c
    s = parse_symbol("AAPL.US")
    assert s.kind == "STOCK" and s.underlying == "AAPL" and s.strike is None, s
    print("PASS test_parse_option_and_stock")


def test_short_put_aligned_when_bullish():
    """卖 put=看多敞口；综合偏多时应判顺势，现价在行权价上方判贴价/价外安全。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    lg = pr.groups[0].legs[0]
    assert lg.side == "空头(卖出)", lg.side
    assert lg.align == "顺势", lg.align
    assert lg.moneyness == "价外", lg.moneyness   # 现价63 > 行权61 → put 价外
    assert lg.pos_delta is not None and lg.pos_delta > 0, lg.pos_delta  # 卖put=正delta敞口
    print(f"PASS test_short_put_aligned_when_bullish → {lg.comment}")


def test_bull_put_spread_detected():
    """卖61P + 买60P → 牛市看跌价差(收权金)，最大盈=净权金、最大亏=(宽−净)。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 260826 60 Put", 4, 0.27)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    sp = pr.groups[0].combos
    assert len(sp) == 1, sp
    s = sp[0]
    assert "牛市看跌价差" in s.label and s.stance == "保守做多", s
    assert s.defined_risk and abs(s.net_credit - 0.19) < 1e-9, s
    assert abs(s.max_profit - 0.19 * 100 * 4) < 1e-6, s.max_profit   # 76
    assert abs(s.max_loss - (1 - 0.19) * 100 * 4) < 1e-6, s.max_loss  # 324
    print(f"PASS test_bull_put_spread_detected → {s.note} 最大盈{s.max_profit:.0f}/最大亏{s.max_loss:.0f}")


def test_iron_condor_detected():
    """put 侧收权金价差 + call 侧收权金价差 → 铁鹰，中性收权金。"""
    pos = [_Pos("SLV260918P55000.US", "SLV 55P", -2, 0.5),
           _Pos("SLV260918P53000.US", "SLV 53P", 2, 0.25),
           _Pos("SLV260918C70000.US", "SLV 70C", -2, 0.6),
           _Pos("SLV260918C72000.US", "SLV 72C", 2, 0.3)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    combos = pr.groups[0].combos
    assert len(combos) == 1 and "铁鹰" in combos[0].label, combos
    assert combos[0].stance.startswith("中性") and combos[0].defined_risk, combos[0]
    print(f"PASS test_iron_condor_detected → {combos[0].label}")


def test_calendar_spread_detected():
    """同行权、跨到期、卖近买远的 call → 日历价差（长桥不显示为组合，需自识别）。"""
    pos = [_Pos("SLV260826C65000.US", "SLV 65C near", -2, 0.4),
           _Pos("SLV260918C65000.US", "SLV 65C far", 2, 0.9)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    combos = pr.groups[0].combos
    assert len(combos) == 1 and "日历价差" in combos[0].label, combos
    assert "跨期" in combos[0].expiry_label, combos[0].expiry_label
    print(f"PASS test_calendar_spread_detected → {combos[0].label} {combos[0].expiry_label}")


def test_spread_protective_leg_not_flagged_countertrend():
    """价差保护腿（买60P）不得被单独判逆势——方向由价差整体承载，只标『组合腿(结构)』。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 260826 60 Put", 4, 0.27)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    legs = {lg.strike: lg for lg in pr.groups[0].legs}
    assert legs[60.0].align == "组合腿(结构)", legs[60.0].align
    assert legs[61.0].align == "组合腿(结构)", legs[61.0].align
    assert not any("相反" in f for f in legs[60.0].flags), legs[60.0].flags
    print("PASS test_spread_protective_leg_not_flagged_countertrend")


def test_assignment_risk_flag_near_expiry_itm():
    """卖 put 临近到期且现价跌破行权价(价内) → 被行权风险旗标。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46)]
    pr = review_portfolio(pos, {"SLV": _ctx(59.0)}, asof=date(2026, 8, 24))  # dte=2, 现价59<61 价内
    lg = pr.groups[0].legs[0]
    assert lg.moneyness == "价内", lg.moneyness
    assert any("被行权" in f for f in lg.flags), lg.flags
    print(f"PASS test_assignment_risk_flag_near_expiry_itm → {lg.flags}")


def test_counter_trend_flag_when_bearish():
    """综合偏空时，卖 put(看多敞口) 应判逆势并旗标。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0, bias="偏空")}, asof=date(2026, 8, 20))
    lg = pr.groups[0].legs[0]
    assert lg.align == "逆势", lg.align
    assert any("相反" in f for f in lg.flags), lg.flags
    print("PASS test_counter_trend_flag_when_bearish")


def test_full_wheel_headline_and_netdelta():
    """完整车轮：卖61P/买60P/买70C，出组合总纲+净Delta，无崩。"""
    pos = [_Pos("SLV260918C70000.US", "SLV 260918 70 Call", 3, 1.05),
           _Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 260826 60 Put", 4, 0.27)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    assert pr.ok and len(pr.groups) == 1
    g = pr.groups[0]
    assert g.net_delta is not None
    assert len(g.legs) == 3 and len(g.combos) == 2
    assert "白银" in pr.headline, pr.headline
    print(f"PASS test_full_wheel_headline_and_netdelta → {pr.headline}")


def test_advice_bull_put_spread_breakeven():
    """牛市看跌价差应给出盈亏平衡与封顶建议；现价在盈亏平衡上方判『时间站你这边』。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 260826 60 Put", 4, 0.27)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    adv = pr.groups[0].advice
    assert adv, "应给出建议"
    joined = " ".join(adv)
    assert "盈亏平衡" in joined and "60.81" in joined, joined   # 61 − 0.19
    assert "封顶" in joined, joined
    print(f"PASS test_advice_bull_put_spread_breakeven → {adv[0]}")


def test_advice_assignment_rollup_when_near_expiry_itm():
    """卖 put 临近到期价内 → 给『接货/展期/止损』三选一建议，带接货成本数字。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46)]
    pr = review_portfolio(pos, {"SLV": _ctx(59.0)}, asof=date(2026, 8, 24))
    joined = " ".join(pr.groups[0].advice)
    assert "接货" in joined and ("展期" in joined or "roll" in joined), joined
    assert "24,400" in joined or "24400" in joined, joined   # 61×100×4 接货成本
    print("PASS test_advice_assignment_rollup_when_near_expiry_itm")


def test_capital_constrained_spread_leg_not_called_naked():
    """价差短腿(有保护)+资金不足接货：建议应说『有保护腿封顶但资金不足垫付』，不得叫『裸卖』。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 260826 60 Put", 4, 0.27)]
    cap = AccountCapital(buy_power=6.0, net_assets=630.0, cash_usd=6.0)
    pr = review_portfolio(pos, {"SLV": _ctx(59.0)}, asof=date(2026, 8, 24), capital=cap)
    joined = " ".join(pr.groups[0].advice)
    assert "保护腿" in joined and "购买力" in joined, joined
    assert "裸卖" not in joined, joined                       # 有保护腿≠裸卖
    assert "到期前平仓" in joined or "展期" in joined, joined
    # 资金分配也应指出购买力不够（这里价差短腿资金约束在腿级建议里体现）
    print("PASS test_capital_constrained_spread_leg_not_called_naked")


def test_naked_short_put_capital_gate():
    """裸卖 put + 资金不足：明确『不能接货，只能平仓/展期/止损』。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 260826 61 Put", -4, 0.46)]
    cap = AccountCapital(buy_power=6.0, net_assets=630.0, cash_usd=6.0)
    pr = review_portfolio(pos, {"SLV": _ctx(59.0)}, asof=date(2026, 8, 24), capital=cap)
    joined = " ".join(pr.groups[0].advice)
    assert "不能接货" in joined and "24,400" in joined, joined
    print("PASS test_naked_short_put_capital_gate")


def test_live_option_price_used_for_valuation():
    """有期权实时价时，浮盈亏用真实 last（不是 BS 理论），并标〔实时价〕。"""
    pos = [_Pos("SLV260826P61000.US", "SLV 61 Put", -4, 0.46)]
    ctx = InstrumentContext(
        etf_symbol="SLV", display_name="白银 Silver (COMEX)", spot=61.2,
        call_wall=70.0, put_wall=55.0, zero_gamma=None,
        bias="偏多", near_bias="中性", mid_bias="偏多", verdict_head="",
        proxy_quality="good", greeks=None,
        live_opt={"SLV260826P61000.US": (0.30, 0.387)})   # 真实 last 0.30
    pr = review_portfolio(pos, {"SLV": ctx}, asof=date(2026, 8, 24))
    lg = pr.groups[0].legs[0]
    assert abs(lg.est_value - 0.30) < 1e-9, lg.est_value          # 用真实 last
    assert abs(lg.pnl - (0.30 - 0.46) * 100 * (-4)) < 1e-6, lg.pnl  # 卖出：+64
    assert "实时价" in lg.comment, lg.comment
    print(f"PASS test_live_option_price_used_for_valuation → pnl {lg.pnl:+.0f}")


def test_unmapped_underlying_listed_not_evaluated():
    """无 undertow 期权代理的标的（如 TSLA）只列出、不评方向、不崩。"""
    pos = [_Pos("TSLA260918C300000.US", "TSLA 260918 300 Call", 1, 5.0)]
    pr = review_portfolio(pos, {"SLV": _ctx(63.0)}, asof=date(2026, 8, 20))
    assert len(pr.unmapped) == 1 and not pr.groups, pr
    assert pr.unmapped[0].align == "—"
    print("PASS test_unmapped_underlying_listed_not_evaluated")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
