"""期权/Gamma 层单元测试（合成数据，不依赖网络）。

运行: python tests/test_gamma.py   或   python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.collect.cboe_options import parse_occ
from undertow.analyze import blackscholes as bs
from undertow.analyze.gamma import analyze_gamma
from undertow.core.models import OptionContract, OptionsSnapshot


def test_parse_occ_put_call_and_root_lengths():
    root, exp, kind, strike = parse_occ("GLD260918P00358000")
    assert (root, exp, kind, strike) == ("GLD", date(2026, 9, 18), "P", 358.0)
    # call + 半美元行权价
    root, exp, kind, strike = parse_occ("SLV260117C00052500")
    assert (root, kind, strike) == ("SLV", "C", 52.5)
    # 单字母 root 也能切对（从右往左切）
    root, _, kind, strike = parse_occ("F260117C00012000")
    assert (root, kind, strike) == ("F", "C", 12.0)


def test_bs_gamma_basic_properties():
    # 到期/零波动 -> 0
    assert bs.gamma(100, 100, 0, 0.2) == 0.0
    assert bs.gamma(100, 100, 0.25, 0) == 0.0
    # ATM gamma 为正且明显大于深度 OTM
    atm = bs.gamma(100, 100, 0.25, 0.2)
    otm = bs.gamma(100, 160, 0.25, 0.2)
    assert atm > 0
    assert atm > otm
    # 数量级合理（ATM 期权每股 gamma 约 0.03~0.05）
    assert 0.02 < atm < 0.08


def _snap(spot: float, rows: list[tuple[float, str, int]], days: int = 14) -> OptionsSnapshot:
    """rows: (strike, 'C'/'P', oi)。用合理 iv 构造近月链。"""
    today = date.today()
    exp = today + timedelta(days=days)
    contracts = [
        OptionContract(expiry=exp, strike=k, kind=kind, open_interest=oi,
                       volume=0, gamma=0.0, delta=0.0, iv=0.25)
        for k, kind, oi in rows
    ]
    return OptionsSnapshot(instrument="t", proxy_symbol="T", spot=spot, asof="t", contracts=contracts)


def test_walls_and_put_call_ratio():
    # 现价 100；最大 put OI 在 95（支撑墙），最大 call OI 在 105（阻力墙）
    snap = _snap(100.0, [
        (95.0, "P", 5000), (90.0, "P", 1000), (100.0, "P", 800),
        (105.0, "C", 4000), (110.0, "C", 1500), (100.0, "C", 600),
    ])
    ga = analyze_gamma(snap, multiplier=2.0, proxy_quality="good")
    assert ga.put_wall == 95.0 and ga.put_wall_oi == 5000
    assert ga.call_wall == 105.0 and ga.call_wall_oi == 4000
    # 商品映射
    assert ga.to_commodity(ga.put_wall) == 190.0
    # P/C 比 = 总put/总call = (5000+1000+800)/(4000+1500+600)
    assert abs(ga.put_call_ratio - (6800 / 6100)) < 1e-9


def test_far_otm_oi_not_picked_as_wall():
    # 远 OTM(150) 有巨量 call OI，但不应被当作阻力墙（超出 ±15% 带）
    snap = _snap(100.0, [(150.0, "C", 99999), (108.0, "C", 3000), (95.0, "P", 2000)])
    ga = analyze_gamma(snap, multiplier=None, proxy_quality="good")
    assert ga.call_wall == 108.0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


def test_structure_delta_phrases():
    from undertow.analyze.gamma import GammaAnalysis, StrikeRow, structure_delta

    def mk(zg, cw, coi, pw, poi, rows):
        return GammaAnalysis(
            instrument="t", proxy_symbol="T", spot=54.0, asof="x", horizon_days=45,
            multiplier=1.12, proxy_quality="good", total_call_oi=0, total_put_oi=0,
            put_call_ratio=1.0, net_gex=-1.0, gex_regime="负Gamma", zero_gamma=zg,
            call_wall=cw, call_wall_oi=coi, put_wall=pw, put_wall_oi=poi,
            nearest_expiry=None, nearest_call_wall=None, nearest_put_wall=None,
            strike_rows=rows)

    prev = mk(55.5, 60.0, 208_000, 50.0, 176_000,
              [StrikeRow(60.0, 208_000, 0, 0.0), StrikeRow(50.0, 0, 176_000, 0.0)])
    curr = mk(54.1, 60.0, 205_000, 50.0, 181_500,
              [StrikeRow(60.0, 205_000, 0, 0.0), StrikeRow(50.0, 0, 181_500, 0.0)])
    notes = structure_delta(prev, curr)
    joined = "；".join(notes)
    assert "零伽马" in joined and "下移" in joined and "向现价贴近" in joined
    assert "call 墙" in joined and "削弱" in joined      # -3,000 手
    assert "put 墙" in joined and "增厚" in joined       # +5,500 手
    # 墙迁移分支
    curr2 = mk(54.1, 61.0, 90_000, 50.0, 176_000,
               [StrikeRow(61.0, 90_000, 0, 0.0), StrikeRow(50.0, 0, 176_000, 0.0)])
    notes2 = structure_delta(prev, curr2)
    assert any("call 墙" in n and "上移" in n for n in notes2)
