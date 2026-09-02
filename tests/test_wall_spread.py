"""墙位卖方价差模块的行为锁。

这个模块直接产出交易候选，却长期零测试覆盖（2026-09-01 发现）。
下面每条测试都对应一次真实的口径错误，改错了就红。
"""
from datetime import date

import pytest

from undertow.analyze import wall_spread as ws


class FakeLeg:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask


class FakeContract:
    def __init__(self, kind, strike, expiry, oi=0, bid=None, ask=None):
        self.kind, self.strike, self.expiry = kind, strike, expiry
        self.open_interest, self.bid, self.ask = oi, bid, ask


class FakeSnap:
    def __init__(self, contracts, spot):
        self.contracts, self.spot = contracts, spot


# ── 出场规则（第三步定版）──────────────────────────────────────────
def test_只在收盘越过卖腿时平仓():
    """第三步实测：破卖腿即平是唯一两侧都不吃亏的规则。
    换墙即平两侧都亏（call −302 vs 持有 −329，put +113 vs +249）。"""
    assert ws.should_exit("P", 55.0, 54.0)[0] is True
    assert ws.should_exit("P", 55.0, 56.0)[0] is False
    assert ws.should_exit("C", 60.0, 61.0)[0] is True
    assert ws.should_exit("C", 60.0, 59.0)[0] is False


def test_出场规则不再接受信号或距离参数():
    """换墙、浮盈、极强信号都被实测否掉，签名里不该再有它们。"""
    import inspect
    ps = set(inspect.signature(ws.should_exit).parameters)
    assert ps == {"kind", "sell_strike", "close_px"}


# ── 建仓参数（第二步定版）──────────────────────────────────────────
def test_只激活白银():
    assert ws.ACTIVE == {"silver"}


def test_建仓参数为第二步实测值():
    p = ws.PARAMS["silver"]
    assert p["dte"] == (2, 4), "短 DTE 双赢：theta 最快 + 暴露最短"
    assert p["width_n"] == (2, 3), "宽1档被手续费吃掉，宽5档破满亏放大更快"
    assert p["offsets"] == (0, 1), "墙内1档破卖腿率与墙上相同、权利金多24%；2档起跳升"
    assert p["min_buf"] == 0.03


def test_生死线数值必须在参数里以便随候选显示():
    p = ws.PARAMS["silver"]
    be_lo, be_hi = p["breakeven_rate"]
    ad_lo, ad_hi = p["adverse_rate"]
    assert be_hi < ad_lo, "平衡破墙率必须低于实测逆势破墙率 —— 这就是未通过的生死线"


def test_未激活品种明确拒绝():
    v = ws.propose(FakeSnap([], 400.0), "gold", date(2026, 9, 1),
                   date(2026, 9, 1), spot=400.0)
    assert not v.ok and "未激活" in v.reason


def test_propose的spot必填():
    import inspect
    assert inspect.signature(ws.propose).parameters["spot"].default \
        is inspect.Parameter.empty


# ── 成交价 ────────────────────────────────────────────────────────
def test_开仓按中价让点差():
    sell, buy = FakeLeg(0.60, 0.80), FakeLeg(0.50, 0.70)
    got = ws._fill(sell, buy)
    mid = ((0.60 + 0.80) / 2 - (0.50 + 0.70) / 2) * 100
    worst = (0.60 - 0.70) * 100
    assert got == pytest.approx(mid + (worst - mid) * 0.25)
    assert got > 0, "两边吃满会得负数，那是毁灭性假设"


def test_平仓成本方向相反():
    """平仓是买回卖腿（吃 ask）、卖出买腿（吃 bid），比开仓不利。"""
    sell, buy = FakeLeg(0.60, 0.80), FakeLeg(0.50, 0.70)
    assert ws.close_cost(sell, buy) > ws._fill(sell, buy)


# ── Candidate 派生量 ──────────────────────────────────────────────
def _cand(**kw):
    d = dict(kind="P", expiry=date(2026, 9, 11), dte=3, sell=55.0, buy=53.5,
             wall=55.0, offset=0, width_n=3, credit=20.0, width=150.0,
             spot=60.0, wall_rule="基准")
    d.update(kw)
    return ws.Candidate(**d)


def test_占用为单边一份保证金():
    """铁鹰在长桥要两份，见 docs/broker/longbridge_margin.md。"""
    c = _cand()
    assert c.occupancy == pytest.approx(130.0)
    assert c.max_loss == pytest.approx(130.0 + ws.FEE_PER_TRADE)


def test_净权利金已扣手续费():
    c = _cand()
    assert c.net_credit == pytest.approx(20.0 - ws.FEE_PER_TRADE)
    assert c.net_daily == pytest.approx((20.0 - ws.FEE_PER_TRADE) / 3)


def test_平衡破墙率():
    """守住赚 ÷ (守住赚 + 破满亏)。超过它这个组合就亏钱。"""
    c = _cand()
    win = 20.0 - ws.FEE_PER_TRADE
    loss = 150.0 - 20.0 + ws.FEE_PER_TRADE
    assert c.breakeven_rate == pytest.approx(win / (win + loss))


def test_零占用不炸():
    assert _cand(width=20.0, credit=20.0).roi == 0.0


# ── 研报渲染 ──────────────────────────────────────────────────────
def test_无候选时也要说清卡在哪():
    """干巴巴一句「无候选」没有信息量。2026-09-02 实测：
    第一步选出的墙缓冲 8.4%，而 DTE 2~4 的深虚期权几乎没有时间价值 ——
    要放宽到 DTE 10 才够手续费门槛。这个诊断必须出现在输出里。"""
    from undertow.report.html import render_wall_spread
    v = ws.Verdict(False, "put 墙 55（缓冲 8.4%）在 2~4 天内权利金不足 $6.4；"
                          "要放宽到 DTE 10 才收得到 $7.2",
                   params=ws.PARAMS["silver"])
    h = render_wall_spread(v, "白银")
    assert "无候选" in h and "DTE 10" in h
    assert "尚未通过生死线" in h, "无候选时也要挂生死线警告"


def test_有候选时必须显示生死线与铁鹰说明():
    from undertow.report.html import render_wall_spread
    v = ws.Verdict(True, "测试", puts=[_cand()], calls=[],
                   params=ws.PARAMS["silver"])
    h = render_wall_spread(v, "白银")
    assert "尚未通过生死线" in h
    assert "不合成铁鹰" in h, "必须说明为什么不出铁鹰（长桥收两份保证金）"
    assert "收盘越过卖腿" in h, "必须写明出场规则"
    assert "2.9%" in h and "7%" in h, "平衡破墙率与实测逆势破墙率都要显示"


def test_put与call分开列出():
    from undertow.report.html import render_wall_spread
    v = ws.Verdict(True, "测试", puts=[_cand()],
                   calls=[_cand(kind="C", sell=65.0, buy=66.5)],
                   params=ws.PARAMS["silver"])
    h = render_wall_spread(v, "白银")
    assert "卖 put" in h and "卖 call" in h
