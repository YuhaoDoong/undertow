"""咨询包（consult packet）的确定性测试（函数式，不依赖 pytest / 网络）。

锚定：包是 JSON 可序列化的、含研判/持仓/体检/guidance/prompt，且不臆造数字。
"""
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.portfolio import review_portfolio, InstrumentContext, AccountCapital
from undertow.analyze.healthcheck import run_healthcheck
from undertow.consult.packet import build_consult_packet


@dataclass
class _Pos:
    symbol: str
    name: str
    quantity: float
    cost_price: float


def _ctx(spot):
    return InstrumentContext(
        etf_symbol="SLV", display_name="白银 Silver (COMEX)", spot=spot,
        call_wall=70.0, put_wall=55.0, zero_gamma=None,
        bias="偏多(弱)", near_bias="中性", mid_bias="偏多(弱)",
        verdict_head="不做空 · 长线拿住", proxy_quality="good", greeks=None)


def _bundle(spot=61.7):
    pos = [_Pos("SLV260826P61000.US", "SLV 61 Put", -4, 0.46),
           _Pos("SLV260826P60000.US", "SLV 60 Put", 4, 0.27)]
    cap = AccountCapital(buy_power=6.0, net_assets=630.0, cash_usd=6.0)
    ctxs = {"SLV": _ctx(spot)}
    rv = review_portfolio(pos, ctxs, asof=date(2026, 8, 24), capital=cap)
    hf = run_healthcheck(rv, cap)
    return rv, hf, ctxs, cap


def test_packet_is_json_serializable_and_complete():
    rv, hf, ctxs, cap = _bundle()
    pk = build_consult_packet(review=rv, health=hf, contexts=ctxs, capital=cap,
                              question="这个价差该平还是展期?", asof=date(2026, 8, 24))
    s = json.dumps(pk, ensure_ascii=False)          # 必须可序列化
    assert pk["schema"] == "undertow.consult/v1"
    assert pk["question"] == "这个价差该平还是展期?"
    assert pk["guidance"] and any("不碰算术" in g or "臆算" in g for g in pk["guidance"])
    assert pk["instruments"]["SLV"]["bias"] == "偏多(弱)"
    assert pk["portfolio"]["groups"][0]["combos"], pk["portfolio"]
    assert pk["healthcheck"], "应带体检"
    assert "prompt" in pk and "牛市看跌价差" in pk["prompt"]
    assert "价差该平还是展期" in pk["prompt"]
    print(f"PASS test_packet_is_json_serializable_and_complete （{len(s)} 字节）")


def test_pretrade_block_included():
    rv, hf, ctxs, cap = _bundle()
    pos2 = [_Pos("SLV260919P58000.US", "SLV 58 Put", -2, 0.6),
            _Pos("SLV260919P56000.US", "SLV 56 Put", 2, 0.3)]
    pr2 = review_portfolio(pos2, {"SLV": _ctx(61.7)}, asof=date(2026, 8, 24), capital=cap)
    h2 = run_healthcheck(pr2, cap)
    pk = build_consult_packet(review=rv, health=hf, contexts=ctxs, capital=cap,
                              question="开这个远一点的价差如何?", mode="pre_trade",
                              pre_trade={"spec": "SLV260919P58000.US:-2:0.6,SLV260919P56000.US:2:0.3",
                                         "review": pr2, "health": h2},
                              asof=date(2026, 8, 24))
    assert pk["mode"] == "pre_trade"
    assert pk["pre_trade"]["spec"].startswith("SLV260919P58000")
    assert pk["pre_trade"]["portfolio"]["groups"][0]["combos"], pk["pre_trade"]
    assert "拟开仓" in pk["prompt"]
    print("PASS test_pretrade_block_included")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
