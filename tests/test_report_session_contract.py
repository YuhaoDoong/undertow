"""W02（Codex A02/A10）：日报 → 候选 → 前瞻台账的时间契约。

原错：cmd_report 用快照文件名日期当可交易日；_exec_day 只在成本闸门的看涨/看跌分支赋值，
墙价差与台账却无条件使用（中性时 NameError 被吞，或沿用上一品种）；--as-of 回放会写真实前瞻账；
台账失败只写 stderr；spread_ledger.backfill 从未被任何入口调用。
"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.analyze import spread_ledger as sl                   # noqa: E402
from undertow.core.clock import ET, certify_session                # noqa: E402

TD = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]


def _ts(m, d, h, mi=0):
    return datetime(2026, m, d, h, mi, tzinfo=ET).timestamp()


def test_certify_session_three_states():
    cases = [(_ts(9, 24, 4), date(2026, 9, 24), "certified"),
             (_ts(9, 24, 18), date(2026, 9, 25), "certified"),       # 盘后 → 次一交易日
             (_ts(9, 25, 11), None, "unmappable"),                   # 盘中
             (_ts(9, 25, 18), date(2026, 9, 28), "provisional"),     # 日历边界后的盘后
             (_ts(9, 26, 10), date(2026, 9, 28), "provisional"),     # 周六
             (_ts(9, 28, 4), date(2026, 9, 28), "provisional"),      # 今天盘前，日历未覆盖
             (_ts(9, 28, 11), None, "unmappable")]
    for ts, want_s, want_st in cases:
        r = certify_session(ts, TD)
        assert (r["session"], r["status"]) == (want_s, want_st), (datetime.fromtimestamp(ts, ET), r)
    assert certify_session(None, TD)["status"] == "unmappable", "缺 captured_at 不得退回文件日期"
    print("PASS test_certify_session_three_states")


class _V:
    ok = False; reason = "无候选"; params = {}; all = []


def _rec(tmp, day, *, status, recorded_at):
    sl.record("silver", "SLV", day, 58.0, _V(), root=tmp,
              session_meta={"snapshot_file_date": day.isoformat(),
                            "captured_at": "2026-09-28T08:00:00+00:00",
                            "status": status, "source": "x"})
    p = tmp / "silver.jsonl"
    rows = [json.loads(l) for l in p.read_text("utf-8").splitlines()]
    for r in rows:
        if r["date"] == day.isoformat():
            r["recorded_at"] = recorded_at
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")


def test_prospective_status_and_certify(tmp_path):
    # 周一盘前记录（暂定）→ 日线出来后认证为 prospective
    _rec(tmp_path, date(2026, 9, 28), status="provisional", recorded_at="2026-09-28T10:00:00+00:00")  # 06:00 ET
    # 同日开盘后才记录 → late_record
    _rec(tmp_path, date(2026, 9, 29), status="provisional", recorded_at="2026-09-29T15:00:00+00:00")  # 11:00 ET
    rows = sl.load("silver", root=tmp_path)
    assert {sl.prospective_status(r) for r in rows} == {"uncertified"}, "未认证前一律不计入"
    c = sl.certify("silver", TD + [date(2026, 9, 28), date(2026, 9, 29)], root=tmp_path)
    assert c == {"certified": 2, "non_trading": 0, "still_provisional": 0}
    st = {r["date"]: sl.prospective_status(r) for r in sl.load("silver", root=tmp_path)}
    assert st == {"2026-09-28": "prospective", "2026-09-29": "late_record"}, st
    # 认证后同日重跑（输入相同、状态回到 provisional）不得判冲突
    sl.record("silver", "SLV", date(2026, 9, 28), 58.0, _V(), root=tmp_path,
              session_meta={"snapshot_file_date": "2026-09-28",
                            "captured_at": "2026-09-28T08:00:00+00:00",
                            "status": "provisional", "source": "x"})
    print("PASS test_prospective_status_and_certify")


def test_certify_marks_holiday_and_keeps_uncovered(tmp_path):
    _rec(tmp_path, date(2026, 9, 28), status="provisional", recorded_at="2026-09-28T10:00:00+00:00")
    _rec(tmp_path, date(2026, 10, 5), status="provisional", recorded_at="2026-10-05T10:00:00+00:00")
    # 日历覆盖到 9/29 但没有 9/28 → 9/28 是非交易日；10/5 超出覆盖 → 保持暂定，不猜
    c = sl.certify("silver", TD + [date(2026, 9, 29)], root=tmp_path)
    assert c == {"certified": 0, "non_trading": 1, "still_provisional": 1}, c
    print("PASS test_certify_marks_holiday_and_keeps_uncovered")


def test_legacy_rows_are_not_prospective(tmp_path):
    sl.record("silver", "SLV", date(2026, 9, 24), 58.0, _V(), root=tmp_path)   # 无 session_meta
    assert sl.prospective_status(sl.load("silver", root=tmp_path)[0]) == "legacy"
    print("PASS test_legacy_rows_are_not_prospective")


def test_cmd_report_contract_in_source():
    """行为链太长，这里锁住源码层面的契约点（仓库既有做法）。"""
    src = (ROOT / "undertow" / "cli.py").read_text("utf-8")
    i0 = src.index("def cmd_report")
    seg = src[i0:src.index("\ndef ", i0 + 10)]
    loop = seg[seg.index("for inst in instruments:"):]
    assert "_exec_day = None" in loop and "_sess_meta: dict = {}" in loop, "必须逐品种初始化"
    assert "_exec_day = (date.fromisoformat(curr_date_s)" not in seg, "成本闸门不得再私自赋值"
    assert "root=_sl.REPLAY_DIR if replay else None" in seg, "回放必须写独立命名空间"
    assert "session_meta=_sess_meta_row(" in seg, "台账必须带认证身份"
    assert '"ledger_issues": ledger_issues' in seg, "台账失败必须进状态文件"
    sh = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    assert "RPT_LEDGER" in sh and "前瞻台账未写入" in sh, "无人值守脚本必须对台账失败告警"
    assert "_spl.certify(" in src and "_spl.backfill(" in src, "价差台账回填必须有入口"
    print("PASS test_cmd_report_contract_in_source")
