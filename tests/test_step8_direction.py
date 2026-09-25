"""第八步：增仓层方向 × 近墙破墙 的口径锁。"""
import importlib.util, json, sys
from datetime import date
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
_s = importlib.util.spec_from_file_location("step8", ROOT / "scripts" / "step8_direction_x_wall.py")
s8 = importlib.util.module_from_spec(_s); _s.loader.exec_module(s8)


def test_pair_aligns_side_with_direction():
    """偏多 → 卖 put 侧为顺；偏空 → 卖 call 侧为顺；中性/缺失不计。"""
    T = date(2026, 9, 14)
    rows = [{"T": T, "k": 2, "kind": "P", "iT": 10, "breach": False, "Fin": 0.2},
            {"T": T, "k": 2, "kind": "C", "iT": 10, "breach": True, "Fin": 0.2},
            {"T": T, "k": 3, "kind": "P", "iT": 10, "breach": False, "Fin": 0.2}]
    al, ag = s8.pair({"rows": rows}, {T.isoformat(): {"call_direction": "偏多"}}, 2)
    assert [r["kind"] for r in al] == ["P"] and [r["kind"] for r in ag] == ["C"]
    al, ag = s8.pair({"rows": rows}, {T.isoformat(): {"call_direction": "偏空"}}, 2)
    assert [r["kind"] for r in al] == ["C"]
    assert s8.pair({"rows": rows}, {T.isoformat(): {"call_direction": "中性"}}, 2) == ([], [])
    print("PASS test_pair_aligns_side_with_direction")


def test_emitted():
    p = ROOT / "data/history/wall_spread/direction_x_wall.json"
    if not p.exists(): pytest.skip("尚未 --emit")
    d = json.loads(p.read_text("utf-8")); assert d["schema"] == 1
    assert all("p" in v and "n_nov" in v for v in d["results"].values())
    print("PASS test_emitted")
