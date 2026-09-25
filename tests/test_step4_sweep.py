"""参数扫描脚本的口径锁（scripts/step4_sweep.py）。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("step4_sweep", ROOT / "scripts" / "step4_sweep.py")
sw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(sw)
import step4_filters as s4   # noqa: E402


def test_grid_sizes_are_what_docs_say():
    assert {f: len(v) for f, v in sw.GRID.items()} == {"布林带宽扩张": 27, "ATR扩张": 27, "RSI": 6, "随机RSI": 2}
    print("PASS test_grid_sizes_are_what_docs_say")


def test_stoch_rsi_sanity():
    flat = [50.0] * 30
    assert sw._stoch_rsi(flat, 14)[-1] == 50.0, "平坦区间必须给 50，不能除零"
    rising = [float(i) for i in range(30)]
    assert sw._stoch_rsi(rising, 14)[-1] == 100.0
    assert sw._stoch_rsi(rising, 14)[5] is None, "样本不足给 None，不给 0"
    print("PASS test_stoch_rsi_sanity")


def test_sweep_shares_outcome_function_with_step4():
    """扫描与主脚本必须用同一个前瞻结果函数 —— 各写一遍必然漂移。"""
    src = (ROOT / "scripts" / "step4_sweep.py").read_text("utf-8")
    assert "s4.breach_outcomes(" in src and "def breach_outcomes" not in src
    print("PASS test_sweep_shares_outcome_function_with_step4")


def test_emitted_sweep_artifact():
    p = ROOT / "data" / "history" / "wall_spread" / "filter_sweep.json"
    if not p.exists():
        pytest.skip("尚未 --emit")
    d = json.loads(p.read_text("utf-8"))
    assert d["schema"] == 1 and d["grid_sizes"] == {f: len(v) for f, v in sw.GRID.items()}
    for fam, rows in d["results"].items():
        # 每格 × 状态数 × 两侧 都在，没有只留"好看的"
        n_states = 1 if fam in ("布林带宽扩张", "ATR扩张") else 2
        assert len(rows) == d["grid_sizes"][fam] * n_states * 2, fam
    print("PASS test_emitted_sweep_artifact")
