"""前瞻台账的历史保全、并发事务与到期模型口径；全部使用临时目录。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from undertow.analyze import spread_ledger as sl
from undertow.analyze import wall_spread as ws


DAY = date(2026, 9, 8)
EXPIRY = date(2026, 9, 11)


def candidate(**changes):
    values = dict(kind="P", expiry=EXPIRY, dte=3, sell=55.0, buy=53.0,
                  wall=55.0, offset=0, width_n=2, credit=20.0, width=200.0,
                  spot=60.0, wall_rule="基准")
    values.update(changes)
    return ws.Candidate(**values)


def verdict(*candidates, reason="首份"):
    return ws.Verdict(bool(candidates), reason, puts=list(candidates), params={})


def record(root, value=None, day=DAY, **kwargs):
    return sl.record("silver", "SLV", day, 60.0,
                     value if value is not None else verdict(candidate()),
                     root=root, **kwargs)


@pytest.mark.parametrize("operation", ["load", "record", "backfill", "summarize"])
def test_corrupt_line_is_never_dropped_or_overwritten(tmp_path, operation):
    path = record(tmp_path)
    raw = path.read_bytes() + b'{"date":"2026-09-09", BROKEN}\n'
    path.write_bytes(raw)
    operations = {
        "load": lambda: sl.load("silver", root=tmp_path),
        "record": lambda: record(tmp_path, day=DAY + timedelta(days=1)),
        "backfill": lambda: sl.backfill("silver", {EXPIRY.isoformat(): 60.0}, root=tmp_path),
        "summarize": lambda: sl.summarize("silver", root=tmp_path),
    }
    # 第二次唤醒必须仍失败；不能把原件 rename 后当成空表继续写。
    for _ in range(2):
        with pytest.raises(sl.LedgerCorruptError, match="原文件未改.*隔离副本"):
            operations[operation]()
    assert path.read_bytes() == raw
    backups = list(tmp_path.glob("silver.jsonl.corrupt-*"))
    assert len(backups) == 2
    assert all(p.read_bytes() == raw for p in backups)


@pytest.mark.parametrize("bad", [b"", b"[]\n", b"null\n", b"{}\n",
                                  b'{"date":"x","date":"y"}\n', b"\xff\n"])
def test_empty_or_invalid_structure_is_explicit_corruption(tmp_path, bad):
    path = tmp_path / "silver.jsonl"
    path.write_bytes(bad)
    with pytest.raises(sl.LedgerCorruptError):
        sl.load("silver", root=tmp_path)
    assert path.read_bytes() == bad
    assert next(tmp_path.glob("*.corrupt-*")).read_bytes() == bad


@pytest.mark.parametrize("change", [
    lambda r: r.update(candidates={}),
    lambda r: r["candidates"][0].update(kind="X"),
    lambda r: r["candidates"][0].pop("width"),
    lambda r: r.update(context={"atr": float("nan")}),
    lambda r: r["candidates"][0].update(credit="20"),
    lambda r: r["candidates"][0].update(pnl=1.0, settle=None),
])
def test_malformed_candidate_or_context_is_not_a_valid_history(tmp_path, change):
    path = record(tmp_path)
    row = json.loads(path.read_text())
    change(row)
    raw = (json.dumps(row) + "\n").encode()
    path.write_bytes(raw)
    with pytest.raises(sl.LedgerCorruptError):
        sl.backfill("silver", {}, root=tmp_path)
    assert path.read_bytes() == raw


def test_same_input_preserves_first_timestamp_and_backfill(tmp_path):
    path = record(tmp_path)
    first = sl.load("silver", root=tmp_path)[0]
    assert datetime.fromisoformat(first["recorded_at"]).utcoffset() == timedelta(0)
    assert sl.backfill("silver", {EXPIRY.isoformat(): 54.0}, root=tmp_path) == (1, 0)
    raw = path.read_bytes()
    assert record(tmp_path) == path
    assert path.read_bytes() == raw
    settled = sl.load("silver", root=tmp_path)[0]
    assert settled["recorded_at"] == first["recorded_at"]
    assert settled["candidates"][0]["pnl"] == pytest.approx(-83.2)


@pytest.mark.parametrize("replacement,context", [
    (verdict(candidate(credit=30.0)), None),
    (verdict(), None),
    (verdict(candidate(), reason="事后改变"), None),
    (verdict(candidate()), {"atr_expand_5": 1.3}),
])
def test_different_same_day_input_cannot_rewrite_first_decision(tmp_path, replacement, context):
    path = record(tmp_path)
    sl.backfill("silver", {EXPIRY.isoformat(): 54.0}, root=tmp_path)
    before = path.read_bytes()
    with pytest.raises(sl.LedgerConflictError, match="首份候选已冻结"):
        record(tmp_path, replacement, context=context)
    assert path.read_bytes() == before


def test_old_schema_without_context_or_timestamp_remains_readable_and_idempotent(tmp_path):
    path = record(tmp_path)
    old = json.loads(path.read_text())
    del old["context"], old["recorded_at"]
    path.write_text(json.dumps(old) + "\n")
    before = path.read_bytes()
    assert sl.load("silver", root=tmp_path) == [old]
    record(tmp_path)
    assert path.read_bytes() == before
    assert sl.backfill("silver", {EXPIRY.isoformat(): 54.0}, root=tmp_path) == (1, 0)


@pytest.mark.parametrize("failure", ["replace", "fsync", "reread"])
def test_failed_atomic_write_keeps_existing_bytes_and_cleans_temp(tmp_path, monkeypatch, failure):
    path = record(tmp_path)
    before = path.read_bytes()
    if failure in ("replace", "fsync"):
        def fail(*args):
            raise OSError("injected disk failure")
        monkeypatch.setattr(sl.os, failure, fail)
        error = OSError
    else:
        real_read = Path.read_bytes

        def bad_reread(p):
            return b"truncated" if p.name.endswith(".tmp") else real_read(p)
        monkeypatch.setattr(Path, "read_bytes", bad_reread)
        error = ValueError
    with pytest.raises(error):
        record(tmp_path, day=DAY + timedelta(days=1))
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_backfill_failed_atomic_commit_does_not_partly_settle(tmp_path, monkeypatch):
    path = record(tmp_path)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(sl.os, "replace", fail)
    with pytest.raises(OSError):
        sl.backfill("silver", {EXPIRY.isoformat(): 54.0}, root=tmp_path)
    assert path.read_bytes() == before


def test_concurrent_records_do_not_lose_other_dates(tmp_path, monkeypatch):
    real_load = sl._load_path

    def slow_load(path, inst):
        rows = real_load(path, inst)
        # 无事务锁时所有线程会先读到同一旧版本，再互相覆盖。
        time.sleep(0.02)
        return rows
    monkeypatch.setattr(sl, "_load_path", slow_load)
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait(timeout=5)
        record(tmp_path, verdict(), day=DAY + timedelta(days=i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    rows = sl.load("silver", root=tmp_path)
    assert [r["date"] for r in rows] == [(DAY + timedelta(days=i)).isoformat() for i in range(8)]


def test_concurrent_record_and_backfill_both_survive(tmp_path, monkeypatch):
    record(tmp_path)
    real_load = sl._load_path

    def slow_load(path, inst):
        rows = real_load(path, inst)
        time.sleep(0.02)
        return rows
    monkeypatch.setattr(sl, "_load_path", slow_load)
    barrier = threading.Barrier(2)

    def writer():
        barrier.wait(timeout=5)
        record(tmp_path, verdict(), day=DAY + timedelta(days=1))

    def fill():
        barrier.wait(timeout=5)
        return sl.backfill("silver", {EXPIRY.isoformat(): 54.0}, root=tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        written, filled = pool.submit(writer), pool.submit(fill)
        written.result(timeout=5)
        assert filled.result(timeout=5) == (1, 0)
    rows = sl.load("silver", root=tmp_path)
    assert len(rows) == 2
    assert rows[0]["candidates"][0]["pnl"] == pytest.approx(-83.2)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "54.0", -1.0, True])
def test_bad_settlement_fails_whole_update(tmp_path, value):
    path = record(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="收盘无效"):
        sl.backfill("silver", {EXPIRY.isoformat(): value}, root=tmp_path)
    assert path.read_bytes() == before


def test_missing_settlement_stays_unknown_but_zero_is_a_real_price(tmp_path):
    path = record(tmp_path)
    before = path.read_bytes()
    assert sl.backfill("silver", {}, root=tmp_path) == (0, 1)
    assert path.read_bytes() == before
    summary = sl.summarize("silver", root=tmp_path)
    assert summary["pending"] == 1 and summary["complete"] is False
    assert summary["total_pnl"] is None and summary["break_rate"] is None
    assert sl.backfill("silver", {EXPIRY.isoformat(): 0.0}, root=tmp_path) == (1, 0)
    c = sl.load("silver", root=tmp_path)[0]["candidates"][0]
    assert c["settle"] == 0.0 and c["pnl"] == pytest.approx(-183.2)


def test_summary_distinguishes_breach_loss_and_unique_dates(tmp_path):
    # 轻微破腿仍净赚；零内在价值也可能不够手续费。第三笔恰好打平。
    record(tmp_path, verdict(candidate(credit=20.0), candidate(credit=2.0),
                             candidate(credit=3.2, sell=54.0, buy=52.0)))
    record(tmp_path, verdict(), day=DAY + timedelta(days=1))
    assert sl.backfill("silver", {EXPIRY.isoformat(): 54.9}, root=tmp_path) == (3, 0)
    summary = sl.summarize("silver", root=tmp_path)
    assert summary["basis"] == "hypothetical_candidates_hold_to_expiry"
    assert "非实盘" in summary["note"] and "重叠" in summary["note"]
    assert summary["days"] == 2 and summary["days_with_candidate"] == 1
    assert summary["settled"] == 3 and summary["settled_decision_dates"] == 1
    assert summary["broke"] == 2
    assert (summary["wins"], summary["losses"], summary["flat"]) == (1, 1, 1)
    assert summary["complete"] and summary["pending"] == 0
