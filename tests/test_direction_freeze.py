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
