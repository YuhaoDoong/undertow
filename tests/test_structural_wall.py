"""结构主墙 vs 局部 pin（codex 2026-09-01 P0-5）。

判据不是距离，是【近端到期占比】：长期对冲/尾部保险的 OI 集中在个别远月，
真正的承接区在各到期上都有分布。实测 2026-09-01：
    GLD put 330  ≤30天占 2%   → 长期堆积，不算结构墙
    GLD put 400  ≤30天占 28%  → 真承接区
"""
from datetime import date, timedelta

from undertow.analyze.gamma import (NEAR_SHARE_MIN, local_pin,
                                    structural_walls)

TODAY = date(2026, 9, 1)


class C:
    def __init__(self, strike, kind, oi, dte):
        self.strike, self.kind, self.open_interest = strike, kind, oi
        self.expiry = TODAY + timedelta(days=dte)


class Snap:
    def __init__(self, contracts):
        self.contracts = contracts


def test_远处大墙不再被band挡在外面():
    """SLV 实况简化：真墙 50 在 −14.5%，band=5% 永远够不着它。"""
    cs = [C(50.0, "P", 60_000, 17), C(50.0, "P", 60_000, 80),
          C(55.0, "P", 20_000, 17), C(58.0, "P", 10_000, 10)]
    w = structural_walls(Snap(cs), TODAY, 58.5, "P")
    assert w[0]["strike"] == 50.0 and w[0]["oi"] == 120_000


def test_长期对冲堆积被近端占比门槛滤掉():
    """GLD 330 实况：20 万张里 ≤30 天只占 2%，集中在 136 天那一个到期。"""
    cs = [C(330.0, "P", 2_000, 10), C(330.0, "P", 198_000, 136),
          C(400.0, "P", 30_000, 20), C(400.0, "P", 70_000, 45)]
    w = structural_walls(Snap(cs), TODAY, 400.9, "P")
    strikes = [x["strike"] for x in w]
    assert 330.0 not in strikes, "近端占比 1% 的长期堆积不该算结构墙"
    assert strikes[0] == 400.0


def test_近端占比门槛就在边界上():
    lo = int(100_000 * (NEAR_SHARE_MIN - 0.01))
    hi = int(100_000 * (NEAR_SHARE_MIN + 0.01))
    below = [C(90.0, "P", lo, 10), C(90.0, "P", 100_000 - lo, 200)]
    above = [C(90.0, "P", hi, 10), C(90.0, "P", 100_000 - hi, 200)]
    assert structural_walls(Snap(below), TODAY, 100.0, "P") == []
    assert len(structural_walls(Snap(above), TODAY, 100.0, "P")) == 1


def test_只看虚值一侧():
    """实值侧的 OI 不构成支撑/阻力。"""
    cs = [C(110.0, "P", 99_999, 20), C(90.0, "P", 10_000, 20)]
    w = structural_walls(Snap(cs), TODAY, 100.0, "P")
    assert [x["strike"] for x in w] == [90.0]


def test_local_pin总是有输出_这正是它不能当墙用的原因():
    """band 内必有最大值 —— 所以它描述的是钉住倾向，不是支撑强度。"""
    cs = [C(99.0, "P", 3, 5), C(98.0, "P", 1, 5)]
    p = local_pin(Snap(cs), TODAY, 100.0, "P")
    assert p is not None and p["strike"] == 99.0 and p["oi"] == 3


def test_local_pin与结构墙可以不一致():
    """两者给出不同答案是正常的，不得用 pin 反转结构墙的结论。"""
    cs = [C(50.0, "P", 100_000, 20), C(57.0, "P", 5_000, 5)]
    w = structural_walls(Snap(cs), TODAY, 58.5, "P")
    p = local_pin(Snap(cs), TODAY, 58.5, "P")
    assert w[0]["strike"] == 50.0
    assert p["strike"] == 57.0


def test_空链不崩():
    assert structural_walls(Snap([]), TODAY, 100.0, "P") == []
    assert local_pin(Snap([]), TODAY, 100.0, "P") is None


def test_返回距现价百分比供策略层自行过滤():
    """结构墙【不保证】权利金可观，策略层必须自己看报价。"""
    cs = [C(50.0, "P", 100_000, 20)]
    w = structural_walls(Snap(cs), TODAY, 58.5, "P")
    assert w[0]["dist_pct"] < -14


# ── 墙位历史图（用户 2026-08-31 要求，2026-09-02 接线）────────────────
def test_墙位历史卡片_空数据不产生空卡():
    from undertow.report.html import render_wall_history
    assert render_wall_history([]) == ""


def test_墙位历史卡片_区分开火与未开火():
    """图例误导是 2026-09-01 用户「图上不止 4 个」的根因：
    旧图例写「极强信号 ≥10×」，但 ≥10× 只是压力比一个条件。"""
    from undertow.report.html import render_wall_history
    rows = [{"date": "2026-09-01", "o": 58, "h": 59, "l": 57, "c": 58,
             "topP": [[55.0, 100]], "topC": [[60.0, 100]],
             "sig": ["看跌", 12.3, True]}]
    h = render_wall_history(rows, "白银")
    assert "已开火" in h and "未开火" in h
    assert "structural_walls" in h, "必须写明墙的口径"


def _marks(svg):
    """只取数据标记，不含图例 —— 图例里本来就同时有 ▲▼ 和 △▽。"""
    import re
    return re.findall(r"[▲▼△▽][\d.]+×", svg)


def test_墙位历史图_未开火用空心标记():
    from undertow.report.viz import wall_history_svg
    base = {"date": "2026-09-01", "o": 58, "h": 59, "l": 57, "c": 58,
            "topP": [[55.0, 100]], "topC": [[60.0, 100]]}
    assert _marks(wall_history_svg([{**base, "sig": ["看跌", 12.0, True]}])) == ["▼12×"]
    assert _marks(wall_history_svg([{**base, "sig": ["看跌", 12.0, False]}])) == ["▽12×"]
    assert _marks(wall_history_svg([{**base, "sig": ["看涨", 12.0, True]}])) == ["▲12×"]
    assert _marks(wall_history_svg([{**base, "sig": ["看涨", 12.0, False]}])) == ["△12×"]


def test_sig缺第三项时按未开火处理():
    """向后兼容：老数据只有 [方向, 倍数]，不得当成已开火。"""
    from undertow.report.viz import wall_history_svg
    rows = [{"date": "2026-09-01", "o": 58, "h": 59, "l": 57, "c": 58,
             "topP": [[55.0, 100]], "topC": [], "sig": ["看跌", 12.0]}]
    assert _marks(wall_history_svg(rows)) == ["▽12×"]


# ── 卖方选墙规则（用户 2026-09-02 第一步产物）──────────────────────
def _snap3(spot, strikes_oi, kind="P", dte=20):
    cs = []
    for k, oi in strikes_oi:
        cs.append(C(k, kind, oi, dte))
        cs.append(C(k, kind, oi, dte + 40))   # 保证近端占比过门槛
    return Snap(cs)


def test_最大墙就是最近墙时用最大墙():
    from undertow.analyze.gamma import pick_sell_wall
    snap = _snap3(60.0, [(55.0, 100_000), (50.0, 40_000), (45.0, 20_000)])
    r = pick_sell_wall(snap, TODAY, 60.0, "P")
    assert r["strike"] == 55.0 and r["rule"] == "基准"


def test_最大墙不是最近墙时上挪一层而非跳到最近():
    """用户 2026-09-02 原话：三道 put 墙 60/55/50，最大是 50，
    应该卖 55（50 往上一层），不是卖 60。"""
    from undertow.analyze.gamma import pick_sell_wall
    snap = _snap3(60.5, [(60.0, 30_000), (55.0, 50_000), (50.0, 200_000)])
    r = pick_sell_wall(snap, TODAY, 60.5, "P")
    assert r["strike"] == 55.0, "应上挪一层到 55，不是跳到最近的 60"
    assert r["rule"] == "上挪一层"


def test_缓冲不足时弃权而不是硬做():
    from undertow.analyze.gamma import pick_sell_wall
    # 55 距 55.3 只有 0.5% < 2% 门槛
    snap = _snap3(55.3, [(55.0, 100_000), (50.0, 40_000)])
    assert pick_sell_wall(snap, TODAY, 55.3, "P") is None


def test_call侧门槛更高():
    from undertow.analyze.gamma import WALL_PICK_MIN_BUF
    assert WALL_PICK_MIN_BUF["C"] > WALL_PICK_MIN_BUF["P"], \
        "call 侧实测需要 3% 才降到 0 破墙，put 侧 2% 即可"


def test_无墙时返回None():
    from undertow.analyze.gamma import pick_sell_wall
    assert pick_sell_wall(Snap([]), TODAY, 60.0, "P") is None
