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


def test_必然计入换手成本且按权益递推():
    """成本对权益的拖累是 (1−c)^n，不是 n×c。"""
    o, c = _bars()
    r = B.run(o, c, [(2, 1), (8, -1), (14, 1)])
    assert r.n == 3
    drag = (1 - B.DEFAULT_COST_PCT / 100) ** 3
    assert r.total_cost_pct == pytest.approx((1 - drag) * 100)
    assert r.total_cost_pct < r.n * B.DEFAULT_COST_PCT, "递推略小于相加"


def test_收益按权益递推而非各段相加():
    """codex P1-2：+100% 后 −50%，相加报 +50%，实际权益是 0%。
    误差可正可负，足以制造虚假的跑赢。"""
    o = [100.0, 100.0, 200.0, 200.0, 100.0, 100.0]
    r = B.run(o, o, [(0, 1), (2, 1)])
    assert [round(s.ret_pct) for s in r.segments] == [100, -50]
    assert r.gross_pct == pytest.approx(0.0), "复利后回到原点"
    assert sum(s.ret_pct for s in r.segments) == pytest.approx(50.0), "相加会得 +50"


def test_末根不可成交的flip不得连累上一段():
    """codex P1-4：最新一根刚翻转是最常见场景，
    原实现会把仍存续的当前持仓段一起丢掉。"""
    o = [100.0, 101.0, 102.0, 103.0, 104.0]
    r = B.run(o, o, [(0, 1), (4, -1)])
    assert r.n == 1, "第二个信号进不了场，但第一段应持有到末尾"
    assert r.segments[0].is_open


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


def test_买入持有基准与策略同期():
    """codex P1-3：策略在首次 flip 的次根开盘才有敞口，
    基准却从 closes[0] 起算 —— 信号前若标的下跌，
    策略靠空仓躲过，会被记成「跑赢买入持有」。"""
    o, c = _bars(20)
    r = B.run(o, c, [(3, 1)])
    assert r.buy_hold_pct == pytest.approx((c[-1] / o[4] - 1) * 100), "从首次进场价起算"
    assert r.buy_hold_pct != pytest.approx((c[-1] / c[0] - 1) * 100)
    assert r.vs_buy_hold == pytest.approx(r.net_pct - r.buy_hold_pct)


def test_信号前下跌不得被算成超额收益():
    """构造：前段大跌，策略在跌完后才进场。
    用 closes[0] 当基准会凭空多出一截「跑赢」。"""
    o = [200.0, 150.0, 100.0, 100.0, 101.0, 102.0]
    r = B.run(o, o, [(2, 1)])
    assert r.buy_hold_pct == pytest.approx((102.0 / 100.0 - 1) * 100)
    assert abs(r.vs_buy_hold) < 1.0, "同期比较下不应有大幅超额"


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
