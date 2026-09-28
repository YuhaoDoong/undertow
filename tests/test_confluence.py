"""conf-v1：同向 OB 交汇判定、缓冲分层、Mantel–Haenszel 风险差、A 检验集守卫。"""
from types import SimpleNamespace

import pytest

from scripts import conf_a_backtest as A
from scripts import conf_b_walls as B
from undertow.analyze import smc


def _z(bias, lo, hi):
    return SimpleNamespace(bias=bias, lo=lo, hi=hi)


def test_confluent_requires_same_side_ob_overlap():
    ev_above = {"side": "above", "zlo": 100.0, "zhi": 102.0}
    assert A.confluent(ev_above, [_z(smc.BULLISH, 101.5, 103)])
    assert not A.confluent(ev_above, [_z(smc.BEARISH, 101.5, 103)])        # 方向不对
    assert not A.confluent(ev_above, [_z(smc.BULLISH, 102.5, 103)])        # 不重叠
    assert A.confluent({"side": "below", "zlo": 100.0, "zhi": 102.0}, [_z(smc.BEARISH, 99, 100.5)])


def test_buffer_layers_and_mh_risk_difference():
    assert [B._layer(x) for x in (0.1, 0.5, 0.99, 1.5, 3)] == [0, 1, 1, 2, 3]
    rows = ([{"buf_atr": 0.2, "g": "a", "breach": b} for b in (1, 1, 0, 0)] +
            [{"buf_atr": 0.2, "g": "b", "breach": b} for b in (1, 0, 0, 0)] +
            [{"buf_atr": 3.0, "g": "a", "breach": b} for b in (0, 0)] + [{"buf_atr": 3.0, "g": "b", "breach": 0}])
    rd = B.mh_rd(rows, lambda r: r["g"] == "a", lambda r: r["g"] == "b")
    assert rd == pytest.approx((2.0 * 0.25 + (2 / 3) * 0.0) / (2.0 + 2 / 3))


def test_a_test_set_requires_review(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["x", "--set", "test"])
    with pytest.raises(SystemExit) as e:
        A.main()
    assert "Codex" in str(e.value)
