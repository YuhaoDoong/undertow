"""第五步：近墙支撑检验的口径锁。"""
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("step5", ROOT / "scripts" / "step5_wall_hold.py")
s5 = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(s5)

from undertow.analyze.gamma import local_wall   # noqa: E402


class _C:
    def __init__(self, kind, strike, expiry, oi):
        self.kind, self.strike, self.expiry, self.open_interest = kind, strike, expiry, oi
        self.is_call = kind == "C"


class _Snap:
    def __init__(self, cs): self._cs = cs
    def with_oi(self): return [c for c in self._cs if c.open_interest > 0]


T0 = date(2026, 9, 14)


def test_local_wall_picks_max_oi_within_band_and_dte():
    cs = [_C("P", 57.0, T0 + timedelta(days=2), 5000), _C("P", 57.0, T0 + timedelta(days=9), 4000),
          _C("P", 56.0, T0 + timedelta(days=2), 8000),           # 单到期 8000 < 57 的跨到期 9000
          _C("P", 52.0, T0 + timedelta(days=2), 50000),          # 带外（>5%）
          _C("P", 57.5, T0 + timedelta(days=40), 90000),         # DTE 超 14
          _C("C", 59.0, T0 + timedelta(days=2), 7000)]
    w = local_wall(_Snap(cs), T0, 58.12, "P")
    assert w["strike"] == 57.0 and w["oi"] == 9000 and w["n_exp"] == 2, w
    assert w["oi_by_expiry"] == {"2026-09-16": 5000, "2026-09-23": 4000}
    assert sum(w["oi_by_expiry"].values()) == w["oi"]
    assert abs(w["buf_pct"] - (1 - 57 / 58.12) * 100) < 1e-9
    assert local_wall(_Snap(cs), T0, 58.12, "C")["strike"] == 59.0
    assert local_wall(_Snap(cs), T0, 58.12, "P", min_oi=20000) is None, "不够厚就不叫墙"
    print("PASS test_local_wall_picks_max_oi_within_band_and_dte")


def test_k_moves_and_base_rate_only_look_forward():
    c = [100, 100, 100, 90, 100, 100]
    mn, mx = s5.k_moves(c, 2)
    assert abs(mn[1] - (-0.10)) < 1e-12, "i=1 看 i+1,i+2 → 含 90"
    assert mn[3] > 0 and mx[3] > 0, "i=3（90 那根）自己不算：往后看两根都是 100 → +11%"
    assert mn[-1] is None and mn[-2] is None, "末尾不足 k 根必须 None"
    assert s5.base_rate(mn, 0.05, 0, len(c), down=True) == 2 / 4   # i=1,2 破 5%
    print("PASS test_k_moves_and_base_rate_only_look_forward")


def test_breach_uses_close_window_from_T():
    assert s5.breached([57.5, 56.9], 57.0, "P") is True
    assert s5.breached([57.5, 57.1], 57.0, "P") is False
    assert s5.breached([59.5, 60.2], 60.0, "C") is True
    print("PASS test_breach_uses_close_window_from_T")


def test_mc_p_is_two_sided_and_bounded():
    p = s5.mc_p([0.3] * 30, 9, sims=2000)
    assert 0.5 < p <= 1.0, "观测=期望附近必须不显著"
    assert s5.mc_p([0.05] * 30, 12, sims=2000) < 0.01, "远超期望必须显著"
    assert s5.mc_p([0.6] * 30, 3, sims=2000) < 0.01, "远低于期望同样显著（守住率高于随机）"
    print("PASS test_mc_p_is_two_sided_and_bounded")


def test_local_wall_nearest_mode():
    """mode=nearest 取带内离现价最近且 ≥min_oi 的档；max 取带内 OI 最大档。两者不同时必须给不同答案。"""
    cs = [_C("P", 57.5, T0 + timedelta(days=2), 3500), _C("P", 56.0, T0 + timedelta(days=2), 9000),
          _C("P", 57.9, T0 + timedelta(days=2), 100)]           # 100 张不够厚，不算墙
    assert local_wall(_Snap(cs), T0, 58.12, "P", mode="max")["strike"] == 56.0
    w = local_wall(_Snap(cs), T0, 58.12, "P", mode="nearest")
    assert w["strike"] == 57.5 and w["mode"] == "nearest"
    print("PASS test_local_wall_nearest_mode")
