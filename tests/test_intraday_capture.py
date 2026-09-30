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
    q = lbb.intraday_quality(_rows(380), D)
    assert q["missing_minutes"] == 10 and q["expected_minutes"] == 390 and q["outside_session"] == 0


@pytest.mark.parametrize("mutate", [
    lambda rs: [[r[0], "NaN", *r[2:]] for r in rs],                                  # Codex 025 probe：全 NaN 曾判 full_session
    lambda rs: [[r[0], r[1], "inf", *r[3:]] for r in rs[:1]] + rs[1:],
    lambda rs: [[r[0].replace("Z", ""), *r[1:]] for r in rs],                          # 无时区
    lambda rs: [[r[0].replace("Z", "-04:00"), *r[1:]] for r in rs],                    # 非 UTC
    lambda rs: [[r[0].replace(":00Z", ":30Z"), *r[1:]] for r in rs],                   # 不在整分钟
    lambda rs: [r[:4] for r in rs],                                                    # 字段数不符
    lambda rs: [[r[0], "0", *r[2:]] for r in rs],                                      # 价格非正
])
def test_quality_rejects_nonfinite_tz_grid_and_shape(mutate):
    assert lbb.intraday_quality(mutate(_rows()), D)["label"] == "invalid"


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


def _at(k):
    return (datetime(2026, 9, 28, 20, 10, tzinfo=timezone.utc) + timedelta(minutes=5 * k)).isoformat()


def test_terminal_rules_and_no_downgrade():
    v = None
    for k in range(lbb.EMPTY_TERMINAL - 1):
        v = lbb.merge_attempt(v, _res("empty", at=_at(k)))
        assert v["state"] == "pending"
    v = lbb.merge_attempt(v, _res("empty", at=_at(lbb.EMPTY_TERMINAL)))
    assert v["state"] == "empty_confirmed" and len(v["attempts"]) == lbb.EMPTY_TERMINAL
    g = lbb.merge_attempt(lbb.merge_attempt(None, _res("not_found", at=_at(0))), _res("not_found", at=_at(1)))
    assert g["state"] == "gone_confirmed"


def test_rapid_retries_do_not_reach_terminal():
    """Codex 025-4：注释说间隔至少一次唤醒，旧代码只数次数 → 人工连按三次立刻 empty_confirmed。"""
    v = None
    for _ in range(5):
        v = lbb.merge_attempt(v, _res("empty"))                                       # 同一时刻 ×5
    assert v["state"] == "pending" and len(v["attempts"]) == 5
    g = lbb.merge_attempt(lbb.merge_attempt(None, _res("not_found")), _res("not_found"))
    assert g["state"] == "pending"
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
    calls, tick = [], [0]

    def spaced(s, d, runner=None):                   # 模拟每次唤醒相隔 5 分钟（终态计数要求间隔，见 025-4）
        calls.append(s)
        tick[0] += 1
        return {**fetch(s), "fetched_at": _at(tick[0])}
    monkeypatch.setattr(lbb, "fetch_intraday_today", spaced)

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
    for name in ("clip", "bound_overall", "run_bound", "ov_text", "intraday_capture", "shadow_window", "shadow_sample"):
        m = re.search(rf"^{name}\(\) {{.*?^}}\n", src, re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "\n".join(out)


@pytest.mark.parametrize("overall,rc,ok_expected,rid_mode", [
    ("complete", 0, True, "same"), ("no_candidates", 0, True, "same"),
    ("partial", 1, False, "same"), ("complete", 1, False, "same"),                    # 025：complete 但 rc≠0 → 不写哨兵
    ("partial", 0, False, "same"), (None, 1, False, "same"),
    ("complete", 0, False, "other"),                                                 # 状态不属于本次运行
    ("complete", 0, False, "session"),                                               # 状态是别的交易日
])
def test_hook_writes_sentinel_only_on_structured_terminal_status(tmp_path, overall, rc, ok_expected, rid_mode):
    fake = tmp_path / "fakepy"
    rid = {"same": "rid", "other": "'stale'", "session": "rid"}[rid_mode]
    sess = "2026-09-27" if rid_mode == "session" else "2026-09-28"
    status_line = ("" if overall is None else
                   f"json.dump({{'schema': 2, 'command': 'shadow intraday', 'run_id': {rid}, 'session': '{sess}', "
                   f"'overall': '{overall}'}}, open(p, 'w'))")
    fake.write_text(f"""#!/bin/zsh
if [[ "$1" == "-m" ]]; then
  for i in "$@"; do
    if [[ "$prev" == "--status-file" ]]; then p="$i"; fi
    if [[ "$prev" == "--run-id" ]]; then r="$i"; fi
    prev="$i"
  done
  python3 -c "import json,sys; p=sys.argv[1]; rid=sys.argv[2]; {status_line}" "$p" "$r"
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


def test_hook_stale_complete_then_crash_writes_no_sentinel(tmp_path):
    """Codex 025-4：旧的 complete 状态文件还在，新进程写状态前崩溃 → 不能写成功哨兵。"""
    (tmp_path / ".status_intraday_2026-09-28.json").write_text(json.dumps(
        {"schema": 2, "command": "shadow intraday", "run_id": None, "session": "2026-09-28", "overall": "complete"}))
    fake = tmp_path / "fakepy"
    fake.write_text("""#!/bin/zsh
if [[ "$1" == "-m" ]]; then echo "Traceback: boom" >&2; exit 1; fi
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
    assert not (tmp_path / ".intraday_2026-09-28.ok").exists()
    assert "状态文件缺失" in (tmp_path / "hb.log").read_text("utf-8")


def test_session_hook_runs_intraday_after_close():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert src.index("intraday_capture() {") < src.index("then intraday_capture; flowside_capture; fi")
    assert "(( ET_MIN >= 965 )); then intraday_capture; flowside_capture; fi" in src and "shadow intraday" in src
    assert "lock_intraday" not in src                                                    # 旧 mkdir 锁已移除


def test_session_hooks_do_not_cut_bytes():
    """macOS `cut -c` 按字节截，会把汉字切成非法 UTF-8 → 整个日志被 grep 当二进制（2026-09-28 实测）。"""
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert "| cut -c" not in src and "clip() {" in src and src.index("clip() {") < src.index("| clip ")


# —— Codex 024-4：缺口台账逐项、按可恢复性排序 ——
def test_gap_ledger_fields_and_priority(tmp_path, monkeypatch):
    from undertow import shadow_cli as sc
    assert sc._expiry_of("GLD260916C415000.US") == date(2026, 9, 16) and sc._expiry_of("GLD.US") is None
    monkeypatch.setattr(lbb, "ROOT_DIR", tmp_path / "bars")
    monkeypatch.setattr(lbb, "INTRADAY_DIR", tmp_path / "intra")
    orig_path_of = lbb.path_of
    monkeypatch.setattr(lbb, "path_of", lambda root, day, base=None: orig_path_of(root, day, base or tmp_path / "bars"))
    d1, d2 = date(2026, 9, 25), date(2026, 9, 28)
    bars = lbb.new_day("GLD", d1)
    bars["contracts"] = {"GLD260926P380000.US": {"status": "not_found", "error": "e", "fetched_at": "t"},
                         "GLD260930P370000.US": {"status": "ok", "bars": []}}
    lbb.save_day(lbb.path_of("GLD", d1), bars)
    intra = lbb.new_intraday_day("GLD", d2)
    intra["contracts"] = {"GLD261002C400000.US": lbb.merge_attempt(None, _res("ok", _rows()))}
    lbb.save_day(lbb.path_of("GLD", d2, tmp_path / "intra"), intra)
    plan = {("GLD", d1): {"GLD260926P380000.US", "GLD260930P370000.US", "GLD261009P360000.US"},
            ("GLD", d2): {"GLD261002C400000.US", "GLD260929P375000.US"}}
    gaps = sc.bars_gap_ledger(plan, today=d2)
    order = [g["symbol"] for g in gaps]
    assert "GLD260930P370000.US" not in order                                     # 已补到的不算缺口
    assert order == ["GLD260929P375000.US", "GLD261009P360000.US",                # 无替代、从未请求，按到期
                     "GLD261002C400000.US",                                        # 已有逐分钟收盘，只缺 OHLC
                     "GLD260926P380000.US"]                                        # 请求过、查不到
    g0 = gaps[0]
    assert g0["expiry"] == "2026-09-29" and g0["fields_missing"] == "ohlc+close" and g0["priority"] == 1
    assert gaps[2]["fields_missing"] == "ohlc" and gaps[3]["last_status"] == "not_found"


# —— Codex 024-5：成交价代理对照的配对规则 ——
def test_trade_proxy_window_volume_weighting_and_pairing():
    import sys
    sys.path.insert(0, str(ROOT))
    from scripts import trade_vs_quote as tq
    rows = [["2026-09-28T13:59:00Z", "9.0", "100", "0", "0"],       # 09:59 窗口外
            ["2026-09-28T14:00:00Z", "1.0", "2", "0", "0"],         # 10:00 窗口内
            ["2026-09-28T14:05:00Z", "2.0", "0", "0", "0"],         # 无成交分钟不计
            ["2026-09-28T14:10:00Z", "4.0", "6", "0", "0"],
            ["2026-09-28T14:20:00Z", "8.0", "5", "0", "0"]]         # 10:20 起点不含
    px, mins = tq.leg_proxy(rows, D)
    assert px == pytest.approx((1 * 2 + 4 * 6) / 8) and len(mins) == 2
    a = {datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)}
    b = {datetime(2026, 9, 28, 14, 1, tzinfo=timezone.utc)}
    assert tq.classify(a, a) == "has_overlap" and tq.classify(a, b) == "both_async"
    assert tq.classify(a, set()) == "one_leg" and tq.classify(set(), set()) == "none"


def test_trade_proxy_sync_uses_only_common_minutes():
    """Codex 025-5 反例：卖腿主要在 10:00 成交、买腿主要在 10:19，只在 10:01 有少量重叠 ——
    窗口代理混了两个时点；同步代理只用 10:01 那一分钟。"""
    import sys
    sys.path.insert(0, str(ROOT))
    from scripts import trade_vs_quote as tq
    s = [["2026-09-28T14:00:00Z", "2.0", "100", "0", "0"], ["2026-09-28T14:01:00Z", "1.9", "1", "0", "0"]]
    b = [["2026-09-28T14:01:00Z", "0.9", "1", "0", "0"], ["2026-09-28T14:19:00Z", "0.5", "100", "0", "0"]]
    ps, ms = tq.leg_proxy(s, D)
    pb, mb = tq.leg_proxy(b, D)
    assert tq.classify(ms, mb) == "has_overlap"
    assert (ps - pb) == pytest.approx((200 + 1.9) / 101 - (0.9 + 50) / 101)           # 窗口代理：两个时点混在一起
    sp, n = tq.sync_proxy(s, b, D)
    assert n == 1 and sp == pytest.approx(1.0)                                       # 同步代理：只有 10:01
    assert tq.sync_proxy(s, [["2026-09-28T14:19:00Z", "0.5", "100", "0", "0"]], D) == (None, 0)


# —— Codex 026：影子窗口 / 全链 / 采样同样只认本次运行的状态 ——
def _fake_py(tmp_path, status_py, rc):
    fake = tmp_path / "fakepy"
    fake.write_text(f"""#!/bin/zsh
if [[ "$1" == "-m" ]]; then
  for i in "$@"; do
    if [[ "$prev" == "--status-file" ]]; then p="$i"; fi
    if [[ "$prev" == "--run-id" ]]; then r="$i"; fi
    prev="$i"
  done
  python3 -c "import json,sys; p=sys.argv[1]; rid=sys.argv[2]; {status_py}" "$p" "$r"
  echo "  2026-09-28|open：应有 4 个候选价差（每个含卖、买两个合约），有效 4 → x"
  exit {rc}
fi
exec python3 "$@"
""")
    fake.chmod(0o755)
    return fake


def _run_hook(tmp_path, fake, call):
    script = _hook_funcs() + f"""
hb() {{ print -r -- "$1" >> "{tmp_path}/hb.log"; }}
notify() {{ :; }}
LOG_DIR="{tmp_path}"; ET_DATE=2026-09-28; PY="{fake}"; ET_MIN=605
{call}
"""
    subprocess.run(["zsh", "-c", script], check=True)
    return (tmp_path / "hb.log").read_text("utf-8") if (tmp_path / "hb.log").exists() else ""


def _st(cmd, overall, rid="rid"):
    return (f"json.dump({{'schema': 2, 'command': '{cmd}', 'run_id': {rid}, 'session': '2026-09-28', "
            f"'overall': '{overall}'}}, open(p, 'w'))")


@pytest.mark.parametrize("window,cmd", [("open", "shadow quote-open"), ("chain", "shadow chain")])
@pytest.mark.parametrize("status,rc,ok", [("complete", 0, True), ("unchanged", 0, True), ("partial", 1, False),
                                          ("complete", 1, False), ("stale_rid", 0, False), ("crash", 1, False)])
def test_shadow_window_sentinel_bound_to_this_run(tmp_path, window, cmd, status, rc, ok):
    (tmp_path / f".status_shadow_{window}_2026-09-28.json").write_text(json.dumps(       # 上一轮留下的 complete
        {"schema": 2, "command": cmd, "run_id": "old", "session": "2026-09-28", "overall": "complete"}))
    py = {"stale_rid": _st(cmd, "complete", "'old'"), "crash": ""}.get(status, _st(cmd, status))
    log = _run_hook(tmp_path, _fake_py(tmp_path, py, rc), f'shadow_window {window} 600 620 "测试窗"')
    assert (tmp_path / f".shadow_{window}_2026-09-28.ok").exists() is ok
    if status == "crash":
        assert "状态文件缺失" in log
    assert not list(tmp_path.glob("*.run-*.json"))                                  # 本次状态已原子发布为固定路径


def test_shadow_window_lock_busy_then_released_lock_recovers(tmp_path):
    import fcntl
    lk = tmp_path / ".lockf_shadow_open"
    fake = _fake_py(tmp_path, _st("shadow quote-open", "complete"), 0)
    with open(lk, "a+") as f:                                                        # 另一轮正持锁 → 撞锁、不写哨兵
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        assert "撞锁" in _run_hook(tmp_path, fake, 'shadow_window open 600 620 "测试窗"')
        assert not (tmp_path / ".shadow_open_2026-09-28.ok").exists()
    # 持锁者释放（进程死亡时内核同样释放）后锁文件仍在，下一轮照常取得 —— 旧 mkdir 锁此时会永久残留
    _run_hook(tmp_path, fake, 'shadow_window open 600 620 "测试窗"')
    assert (tmp_path / ".shadow_open_2026-09-28.ok").exists() and lk.exists()


@pytest.mark.parametrize("status,rc,good", [("complete", 0, True), ("stale_rid", 0, False), ("crash", 1, False)])
def test_shadow_sample_reports_only_this_run(tmp_path, status, rc, good):
    py = {"stale_rid": _st("shadow sample", "complete", "'old'"), "crash": ""}.get(status, _st("shadow sample", status))
    log = _run_hook(tmp_path, _fake_py(tmp_path, py, rc), "shadow_sample 585 779")
    assert ("✅" in log) is good and ("⏳" in log) is (not good)



def test_zero_price_before_first_trade_is_valid():
    """长桥在当天首笔成交之前返回 price=0、volume=0：属正常「尚未成交」，不能判 invalid（2026-09-29 首版误判）。"""
    rows = _rows()
    for r in rows[:30]:
        r[1], r[2], r[3] = "0", "0", "0"
    q = lbb.intraday_quality(rows, D)
    assert q["label"] == "full_session" and q["traded_minutes"] == 360
    rows[40][1] = "0"                                                           # 有成交量却价格为 0 → 异常
    assert lbb.intraday_quality(rows, D)["label"] == "invalid"


def test_flowside_capture_wired_after_intraday_and_defined_before_use():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert src.index("flowside_capture() {") < src.index("intraday_capture; flowside_capture; fi")
    daily = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    assert "scripts/flow_side_check.py" in daily and "FS_RC" in daily
