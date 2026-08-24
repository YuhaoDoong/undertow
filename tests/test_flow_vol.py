"""波动率面（ATM IV / 25Δ·10Δ 偏斜）：读数、插值、买方确认判读。

一次黄金分析：大涨若无买方追价（ATM IV 反被压、skew 不收敛），
上涨更像空头回补、动力存疑。
"""
from datetime import date, timedelta

from undertow.analyze.flow import (
    VolRead, _interp, _vol_verdict, read_vol, vol_surface,
)
from undertow.core.models import OptionContract, OptionsSnapshot

TODAY = date(2026, 7, 2)
EXP = TODAY + timedelta(days=30)


def _chain(spot: float, atm_iv: float, skew: float = 0.02) -> OptionsSnapshot:
    """造一条对称链：IV 随 |行权价−现价| 线性走陡（put 侧更陡 = 正 skew）。

    delta 用粗糙但单调的近似即可（read_vol 只需单调 + 插值）。
    """
    contracts = []
    for i in range(-5, 6):
        k = spot + i * spot * 0.02          # 现价 ±10%，步长 2%
        mny = (k - spot) / spot
        call_delta = min(0.98, max(0.02, 0.5 - mny * 4.5))
        put_delta = call_delta - 1.0
        civ = atm_iv + max(0.0, mny) * 0.10 + max(0.0, -mny) * 0.05
        piv = atm_iv + max(0.0, -mny) * (0.10 + skew) + max(0.0, mny) * 0.05
        contracts.append(OptionContract(EXP, k, "C", 1000, 10, 0.0, call_delta, civ))
        contracts.append(OptionContract(EXP, k, "P", 1000, 10, 0.0, put_delta, piv))
    return OptionsSnapshot("gold", "GLD", spot, "test", contracts)


def test_interp_linear_and_clamp():
    pts = [(1.0, 10.0), (3.0, 30.0)]
    assert _interp(pts, 2.0) == 20.0
    assert _interp(pts, 0.0) == 10.0     # 左侧截断
    assert _interp(pts, 9.0) == 30.0     # 右侧截断
    assert _interp([(1.0, 1.0)], 1.0) is None  # 点太少


def test_read_vol_atm_and_skew():
    v = read_vol(_chain(spot=370.0, atm_iv=0.24), today=TODAY)
    assert v is not None
    assert v.expiry == EXP and v.days_out == 30
    assert abs(v.atm_iv_pp - 24.0) < 0.8          # ATM 接近 24
    assert v.skew25_pp > 0                        # put 侧更陡 → 正偏斜
    assert v.skew10_pp > v.skew25_pp              # 更深 OTM 偏斜更大


def test_read_vol_respects_forced_expiry_missing():
    snap = _chain(spot=370.0, atm_iv=0.24)
    assert read_vol(snap, today=TODAY, expiry=TODAY + timedelta(days=60)) is None


def test_vol_surface_rally_without_buyer_confirmation():
    """复刻该场景：价格 +2.6%、ATM IV 大降、skew 几乎不动 → 未确认涨势。"""
    prev = _chain(spot=368.0, atm_iv=0.24)
    curr = _chain(spot=378.0, atm_iv=0.225)
    vs = vol_surface(prev, curr, today=TODAY)
    assert vs is not None and vs.prev is not None
    assert vs.d_spot_pct > 2.0
    assert vs.d_atm_pp < -1.0
    assert "未确认涨势" in vs.verdict or "空头回补" in vs.verdict


def test_vol_verdict_matrix():
    # 价涨 + IV 升 = 买方追价确认
    assert "确认" in _vol_verdict(2.0, +0.8, 0.0)
    # 价涨 + IV 压 + skew 收敛 = 中性
    assert "中性" in _vol_verdict(2.0, -1.0, -0.8)
    # 价跌 + IV 升 + skew 走陡 = 下行确认
    assert "下行" in _vol_verdict(-2.0, +0.8, +0.8)
    # 价跌 + IV 回落 = 恐慌有限
    assert "恐慌有限" in _vol_verdict(-2.0, -0.8, 0.0)
    # 价平 + skew 走陡 = 担忧升温
    assert "升温" in _vol_verdict(0.1, 0.0, +0.8)


def test_vol_surface_single_snapshot():
    vs = vol_surface(None, _chain(spot=370.0, atm_iv=0.24), today=TODAY)
    assert vs is not None and vs.prev is None
    assert "当日水平" in vs.verdict
