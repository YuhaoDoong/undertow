"""模拟仓自动选档规则 wall-dynamic-v1（草案）：墙识别、缓冲、买腿只需 ask、宽度上限、无合格即不开。纯合成数据。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import paper_strike_rule as sr  # noqa: E402

LISTED = [372, 373, 374, 375, 376, 377, 378, 379, 380]
OI = {380: 19352, 375: 13312, 378: 800, 379: 545, 372: 1400}


def Q(**kv):
    return {k: {"bid": b, "ask": a, "error": ""} for k, (b, a) in kv.items()}


def test_wall_too_close_sells_just_below_wall():
    """GLD 381.0：380 墙离价 0.26% < 0.5% → 卖 379（0.52%）；买腿由窄到宽取第一个过 15% 的。"""
    q = {379: {"bid": 0.92, "ask": 1.0, "error": ""}, 378: {"bid": 0.68, "ask": 0.74, "error": ""},
         377: {"bid": 0.43, "ask": 0.54, "error": ""}}
    r = sr.select_put_spread(381.0, OI, LISTED, q)
    assert r["ok"] and r["nearest_wall"] == 380 and r["sell"] == 379 and r["buy"] == 378 and r["credit"] == 0.18


def test_wall_with_buffer_is_sold_directly():
    q = {380: {"bid": 0.84, "ask": 0.98, "error": ""}, 379: {"bid": 0.58, "ask": 0.72, "error": ""},
         378: {"bid": 0.44, "ask": 0.53, "error": ""}}
    r = sr.select_put_spread(382.24, OI, LISTED, q)                    # 380 离价 0.59% ≥ 0.5%
    assert r["ok"] and r["sell"] == 380 and r["buy"] == 378
    assert r["tried"][0]["reject"] == "credit_ratio_low" and r["tried"][0]["ratio"] < 0.15   # 380/379 只有 12%


def test_buy_leg_needs_only_ask_and_no_qualifying_means_no_trade():
    listed = [52, 52.5, 53, 53.5, 54, 54.5]
    oi = {55: 11697, 54: 9069, 53: 16106, 54.5: 4766}
    q = {54: {"bid": 0.13, "ask": 0.15, "error": ""}, 53.5: {"bid": None, "ask": 0.05, "error": ""},
         53: {"bid": 0.03, "ask": 0.04, "error": ""}}
    r = sr.select_put_spread(54.993, oi, listed, q)                    # 55 在现价上方不算；最近的墙是 54
    assert r["nearest_wall"] == 54 and r["sell"] == 54 and r["ok"] and r["buy"] == 53.5 and r["credit"] == 0.08
    q[53.5]["ask"] = 0.07                                              # 0.06/0.5=12%；53: 0.09/1=9% → 不开
    r = sr.select_put_spread(54.993, oi, listed, q)
    assert not r["ok"] and r["reason"].startswith("no_qualifying_width")


def test_sell_leg_without_bid_or_no_wall():
    r = sr.select_put_spread(381.0, OI, LISTED, {379: {"bid": None, "ask": 1.0, "error": ""}})
    assert not r["ok"] and r["reason"].startswith("sell_no_bid")
    assert sr.select_put_spread(381.0, {390: 5000}, LISTED, {})["reason"].startswith("no_wall")


def test_width_cap_stops_super_wide_protection():
    """宽度上限 1.5%×现价：SLV 55 附近上限约 0.83，55/52 这类 3 美元宽的不会被选。"""
    listed = [52, 54.5]
    q = {54.5: {"bid": 0.2, "ask": 0.22, "error": ""}, 52: {"bid": 0.01, "ask": 0.02, "error": ""}}
    r = sr.select_put_spread(55.2, {54.5: 5000}, listed, q)
    assert not r["ok"] and r["tried"] == []


def test_crossed_quotes_rejected_even_if_other_side_present():
    """Codex 028 probe：交叉报价曾被接受。"""
    q = {379: {"bid": 1.2, "ask": 1.0, "error": ""}, 378: {"bid": 0.6, "ask": 0.7, "error": ""}}
    r = sr.select_put_spread(381.0, OI, LISTED, q)
    assert not r["ok"] and r["reason"].startswith("sell_crossed")
    q = {379: {"bid": 0.92, "ask": 1.0, "error": ""}, 378: {"bid": 0.8, "ask": 0.7, "error": ""},
         377: {"bid": 0.4, "ask": 0.5, "error": ""}}
    r = sr.select_put_spread(381.0, OI, LISTED, q)
    assert r["tried"][0]["reject"] == "buy_crossed" and r["buy"] == 377


def test_fee_constraint_blocks_tiny_width():
    """Codex 028 probe：宽 0.05、权利金 0.01（20%）曾被选中，毛收入 $1 < 手续费 $3.20。"""
    listed = [99.9, 99.95]
    q = {99.95: {"bid": 0.03, "ask": 0.04, "error": ""}, 99.9: {"bid": 0.01, "ask": 0.02, "error": ""}}
    r = sr.select_put_spread(100.5, {99.95: 1000}, listed, q)
    assert not r["ok"] and r["tried"][0]["reject"].startswith("fee_exceeds_credit")


def test_shadow_without_ratio_computed_on_same_candidates():
    q = {379: {"bid": 0.92, "ask": 1.0, "error": ""}, 378: {"bid": 0.8, "ask": 0.85, "error": ""},
         377: {"bid": 0.55, "ask": 0.6, "error": ""}}
    r = sr.select_with_shadow(381.0, OI, LISTED, q)
    assert r["main"]["buy"] == 377 and r["shadow_no_ratio"]["buy"] == 378          # 378: 0.07/1=7%（含费仍为正）只在影子里过
    assert r["main"]["params_hash"] != r["shadow_no_ratio"]["params_hash"]
    assert not sr.select_put_spread(float("nan"), OI, LISTED, q)["ok"]
