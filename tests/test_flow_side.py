"""资金流买卖方推断验证（协议 v0）：报价规则优先、tick 规则兜底、分档与一致率。"""
from datetime import datetime, timedelta, timezone

import pytest

from undertow.analyze import flow_side as fs

T0 = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)


def m(i):
    return T0 + timedelta(minutes=i)


def test_quote_rule_then_tick_rule():
    rows = [(m(0), 1.00, 10), (m(1), 1.05, 5), (m(2), 1.05, 3), (m(10), 1.02, 7), (m(11), 1.02, 0)]
    quotes = [(m(0), 0.98, 1.00)]                          # 0 分钟：价 1.00 > 中价 0.99 → buy（报价规则）
    c = fs.classify_minutes(rows, quotes)
    assert c["by_quote"] == 18 and c["by_tick"] == 7                             # 0–2 分钟在报价 ±2 分钟窗内
    assert c["buy"] == 10 + 5 + 3 and c["sell"] == 7                             # 第 10 分钟无报价：1.02 < 1.05 → sell（tick）
    assert c["unclassified"] == 0 and fs.buy_share(c) == pytest.approx(18 / 25)


def test_first_minute_unclassified_without_quote_and_bad_quotes_ignored():
    rows = [(m(0), 1.0, 4), (m(1), 0.9, 6)]
    c = fs.classify_minutes(rows, [(m(0), 1.2, 1.0), (m(1), None, 1.0)])   # 交叉、缺一侧的报价都不用
    assert c["unclassified"] == 4 and c["sell"] == 6 and c["by_quote"] == 0


def test_labels_and_agreement():
    assert fs.trade_label(0.6) == "buy" and fs.trade_label(0.5) == "mixed" and fs.trade_label(None) == "unknown"
    assert fs.inferred_label(0.3) == "buy" and fs.inferred_label(-0.1) == "sell" and fs.inferred_label(0.0) is None
    rows = [{"inferred": "buy", "traded": "buy", "weight": 3, "has_data": True},
            {"inferred": "buy", "traded": "sell", "weight": 1, "has_data": True},
            {"inferred": "sell", "traded": "mixed", "weight": 2, "has_data": True},
            {"inferred": "sell", "traded": "unknown", "weight": 4, "has_data": False}]
    a = fs.agreement(rows)
    assert a["n_labelled"] == 2 and a["agree_rate"] == 0.5 and a["agree_rate_weighted"] == 0.75
    assert a["weight_covered"] == 0.6 and a["n_mixed"] == 1 and a["n_unknown"] == 1


# —— v1：主表只用严格过去的报价（Codex 030 反例）——
def test_v1_future_quote_cannot_change_main_classification():
    rows = [(m(0), 1.1, 100)]
    prior = (m(-3), m(-1), 1.2, 1.4)                         # 桶在分钟起点前结束：严格过去
    future = (m(0) + timedelta(seconds=30), m(1), 0.8, 1.0)   # 桶在分钟之后
    a = fs.classify_v1(rows, [prior], "quote_past")
    b = fs.classify_v1(rows, [prior, future], "quote_past")
    assert a["sell"] == b["sell"] == 100 and a["buy"] == b["buy"] == 0
    assert fs.classify_v1(rows, [future], "quote_past")["unclassified"] == 100      # 只有之后的报价 → 主表不分类
    assert fs.classify_v1(rows, [future], "quote_any_window")["buy"] == 100        # 敏感性口径会被之后的报价改写


def test_v1_quote_age_limit_and_bucket_spanning_minute():
    rows = [(m(0), 1.1, 10)]
    old = (m(-30), m(-20), 1.2, 1.4)                          # 最短年龄 20 分钟 > 15
    assert fs.classify_v1(rows, [old], "quote_past")["unclassified"] == 10
    spanning = (m(-1), m(1), 1.2, 1.4)                        # 桶跨过分钟起点：不算严格过去
    assert fs.classify_v1(rows, [spanning], "quote_past")["unclassified"] == 10
    c = fs.classify_v1(rows, [(m(-5), m(-2), 1.2, 1.4)], "quote_past")
    assert c["quote_age_min_s"] == [120.0] and c["quote_age_max_s"] == [300.0]


def test_v1_modes_reported_separately_and_confusion():
    rows = [(m(0), 1.0, 5), (m(1), 1.2, 5)]
    assert fs.classify_v1(rows, [], "tick_only")["buy"] == 5 and fs.classify_v1(rows, [], "quote_past")["unclassified"] == 10
    assert fs.minute_sign_volume_share({"buy": 3, "sell": 1}) == 0.75
    cm = fs.confusion([{"inferred": "buy", "traded": "buy"}, {"inferred": "buy", "traded": "sell"},
                       {"inferred": "sell", "traded": "unknown"}])
    assert cm["matrix"] == {"buy→buy": 1, "buy→sell": 1, "sell→unknown": 1} and cm["base_rate_traded_buy"] == 0.5
