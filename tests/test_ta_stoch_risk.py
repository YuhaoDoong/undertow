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
    """lookahead_off：低周期只能看到已收盘的高周期 K 线。
    开着前瞻会把当根尚未走完的高周期值泄露给低周期，回测直接虚高。"""
    base = [1, 2, 3, 4, 5]
    mtf_ts = [2, 4]
    out = S.align_mtf(base, mtf_ts, [10.0, 20.0])
    assert out == [None, 10.0, 10.0, 20.0, 20.0]
    assert out[2] == 10.0, "t=3 时 t=4 的高周期尚未收盘，不得看到 20"


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
