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
