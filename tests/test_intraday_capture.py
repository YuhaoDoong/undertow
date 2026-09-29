"""当天逐分钟采集（用户 2026-09-28「记住数据最重要」；Codex 024 修订）：
质量标签、终态规则、整计划状态、零计划区分、按所需字段判覆盖、文件锁、hook 只认结构化状态。"""
import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from undertow.collect import longbridge_bars as lbb

D = date(2026, 9, 28)                       # 夏令时：常规时段 13:30Z–20:00Z，390 分钟
ROOT = Path(__file__).resolve().parents[1]


def _rows(n=390, start="2026-09-28T13:30:00", vol0=False):
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    return [[(t0 + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "1.0", "0" if vol0 else "3", "300", "0"]
            for i in range(n)]


# —— 质量标签 ——
def test_quality_labels():
    assert lbb.intraday_quality(_rows(), D)["label"] == "full_session"
    assert lbb.intraday_quality(_rows(vol0=True), D)["label"] == "full_session"      # 整天无成交也算覆盖完整
    assert lbb.intraday_quality(_rows(), D)["traded_minutes"] == 390
    assert lbb.intraday_quality(_rows(76), D)["label"] == "partial_session"          # 盘中片段
    assert lbb.intraday_quality(_rows(380), D)["label"] == "partial_session"         # 收盘前截断
    assert lbb.intraday_quality([], D)["label"] == "empty"
    dup = _rows(390); dup[5] = dup[4]
    assert lbb.intraday_quality(dup, D)["label"] == "invalid"
    bad = _rows(390); bad[3][1] = "x"
    assert lbb.intraday_quality(bad, D)["label"] == "invalid"


def test_fetch_parses_checks_date_and_labels():
    raw = [{"time": r[0], "price": r[1], "volume": r[2], "turnover": r[3], "avg_price": r[4]} for r in _rows()]
    r = lbb.fetch_intraday_today("X.US", D, runner=lambda a: ("ok", raw))
    assert r["status"] == "ok" and r["quality"]["label"] == "full_session"
    with pytest.raises(lbb.BarsUnavailable):
        lbb.fetch_intraday_today("X.US", date(2026, 9, 29), runner=lambda a: ("ok", raw))
    assert lbb.fetch_intraday_today("X.US", D, runner=lambda a: ("ok", []))["status"] == "empty"


# —— 终态规则与尝试留痕 ——
def _res(status, rows=None, at="2026-09-28T20:10:00+00:00"):
    r = {"status": status, "fetched_at": at}
    if status in ("ok", "empty"):
        r["rows"] = rows or []
        r["quality"] = lbb.intraday_quality(r["rows"], D)
    return r


def test_terminal_rules_and_no_downgrade():
    v = None
    for _ in range(lbb.EMPTY_TERMINAL - 1):
        v = lbb.merge_attempt(v, _res("empty"))
        assert v["state"] == "pending"
    v = lbb.merge_attempt(v, _res("empty"))
    assert v["state"] == "empty_confirmed" and len(v["attempts"]) == lbb.EMPTY_TERMINAL
    g = lbb.merge_attempt(lbb.merge_attempt(None, _res("not_found")), _res("not_found"))
    assert g["state"] == "gone_confirmed"
    full = lbb.merge_attempt(None, _res("ok", _rows()))
    worse = lbb.merge_attempt(full, _res("ok", _rows(10)))
    assert worse["state"] == "complete" and len(worse["rows"]) == 390 and len(worse["attempts"]) == 2


# —— 按所需字段判覆盖（Codex 024-2 反例）——
def test_covered_requires_full_session_and_close_only(tmp_path):
    cur = lbb.new_intraday_day("GLD", D)
    cur["contracts"] = {"EMPTY_OK.US": {"status": "ok", "rows": []},                     # 旧逻辑误判覆盖
                        "MORNING.US": {"status": "ok", "rows": _rows(1), "quality": lbb.intraday_quality(_rows(1), D)},
                        "FULL.US": lbb.merge_attempt(None, _res("ok", _rows()))}
    lbb.save_day(lbb.path_of("GLD", D, tmp_path), cur)
    assert lbb.intraday_covered("GLD", D, tmp_path) == {"FULL.US"}
    assert lbb.intraday_covered("GLD", D, tmp_path, need="ohlc") == set()                # 分钟收盘不能替代 OHLC


# —— 命令：整计划状态、零计划区分、文件锁 ——
def _env(tmp_path, monkeypatch, hm, fetch, plan=None, today_rows=True):
    from undertow import shadow_cli as sc

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 28, *hm, tzinfo=sc.ET)
    monkeypatch.setattr(sc, "datetime", FakeDT)
    monkeypatch.setattr(sc, "market_today", lambda: D)
    monkeypatch.setattr(lbb, "INTRADAY_DIR", tmp_path / "intra")
    monkeypatch.setattr(sc, "INTRADAY_LOCK", tmp_path / ".intraday.flock")
    led = tmp_path / "led"; led.mkdir(exist_ok=True)
    if today_rows:
        (led / "gold.jsonl").write_text(json.dumps({"key": "gold|2026-09-28", "session": "2026-09-28", "legs": []}) + "\n")
    monkeypatch.setattr(sc, "_vdir", lambda replay: led if not replay else tmp_path / "none")
    monkeypatch.setattr(sc, "bars_plan", lambda rows, last_day: plan if plan is not None
                        else {("GLD", D): {"GLD.US", "OPT.US"}})
    monkeypatch.setattr(sc, "INTRADAY_PACE_S", 0)
    calls = []
    monkeypatch.setattr(lbb, "fetch_intraday_today", lambda s, d, runner=None: calls.append(s) or fetch(s))

    class A:
        force = False
        status_file = str(tmp_path / "st.json")
    return sc, A, calls, (lambda: json.loads((tmp_path / "st.json").read_text()))


def test_empty_is_not_complete_until_terminal(tmp_path, monkeypatch):
    fetch = lambda s: _res("ok", _rows()) if s == "GLD.US" else _res("empty")
    sc, A, calls, st = _env(tmp_path, monkeypatch, (16, 30), fetch)
    assert sc.cmd_intraday(A()) == 1 and st()["overall"] == "partial"                   # 以前：rc=0、complete
    assert st()["counts"]["complete"] == 1 and st()["counts"]["pending"] == 1
    calls.clear()
    assert sc.cmd_intraday(A()) == 1 and calls == ["OPT.US"]                             # 已完成的不重抓
    assert sc.cmd_intraday(A()) == 0 and st()["overall"] == "complete"                   # 第 3 次 empty → 确认空
    assert st()["counts"]["empty_confirmed"] == 1
    calls.clear()
    assert sc.cmd_intraday(A()) == 0 and calls == [] and st()["counts"]["requested_now"] == 0   # 全部完成、无新增


def test_not_found_terminal_and_partial_mix(tmp_path, monkeypatch):
    fetch = lambda s: _res("ok", _rows(50)) if s == "GLD.US" else _res("not_found")
    sc, A, calls, st = _env(tmp_path, monkeypatch, (16, 30), fetch)
    sc.cmd_intraday(A()); sc.cmd_intraday(A())
    c = st()["counts"]
    assert st()["overall"] == "partial" and c["gone_confirmed"] == 1 and c["pending"] == 1   # 截断片段仍待续


def test_zero_plan_distinguishes_unavailable_and_no_candidates(tmp_path, monkeypatch):
    sc, A, calls, st = _env(tmp_path, monkeypatch, (16, 30), lambda s: _res("empty"), plan={}, today_rows=False)
    assert sc.cmd_intraday(A()) == 1 and st()["overall"] == "plan_unavailable"
    sc, A, calls, st = _env(tmp_path, monkeypatch, (16, 30), lambda s: _res("empty"), plan={}, today_rows=True)
    assert sc.cmd_intraday(A()) == 0 and st()["overall"] == "no_candidates"


def test_before_close_does_nothing_and_forced_partial_is_refetched(tmp_path, monkeypatch):
    sc, A, calls, st = _env(tmp_path, monkeypatch, (11, 0), lambda s: _res("ok", _rows(90)))
    assert sc.cmd_intraday(A()) == 0 and calls == []
    a = A(); a.force = True
    assert sc.cmd_intraday(a) == 1 and len(calls) == 2                                   # 盘中片段 → partial
    sc2, A2, calls2, st2 = _env(tmp_path, monkeypatch, (16, 30), lambda s: _res("ok", _rows()))
    assert sc2.cmd_intraday(A2()) == 0 and len(calls2) == 2                              # 收盘后重抓成全时段


def test_flock_busy_then_released_after_holder_dies(tmp_path, monkeypatch):
    sc, A, calls, st = _env(tmp_path, monkeypatch, (16, 30), lambda s: _res("ok", _rows()))
    holder = subprocess.Popen(["python3", "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a+'); "
                               "fcntl.flock(f.fileno(), fcntl.LOCK_EX); print('locked', flush=True); time.sleep(60)",
                               str(tmp_path / ".intraday.flock")], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    try:
        assert sc.cmd_intraday(A()) == 4 and calls == [] and st()["overall"] == "busy"
    finally:
        holder.kill(); holder.wait()                                                     # 模拟持锁进程被杀
    assert sc.cmd_intraday(A()) == 0 and len(calls) == 2                                 # 内核已释放，下一次恢复


# —— hook：只认结构化状态 ——
def _hook_funcs():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    out = []
    for name in ("clip", "intraday_capture"):
        m = re.search(rf"^{name}\(\) {{.*?^}}\n", src, re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


@pytest.mark.parametrize("overall,rc,ok_expected", [("complete", 0, True), ("no_candidates", 0, True),
                                                   ("partial", 1, False), ("complete", 1, True),
                                                   ("partial", 0, False), (None, 1, False)])
def test_hook_writes_sentinel_only_on_structured_terminal_status(tmp_path, overall, rc, ok_expected):
    fake = tmp_path / "fakepy"
    status_line = ("" if overall is None else
                   f"json.dump({{'overall': '{overall}'}}, open(p, 'w'))")
    fake.write_text(f"""#!/bin/zsh
if [[ "$1" == "-m" ]]; then
  for i in "$@"; do if [[ "$prev" == "--status-file" ]]; then p="$i"; fi; prev="$i"; done
  python3 -c "import json,sys; p=sys.argv[1]; {status_line}" "$p"
  echo "当天逐分钟 2026-09-28：测试"
  exit {rc}
fi
exec python3 "$@"
""")
    fake.chmod(0o755)
    script = _hook_funcs() + f"""
hb() {{ print -r -- "$1" >> "{tmp_path}/hb.log"; }}
notify() {{ :; }}
LOG_DIR="{tmp_path}"; ET_DATE=2026-09-28; PY="{fake}"
intraday_capture
"""
    subprocess.run(["zsh", "-c", script], check=True)
    assert (tmp_path / ".intraday_2026-09-28.ok").exists() is ok_expected


def test_session_hook_runs_intraday_after_close():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert src.index("intraday_capture() {") < src.index("then intraday_capture; fi")
    assert "(( ET_MIN >= 965 )); then intraday_capture; fi" in src and "shadow intraday" in src
    assert "lock_intraday" not in src                                                    # 旧 mkdir 锁已移除


def test_session_hooks_do_not_cut_bytes():
    """macOS `cut -c` 按字节截，会把汉字切成非法 UTF-8 → 整个日志被 grep 当二进制（2026-09-28 实测）。"""
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert "| cut -c" not in src and "clip() {" in src and src.index("clip() {") < src.index("| clip ")
