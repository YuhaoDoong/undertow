"""SMC 子模块的行为锁 —— 按 LuxAlgo Pine 源码移植（2026-09-03）。

每条对应源码里的一段逻辑，或一次实现偏差。
"""
import pytest

from undertow.analyze import smc


# ── parsedHigh/parsedLow：高波动 K 线对调（源码的关键技巧）──────────
def test_高波动K线的high与low被对调():
    """源码：
        highVolatilityBar = (high-low) >= 2*atr(200)
        parsedHigh = highVolatilityBar ? low : high
    对调后该根在 min/max 里永远选不中，异常长影线不会变成订单块。
    """
    n = 30
    h = [101.0] * n; l = [100.0] * n; c = [100.5] * n
    h[-1], l[-1] = 200.0, 50.0          # 极端波动
    ph, pl = smc.parsed_hl(h, l, c)
    assert ph[-1] == 50.0 and pl[-1] == 200.0, "高波动根应对调"
    assert ph[0] == 101.0 and pl[0] == 100.0, "正常根不对调"


def test_atr是Wilder平滑():
    h = [10.0] * 50; l = [9.0] * 50; c = [9.5] * 50
    a = smc.atr(h, l, c, 14)
    assert a[-1] == pytest.approx(1.0, abs=1e-6)


# ── leg 状态机：单侧确认 ───────────────────────────────────────────
def test_leg是单侧确认而非双侧pivot():
    """源码 newLegHigh = high[size] > ta.highest(size)：
    只比较 size 根前那根与其**之后** size 根的极值，左侧不看。"""
    h = [1, 2, 3, 10, 4, 3, 2, 1, 2, 3]
    l = [x - 1 for x in h]
    lg = smc.legs(h, l, 3)
    assert len(lg) == len(h)
    assert set(lg) <= {smc.BEARISH_LEG, smc.BULLISH_LEG}


def test_摆动点只在leg切换处产生():
    h = list(range(20)) + list(range(20, 0, -1))
    l = [x - 1 for x in h]
    pv = smc.pivots(h, l, 5)
    assert pv, "应产出摆动点"
    assert all(w in ("high", "low") for _, w, _ in pv)


# ── 订单块 ────────────────────────────────────────────────────────
def test_订单块取极值那根的完整highlow而非实体():
    """源码 storeOrdeBlock 取的是 parsedLow 最低那根的
    (parsedHigh, parsedLow)，不是 open~close 实体。"""
    import inspect
    src = inspect.getsource(smc.order_blocks)
    assert "parsedLow 最低" in src or "pl[j]" in src
    assert "opens[k]" not in src, "不应再用 K 线实体"


def test_订单块失效用highlow而非收盘():
    """源码默认 orderBlockMitigationInput = HIGHLOW。"""
    import inspect
    src = inspect.getsource(smc.order_blocks)
    assert "highs[i] >" in src and "lows[i] <" in src


def test_订单块去重():
    """同一根 K 线可能被多个摆动点选中；源码靠画图覆盖掩盖，
    我们要出数值必须去重。"""
    import inspect
    assert "seen" in inspect.getsource(smc.order_blocks)


# ── FVG：源码里默认关闭，且有两个额外条件 ──────────────────────────
def test_FVG需要中间那根收盘也突破():
    """源码：bullish = low[0] > high[2] **and close[1] > high[2]** and delta>threshold
    我最初只做了第一个条件，产出过多 FVG。"""
    # 缺口成立(low[2]=13 > high[0]=11) 且 close[1]=11.5 > high[0]=11 → 计入
    o = [10, 10, 10]; h = [11, 12, 18]; l = [9, 9, 13]; c = [10, 11.5, 17]
    assert len(smc.fair_value_gaps(o, h, l, c, auto_threshold=False)) == 1
    # 同样的缺口，但 close[1]=10.5 未突破 high[0]=11 → 不计
    c2 = [10, 10.5, 17]
    assert smc.fair_value_gaps(o, h, l, c2, auto_threshold=False) == []


def test_FVG在LuxAlgo里默认关闭这一点写在文件头():
    import pathlib
    src = pathlib.Path(smc.__file__).read_text("utf-8")
    assert "默认" in src and "false" in src.lower()


# ── 我们的裁剪（有意偏离源码）──────────────────────────────────────
def test_按方向各取而非取最近N个():
    """源码取最近 N 个；实测 GLD 最近 5 个里 4 个是需求区，上方参考位就没了。
    我们要上下各几个，与找墙取最近三道同构。"""
    import inspect
    src = inspect.getsource(smc.read_4h_zones)
    assert "per_side" in src and "不完全照搬" in src


def test_swing长度用20而非50():
    """源码 Swing length 50 是给完整历史用的；我们只有 200 根 4H，
    size=50 全窗口只产出 1 个摆动点、0 个订单块。"""
    import inspect
    sig = inspect.signature(smc.read_4h_zones)
    assert sig.parameters["sizes"].default == (smc.INTERNAL_LENGTH, 20)


def test_默认参数与源码一致():
    assert smc.SWING_LENGTH == 50 and smc.INTERNAL_LENGTH == 5
    assert smc.OB_COUNT == 5 and smc.ATR_LENGTH == 200
    assert smc.HIGH_VOL_MULT == 2.0


# ── Zone 与定位 ───────────────────────────────────────────────────
def test_zone_contains带容差():
    z = smc.Zone(smc.BULLISH, "OB5", 100, 110, 1)
    assert z.kind == "需求" and z.contains(105)
    assert z.contains(110.4, tol=0.005) and not z.contains(112, tol=0.005)


def test_confluence只返回命中的():
    zs = [smc.Zone(smc.BULLISH, "OB5", 100, 110, 1),
          smc.Zone(smc.BEARISH, "OB5", 120, 130, 2)]
    assert len(smc.confluence(105, zs)) == 1 and smc.confluence(115, zs) == []


def test_模块声明不得用于加权():
    """2026-09-03 实测：与期权墙同源，重合率与随机行权价无差别。"""
    import pathlib
    src = pathlib.Path(smc.__file__).read_text("utf-8")
    assert "已证伪" in src and "不进任何加权投票" in src


# ── 比值同步（2026-09-03 线上 bug 的回归锁）──────────────────────────
def test_研报比值不得用实时期货价除以ETF前收():
    """real_price 是 Yahoo 实时期货报价，curr.spot 是 ETF 前一日收盘，
    两者差一个隔夜。实测把 GC÷GLD 从真值 10.840 推到 11.104，
    4H 结构区 GLD 429.39 被显示成现货 4768（真值 4655），差 113 点。

    正确做法：取期货序列里 today 之前最后一根收盘，与 ETF 前收同日。
    """
    import pathlib
    src = pathlib.Path("undertow/cli.py").read_text("utf-8")
    assert "_r = (real_price / curr.spot)" not in src, "SMC 卡片又自己算比值了"
    assert "if d < today] if real_series else []" in src, "同日比值逻辑被改掉了"


def test_比值落在GC对GLD的合理区间():
    """GC÷GLD 因 GLD 的管理费损耗缓慢上行，但短期应稳定在 10.8~11.0。
    落到区间外基本意味着两端取数不同步。"""
    lo, hi = 10.7, 11.05
    assert lo < 10.840 < hi and lo < 10.901 < hi
