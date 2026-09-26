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



# ── Codex 006 C05 ──────────────────────────────────────────────────────
def test_load_json_rejects_wrong_top_level_type(tmp_path):
    p = tmp_path / "x.json"; p.write_text('{"a": 1}')
    with pytest.raises(ValueError):
        ah.load_json(p, [])
    assert ah.load_json(p, {}) == {"a": 1}


def test_concurrent_first_publication_is_not_lost(tmp_path):
    """两个进程同时首发同一个 key：必须一份成为首发、另一份进修订，不能后写者静默覆盖。"""
    import subprocess, sys, textwrap
    target = tmp_path / "h.json"
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from pathlib import Path
        from undertow.collect import asof_history as ah
        p = Path({str(target)!r})
        with ah.locked(p):
            rows = ah.load_json(p, [])
            time.sleep(0.3)                       # 放大竞争窗口
            merged, revs = ah.freeze_merge(rows, [{{"date": "2026-09-25", "v": sys.argv[1]}}], key=lambda r: r["date"])
            ah.atomic_write_json(p, merged)
            ah.append_revisions(p, revs)
    """)
    procs = [subprocess.Popen([sys.executable, "-c", code, v]) for v in ("first", "second")]
    assert all(pr.wait(timeout=30) == 0 for pr in procs)
    rows = ah.load_json(target, [])
    revs = [json.loads(l) for l in target.with_name(target.name + ".revisions.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and len(revs) == 1
    assert {rows[0]["v"], revs[0]["revision"]["v"]} == {"first", "second"}


def test_vrp_cache_record_is_fingerprinted_latest_cache():
    from dataclasses import dataclass
    from datetime import date as _d
    from undertow.cli import vrp_cache_record

    @dataclass
    class H:
        mean_vrp: float = 1.0
    iv = [(_d(2026, 9, 1), 20.0), (_d(2026, 9, 2), 21.0)]
    r1 = vrp_cache_record(H(), iv_series=iv, px_dates=[_d(2026, 9, 1)], px_closes=[100.0], computed_at="t")
    r2 = vrp_cache_record(H(), iv_series=iv[:1], px_dates=[_d(2026, 9, 1)], px_closes=[100.0], computed_at="t")
    assert r1["kind"] == "latest_cache" and r1["not_for_asof_backtest"] is True and r1["result"] == {"mean_vrp": 1.0}
    assert r1["inputs"]["iv"]["sha"] != r2["inputs"]["iv"]["sha"] and r1["inputs"]["iv"]["last"] == "2026-09-02"
    assert len(r1["code_sha"]) == 16


def test_persist_vrp_is_atomic():
    src = (Path(__file__).resolve().parents[1] / "undertow" / "cli.py").read_text("utf-8")
    body = src[src.index("def _persist_vrp("):src.index("def _persist_signal_probe(")]
    assert "atomic_write_json" in body and "write_text" not in body
