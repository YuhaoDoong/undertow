"""第六步 VRP 脚本的口径锁。"""
import importlib.util
import json
import math
import sys
import statistics as st
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as N

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("step6", ROOT / "scripts" / "step6_vrp.py")
s6 = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(s6)
from undertow.analyze.vrp_research import nonoverlapping_rows, realized_expiry_window, short_summary


def test_stats_helpers():
    assert s6.one_sample_t([1.0, 1.0, 1.0]) is None
    assert s6.one_sample_t([1.0, 2.0]) is None
    assert s6.welch([], [1, 2, 3]) is None
    assert s6.welch([1, 1, 1], [2, 2, 2]) is None
    assert s6.one_sample_t([-1, 0, 1]) == 0.0, "可估计的零不能变成缺失"
    assert s6.one_sample_t([2, 3, 4, 3, 2, 4, 3]) > 5
    assert abs(s6.welch([1, 2, 3], [1, 2, 3])) < 1e-12
    assert s6.welch([10, 11, 12], [1, 2, 3]) > 5
    d = s6.describe([-3, -1, 0, 2, 5])
    assert d["n"] == 5 and d["pos"] == 0.4 and d["median"] == 0
    assert s6.describe([]) is None
    print("PASS test_stats_helpers")


def test_short_dte_realized_window_starts_at_T():
    dates = [date(2026, 9, d) for d in (17, 18, 21, 22, 23, 24, 25)]
    closes = [1, 100, 110, 99, 100, 90, 95]
    window, reason = realized_expiry_window(dates, closes, dates[2], dates[-1])
    expected = st.pstdev([math.log(closes[i] / closes[i - 1]) for i in range(2, 7)]) * math.sqrt(252) * 100
    assert reason is None and window["rv"] == pytest.approx(expected)
    assert window["base_date"] == "2026-09-18"
    assert window["return_dates"] == [d.isoformat() for d in dates[2:]]
    assert window["n_returns"] == 5
    closes[0] = 10000
    assert realized_expiry_window(dates, closes, dates[2], dates[-1])[0]["rv"] == window["rv"]


@pytest.mark.parametrize("dates,session,expiry,reason", [
    ([18, 21, 22, 23], 21, 25, "unmatured_expiry"),
    ([18, 21, 22, 23, 25], 21, 24, "missing_expiry_close"),
    ([21, 22, 23], 21, 23, "missing_base_close"),
    ([18, 21, 22], 21, 21, "insufficient_returns"),
])
def test_expiry_window_rejects_incomplete_history(dates, session, expiry, reason):
    ds = [date(2026, 9, d) for d in dates]
    result = realized_expiry_window(ds, [100.0] * len(ds), date(2026, 9, session), date(2026, 9, expiry))
    assert result == (None, reason)


def _short_fixture(monkeypatch, *, days=(18, 21, 22, 23, 24, 25)):
    ds = [date(2026, 9, d) for d in days]
    ser = N(dates=ds, closes=[90, 91, 89, 92, 93, 94][:len(ds)])
    source = N(fetch_series=lambda inst: ser)
    expiry = date(2026, 9, 25)
    snap = N(spot=100.0, asof="2026-09-18T16:00:00",
             contracts=[N(strike=90, expiry=expiry, iv=.30), N(strike=100, expiry=expiry, iv=.90)])
    monkeypatch.setattr(s6.s5, "load_days", lambda *a: ({date(2026, 9, 21): (123.0, {})}, 2))
    monkeypatch.setattr(s6, "snapshot_from_payload", lambda *a: snap)
    return N(options=N(symbol="X")), source, snap


def test_short_dte_uses_real_previous_close_and_audits(monkeypatch):
    inst, source, snap = _short_fixture(monkeypatch)
    audit = {}
    rows = s6.short_dte("x", inst, None, source, audit=audit)
    assert len(rows) == 1
    row = rows[0]
    assert row["iv"] == 30 and row["atm_strikes"] == [90]
    assert row["decision_price"] == 90 and row["snapshot_spot"] == 100
    assert row["expiry"] == row["return_end"] == "2026-09-25"
    assert row["captured_at"] == 123.0 and row["quote_asof"] == snap.asof
    assert audit["included"] == 1 and audit["unmapped_snapshots"] == 2
    snap.spot = 0  # 快照现价损坏也不能影响真实前收选档。
    assert s6.short_dte("x", inst, None, source)[0]["iv"] == 30


def test_short_dte_does_not_score_unmatured_expiry(monkeypatch):
    inst, source, _ = _short_fixture(monkeypatch, days=(18, 21, 22, 23))
    audit = {}
    assert s6.short_dte("x", inst, None, source, audit=audit) == []
    assert audit["excluded"] == {"unmatured_expiry": 1}
    assert audit["included"] == 0


def test_short_nonoverlap_uses_actual_return_intervals_and_same_samples():
    def row(start, end, vrp):
        return {"T": start, "return_start": start, "return_end": end, "vrp": vrp}
    rows = [row("2026-09-01", "2026-09-04", 1), row("2026-09-02", "2026-09-03", -100),
            row("2026-09-04", "2026-09-08", -100), row("2026-09-08", "2026-09-10", 2),
            row("2026-09-11", "2026-09-14", 3)]
    chosen = nonoverlapping_rows(rows)
    assert [r["vrp"] for r in chosen] == [1, 2, 3]
    result = short_summary(rows)
    assert result["all"]["mean"] < 0
    assert result["nonoverlap"]["mean"] == 2
    assert result["n_nonoverlap"] == 3
    assert result["t_nonoverlap"] == pytest.approx(2 * math.sqrt(3))
    assert short_summary([])["t_nonoverlap"] is None


def test_long_history_reports_matched_effect_and_test(monkeypatch):
    ds = [date(2025, 1, 1) + timedelta(days=i) for i in range(150)]
    rest_idx = {41, 83, 125}
    atr = [1.0] * 150
    for i in range(5, 150):
        atr[i] = atr[i - 5] * (1 if i in rest_idx else 2)
    iv = [(d, 10 if i in rest_idx else {20: 20, 62: 30, 104: 40}.get(i, 1)) for i, d in enumerate(ds)]
    monkeypatch.setattr(s6, "_atr_series", lambda *a: atr)
    monkeypatch.setattr(s6, "_pct_rank_series", lambda xs: [.5] * len(xs))
    monkeypatch.setattr(s6, "forward_realized_vol", lambda *a: {d: 0 for d in ds[:-21]})
    px = N(fetch_series=lambda inst: N(dates=ds, closes=[100.] * 150, highs=[101.] * 150, lows=[99.] * 150))
    result = s6.long_history(N(vol_index="TEST"), N(fetch_series=lambda sym: iv), px)
    state = result["states"]["ATR扩张≥1.3"]
    assert state["mean"] < 10, "全样本描述刻意与不重叠样本反号"
    assert state["nonoverlap"]["n"] == state["n_nov"] == 3
    assert state["rest_nonoverlap"]["n"] == 3
    assert state["nonoverlap"]["mean"] == 30
    assert state["mean_diff_nonoverlap"] == 20 and state["welch_vs_rest"] > 0
    empty = result["states"]["ATR分位≤10%"]
    assert empty["n_nov"] == 0 and empty["welch_vs_rest"] is None
    assert empty["mean_diff_nonoverlap"] is None


def test_main_renders_missing_inference_and_emits_v2(monkeypatch, tmp_path, capsys):
    inst = N(vol_index="X", price=True)
    cfg = N(instruments={"empty": inst}, get=lambda key: inst)
    monkeypatch.setattr(s6, "load_config", lambda: cfg)
    monkeypatch.setattr(s6, "CboeVolSource", lambda: None)
    monkeypatch.setattr(s6, "CboeHistorySource", lambda: None)
    monkeypatch.setattr(s6, "SnapshotStore", lambda: None)
    monkeypatch.setattr(s6, "long_history", lambda *a, **kw: None)
    def empty_short(*a, audit=None):
        audit.update(decision_days=1, included=0, unmapped_snapshots=0, excluded={"unmatured_expiry": 1})
        return []
    monkeypatch.setattr(s6, "short_dte", empty_short)
    out = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["step6", "--emit", "--output", str(out)])
    s6.main()
    saved = json.loads(out.read_text())
    assert saved["schema"] == 2
    assert saved["short_summary"]["silver"]["t_nonoverlap"] is None
    assert saved["short_audit"]["silver"]["excluded"] == {"unmatured_expiry": 1}
    assert "无完整的历史配对样本" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.tmp.*"))


def test_emitted_artifact():
    p = ROOT / "data" / "history" / "wall_spread" / "vrp_states.json"
    if not p.exists():
        pytest.skip("尚未 --emit")
    d = json.loads(p.read_text("utf-8"))
    assert d["schema"] == 1 and d["window"] == 21
    for k, v in d["long"].items():
        assert set(v["states"]) >= {"ATR扩张≥1.3", "ATR分位≥90%", "IV分位≥70%"}, k
        # 不重叠子样本按【交易日索引】每 21 根取 1，而 rows 会跳过缺 IV 的日子，
        # 所以 n_nonoverlap×21 可以略大于 n（实测 aapl 188×21=3948 > 3925）。
        # 能守住的不变式只有：子样本不多于全样本，且比全样本小得多。
        assert v["n_nonoverlap"] <= v["n"] // 15, k
    assert "silver" in d["short"] and len(d["short"]["silver"]) >= 30
    print("PASS test_emitted_artifact")
