"""近周到期阶梯（expiry_ladder）单元测试（合成数据，不依赖网络）。

验证：到期选取（未来3周五+最近月度、跳过非周五/0DTE/超窗口/冷门）、
按 ISO 周历差贴标签、逐到期独立墙位、逐到期独立买卖方（同口径复用 flow）。
运行: python tests/test_expiry_ladder.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.core.models import OptionsSnapshot, OptionContract
from undertow.analyze.expiry_ladder import build_ladder, _is_third_friday, _friday_label

TODAY = date(2026, 8, 17)  # 周一
FRI1 = date(2026, 8, 21)   # 本周五（本例=8月月度 OPEX，第三个周五）
FRI2 = date(2026, 8, 28)   # 下周五
FRI3 = date(2026, 9, 4)    # 下下周五
MONTHLY = date(2026, 9, 18)  # 月度 OPEX（第三个周五，超 3 周）
WED = date(2026, 8, 19)    # 周三（非周五，不进周五序列）
FAR = date(2026, 12, 18)   # 超出 75 天窗口


def _c(strike, kind, oi, iv=0.20, expiry=FRI1) -> OptionContract:
    return OptionContract(expiry=expiry, strike=strike, kind=kind,
                          open_interest=oi, volume=0, gamma=0.0, delta=0.0, iv=iv)


def _snap(contracts, spot=100.0) -> OptionsSnapshot:
    return OptionsSnapshot(instrument="t", proxy_symbol="T", spot=spot,
                           asof="2026-08-17", contracts=contracts)


def _ladder_strikes(exp, iv_c=0.20):
    """给某到期造一组现价附近的 call/put 行权价（总 OI 达标、带内有墙）。"""
    return [
        _c(95, "P", 1500, expiry=exp), _c(90, "P", 2500, expiry=exp),
        _c(105, "C", 1200, iv=iv_c, expiry=exp), _c(110, "C", 3000, iv=iv_c, expiry=exp),
    ]


def test_third_friday_and_labels():
    assert _is_third_friday(FRI1) and _is_third_friday(MONTHLY)
    assert not _is_third_friday(FRI2)          # 第四个周五
    assert _friday_label(FRI1, TODAY) == "本周五"
    assert _friday_label(FRI2, TODAY) == "下周五"
    assert _friday_label(FRI3, TODAY) == "下下周五"


def test_selects_three_fridays_plus_monthly():
    contracts = []
    for e in (FRI1, FRI2, FRI3, MONTHLY, WED, FAR):
        contracts += _ladder_strikes(e)
    sl = build_ladder(_snap(contracts), _snap(contracts), today=TODAY, multiplier=1.0)
    exps = [s.expiry for s in sl]
    # 只选未来3周五 + 最近月度；非周五(WED)、超窗口(FAR)均不入
    assert exps == [FRI1, FRI2, FRI3, MONTHLY]
    labels = {s.expiry: s.label for s in sl}
    assert labels[FRI1].startswith("本周五") and "月度OPEX" in labels[FRI1]  # 本周五恰逢月度
    assert labels[FRI2] == "下周五"
    assert labels[FRI3] == "下下周五"
    assert labels[MONTHLY] == "月度OPEX"
    assert WED not in exps and FAR not in exps


def test_per_expiry_walls_independent():
    """每个到期的墙位来自【它自己】的 OI，不被别的到期污染。"""
    contracts = _ladder_strikes(FRI1) + _ladder_strikes(FRI2)
    # 给 FRI2 换一堵不同的 call 墙（108 巨量），确认切片各看各的
    contracts += [_c(108, "C", 9000, expiry=FRI2)]
    sl = {s.expiry: s for s in build_ladder(_snap(contracts), _snap(contracts),
                                            today=TODAY, multiplier=1.0)}
    assert abs(sl[FRI1].call_wall - 110) < 1e-6   # FRI1 最大 call OI 在 110
    assert abs(sl[FRI2].call_wall - 108) < 1e-6   # FRI2 被 108 的 9000 抢走
    assert sl[FRI2].call_wall_oi == 9000


def test_per_expiry_flow_isolated():
    """某到期的卖方建仓只影响【该到期】的买卖方判定，不外溢到别的到期。"""
    prev = _ladder_strikes(FRI1) + _ladder_strikes(FRI2)
    # FRI1 的 110C：OI 3000→7000（增），IV 0.20→0.15（降）= 卖方写权压制（bearish）
    curr = [
        _c(95, "P", 1500, expiry=FRI1), _c(90, "P", 2500, expiry=FRI1),
        _c(105, "C", 1200, expiry=FRI1), _c(110, "C", 7000, iv=0.15, expiry=FRI1),
    ] + _ladder_strikes(FRI2)  # FRI2 完全不变
    sl = {s.expiry: s for s in build_ladder(_snap(prev), _snap(curr),
                                            today=TODAY, multiplier=1.0)}
    f1 = sl[FRI1]
    assert f1.has_flow
    # FRI1 的 110C 应判为卖方（bearish）
    c110 = next(c for c in f1.changes if c.kind == "C" and abs(c.strike - 110) < 1e-6)
    assert c110.d_oi == 4000 and c110.bias == "bearish"
    assert f1.flow_tilt.startswith("偏空")
    # FRI2 无变化 → 无方向压力
    assert not sl[FRI2].flow_tilt.startswith("偏空")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
