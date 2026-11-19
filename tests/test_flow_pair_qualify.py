"""P0（Codex 071）快照资格表的合成夹具：同日重抓/周末未结算、收盘当晚抓、盘中抓、缺日、旧链、坏文件、A5 覆盖。
全部合成，不读真实快照；analyze_flow / snapshot_from_payload / settled_oi_change 打桩。"""
import gzip
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import flow_pair_qualify as q  # noqa: E402
from undertow.core import market_calendar as mc  # noqa: E402

ET = ZoneInfo("America/New_York")


def ts(d, h, m=0):
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET).timestamp()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "snapshot_from_payload", lambda p, k, s: p)
    monkeypatch.setattr(q, "settled_oi_change", lambda a, b: abs(a["oi"] - b["oi"]))
    monkeypatch.setattr(q, "analyze_flow", lambda a, b, **kw: SimpleNamespace(
        upside_pressure=float(b["up"]), downside_pressure=float(b["dn"]),
        call=SimpleNamespace(abstain=False, direction="偏多" if b["up"] > b["dn"] else "偏空")))
    d = tmp_path / "XYZ"
    d.mkdir()

    def put(fd, cap, oi, up=0, dn=0, raw=None):
        body = raw if raw is not None else gzip.compress(json.dumps(
            {"payload": {"oi": oi, "up": up, "dn": dn}, "captured_at": cap}).encode())
        (d / f"{fd}.json.gz").write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    tdays = mc.trading_days(date(2026, 11, 2), date(2026, 11, 30))
    return SimpleNamespace(put=put, dir=tmp_path, tdays=tdays)


def run(env, stale=frozenset()):
    rows, snaps = q.qualify_files("XYZ", date(2026, 11, 30), env.tdays, set(stale), snap_dir=env.dir)
    return {r["file_date"]: r for r in rows}, q.session_pairs(rows, snaps, env.tdays), q.file_pairs(rows)


def test_adjacent_weekend_recapture_and_monday(env):
    env.put("2026-11-12", ts("2026-11-12", 5), 100)
    env.put("2026-11-13", ts("2026-11-13", 5), 105, up=3, dn=1)
    env.put("2026-11-14", ts("2026-11-14", 6), 105)                 # 周六：周五结算未到，OI 不变
    env.put("2026-11-16", ts("2026-11-16", 5), 120, up=1, dn=4)
    f, sp, fp = run(env)
    assert f["2026-11-12"]["oi_inference"] == "first_file_assumed_latest_possible"
    assert (f["2026-11-13"]["oi_inference"], f["2026-11-13"]["oi_session"]) == ("new_settlement_adjacent", "2026-11-12")
    assert (f["2026-11-14"]["oi_inference"], f["2026-11-14"]["oi_session"]) == ("same_oi_as_prev", "2026-11-12")
    assert f["2026-11-14"]["quote_session"] == "2026-11-13"            # 报价比 OI 新一天
    assert (f["2026-11-16"]["oi_inference"], f["2026-11-16"]["oi_session"]) == ("new_settlement_adjacent", "2026-11-13")
    by_x = {r["X"]: r for r in sp}
    assert by_x["2026-11-12"]["reason"] == "session_uncertain"        # 依赖首份假定
    good = by_x["2026-11-13"]
    assert good["status"] == "eligible" and good["prev_file"] == "2026-11-13" and good["curr_file"] == "2026-11-16"
    assert good["net"] == -3                                           # 代表文件选报价与 OI 同日的 11/13，不选周六那份
    adj = {(r["prev_file"], r["curr_file"]): r["filename_adjacent"] for r in fp}
    assert adj[("2026-11-12", "2026-11-13")] is True                   # 1119 旧脚本把这个标签算反了
    assert adj[("2026-11-14", "2026-11-16")] is False


def test_evening_and_intraday_capture_bound_by_calendar_day(env):
    env.put("2026-11-12", ts("2026-11-12", 21), 100)                   # 收盘当晚：结算未落地
    env.put("2026-11-13", ts("2026-11-13", 11), 110)                   # 盘中抓
    f, _, _ = run(env)
    assert f["2026-11-12"]["oi_session_max_possible"] == "2026-11-11"
    assert f["2026-11-12"]["quote_session"] == "2026-11-12"
    assert f["2026-11-13"]["decision_session"] is None and f["2026-11-13"]["quote_session"] is None
    assert (f["2026-11-13"]["oi_inference"], f["2026-11-13"]["oi_session"]) == ("new_settlement_adjacent", "2026-11-12")


def test_missing_session_is_span_uncertain_not_one_day(env):
    env.put("2026-11-10", ts("2026-11-10", 5), 100)
    env.put("2026-11-11", ts("2026-11-11", 5), 110)
    env.put("2026-11-13", ts("2026-11-13", 5), 130, up=9)              # 11/12 文件缺：一次变化可能跨两个结算日
    f, sp, _ = run(env)
    assert f["2026-11-13"]["oi_inference"] == "new_settlement_span_uncertain"
    assert {r["X"]: r["reason"] for r in sp}["2026-11-12"] == "session_uncertain"
    assert all(r["net"] is None for r in sp if r["status"] != "eligible")


def test_corrupt_file_reported_and_chain_continues_stale_excluded(env):
    env.put("2026-11-10", ts("2026-11-10", 5), 100)
    env.put("2026-11-11", ts("2026-11-11", 5), 110)
    bad = env.put("2026-11-12", ts("2026-11-12", 5), 120)
    env.put("2026-11-13", ts("2026-11-13", 5), 125)
    env.put("2026-11-14", None, 0, raw=b"not gzip")                   # 周末坏文件：不占结算日
    env.put("2026-11-16", ts("2026-11-16", 5), 130)
    f, sp, _ = run(env, stale={bad})
    assert f["2026-11-14"]["load_error"] and f["2026-11-14"]["oi_inference"] == "load_error"
    assert f["2026-11-16"]["compared_with"] == "2026-11-13"            # 跳过坏文件比较，不断链
    assert f["2026-11-16"]["oi_inference"] == "new_settlement_adjacent"
    reasons = {r["X"]: r["reason"] for r in sp}
    assert reasons["2026-11-11"] == "stale_chain" and reasons["2026-11-12"] == "stale_chain"
    assert reasons["2026-11-13"] is None


def test_corrupt_trading_day_file_counts_as_missing(env):
    env.put("2026-11-10", ts("2026-11-10", 5), 100)
    env.put("2026-11-11", ts("2026-11-11", 5), 110)
    env.put("2026-11-12", None, 0, raw=b"not gzip")
    env.put("2026-11-13", ts("2026-11-13", 5), 130)
    f, _, _ = run(env)
    assert f["2026-11-13"]["oi_inference"] == "new_settlement_span_uncertain"   # 坏文件 = 缺一天，不把两日变化当一日


def test_a5_needs_five_eligible_sessions_and_flip_baseline():
    td = mc.trading_days(date(2026, 11, 2), date(2026, 11, 30))
    xs = [d.isoformat() for d in td[:7]]
    sp = [{"X": x, "status": "eligible", "net": n} for x, n in zip(xs, [1, -1, 2, -2, 3, -3, 4])]
    sp[5] = {"X": xs[5], "status": "ineligible", "net": None}
    st, a5 = q.stats(sp, td)
    got = {r["X"]: (r["coverage"], r["A5"]) for r in a5}
    assert got[xs[4]] == ("5/5", 3) and got[xs[3]] == ("4/5", None)    # warmup 不出值
    assert got[xs[6]] == ("4/5", None)                                 # 窗口内缺一天不出值
    assert st["independent_flip_baseline_2p(1-p)"] == round(2 * (4 / 6) * (2 / 6), 3)
    assert st["adjacent_eligible_pairs"] == 4                          # 缺日两侧不算相邻
