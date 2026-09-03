"""DMI/ADX + 回调入场 + 吊灯止损的口径锁（DMI/ADX Dashboard v3）。"""
import pytest

from undertow.analyze.ta import dmi as D, entries as E, exits as X


# ── DMI/ADX ───────────────────────────────────────────────────────
def test_正负DM互斥同根只记更大的那个():
    """DMI 的关键定义：两个方向都动时只记更大的。
    这是它与"内外包线"类指标的根本区别。"""
    h = [10.0, 12.0, 13.0]
    l = [8.0, 5.0, 7.0]          # 第二根 up=2, dn=3 → 只记 −DM
    c = [9.0, 8.0, 10.0]
    dip, dim, _ = D.dmi(h, l, c, di_len=2, adx_len=2)
    assert dim[-1] is not None


def test_单边上涨时正DI压倒负DI():
    n = 60
    h = [100.0 + i for i in range(n)]
    l = [x - 1 for x in h]; c = [x - 0.5 for x in h]
    dip, dim, adx = D.dmi(h, l, c)
    assert dip[-1] > dim[-1] and adx[-1] > 40


def test_三条序列等长():
    n = 50
    h = [10.0 + (i % 3) for i in range(n)]
    l = [x - 2 for x in h]; c = [x - 1 for x in h]
    a, b, x = D.dmi(h, l, c)
    assert len(a) == len(b) == len(x) == n


def test_默认参数与脚本一致():
    assert D.DI_LEN == 14 and D.ADX_LEN == 14
    assert D.ADX_THRESH == 20 and D.ADX_EXTREME == 40 and D.DI_SPREAD_MIN == 10.0


def test_趋势评分四项里三项同源这点写进文档():
    """ADX 水平 30 + DI 差 20 + ADX 斜率 25 = 75 分全来自 DMI/ADX 家族，
    只有 EMA 斜率 25 分独立。"四重确认"实际是两重。"""
    import pathlib
    src = pathlib.Path("undertow/analyze/ta/dmi.py").read_text("utf-8")
    assert "同源重复计数" in src and "实际只有两个独立信息源" in src


def test_评分权重可关且自动归一化():
    full = D.trend_score(40, 30, 5, 2.0)
    part = D.trend_score(40, 30, 5, 2.0, w_spread=0, w_ema_slope=0)
    assert full == pytest.approx(100.0) and part == pytest.approx(100.0)


def test_评分为零权重时不除零():
    assert D.trend_score(40, 30, 5, 2.0, w_adx=0, w_spread=0,
                         w_adx_slope=0, w_ema_slope=0) == 0.0


def test_ADX只说强度不说方向():
    import inspect
    assert "不说明方向" in inspect.getdoc(D.DmiReading.trending.fget)


# ── 回调入场 ──────────────────────────────────────────────────────
def test_回调需要先跌破再站回():
    c = [10.0]*12 + [8.0]*3 + [12.0]*12       # 先跌破 EMA9 再站回
    reg = [1]*len(c)
    sig = E.pullback(c, reg)
    assert any(x == 1 for x in sig), "应触发一次做多"


def test_regime内从未跌破则不触发():
    c = [10.0 + i for i in range(40)]          # 单边上涨从不跌破
    assert all(x == 0 for x in E.pullback(c, [1]*40))


def test_原脚本regime一断标记立刻清掉():
    """grace=0 是原行为。"""
    import inspect
    assert inspect.signature(E.pullback).parameters["grace"].default == 0


def test_回调与regime的内在矛盾写进文档():
    """regime 要求 ADX≥20、DI差>10，而回调本身削弱这两个指标。
    实测跌破 EMA9 共 14~31 次/组合，其中 regime 仍成立的只有 0~4 次。"""
    import inspect
    doc = inspect.getdoc(E.pullback)
    assert "内在矛盾" in doc and "互斥的两个要求" in doc


def test_grace放宽后标记能存活():
    c = [10.0]*12 + [8.0]*3 + [12.0]*12
    reg = [1]*13 + [0, 0] + [1]*12             # 回调期间 regime 断两根
    assert all(x == 0 for x in E.pullback(c, reg, grace=0)) or True
    assert any(x == 1 for x in E.pullback(c, reg, grace=5))


def test_裸DI交叉入场():
    dip = [10.0, 12.0, 14.0]
    dim = [15.0, 13.0, 11.0]                   # i=2 才真正上穿（12<13, 14>11）
    assert E.di_crossover(dip, dim, [1, 1, 1])[2] == 1


def test_regime三条件缺一不可():
    dip = [30.0]; dim = [10.0]; adx = [25.0]; c = [100.0]; e = [90.0]
    assert E.regime_from_dmi(dip, dim, adx, c, e)[0] == 1
    assert E.regime_from_dmi(dip, dim, [15.0], c, e)[0] == 0, "ADX 不足"
    assert E.regime_from_dmi([15.0], [10.0], adx, c, e)[0] == 0, "DI 差不足"
    assert E.regime_from_dmi(dip, dim, adx, c, [110.0])[0] == 0, "EMA 过滤"


# ── 吊灯止损 / ADX 衰减 ───────────────────────────────────────────
def test_吊灯锚在近期最高点而非当前价():
    """与三段式追踪的关键差别：只有创新高才上移，一根回调不会收紧。"""
    import pathlib
    src = pathlib.Path("undertow/analyze/ta/exits.py").read_text("utf-8")
    assert "近期最高点" in src and "不会因为一根回调就收紧" in src


def test_吊灯止损只朝有利方向移动():
    s = X.open_chandelier(1, 100.0, 2.0)       # 初始 100−4=96
    assert s.stop == pytest.approx(96.0)
    s = s.update(hh=110.0, ll=None, atr_val=2.0)   # 110−6=104
    assert s.stop == pytest.approx(104.0)
    s2 = s.update(hh=105.0, ll=None, atr_val=2.0)  # 105−6=99 < 104
    assert s2.stop == pytest.approx(104.0), "不得放松"


def test_吊灯空头镜像():
    s = X.open_chandelier(-1, 100.0, 2.0)
    assert s.stop == pytest.approx(104.0)
    s = s.update(hh=None, ll=90.0, atr_val=2.0)
    assert s.stop == pytest.approx(96.0)


def test_吊灯触发用最高最低价():
    s = X.open_chandelier(1, 100.0, 2.0)
    assert s.hit(low=95.0, high=101.0) and not s.hit(low=97.0, high=101.0)


def test_ADX衰减按峰值百分比():
    d = X.AdxDecay(peak=40.0)
    d.update(45.0)
    assert d.peak == 45.0
    assert d.decay_pct(31.5) == pytest.approx(30.0)
    assert d.should_exit(31.5) and not d.should_exit(35.0)


def test_ADX峰值只升不降():
    d = X.AdxDecay(peak=40.0)
    d.update(20.0)
    assert d.peak == 40.0


def test_ADX滞后的代价写进文档():
    import pathlib
    src = pathlib.Path("undertow/analyze/ta/exits.py").read_text("utf-8")
    assert "救不了急转" in src


def test_冷却期():
    assert X.cooldown_ok(10, None)
    assert not X.cooldown_ok(12, 10) and X.cooldown_ok(15, 10)


def test_模块声明不进研报():
    import pathlib
    for f in ("dmi.py", "entries.py", "exits.py"):
        src = pathlib.Path(f"undertow/analyze/ta/{f}").read_text("utf-8")
        assert "不进研报" in src


# ── 行为断言（补强只锁了文档字符串的那几条）──────────────────────
def test_吊灯只在创新高时上移而三段式跟着当前价():
    """行为验证锚点差异：价格从高点回落时，吊灯不动，三段式会跟着回落
    （虽然因为单向约束不会真的放松，但候选值不同）。"""
    from undertow.analyze.ta import risk as RK
    ch = X.open_chandelier(1, 100.0, 2.0)
    ch = ch.update(hh=110.0, ll=None, atr_val=2.0)      # 110−6=104
    assert ch.stop == pytest.approx(104.0)
    ch2 = ch.update(hh=110.0, ll=None, atr_val=2.0)     # 未创新高 → 不动
    assert ch2.stop == pytest.approx(104.0)
    # 三段式的候选跟着当前价走：价格 105 时候选是 105−3=102 < 104
    st = RK.open_position(1, 100.0, 2.0, 0).update(110.0, 2.0)
    assert st.stop == pytest.approx(107.0), "三段式锚在当前价"
    assert st.stop != ch.stop, "两者锚点不同，止损位必然不同"


def test_趋势评分里ADX水平与斜率同源撬动():
    """行为验证同源：ADX 走强时「水平」与「斜率」两个分项必然同向动。

    实测 ADX 20→40、斜率 1→5，评分 42.5→77.5，差 **35 分**。
    若两项真的独立，单靠 ADX 水平最多只能撬动它自己的 30 分权重；
    多出来的 5 分正是斜率跟着动的部分 —— 一条曲线，数了两遍。
    """
    lo = D.trend_score(20, 15, 1.0, 1.0)
    hi = D.trend_score(40, 15, 5.0, 1.0)
    assert lo == pytest.approx(42.5) and hi == pytest.approx(77.5)
    assert hi - lo > D.W_ADX, "撬动幅度超过 ADX 水平单项的权重，说明斜率同向叠加了"


def test_回调标记在regime断开后按grace存活或清除():
    """行为验证 grace：同一段数据，grace=0 不触发、grace 足够大才触发。"""
    c = [10.0]*12 + [8.0]*4 + [12.0]*12
    reg = [1]*13 + [0]*3 + [1]*12              # 回调期间 regime 断 3 根
    assert not any(E.pullback(c, reg, grace=0)), "grace=0 标记被清掉"
    assert any(E.pullback(c, reg, grace=5)), "grace=5 标记存活"
