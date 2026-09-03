"""趋势指标回溯的成交假设锁 —— 2026-09-03 的修正。"""
import pytest

from undertow.analyze.ta import backtest as B


def _bars(n=30):
    o = [100.0 + i for i in range(n)]
    c = [x + 0.5 for x in o]
    return o, c


def test_成交价用次根开盘而非信号根收盘():
    """Pine strategy.entry 是次根开盘成交。用信号根收盘会系统性偏乐观：
    2026-09-03 实测 6 个组合里 5 个变差，SLV 1d 差 14.6pp，
    「跑赢 3.9 点」翻成「跑输 10.6 点」。"""
    o, c = _bars()
    r = B.run(o, c, [(5, 1), (10, -1)])
    assert r.segments[0].entry_px == o[6], "应为信号根的下一根开盘"
    assert r.segments[0].entry_px != c[5], "不得用信号根收盘"


def test_进场价拿不到次根开盘时丢弃该段():
    o, c = _bars(10)
    r = B.run(o, c, [(9, 1)])          # 9+1=10 越界，进不了场
    assert r.n == 0, "进不了场的段应丢弃而非用收盘价凑"


def test_中间段的出场也用次根开盘():
    o, c = _bars(30)
    r = B.run(o, c, [(2, 1), (8, -1), (14, 1)])
    assert r.segments[0].exit_px == o[9] and not r.segments[0].is_open
    assert r.segments[1].exit_px == o[15] and not r.segments[1].is_open


def test_必然计入换手成本():
    o, c = _bars()
    r = B.run(o, c, [(2, 1), (8, -1), (14, 1)])
    assert r.total_cost_pct == pytest.approx(r.n * B.DEFAULT_COST_PCT)
    assert r.net_pct == pytest.approx(r.gross_pct - r.total_cost_pct)


def test_默认成本非零():
    """TradingView strategy 测试器默认 0 手续费 0 滑点，我们不能跟。"""
    assert B.DEFAULT_COST_PCT > 0


def test_做空段收益方向取反():
    o = [100.0] * 5 + [90.0] * 5
    c = list(o)
    r = B.run(o, c, [(0, -1), (7, 1)])
    assert r.segments[0].ret_pct > 0, "下跌段做空应为正收益"


def test_末段未平仓用最新收盘做市值标记():
    """进场价必须是真实成交（次根开盘），但未平仓头寸的估值本来就用最新价。"""
    o, c = _bars(20)
    r = B.run(o, c, [(3, 1)])
    s = r.segments[0]
    assert s.exit_idx == len(c) - 1 and s.is_open
    assert s.exit_px == c[-1] and s.entry_px == o[4]


def test_买入持有基准同期计算():
    o, c = _bars(20)
    r = B.run(o, c, [(3, 1)])
    assert r.buy_hold_pct == pytest.approx((c[-1] / c[0] - 1) * 100)
    assert r.vs_buy_hold == pytest.approx(r.net_pct - r.buy_hold_pct)


def test_胜率带着不可用于评价的警告():
    """趋势跟踪天生低胜率靠厚尾（实测 30~44%），用胜率评价是错的。"""
    import inspect
    assert "用胜率评价这类指标是错的" in inspect.getdoc(B.Result.win_rate.fget)


def test_盈亏比在无亏损段时为无穷():
    o = [100.0 + i for i in range(20)]
    r = B.run(o, list(o), [(2, 1)])
    assert r.profit_factor == float("inf")


def test_模块声明这不是策略回测():
    import pathlib
    src = pathlib.Path("undertow/analyze/ta/backtest.py").read_text("utf-8")
    assert "这不是策略回测" in src and "描述性统计" in src
    assert "换个成交假设就能翻盘" in src
