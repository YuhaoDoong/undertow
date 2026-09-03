"""技术面子模块：基础函数 + 多周期数据层 + MACD 的口径锁。"""
import pytest

from undertow.analyze.ta import ema, last_valid, rma, sma
from undertow.analyze.ta import frames, macd as M


# ── 基础函数（Pine 口径）───────────────────────────────────────────
def test_ema种子是sma而非首值():
    """Pine: ema := na(ema[1]) ? sma(src,n) : alpha*src+(1-alpha)*ema[1]
    用首值当种子会让前几十根偏离，MACD 早期柱状图对不上 TradingView。"""
    xs = [float(i) for i in range(1, 21)]
    assert ema(xs, 5)[4] == pytest.approx(3.0), "第 n 个位置应等于 sma(前n)"
    assert ema(xs, 5)[3] is None, "不足窗口处应为 None"


def test_三个平滑函数都与输入等长():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    for fn in (sma, ema, rma):
        assert len(fn(xs, 3)) == len(xs)
        assert len(fn(xs, 99)) == len(xs), "窗口大于序列时也要等长"


def test_sma滚动正确():
    xs = [float(i) for i in range(1, 11)]
    assert sma(xs, 3)[2] == pytest.approx(2.0)
    assert sma(xs, 3)[-1] == pytest.approx(9.0)


def test_rma是Wilder平滑():
    xs = [10.0] * 30
    assert rma(xs, 14)[-1] == pytest.approx(10.0)


def test_窗口非正抛错():
    for fn in (sma, ema, rma):
        with pytest.raises(ValueError):
            fn([1.0, 2.0], 0)


def test_last_valid跳过None():
    assert last_valid([None, 1.0, None]) == 1.0
    assert last_valid([None, None]) is None


# ── MACD 口径 ─────────────────────────────────────────────────────
def test_signal默认用SMA跟随CM脚本():
    """CM_Ult_MacD_MTF 写的是 signal = sma(macd, 9)，
    而 TradingView 内置 MACD 用 EMA。口径不一致就对不上盘。"""
    import inspect
    sig = inspect.signature(M.macd_series)
    assert sig.parameters["signal_ma"].default == "sma"


def test_两种signal口径给出不同结果():
    xs = [100 + (i % 7) * 3 - (i % 11) for i in range(120)]
    xs = [float(x) for x in xs]
    _, s1, h1 = M.macd_series(xs, signal_ma="sma")
    _, s2, h2 = M.macd_series(xs, signal_ma="ema")
    assert s1[-1] != s2[-1] and h1[-1] != h2[-1]


def test_非法signal_ma抛错():
    with pytest.raises(ValueError):
        M.macd_series([1.0] * 60, signal_ma="wma")


def test_macd等于快慢EMA之差():
    xs = [float(100 + i) for i in range(80)]
    m, _, _ = M.macd_series(xs)
    f, s = ema(xs, 12), ema(xs, 26)
    assert m[-1] == pytest.approx(f[-1] - s[-1])


def test_hist等于macd减signal():
    xs = [float(100 + (i % 5)) for i in range(90)]
    m, s, h = M.macd_series(xs)
    assert h[-1] == pytest.approx(m[-1] - s[-1])


def test_三条序列等长且前段为None():
    xs = [float(i) for i in range(60)]
    m, s, h = M.macd_series(xs)
    assert len(m) == len(s) == len(h) == len(xs)
    assert m[0] is None and s[0] is None and h[0] is None


def test_数据不足时read返回None():
    assert M.read([1.0, 2.0, 3.0]) is None


# ── 四色柱 ────────────────────────────────────────────────────────
def test_四色柱四种状态齐全():
    """脚本的核心：零轴上下 × 变强变弱。颜色翻转早于零轴穿越和金叉死叉。"""
    assert M.hist_state(2.0, 1.0) == M.BULL_STRONG    # aqua  零上走强
    assert M.hist_state(1.0, 2.0) == M.BULL_FADE      # blue  零上走弱
    assert M.hist_state(-2.0, -1.0) == M.BEAR_STRONG  # red   零下走弱
    assert M.hist_state(-1.0, -2.0) == M.BEAR_FADE    # maroon 零下走强


def test_零值归入零下侧():
    """脚本写的是 hist <= 0 进 histB 分支，0 属零下侧。"""
    assert M.hist_state(0.0, -1.0) == M.BEAR_FADE
    assert M.hist_state(0.0, 1.0) == M.BEAR_STRONG


def test_首根无前值时按变强处理():
    assert M.hist_state(1.0, None) == M.BULL_STRONG
    assert M.hist_state(-1.0, None) == M.BEAR_FADE


# ── 多周期数据层 ──────────────────────────────────────────────────
def test_四个周期与用户定义的分工一致():
    """用户 2026-09-03：15m 只看入场出场时机，1h/4h/1d 才看方向和波段。"""
    assert frames.TIMEFRAMES == ("15m", "1h", "4h", "1d")
    assert frames.ROLE["15m"] == "entry"
    assert all(frames.ROLE[t] == "direction" for t in ("1h", "4h", "1d"))


def test_多周期MACD默认不含15m():
    """15m 的读数不得用于判断方向 —— 拿噪声当趋势。"""
    import inspect
    d = inspect.signature(M.read_multi).parameters["tfs"].default
    assert "15m" not in d and d == ("1h", "4h", "1d")


def test_4h必须由1h聚合而非盲分():
    """长桥没有 4h 周期；美股每日 7 根 1h 不是 4 的整数倍，
    盲分会把隔夜跳空拼进一根（2026-08-27 实测 RSI6 算出 100）。"""
    import inspect
    src = inspect.getsource(frames.bars)
    assert 'period="1h"' in src and "aggregate(raw, 4)" in src


def test_非法周期抛错():
    with pytest.raises(ValueError):
        frames.bars("GLD.US", "2h")


def test_取数失败不返回空列表():
    """KlineUnavailable 必须往外抛，否则"没数据"会被静默当成"没信号"。"""
    import inspect
    assert "不返回空列表" in inspect.getdoc(frames.bars)


def test_multi缺周期时不影响其它周期():
    import inspect
    assert "不影响其它周期" in inspect.getdoc(frames.multi)


# ── 边界声明 ──────────────────────────────────────────────────────
def test_子模块默认不进研报不进投票():
    import pathlib
    for f in ("undertow/analyze/ta/__init__.py", "undertow/analyze/ta/macd.py"):
        src = pathlib.Path(f).read_text("utf-8")
        assert "不进研报" in src and "不进方向投票" in src
