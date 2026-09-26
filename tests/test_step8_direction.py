"""第八步：增仓层方向 × 近墙破墙 的口径锁。"""
import importlib.util, json, sys
import gzip
from datetime import date, datetime
from zoneinfo import ZoneInfo
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
    d = json.loads(p.read_text("utf-8")); assert d["schema"] in (1, 2)
    assert all("p" in v and "n_nov" in v for v in d["results"].values())
    print("PASS test_emitted")


def _row(i, breach, expected=0.2):
    return {"iT": i, "breach": breach, "Fin": expected}


def test_stats_pairs_dates_before_thinning_and_keeps_raw():
    # 第0日仅顺侧有墙，第1日仅逆侧有墙；两组不得各从自己的首日抽样。
    al = [_row(0, True), _row(2, False), _row(3, True), _row(4, False)]
    ag = [_row(1, False), _row(2, True), _row(3, False), _row(4, True)]
    result = s8.stats(al, ag, 2)
    assert result["sample_indices"] == [2, 4]
    assert result["n_nov"] == (2, 2)
    assert result["r_al"] == 0 and result["r_ag"] == 1
    assert result["al_only"] == 0 and result["ag_only"] == 2
    assert result["p"] == 0.5
    assert result["unpaired_al"] == result["unpaired_ag"] == 1
    assert result["raw"]["r_al"] == result["raw"]["r_ag"] == 0.5


def test_exact_p_uses_discordant_pairs_and_handles_no_data():
    al = [_row(i, False) for i in range(6)]
    ag = [_row(i, True) for i in range(6)]
    result = s8.stats(al, ag, 1)
    assert result["p"] == pytest.approx(0.03125)
    assert s8.stats(al, al, 1)["p"] == 1.0
    assert s8.stats([], [], 2)["p"] is None
    assert s8.stats(al, [], 2)["n_nov"] == (0, 0)
    with pytest.raises(ValueError, match="重复"):
        s8.stats([al[0], al[0]], ag, 1)


def test_direction_date_is_derived_from_capture_not_filename(tmp_path):
    class Store:
        def path_of(self, kind, symbol, d):
            return tmp_path / f"{d}.json.gz"

    store = Store()
    et = ZoneInfo("America/New_York")

    def put(day, hour):
        ts = datetime.fromisoformat(f"{day}T{hour}").replace(tzinfo=et).timestamp()
        store.path_of("options", "SYN", date.fromisoformat(day)).write_bytes(
            gzip.compress(json.dumps({"captured_at": ts}).encode()))

    put("2026-09-07", "20:00:00")
    put("2026-09-08", "20:00:00")  # 文件日8号，实际9号才能用。
    put("2026-09-09", "10:00:00")  # 盘中不能用于9号开盘。
    ledger = {"2026-09-08": {"prev_date": "2026-09-07", "call_direction": "偏多"},
              "2026-09-09": {"prev_date": "2026-09-08", "call_direction": "偏空"},
              "2026-09-10": {"prev_date": "2026-09-09", "call_direction": "偏空"}}
    days = [date(2026, 9, x) for x in (8, 9, 10)]
    aligned, audit = s8.align_ledger(ledger, store, "SYN", days)
    assert set(aligned) == {"2026-09-09"}
    assert aligned["2026-09-09"]["source_date"] == "2026-09-08"
    assert len(audit["dropped"]) == 2
    assert "盘中" in audit["dropped"][0]["reason"]


def test_direction_unknown_capture_or_prior_input_is_not_reused(tmp_path):
    class Store:
        def path_of(self, kind, symbol, d):
            return tmp_path / f"{d}.json.gz"

    store = Store()
    store.path_of("options", "SYN", date(2026, 9, 8)).write_bytes(
        gzip.compress(json.dumps({"captured_at": None}).encode()))
    aligned, audit = s8.align_ledger(
        {"2026-09-08": {"prev_date": "2026-09-07", "call_direction": "偏多"}},
        store, "SYN", [date(2026, 9, 8)])
    assert not aligned and len(audit["dropped"]) == 1
    assert "captured_at" in audit["dropped"][0]["reason"]
