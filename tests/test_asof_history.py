"""S04 首份发布冻结：outlook_scores / resonance / ratio_watch 同日重跑不再覆盖。"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
import pytest
from undertow.collect import asof_history as ah


def test_freeze_merge_keeps_first_and_records_revision():
    old = [{"date": "2026-09-25", "spot": 64.02, "published_at": "t0"}]
    merged, revs = ah.freeze_merge(old, [{"date": "2026-09-25", "spot": 64.71}], key=lambda r: r["date"])
    assert merged[0]["spot"] == 64.02 and len(revs) == 1 and revs[0]["revision"]["spot"] == 64.71
    merged, revs = ah.freeze_merge(merged, [{"date": "2026-09-25", "spot": 64.02}], key=lambda r: r["date"])
    assert revs == [], "相同内容幂等"
    merged, revs = ah.freeze_merge(merged, [{"date": "2026-09-25", "spot": 99}], key=lambda r: r["date"], volatile=("spot",))
    assert revs == [], "声明为易变的字段不算修订"
    merged, _ = ah.freeze_merge(merged, [{"date": "2026-09-26", "spot": 1}], key=lambda r: r["date"])
    assert len(merged) == 2 and merged[-1]["published_at"]


def test_load_json_refuses_corrupt(tmp_path):
    p = tmp_path / "x.json"; p.write_text("{坏")
    with pytest.raises(ValueError):
        ah.load_json(p, [])
    assert p.read_text() == "{坏", "原文件不得被改"


def test_resonance_and_scores_freeze(tmp_path, monkeypatch):
    from undertow import cli
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    cli._persist_resonance({"instrument": "silver", "date": "2026-09-25", "spot": 64.02})
    cli._persist_resonance({"instrument": "silver", "date": "2026-09-25", "spot": 64.71})
    rows = json.loads((tmp_path / "history/resonance/silver.json").read_text())
    assert len(rows) == 1 and rows[0]["spot"] == 64.02
    assert (tmp_path / "history/resonance/silver.json.revisions.jsonl").exists()
    cli._score_trend("gold", "2026-09-25", 1.5); cli._score_trend("gold", "2026-09-25", -0.5)
    d = json.loads((tmp_path / "history/outlook_scores.json").read_text())
    assert d["gold"]["2026-09-25"] == 1.5
    assert "-0.5" in (tmp_path / "history/outlook_scores.json.revisions.jsonl").read_text()


def test_ratio_watch_freeze(tmp_path, monkeypatch):
    from undertow.analyze import ratio_watch as rw
    monkeypatch.setattr(rw, "STORE", tmp_path / "ratio_watch.json")
    fields = {f: None for f in rw.RatioRow.__dataclass_fields__}
    a = rw.RatioRow(**{**fields, "date": "2026-09-25", "pair": "金银比", "ratio": 66.3})
    b = rw.RatioRow(**{**fields, "date": "2026-09-25", "pair": "金银比", "ratio": None})
    assert rw.save([a]) == 1 and rw.save([b]) == 0
    rows = json.loads((tmp_path / "ratio_watch.json").read_text())
    assert rows[0]["ratio"] == 66.3, "只跑白银的手动运行不得把已发布的金银比写成 null"


def test_report_guards_ratio_watch_against_replay_and_partial_rows():
    src = (ROOT / "undertow" / "cli.py").read_text("utf-8")
    assert "n = 0 if replay else _rw_save([r for r in _ratio_rows if r.ratio is not None])" in src
