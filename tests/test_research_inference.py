"""S03（Codex 005 R08/R10）：研究脚本的边界有效区间与配对符号检验。"""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step9_wall_hold_rates as s9      # noqa: E402
import step10_pin_test as s10           # noqa: E402


def test_garwood_matches_chi_square_table():
    lo, hi = s9.garwood(3, 3.0)
    assert lo == pytest.approx(0.2062, abs=1e-3) and hi == pytest.approx(2.9224, abs=1e-3)
    lo, hi = s9.garwood(10, 10.0)
    assert lo == pytest.approx(0.4795, abs=1e-3) and hi == pytest.approx(1.8390, abs=1e-3)


def test_garwood_zero_events_gives_nonzero_upper():
    """R08：观测 0 次不再退化成 [0,0]。"""
    lo, hi = s9.garwood(0, 4.0)
    assert lo == 0.0 and hi == pytest.approx(-math.log(0.025) / 4.0, abs=1e-6)
    assert s9.verdict(lo, hi, 0.0, 4.0) == "支持墙有增量", "0 次/期望 4：P(0|4)=1.8% < 2.5%，这个非零上界是有效证据"
    assert s9.verdict(*s9.garwood(0, 2.0), 0.0, 2.0).startswith("期望破墙仅"), "期望 <3 仍不判定"


def test_combined_interval_is_wider_than_observation_only():
    lo0, hi0 = s9.garwood(8, 6.0)
    lo, hi = s9.combined_interval(8, 4.5, 7.5, 0.05)
    assert lo < lo0 and hi > hi0
    assert s9.combined_interval(8, 0.0, 7.5, 0.05) == (None, None)
    assert s9.N_CELLS == 96


def test_sign_test_exact():
    assert s10.sign_test_p(7, 0) == pytest.approx(1 / 128)
    assert s10.sign_test_p(0, 3) == 1.0
    assert s10.sign_test_p(0, 0) is None
