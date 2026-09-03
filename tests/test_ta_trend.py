"""Supertrend 与 UT Bot 的口径锁（两者同族，差异在参数与轨道结构）。"""
import pytest

from undertow.analyze.ta import atr, true_range
from undertow.analyze.ta import supertrend as ST, ut_bot as UT


# ── ATR 基础 ──────────────────────────────────────────────────────
def test_真实波幅首根退化为高低差():
    h, l, c = [10.0, 12.0], [8.0, 9.0], [9.0, 11.0]
    assert true_range(h, l, c)[0] == 2.0


def test_真实波幅含跳空():
    """前收 20，本根 10~12，真实波幅应取 |high−前收| = 10 而非 high−low = 2。"""
    h, l, c = [20.0, 12.0], [20.0, 10.0], [20.0, 11.0]
    assert true_range(h, l, c)[1] == pytest.approx(10.0)


def test_atr两种口径可切且结果不同():
    """Supertrend 脚本有 changeATR 开关：true→ta.atr(RMA)，false→sma(tr)。"""
    h = [10.0 + (i % 5) for i in range(40)]
    l = [x - 2 for x in h]
    c = [x - 1 for x in h]
    assert atr(h, l, c, 10, method="rma")[-1] != atr(h, l, c, 10, method="sma")[-1]
    with pytest.raises(ValueError):
        atr(h, l, c, 10, method="ema")


# ── Supertrend ────────────────────────────────────────────────────
def test_默认参数与脚本一致():
    assert ST.PERIOD == 10 and ST.MULT == 3.0


def test_翻转判定必须用前一根的轨():
    """源码 trend := trend==-1 and close > dn1 ? 1 : trend==1 and close < up1 ? -1
    用的是 dn1/up1（前一根）。写成当根值会让信号提前一根 ——
    4h 上就是提前 4 小时，回测凭空多出一截收益。"""
    import inspect
    src = inspect.getsource(ST.supertrend)
    assert "closes[i] > d1" in src and "closes[i] < u1" in src
    assert "前一根" in src


def test_下轨棘轮只抬不落():
    """up := close[1] > up1 ? max(up, up1) : up"""
    import inspect
    src = inspect.getsource(ST.supertrend)
    assert "max(u, u1)" in src and "min(d, d1)" in src


def test_源是hl2不是close():
    import inspect
    assert "(h + l) / 2" in inspect.getsource(ST.supertrend)


def test_三条序列等长():
    n = 60
    h = [100.0 + i * 0.5 for i in range(n)]
    l = [x - 2 for x in h]; c = [x - 1 for x in h]
    up, dn, tr = ST.supertrend(h, l, c)
    assert len(up) == len(dn) == len(tr) == n


def test_单边上涨里保持多头且下轨不回落():
    n = 80
    h = [100.0 + i for i in range(n)]
    l = [x - 2 for x in h]; c = [x - 0.5 for x in h]
    up, _, tr = ST.supertrend(h, l, c)
    assert tr[-1] == 1
    v = [x for x in up if x is not None]
    assert all(b >= a - 1e-9 for a, b in zip(v, v[1:])), "多头段下轨不应回落"


def test_数据不足返回None():
    assert ST.read([1.0], [1.0], [1.0]) is None


# ── UT Bot ────────────────────────────────────────────────────────
def test_UT默认倍数是1远比Supertrend敏感():
    """这是两者最实质的差异：1×ATR vs 3×ATR。"""
    assert UT.KEY == 1.0 and ST.MULT == 3.0


def test_UT只有一条轨而Supertrend有两条():
    st = ST.supertrend([10.0] * 40, [8.0] * 40, [9.0] * 40)
    ut = UT.ut_bot([10.0] * 40, [8.0] * 40, [9.0] * 40)
    assert len(st) == 3, "Supertrend 返回 up/dn/trend"
    assert len(ut) == 2, "UT Bot 返回 stop/pos"


def test_UT源码里ema周期1等于src本身这点写进注释():
    """ema(src,1) 的 alpha=2/(1+1)=1，就是 src。作者绕这圈只为用 crossover()。"""
    import pathlib
    src = pathlib.Path("undertow/analyze/ta/ut_bot.py").read_text("utf-8")
    assert "周期 1 的 EMA 就是 src 本身" in src


def test_UT源码里buy条件冗余这点写进注释():
    """buy = src > stop and above，而 above=crossover(src,stop) 已蕴含 src>stop。"""
    import pathlib
    src = pathlib.Path("undertow/analyze/ta/ut_bot.py").read_text("utf-8")
    assert "冗余" in src


def test_UT在同一段数据上信号远多于Supertrend():
    """实测 200 根上 UT 是 ST 的 3~5 倍。这决定了两者的换手成本量级。"""
    import random
    random.seed(7)
    c, p = [], 100.0
    for _ in range(300):
        p *= 1 + random.gauss(0, 0.01)
        c.append(p)
    h = [x * 1.005 for x in c]; l = [x * 0.995 for x in c]
    assert len(UT.flips(h, l, c)) > len(ST.flips(h, l, c))


def test_平均K线首根用开收均值():
    o = [10.0, 11.0]; h = [12.0, 13.0]; l = [9.0, 10.0]; c = [11.0, 12.0]
    assert UT.heikin_ashi(o, h, l, c)[0] == pytest.approx((10 + 12 + 9 + 11) / 4)


def test_UT默认不用平均K线():
    """源码 h = input(false)。"""
    import inspect
    assert inspect.signature(UT.ut_bot).parameters["src"].default is None


# ── 边界 ──────────────────────────────────────────────────────────
def test_两个子模块都声明不进研报():
    import pathlib
    for f in ("supertrend.py", "ut_bot.py"):
        src = pathlib.Path(f"undertow/analyze/ta/{f}").read_text("utf-8")
        assert "不进研报" in src and "不进方向投票" in src
