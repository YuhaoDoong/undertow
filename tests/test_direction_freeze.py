"""方向台账族 D 冻结（Codex 022）：正式版本/起点、起点前不记录、健康检查不读收益、冻结清单拒绝未提交改动。"""
from datetime import date
from types import SimpleNamespace

from undertow.analyze import conviction as cv
from undertow.analyze import direction_stats as ds


def test_frozen_version_and_start():
    assert cv.RULE["version"] == "conviction-h1-v1-20260928" and cv.RULE["status"].startswith("frozen")
    assert ds.FAMILY_D_START == date(2026, 9, 29)
    # 冻结只改版本号与状态：数值常量与开发期相同
    assert (cv.RULE["s_skew_pp"], cv.RULE["near_pct"], cv.RULE["f_ratio"], cv.RULE["v_atm_pp"]) == (0.5, 0.05, 2.0, 0.3)


def test_conviction_record_skips_sessions_before_start(monkeypatch, capsys):
    from undertow import dirledger_cli as dl
    monkeypatch.setattr(dl, "market_today", lambda: date(2026, 9, 28))
    called = []
    monkeypatch.setattr(dl, "record_one", lambda *a, **k: called.append(1))
    assert dl.cmd_conviction_record(SimpleNamespace()) == 0
    assert not called and "早于方向台账族 D 正式起点" in capsys.readouterr().out


def test_health_reader_drops_return_fields(tmp_path):
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import direction_health as dh
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps({"session": "2026-09-29", "outcome": {"ret_1d": 0.1}, "scored_at": "t",
                             "outcome_history": [], "status": "eligible"}) + "\n")
    rows = dh._rows(p)
    assert rows and not any(k in rows[0] for k in dh.FORBIDDEN)


def test_freeze_manifest_marks_frozen_fields():
    from scripts import direction_freeze_manifest as fm
    m = fm.build(frozen=True, effective_commit="abc")
    assert m["status"].startswith("frozen") and m["effective_commit"] == "abc" and m["frozen_at"]
    assert m["formal_start"] == "2026-09-29" and "FAMILY_D_START" in m["rules"]["direction_stats"]
    d = fm.build()
    assert d["status"].startswith("draft") and d["effective_commit"] is None


# —— Codex 023：健康检查区分 not_started/pending/complete/partial/missing；空目录不算通过 ——
def _dh():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import direction_health as dh
    return dh


def test_health_states_without_rows():
    from datetime import datetime, timezone
    dh = _dh()
    cut = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
    before, after = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc), datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    kw = dict(start="2026-09-29", cutoff=cut, post_start=True)
    assert dh.session_status({}, ("gold",), "2026-09-28", now=after, **kw)[0] == "not_started"
    assert dh.session_status({}, ("gold",), "2026-09-29", now=before, **kw)[0] == "pending"
    assert dh.session_status({}, ("gold",), "2026-09-29", now=after, **kw)[0] == "missing"      # 空目录 ≠ 通过
    st, per = dh.session_status({"gold": {"status": "missing_at_cutoff"}}, ("gold", "silver"), "2026-09-29",
                                now=after, **kw)
    assert st == "missing" and per == {"gold": "missing_at_cutoff", "silver": "absent"}


def test_health_complete_partial_and_quality_after_start(monkeypatch):
    from datetime import datetime, timezone
    dh = _dh()
    cut = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
    after = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dh, "check_formal", lambda r, post_start, level=False:
                        [] if r.get("quality_ok") is True or not post_start else ["起点后 quality_ok 必须为 True"])
    ok = {"status": "eligible", "quality_ok": True}
    kw = dict(start="2026-09-29", cutoff=cut, now=after, post_start=True)
    assert dh.session_status({"gold": ok, "silver": ok}, ("gold", "silver"), "2026-09-29", **kw)[0] == "complete"
    st, per = dh.session_status({"gold": ok, "silver": {"status": "eligible", "quality_ok": None}},
                                ("gold", "silver"), "2026-09-29", **kw)
    assert st == "partial" and per["silver"].startswith("bad")


def test_health_check_formal_real_blob_roundtrip(tmp_path, monkeypatch):
    """levels 行的单快照身份与原文恢复（真实 cas，临时目录）。"""
    import gzip, hashlib, json
    from undertow.collect import cas
    dh = _dh()
    monkeypatch.setattr(cas, "ROOT", tmp_path / "cas")
    comp = gzip.compress(json.dumps({"payload": {"a": 1}, "captured_at": 1.0}).encode())
    sha = cas.put_blob(comp)
    row = {"status": "eligible", "identity_ok": True, "quality_ok": True, "curr_blob": sha,
           "curr_sha": hashlib.sha256(comp).hexdigest()[:16],
           "decision_cutoff": "2026-09-29T09:30:00-04:00", "recorded_at": "2026-09-29T10:00:00+00:00",
           "curr_captured_at": "2026-09-29T09:00:00+00:00", "level": {"skew25_pp": -0.1, "skew10_pp": 0.2}}
    assert dh.check_formal(row, post_start=True, level=True) == []
    bad = dict(row, curr_sha="0" * 16, level={"skew25_pp": None, "skew10_pp": 0.1})
    probs = dh.check_formal(bad, post_start=True, level=True)
    assert any("sha 不符" in p for p in probs) and any("水平缺失" in p for p in probs)
