"""随机指标 + 分阶段止损的口径锁（取自 EMA Trend + MTF Stochastic Strategy）。"""
import pytest

from undertow.analyze.ta import highest, linreg, lowest
from undertow.analyze.ta import risk as R, stoch as S
from undertow.analyze.ta.frames import MTF_PARENT


# ── 基础 ──────────────────────────────────────────────────────────
def test_linreg是拟合不是均值可以外推出源值域():
    """MTF 分支对 k 套了 linreg，实测 GLD 1h 的 MTF-k 出现 −6.9。
    随机指标本身在 [0,100]，linreg 后不再受此约束 ——
    脚本的 crossover(mtfK, 50) 作用在一个无界量上。"""
    xs = [float(100 - i * 10) for i in range(10)]   # 单调下降
    v = linreg(xs, 5)[-1]
    assert v < min(xs[-5:]) or v == pytest.approx(xs[-1]), "拟合端点可低于窗口最小值"


def test_linreg对直线还原斜率():
    xs = [2.0 * i + 3 for i in range(20)]
    assert linreg(xs, 10)[-1] == pytest.approx(xs[-1])


def test_linreg窗口过小抛错():
    with pytest.raises(ValueError):
        linreg([1.0, 2.0], 1)


def test_highest_lowest对齐():
    xs = [3.0, 1.0, 4.0, 1.0, 5.0]
    assert highest(xs, 3)[-1] == 5.0 and lowest(xs, 3)[-1] == 1.0
    assert highest(xs, 3)[0] is None


# ── 随机指标 ──────────────────────────────────────────────────────
def test_stoch公式与Pine一致():
    c = [10.0, 12.0, 15.0]; h = [11.0, 13.0, 16.0]; l = [9.0, 10.0, 12.0]
    v = S.raw_stoch(c, h, l, 3)[-1]
    assert v == pytest.approx((15 - 9) / (16 - 9) * 100)


def test_区间为零时取中值不除零():
    c = [10.0] * 5; h = [10.0] * 5; l = [10.0] * 5
    assert S.raw_stoch(c, h, l, 3)[-1] == 50.0


def test_kd平滑参数与脚本一致():
    assert S.LEN == 11 and S.SMOOTH_K == 3 and S.SMOOTH_D == 3
    assert S.UP_LINE == 80 and S.LOW_LINE == 20


def test_MTF对齐不得前瞻():
    """第 k 根高周期 K 线，只有在第 k+1 根开始之后才可见；末根永远不可见。

    2026-09-03 修掉的 P0：长桥的 ts 是 K 线**开始**时间，原来的
    `mtf_ts <= t` 让 4h 在 16:30 就读到当天日线（ts=04:00）的收盘价，
    泄露 3.5 小时。"""
    out = S.align_mtf([1, 2, 3, 4, 5], [2, 4], [10.0, 20.0])
    assert out == [None, None, None, 10.0, 10.0]
    assert out[1] is None, "t=2 时第 0 根刚开始，远未收盘"
    assert out[4] == 10.0, "末根(ts=4)可能未收盘，只能看到第 0 根"


def test_MTF末根永远不可见():
    out = S.align_mtf([100], [2, 4], [10.0, 20.0])
    assert out == [10.0], "即使时间远超，也不得看到末根"


def test_MTF按开始时间对齐会泄露的具体场景():
    """回归锁：日线 ts=04:00、4h ts=16:30 的真实时间戳形态。"""
    day_ts = [1000, 2000]        # 两根日线的开始时间
    h4_ts = [1500, 1800, 2500]   # 4h，其中 1500/1800 在第 0 根日线当天
    out = S.align_mtf(h4_ts, day_ts, [7.0, 8.0])
    assert out[0] is None and out[1] is None, "当天盘中不得读到当天日线"
    assert out[2] == 7.0, "次日才能读到前一日"


def test_MTF映射到相邻上一级():
    assert MTF_PARENT == {"15m": "1h", "1h": "4h", "4h": "1d", "1d": None}


# ── 分阶段止损 ────────────────────────────────────────────────────
def test_进场价必须是实际成交价而非信号价():
    """原脚本 entryPrice := close 记的是信号根收盘，但 strategy.entry
    在次根开盘成交。跳空时 R 的基准是个从未成交过的价格，
    会过早判定盈利 1R 而把止损移到「保本」—— 那个位置对实际成本是亏的。"""
    import inspect
    doc = inspect.getdoc(R.Position)
    assert "实际成交价" in doc and "不是信号根收盘" in doc


def test_三段推进的顺序与阈值():
    p = R.open_position(1, 100.0, 2.0, 0)      # 止损距 3.0，止损 97
    assert p.stop == pytest.approx(97.0) and p.stage == R.INITIAL
    p = p.update(101.0, 2.0)
    assert p.stage == R.INITIAL, "0.33R 时不动"
    p = p.update(103.0, 2.0)
    assert p.stop == pytest.approx(100.0) and p.stage == R.BREAKEVEN, "1R 移保本"
    p = p.update(104.5, 2.0)
    assert p.stage == R.TRAILING and p.stop > 100.0, "1.5R 开始追踪"


def test_阶段只前进不后退():
    """止损只收紧不放松，阶段标签也不该回退 ——
    原脚本每根重算 if/elif，价格回落到 1.5R 以下时标签会从「追踪中」退回「已保本」。"""
    p = R.open_position(1, 100.0, 2.0, 0).update(107.0, 2.0)
    assert p.stage == R.TRAILING
    p2 = p.update(103.5, 2.0)                  # R 掉到 1.17
    assert p2.stage == R.TRAILING, "不得退回已保本"
    assert p2.stop == p.stop, "止损同时也不得放松"


def test_止损只朝有利方向移动():
    p = R.open_position(1, 100.0, 2.0, 0).update(107.0, 2.0)
    high = p.stop
    p = p.update(105.0, 2.0)                    # 价格回落
    assert p.stop == high, "止损不得放松"


def test_空头方向镜像():
    p = R.open_position(-1, 100.0, 2.0, 0)
    assert p.stop == pytest.approx(103.0)
    p = p.update(97.0, 2.0)
    assert p.stop == pytest.approx(100.0) and p.stage == R.BREAKEVEN
    p2 = p.update(95.5, 2.0)
    assert p2.stage == R.TRAILING and p2.stop < 100.0


def test_止损触发用最高最低价而非收盘():
    """收盘价判定会漏掉盘中被扫的情况，回测显著虚高。"""
    p = R.open_position(1, 100.0, 2.0, 0)       # 止损 97
    assert p.hit(low=96.0, high=101.0), "盘中跌破即触发"
    assert not p.hit(low=97.5, high=101.0)


def test_R倍数计算():
    p = R.open_position(1, 100.0, 2.0, 0)       # 止损距 3.0
    assert p.r_multiple(103.0) == pytest.approx(1.0)
    assert p.r_multiple(97.0) == pytest.approx(-1.0)


def test_时间止损():
    p = R.open_position(1, 100.0, 2.0, bar=10)
    assert not p.timed_out(69) and p.timed_out(70)
    assert not p.timed_out(9999, max_bars=0), "0 表示关闭"


# ── 风险仓位 ──────────────────────────────────────────────────────
def test_风险仓位按止损距离反推手数():
    """每笔亏损固定为权益的 risk_pct%，止损远则手数少。
    这比固定手数或固定比例仓位正确：后两者在波动大时敞口自动放大。"""
    assert R.size(10000, 5.0, risk_pct=1.0) == pytest.approx(20.0)
    assert R.size(10000, 10.0, risk_pct=1.0) == pytest.approx(10.0)


def test_止损距离为零时仓位为零不除零():
    assert R.size(10000, 0.0) == 0.0
    assert R.size(0, 5.0) == 0.0


def test_模块声明不进研报():
    import pathlib
    for f in ("stoch.py", "risk.py"):
        src = pathlib.Path(f"undertow/analyze/ta/{f}").read_text("utf-8")
        assert "不进研报" in src


# ── 行为断言（补强只锁了文档字符串的那几条）──────────────────────
def test_进场价不同则R与止损位都不同():
    """行为验证：信号根收盘 100、次根开盘 102 时，同样的 ATR 给出不同的
    止损位与 R。用错价格不是标签问题，是算错止损。"""
    a = R.open_position(1, 100.0, 2.0, 0)      # 若误用信号根收盘
    b = R.open_position(1, 102.0, 2.0, 0)      # 实际成交价
    assert a.stop != b.stop
    assert a.r_multiple(103.0) != b.r_multiple(103.0)
    # 关键后果：用 a 的基准会把 103 判成 1R（该移保本），用 b 只有 0.33R
    assert a.r_multiple(103.0) == pytest.approx(1.0)
    assert b.r_multiple(103.0) < 0.5


def test_跳空时误用信号价会过早移保本到亏损位():
    """具体重现原脚本的后果：次根跳空高开 2%，
    误用信号价算 R 会在实际仍亏损时判定「已保本」。"""
    signal_close, actual_open = 100.0, 102.0
    wrong = R.open_position(1, signal_close, 2.0, 0).update(103.0, 2.0)
    right = R.open_position(1, actual_open, 2.0, 0).update(103.0, 2.0)
    assert wrong.stage == R.BREAKEVEN and wrong.stop == pytest.approx(100.0)
    assert right.stage == R.INITIAL
    # 100 这个「保本位」对实际成本 102 来说是亏 2%
    assert wrong.stop < actual_open
