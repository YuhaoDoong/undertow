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
    ok = {n: {"status": "ok"} for n in ds.REQUIRED_STAGES}
    (tmp_path / f".daily_done_{DAY}.json").write_text(json.dumps({"schema": 2, "identity": "aaa", "stages": ok}))
    assert ds.done_match(DAY, "aaa", tmp_path) and not ds.done_match(DAY, "bbb", tmp_path) and not ds.done_match(DAY, "", tmp_path)


def test_legacy_or_incomplete_done_is_not_accepted(tmp_path):
    """Codex 036 §4 探针：旧格式只有 identity 的完成文件、或必需阶段缺失的凭证，都不能 done-match。"""
    f = tmp_path / f".daily_done_{DAY}.json"
    f.write_text(json.dumps({"identity": "aaa"}))
    assert not ds.done_match(DAY, "aaa", tmp_path)
    st = {n: {"status": "ok"} for n in ds.REQUIRED_STAGES}; st["report"] = {"status": "partial"}
    f.write_text(json.dumps({"schema": 2, "identity": "aaa", "stages": st}))
    assert not ds.done_match(DAY, "aaa", tmp_path)
    st["report"] = {"status": "ok"}; st["dirledger_record"] = {"status": "ok_ineligible"}
    f.write_text(json.dumps({"schema": 2, "identity": "aaa", "stages": st}))
    assert ds.done_match(DAY, "aaa", tmp_path)                       # 终态行有不合格 = 完成（质量缺口另报）


def test_classify_missing_reasons():
    st = {"schema": 1, "items": [{"instrument": "silver", "status": "unchanged"}, {"instrument": "wti", "status": "failed"}]}
    c = lambda **kw: ds.classify_missing(DAY, "X", status=st, holiday=False, **kw)
    assert c(last_run_et="05:45", key="silver").startswith("missed_window")       # 10/05 实况：末班 05:45
    assert c(last_run_et=None, key="silver").startswith("missed_window")          # 08:40 睡眠 → 09:05 才醒
    assert c(last_run_et="08:50", key="silver").startswith("not_published")
    assert c(last_run_et="08:50", key="wti") == "fetch_failed"
    assert ds.classify_missing(DAY, "X", status=st, last_run_et="08:50", holiday=True) == "non_trading_day_expected"
    # Codex 034 §3.3：状态缺失/损坏、或品种没有明确结果 → unknown，不能推断「源未发布」
    assert ds.classify_missing(DAY, "X", status=None, last_run_et="08:50", holiday=False, key="silver").startswith("unknown")
    assert c(last_run_et="08:50", key="gold").startswith("unknown")


def test_gapcheck_writes_dedup_records_and_holiday_expects_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "_ledger_gaps", lambda day, root, registry=None: [])
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
    assert "MODE_GAPCHECK=1" in head and "daily_stage.py gapcheck-pending" in head      # 窗口外任意唤醒补查到期会话
    assert "ET_HOUR >= 9 && ET_HOUR < 12" not in head                                        # 不再限 9–12 点
    assert ".daily_gapcheck_" not in src                                                     # 不再「查一次就封账」
    assert src.index("daily_stage.py gapcheck-due") < src.index("daily_stage.py snapshots")
    assert "done-match" in src and "RESUME=1" in src and "fi   # ! RESUME" in src           # 快照齐但下游未完成 → 续跑
    i_snap = src.index("python3 -m undertow snapshot --status-file")
    assert src.index("末班抓取后仍缺当日快照") > i_snap                                     # 末班结论在抓取之后
    assert "mark-done" in src[src.index("case $PUB_RC in"):]                                 # 发布成功才标完成


def test_non_trading_day_gate():
    """周末确定休市 → 跳过（10/10、10/11 曾每次唤醒都抓、末班误报「仍缺当日快照」）；日历未覆盖 → 不当作休市。"""
    assert ds.is_non_trading("2026-10-10") and ds.is_non_trading("2026-10-11")
    assert not ds.is_non_trading("2026-10-12")
    assert not ds.is_non_trading("2099-01-03")                        # 未覆盖 → 未知，按交易日跑
    src = (ROOT / "scripts/daily_update.sh").read_text("utf-8")
    gate = src.index('daily_stage.py trading-day "$ET_DATE"')
    assert src.index("if (( MODE_GAPCHECK )); then") < gate < src.index('daily_stage.py lastrun "$ET_DATE"')
    assert gate < src.index("末班预警") and gate < src.index("🚨 末班抓取后仍缺当日快照")


def test_stage_ledger_publish_success_is_not_completion(tmp_path):
    """Codex 034 §3.1：报告 partial / 台账失败但发布成功 → 不能算完成；只有必需阶段全部 ok 才完成。"""
    ident = "abc"
    for n in ds.REQUIRED_STAGES:
        ds.set_stage(DAY, ident, n, "ok", 0, logs=tmp_path)
    ds.set_stage(DAY, ident, "report", "partial", 1, logs=tmp_path)
    assert ds.missing_required(DAY, ident, logs=tmp_path) == ["report"]
    ds.set_stage(DAY, ident, "report", "ok", 0, logs=tmp_path)
    ds.set_stage(DAY, ident, "paper_book", "failed", 2, logs=tmp_path)           # 可选失败不挡完成，但保留状态
    assert ds.missing_required(DAY, ident, logs=tmp_path) == []
    assert ds.load_stages(DAY, ident, logs=tmp_path)["paper_book"]["status"] == "failed"
    assert ds.stage_ok(DAY, ident, "report", logs=tmp_path) and not ds.stage_ok(DAY, ident, "paper_book", logs=tmp_path)


def test_stage_ledger_resets_on_new_identity_and_isolates_corrupt(tmp_path):
    ds.set_stage(DAY, "id1", "report", "ok", 0, logs=tmp_path)
    assert ds.load_stages(DAY, "id2", logs=tmp_path) == {}                          # 新快照落盘 → 新一轮
    f = tmp_path / f".daily_stages_{DAY}.json"
    f.write_text("{坏", "utf-8")
    assert ds.load_stages(DAY, "id1", logs=tmp_path) == {}
    assert list(tmp_path.glob(f".daily_stages_{DAY}.json.corrupt-*")), "损坏文件隔离保留"
    import pytest
    with pytest.raises(ValueError):
        ds.set_stage(DAY, "", "report", "ok", 0, logs=tmp_path)                    # 没有快照身份不记
    with pytest.raises(ValueError):
        ds.set_stage(DAY, "id1", "nonexistent", "ok", 0, logs=tmp_path)


def test_daily_script_marks_every_required_stage():
    src = (ROOT / "scripts/daily_update.sh").read_text("utf-8")
    for n in ds.REQUIRED_STAGES:
        assert f"stage_mark {n} " in src, n
    assert src.index("stage_done() {") < src.index("stage_done report")              # 函数定义在调用之前
    assert src.index("stage_mark publish ok 0") < src.index('daily_stage.py mark-done "$ET_DATE"')


def test_due_sessions_watermark_cutoff_and_unknown_calendar(tmp_path):
    """截止 09:30 前不查当天；午后/次日醒来补查之前所有会话；最多 5 天；日历未知停住。"""
    cal = lambda d: d.weekday() < 5 if d.isoformat() < "2026-10-20" else None
    at = lambda s: datetime.fromisoformat(s)
    days, unk = ds.due_sessions(at("2026-10-07T09:05:00-04:00"), tmp_path, start="2026-10-07", is_trading=cal)
    assert days == [] and unk is None                                       # 09:05 只能预检，不出最终缺口
    days, _ = ds.due_sessions(at("2026-10-07T09:31:00-04:00"), tmp_path, start="2026-10-07", is_trading=cal)
    assert days == [("2026-10-07", True)]
    days, _ = ds.due_sessions(at("2026-10-15T15:00:00-04:00"), tmp_path, start="2026-10-07", is_trading=cal)
    assert [d for d, t in days if t] == ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]  # 5 个交易日，周末不占名额
    ds.set_watermark("2026-10-13", tmp_path)
    days, _ = ds.due_sessions(at("2026-10-15T15:00:00-04:00"), tmp_path, start="2026-10-07", is_trading=cal)
    assert [d for d, _ in days] == ["2026-10-14", "2026-10-15"]
    ds.set_watermark("2026-10-19", tmp_path)
    days, unk = ds.due_sessions(at("2026-10-21T15:00:00-04:00"), tmp_path, start="2026-10-07", is_trading=cal)
    assert days == [] and unk == "2026-10-20"                               # 日历未覆盖 → 不默认是交易日


def test_gapcheck_due_requeues_failures_and_open_gaps(tmp_path, monkeypatch):
    """Codex 036 §3 探针：有缺口或检查失败时，旧版推进水位后再也不查。现：水位照推进（不挡新日期），失败下次唤醒重试，
    开放缺口 24 小时后复查；补齐出队；新日期照常首次检查。"""
    from datetime import timedelta
    t0 = datetime.fromisoformat("2026-10-13T10:00:00-04:00")
    days = {"v": [("2026-10-13", True)]}
    monkeypatch.setattr(ds, "due_sessions", lambda n, logs: (days.pop("v", []), None))
    res = {"2026-10-13": {"gaps": [{"dataset": "x", "instrument": "gold", "reason": "no_row"}], "resolved": [],
                          "open_keys": ["k"], "check_failed": []}}
    calls = []
    def chk(day):
        calls.append(day)
        r = res[day]
        if r == "boom":
            raise RuntimeError("x")
        return r
    assert ds.gapcheck_due(t0, tmp_path, check=chk) == 2
    assert json.loads((tmp_path / ".gapcheck_watermark.json").read_text())["last_checked"] == "2026-10-13"
    assert ds.gapcheck_due(t0 + timedelta(hours=1), tmp_path, check=chk) == 0 and calls == ["2026-10-13"]   # 未到复查时刻
    res["2026-10-13"] = {"gaps": [], "resolved": [{"base_key": "k"}], "open_keys": [], "check_failed": []}
    ds.gapcheck_due(t0 + timedelta(hours=25), tmp_path, check=chk)
    assert calls == ["2026-10-13", "2026-10-13"]                                   # 24 小时后复查
    assert json.loads((tmp_path / ".gapcheck_queue.json").read_text()) == {}         # 已补齐 → 出队
    days["v"] = [("2026-10-14", True)]
    res["2026-10-14"] = {"gaps": [], "resolved": [], "open_keys": [], "check_failed": ["check_failed：KeyError"]}
    assert ds.gapcheck_due(t0 + timedelta(days=1, hours=1), tmp_path, check=chk) == 1   # 部分检查失败（机器字段）→ 入队
    res["2026-10-14"] = "boom"
    assert ds.gapcheck_due(t0 + timedelta(days=1, hours=2), tmp_path, check=chk) == 1   # 失败下一次唤醒就重试
    assert calls[-2:] == ["2026-10-14", "2026-10-14"]
    q = json.loads((tmp_path / ".gapcheck_queue.json").read_text())
    assert q["2026-10-14"]["why"] == "check_failed" and q["2026-10-14"]["attempts"] == 2


def test_gapcheck_late_fill_appends_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "sym_to_key", lambda: {"GLD": "gold"})
    logs, gaps = tmp_path / "logs", tmp_path / "gaps"; logs.mkdir()
    state = {"g": [{"dataset": "shadow:v", "instrument": "gold", "missing_stage": "capture", "reason": "no_row"}]}
    monkeypatch.setattr(ds, "_ledger_gaps", lambda day, root, registry=None: state["g"])
    _snap(tmp_path, "GLD")
    kw = dict(root=tmp_path, logs=logs, gaps_dir=gaps, syms=["GLD"], holiday=False)
    assert len(ds.gapcheck(DAY, **kw)["gaps"]) == 1
    state["g"] = []
    r = ds.gapcheck(DAY, **kw)
    assert r["gaps"] == [] and r["resolved"][0]["resolves"] == f"{DAY}|shadow:v|gold"
    rows = [json.loads(x) for x in (gaps / "2026-10.jsonl").read_text().splitlines()]
    assert [x["status"] for x in rows] == ["open", "resolved"]                 # 原记录保留、追加 resolved
    ds.gapcheck(DAY, **kw)
    assert len((gaps / "2026-10.jsonl").read_text().splitlines()) == 2       # 不重复追加
    state["g"] = [{"dataset": "shadow:v", "instrument": "gold", "missing_stage": "capture", "reason": "no_row"}]
    r = ds.gapcheck(DAY, **kw)                                                # 补齐后又缺 → reopened（不被永久 key 去重吞掉）
    assert r["reopened"] and r["reopened"][0]["status"] == "reopened"
    state["g"] = [{"dataset": "shadow", "instrument": "*", "missing_stage": "check", "check_failed": True, "reason": "check_failed：X"}]
    r = ds.gapcheck(DAY, **kw)                                                # 检查失败：不记数据缺口、也不判已补齐
    assert r["check_failed"] and not r["resolved"] and not r["gaps"]


def test_ledger_gaps_registry_reports_missing_directory(tmp_path):
    """Codex 034 §3.3 探针：整个方向台账目录不存在 → 注册表里每个品种都是缺口（不是 0 条）。"""
    reg = [("dirledger:x", "data/history/direction_ledger/x/prospective", ("gold", "silver"))]
    g = [x for x in ds._ledger_gaps(DAY, tmp_path, registry=reg) if x["dataset"] == "dirledger:x"]
    assert [x["instrument"] for x in g] == ["gold", "silver"] and "不存在" in g[0]["reason"]
    d = tmp_path / "data/history/direction_ledger/x/prospective"; d.mkdir(parents=True)
    (d / "gold.jsonl").write_text(json.dumps({"session": DAY, "before_open": False, "identity_ok": True}) + "\n")
    (d / "silver.jsonl").write_text(json.dumps({"session": DAY, "before_open": True, "identity_ok": False,
                                                "identity_problems": ["curr_file_missing"]}) + "\n")
    g = {x["instrument"]: x["reason"] for x in ds._ledger_gaps(DAY, tmp_path, registry=reg) if x["dataset"] == "dirledger:x"}
    assert g["gold"].startswith("late_row") and g["silver"].startswith("identity_problem")


def test_record_stage_judged_by_rows_not_exit_code(tmp_path):
    """记录类阶段：当日所有注册品种都有行才算完成（冻结的 dirledger_cli 把「已冻结冲突」也报 rc=1，不能看退出码）。"""
    d = tmp_path / "led"; d.mkdir()
    (d / "gold.jsonl").write_text(json.dumps({"session": DAY}) + "\n")
    assert ds.record_missing("x", DAY, registry=(d, ("gold", "silver"))) == ["silver"]
    (d / "silver.jsonl").write_text(json.dumps({"session": "2026-10-02"}) + "\n" + json.dumps({"session": DAY}) + "\n")
    assert ds.record_missing("x", DAY, registry=(d, ("gold", "silver"))) == []
    src = (ROOT / "scripts/daily_update.sh").read_text("utf-8")
    for st in ("shadow_capture", "dirledger_record", "conviction_record", "expiry_profile"):
        assert f'stage_mark {st} "$(rec_status {st})"' in src


def test_record_status_separates_terminal_from_eligible(tmp_path):
    """Codex 036 §4 探针：before_open=False / identity_ok=False 的行不能报合格完成；缺失、损坏、冲突分开。"""
    d = tmp_path / "led"; d.mkdir()
    (d / "a.jsonl").write_text(json.dumps({"session": DAY, "before_open": True, "identity_ok": True}) + "\n")
    (d / "b.jsonl").write_text(json.dumps({"session": DAY, "before_open": False, "identity_ok": False}) + "\n")
    (d / "c.jsonl").write_text(json.dumps({"session": DAY, "before_open": True, "identity_ok": None}) + "\n")
    (d / "e.jsonl").write_text("{坏\n")
    (d / "f.jsonl").write_text(json.dumps({"session": DAY, "x": 1}) + "\n" + json.dumps({"session": DAY, "x": 2}) + "\n")
    st = ds.record_status("dirledger_record", DAY, registry=(d, ("a", "b", "c", "g", "e", "f")))
    assert {k: v["state"] for k, v in st.items()} == {
        "a": "completed_eligible", "b": "completed_ineligible", "c": "completed_ineligible",
        "g": "missing", "e": "corrupt", "f": "identity_conflict"}
