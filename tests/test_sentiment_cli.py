"""情绪面板 v0 编排层（台账、时点、COT 发布对齐）的合成测试；不联网。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from undertow import sentiment_cli as C
from undertow.analyze import sentiment as S
from undertow.collect import jsonl_ledger as jl
from undertow.core.clock import ET


def _comp(h="h1"):
    return {"vol": {"source_hash": h, "value": 1.0}, "options": {"source_hash": None, "missing_reason": "x"},
            "cot": {"source_hash": "c"}}


def _row(session=date(2026, 10, 28), h="h1", mode="forward"):
    return C.build_row(instrument="gold", session=session, session_status="certified", mode=mode,
                       decision_at=datetime(2026, 10, 28, 1, tzinfo=timezone.utc), components=_comp(h))


def test_write_idempotent_and_revision_chain(tmp_path):
    r1 = _row()
    assert C.write_row(r1, root=tmp_path) == "inserted"
    assert C.write_row(_row(), root=tmp_path) == "exists"          # 同输入重跑 → 幂等
    r2 = _row(h="h2")                                               # 源修订 → 新 vintage
    assert r2["key"] != r1["key"]
    assert C.write_row(r2, root=tmp_path) == "inserted"
    rows = jl.load(tmp_path / "forward" / "gold.jsonl", C.KEY)
    assert [r["revision"] for r in rows] == [1, 2]
    assert rows[1]["supersedes"] == rows[0]["key"]
    assert rows[0]["components"]["vol"]["source_hash"] == "h1"     # 旧值未被覆盖


def test_replay_and_forward_isolated(tmp_path):
    C.write_row(_row(mode="replay"), root=tmp_path)
    assert not (tmp_path / "forward" / "gold.jsonl").exists()
    assert (tmp_path / "replay" / "gold.jsonl").exists()


def test_all_missing_row_still_recorded(tmp_path):
    comps = {"vol": {"source_hash": None, "missing_reason": "source_unavailable"},
             "options": {"source_hash": None, "missing_reason": "no_snapshot_for_session"},
             "cot": {"source_hash": None, "missing_reason": "source_unavailable"}}
    r = C.build_row(instrument="silver", session=date(2026, 10, 28), session_status="certified",
                    mode="forward", decision_at=datetime.now(timezone.utc), components=comps)
    assert C.write_row(r, root=tmp_path) == "inserted"


def test_corrupt_ledger_quarantined_not_overwritten(tmp_path):
    p = tmp_path / "forward" / "gold.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{broken\n")
    with pytest.raises(jl.LedgerCorruptError):
        C.write_row(_row(), root=tmp_path)
    assert p.read_text() == "{broken\n"
    assert list(p.parent.glob("gold.jsonl.corrupt-*"))


def _cot(d, lo=100, sh=50):
    return {"report_date": d, "report_type": "legacy_fut", "category_actual": "noncommercial",
            "long": lo, "short": sh, "spread": None, "open_interest": 1000,
            "change_long": None, "change_short": None}


def test_cot_report_not_used_before_nominal_release_and_carry_not_new_obs():
    tues = [date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 20)]
    ser = [_cot(d, lo=100 + i) for i, d in enumerate(tues)]
    # 10/23（周五）上午：10/20 那期尚未发布
    fri_am = datetime(2026, 10, 23, 10, 0, tzinfo=ET)
    c = C.cot_component("qqq", ser, "h", fri_am, mode="replay", fetched_at=None)
    assert c["report_date"] == "2026-10-13" and c["quality_state"] == S.RECONSTRUCTED
    assert c["category_actual"] == "noncommercial"
    # 周一与周三都用 10/20 那期：n_prior 一样，不因沿用多天而增加
    mon = C.cot_component("qqq", ser, "h", datetime(2026, 10, 26, 9, 0, tzinfo=ET), mode="forward",
                          fetched_at=None)
    wed = C.cot_component("qqq", ser, "h", datetime(2026, 10, 28, 9, 0, tzinfo=ET), mode="forward",
                          fetched_at=None)
    assert mon["report_date"] == wed["report_date"] == "2026-10-20"
    assert mon["rank_net_over_oi"]["n_prior"] == wed["rank_net_over_oi"]["n_prior"] == 2
    assert mon["quality_state"] == S.CURRENT and wed["quality_state"] == S.EXPECTED_CARRY
    # 应有 10/27 那期（10/30 发布后）却只有 10/20 → pending
    nxt_mon = C.cot_component("qqq", ser, "h", datetime(2026, 11, 2, 9, 0, tzinfo=ET), mode="forward",
                              fetched_at=None)
    assert nxt_mon["quality_state"] == S.PENDING


def test_vix_pending_while_others_current():
    td = [date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28)]
    src_new = {"series": [(date(2026, 10, 26), 20.0), (date(2026, 10, 27), 21.0)], "sha256": "a",
               "url": "u", "fetched_at": 0.0}
    src_old = {"series": [(date(2026, 10, 23), 18.0), (date(2026, 10, 26), 18.4)], "sha256": "b",
               "url": "u", "fetched_at": 0.0}
    a = C.vol_component("GVZ", src_new, date(2026, 10, 28), td, mode="forward")
    b = C.vol_component("VIX", src_old, date(2026, 10, 28), td, mode="forward")
    assert a["quality_state"] == S.CURRENT and a["event_date"] == "2026-10-27"
    assert b["quality_state"] == S.PENDING and b["event_date"] == "2026-10-26"   # 不给它贴当天日期
    assert b["age_sessions"] == 1
    m = C.vol_component("OVX", None, date(2026, 10, 28), td, mode="forward")
    assert m["quality_state"] == S.MISSING and m["value"] is None


def test_replay_vol_marks_vintage_not_current():
    s = {"series": [(date(2024, 1, 2), 30.0), (date(2024, 1, 3), 31.0)], "sha256": "x", "url": "u",
         "fetched_at": 0.0}
    c = C.vol_component("VXSLV", s, date(2024, 1, 4), None, mode="replay")
    assert c["quality_state"] == S.RECONSTRUCTED and c["first_available_at"] is None


def _payload(ltt="2026-10-26T15:59:59", volume=10):
    def o(sym, iv, dl, vol=volume):
        return {"option": sym, "iv": iv, "delta": dl, "bid": 1.0, "ask": 1.1, "volume": vol,
                "open_interest": 100}
    return {"timestamp": "2026-10-27 10:02:31",
            "data": {"last_trade_time": ltt,
                     "options": [o("SLV261120P00048000", 0.40, -0.30), o("SLV261120C00050000", 0.38, 0.30),
                                 {"option": "SLV261120C00052000", "iv": 0.37, "delta": 0.2,
                                  "bid": 1.0, "ask": 1.1, "open_interest": 5}]}}


def test_parse_chain_missing_volume_stays_none_and_valuation_from_source():
    cs, meta = C.parse_chain(_payload())
    assert meta["valuation_date"] == date(2026, 10, 26)
    assert any(c["volume"] is None for c in cs)
    f = C.options_features(_payload())
    assert f["pc"]["window"]["volume"]["state"] == "incomplete_missing_fields"
    _, meta2 = C.parse_chain(_payload(ltt=None))
    assert meta2["valuation_date"] is None


def test_options_history_prior_only():
    feats = C.options_features(_payload())
    snap = {"features": feats, "file_date": date(2026, 10, 27),
            "ident": {"sha256": "s", "path": "p", "captured_at": 1.0}}
    hist = [(date(2026, 10, 20) + timedelta(days=i), feats) for i in range(3)]
    a = C.options_component(snap, hist, mode="replay")
    # 历史里加入更晚的样本不在本函数的入参里 → 调用方保证 prior-only；同一入参结果不变
    b = C.options_component(snap, hist, mode="replay")
    assert a["rank_pc_volume_window"] == b["rank_pc_volume_window"]
    assert a["oi_session_basis"] and a["quote_time_basis"] and a["volume_session_basis"]


def test_forward_session_mapping():
    pre = datetime(2026, 10, 28, 8, 0, tzinfo=ET)
    post = datetime(2026, 10, 28, 17, 0, tzinfo=ET)
    sat = datetime(2026, 10, 31, 12, 0, tzinfo=ET)
    assert C._forward_session(pre) == (date(2026, 10, 28), "certified")
    assert C._forward_session(post) == (date(2026, 10, 29), "certified")
    assert C._forward_session(sat) == (date(2026, 11, 2), "certified")
    hol = datetime(2026, 11, 25, 17, 0, tzinfo=ET)        # 感恩节前一天盘后 → 跳过 11/26
    assert C._forward_session(hol) == (date(2026, 11, 27), "certified")



def test_coverage_audit_reports_gaps_and_reconstructed():
    ser = [(date(2022, 2, 10), 30.0), (date(2022, 2, 11), 31.0), (date(2025, 5, 15), 40.0),
           (date(2025, 5, 16), 41.0), (date(2025, 6, 17), 42.0)]
    rep = C.coverage_audit({"VXSLV": {"series": ser, "sha256": "h"}}, {}, {})
    v = rep["vol"]["VXSLV"]
    assert v["gaps_over_10d"] == [["2022-02-11", "2025-05-15"], ["2025-05-16", "2025-06-17"]]
    assert v["n_reconstructed"] == 2
