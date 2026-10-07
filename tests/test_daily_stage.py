"""daily 阶段状态与缺口检查（Codex 033 A2）：损坏快照不算齐、同一快照身份才算完成、截止后缺口归因、非交易日不期望数据。"""
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import daily_stage as ds  # noqa: E402

DAY = "2026-10-05"


def _snap(root, sym, body=b'{"x": 1}'):
    f = root / "data/snapshots/options" / sym / f"{DAY}.json.gz"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(gzip.compress(body))
    return f


def test_corrupt_snapshot_is_not_complete_and_identity_only_when_all_valid(tmp_path):
    _snap(tmp_path, "GLD")
    f = _snap(tmp_path, "SLV"); f.write_bytes(b"not gz")
    st = ds.snapshot_state(DAY, ["GLD", "SLV", "USO"], tmp_path)
    assert st == {"missing": ["USO"], "corrupt": ["SLV"], "identity": ""}
    _snap(tmp_path, "SLV"); _snap(tmp_path, "USO")
    st = ds.snapshot_state(DAY, ["GLD", "SLV", "USO"], tmp_path)
    assert not st["missing"] and not st["corrupt"] and len(st["identity"]) == 16


def test_done_requires_same_snapshot_identity(tmp_path):
    (tmp_path / f".daily_done_{DAY}.json").write_text(json.dumps({"identity": "aaa"}))
    assert ds.done_match(DAY, "aaa", tmp_path) and not ds.done_match(DAY, "bbb", tmp_path) and not ds.done_match(DAY, "", tmp_path)


def test_classify_missing_reasons():
    st = {"items": [{"instrument": "silver", "status": "unchanged"}, {"instrument": "wti", "status": "failed"}]}
    c = lambda **kw: ds.classify_missing(DAY, "X", status=st, holiday=False, **kw)
    assert c(last_run_et="05:45", key="silver").startswith("missed_window")       # 10/05 实况：末班 05:45
    assert c(last_run_et=None, key="silver").startswith("missed_window")          # 08:40 睡眠 → 09:05 才醒
    assert c(last_run_et="08:50", key="silver").startswith("not_published")
    assert c(last_run_et="08:50", key="wti") == "fetch_failed"
    assert ds.classify_missing(DAY, "X", status=st, last_run_et="08:50", holiday=True) == "non_trading_day_expected"


def test_gapcheck_writes_dedup_records_and_holiday_expects_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_ledger_gaps", lambda day, root: [])
    monkeypatch.setattr(ds, "sym_to_key", lambda: {"GLD": "gold", "SLV": "silver"})
    logs, gaps = tmp_path / "logs", tmp_path / "gaps"; logs.mkdir()
    (logs / f".daily_lastrun_{DAY}").write_text("05:45")
    _snap(tmp_path, "GLD")
    now = datetime(2026, 10, 5, 13, 10, tzinfo=timezone.utc)
    r = ds.gapcheck(DAY, root=tmp_path, logs=logs, gaps_dir=gaps, syms=["GLD", "SLV"], holiday=False, now=now)
    assert [g["instrument"] for g in r["gaps"]] == ["SLV"] and r["gaps"][0]["reason"].startswith("missed_window")
    ds.gapcheck(DAY, root=tmp_path, logs=logs, gaps_dir=gaps, syms=["GLD", "SLV"], holiday=False, now=now)
    lines = (gaps / "2026-10.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["protocol_version"] == ds.PROTOCOL           # 重复检查不重复记
    assert ds.gapcheck(DAY, root=tmp_path, logs=logs, gaps_dir=gaps, syms=["GLD", "SLV"], holiday=True)["gaps"] == []


def test_daily_script_stage_flow():
    src = (ROOT / "scripts/daily_update.sh").read_text("utf-8")
    head = src[:src.index("RUNLOG=")]
    assert "MODE_GAPCHECK=1" in head and "ET_HOUR >= 9 && ET_HOUR < 12" in head      # 截止后只读缺口检查
    assert src.index("daily_stage.py gapcheck") < src.index("daily_stage.py snapshots")
    assert "done-match" in src and "RESUME=1" in src and "fi   # ! RESUME" in src           # 快照齐但下游未完成 → 续跑
    i_snap = src.index("python3 -m undertow snapshot --status-file")
    assert src.index("末班抓取后仍缺当日快照") > i_snap                                     # 末班结论在抓取之后
    assert "mark-done" in src[src.index("case $PUB_RC in"):]                                 # 发布成功才标完成
