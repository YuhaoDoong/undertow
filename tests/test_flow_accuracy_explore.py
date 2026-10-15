"""flow-accuracy-explore-v1 的对齐与计分（合成输入）。"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("fae", Path(__file__).resolve().parents[1] / "scripts/flow_accuracy_explore.py")
fae = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fae)


def px():
    # 10/09(五) 10/12(一) 10/13(二) 10/14(三)：(o,h,l,c)
    return {"2026-10-09": (100, 101, 99, 100), "2026-10-12": (100, 103, 99, 102),
            "2026-10-13": (102, 102, 97, 98), "2026-10-14": (98, 98, 98, 98)}


def test_weekend_row_maps_to_next_session_and_latest_D_wins():
    rows = [{"date": "2026-10-10", "call_direction": "偏空"}, {"date": "2026-10-11", "call_direction": "偏多"}]
    r = fae.analyze("x", "X", rows, px())
    p = r["periods"]["all"]["open_to_close"]
    assert r["counts"]["superseded_same_T"] == 1 and p["n"] == 1 and p["hits"] == 1   # 10/12 开 100 收 102，偏多命中


def test_tie_excluded_and_pending_counted():
    rows = [{"date": "2026-10-14", "call_direction": "偏多"}, {"date": "2026-10-20", "call_direction": "偏多"}]
    r = fae.analyze("x", "X", rows, px())
    assert r["periods"]["all"]["open_to_close"]["n"] == 0 and r["counts"]["pending"] == 1


def test_binomial_and_wilson():
    assert fae.binom_p(5, 10) == 1.0 and round(fae.binom_p(9, 10), 4) == 0.0215
    assert fae.wilson_lo(0, 0) is None and 0.5 < fae.wilson_lo(23, 32) < 0.6
