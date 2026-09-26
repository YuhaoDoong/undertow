"""W04 墙守住率研究的口径锁（scripts/step9_wall_hold_rates.py）。"""
import importlib.util, json, sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
_s = importlib.util.spec_from_file_location("step9", ROOT / "scripts" / "step9_wall_hold_rates.py")
s9 = importlib.util.module_from_spec(_s); _s.loader.exec_module(s9)


def test_verdict_rules_preregistered():
    assert s9.MIN_USEFUL == 0.70 and s9.MIN_EXPECTED == 3.0
    assert s9.verdict(0.2, 0.9, 0.5, 10) == "支持墙有增量"
    assert s9.verdict(0.75, 1.4, 1.0, 10) == "排除实用增量"
    assert s9.verdict(0.4, 1.4, 0.9, 10) == "未决"
    # 期望破墙太少：观测 0 次也不能说墙有用（bootstrap 会退化成 [0,0]）
    assert s9.verdict(0.0, 0.0, 0.0, 0.2).startswith("期望破墙仅")
    print("PASS test_verdict_rules_preregistered")


def test_boot_ratio_degenerate_cases():
    assert s9.boot_ratio([{"o": 0, "e": 0.0}] * 10, "o", "e") == (None, None), "期望恒 0 不可估计"
    assert s9.boot_ratio([{"o": 1, "e": 0.5}] * 3, "o", "e") == (None, None), "少于 5 行不给区间"
    lo, hi = s9.boot_ratio([{"o": i % 2, "e": 0.5} for i in range(40)], "o", "e")
    assert lo < 1.0 < hi
    print("PASS test_boot_ratio_degenerate_cases")


def test_crosses_and_base_hold_direction():
    assert s9.crosses("P", 57.0, 56.9) and not s9.crosses("P", 57.0, 57.0)
    assert s9.crosses("C", 60.0, 60.1) and not s9.crosses("C", 60.0, 59.9)
    c = [100.0, 100.0, 90.0, 100.0, 100.0, 100.0]
    ch, eh = s9.base_hold(c, 0, len(c), 0.05, 2, "P")
    assert ch < 1.0 and eh <= 1.0 and ch <= eh, "期间未破必然 ≤ 窗末未破"
    print("PASS test_crosses_and_base_hold_direction")


def test_emitted_artifact():
    p = ROOT / "data/history/wall_spread/rerun_20260926/wall_hold_rates.json"
    if not p.exists(): pytest.skip("尚未 --emit")
    d = json.loads(p.read_text("utf-8")); assert d["schema"] == 1 and d["min_useful_OE"] == 0.70
    for k, v in d["instruments"].items():
        for kk, it in v["summary"].items():
            assert it["close_hold"] <= it["endpoint_hold"] + 1e-9, (k, kk)
            assert it["intraday_hold"] <= it["close_hold"] + 1e-9, (k, kk)
    print("PASS test_emitted_artifact")
