"""第六步 VRP 脚本的口径锁。"""
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("step6", ROOT / "scripts" / "step6_vrp.py")
s6 = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(s6)


def test_stats_helpers():
    assert s6.one_sample_t([1.0, 1.0, 1.0]) == 0.0, "零方差不能除零"
    assert s6.one_sample_t([2, 3, 4, 3, 2, 4, 3]) > 5
    assert abs(s6.welch([1, 2, 3], [1, 2, 3])) < 1e-12
    assert s6.welch([10, 11, 12], [1, 2, 3]) > 5
    d = s6.describe([-3, -1, 0, 2, 5])
    assert d["n"] == 5 and d["pos"] == 0.4 and d["median"] == 0
    assert s6.describe([]) is None
    print("PASS test_stats_helpers")


def test_short_dte_realized_window_starts_at_T():
    """快照在 T 盘前抓，其 IV 承保的是 T 当天起到到期的波动 —— 实现波动必须含 T 这一根，
    不含 T−1 之前的任何一根（那是过去，不是它承保的）。"""
    src = (ROOT / "scripts" / "step6_vrp.py").read_text("utf-8")
    assert "for j in range(iT, end + 1)" in src, "收益窗必须从 iT 开始（= T−1 收盘到 T 收盘那根）"
    assert "forward_realized_vol" in src, "长历史必须复用 vrp_history 的前视对齐实现，不许另写"
    print("PASS test_short_dte_realized_window_starts_at_T")


def test_emitted_artifact():
    p = ROOT / "data" / "history" / "wall_spread" / "vrp_states.json"
    if not p.exists():
        pytest.skip("尚未 --emit")
    d = json.loads(p.read_text("utf-8"))
    assert d["schema"] == 1 and d["window"] == 21
    for k, v in d["long"].items():
        assert set(v["states"]) >= {"ATR扩张≥1.3", "ATR分位≥90%", "IV分位≥70%"}, k
        assert v["n_nonoverlap"] * 21 <= v["n"] + 21
    assert "silver" in d["short"] and len(d["short"]["silver"]) >= 30
    print("PASS test_emitted_artifact")
