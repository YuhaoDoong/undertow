"""W05/W06 前瞻配对影子账：选腿、报价、结算、冻结、统计的口径锁。"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.analyze import shadow as sh                      # noqa: E402
from undertow.analyze import wall_spread as ws                 # noqa: E402
from undertow.collect import jsonl_ledger as jl                # noqa: E402

T = date(2026, 9, 14)
EXP = date(2026, 9, 16)


class _C:
    def __init__(self, kind, strike, expiry, bid=None, ask=None, oi=0, delta=None, iv=None):
        self.kind, self.strike, self.expiry, self.bid, self.ask, self.open_interest = kind, strike, expiry, bid, ask, oi
        self.delta, self.iv = delta, iv


class _Snap:
    def __init__(self, cs): self.contracts = cs


def _snap():
    cs = []
    for k in (54.0, 55.0, 56.0, 57.0, 58.0, 59.0, 60.0, 61.0, 62.0):
        cs.append(_C("P", k, EXP, 0.10, 0.12, delta=-max(0.02, 0.5 - (58.12 - k) * 0.15), iv=0.30))
        cs.append(_C("C", k, EXP, 0.10, 0.12, delta=max(0.02, 0.5 - (k - 58.12) * 0.15), iv=0.30))
    cs.append(_C("P", 57.0, date(2026, 9, 30), 0.5, 0.6, delta=-0.40, iv=0.25))   # DTE 16：不是目标到期
    cs.append(_C("P", 58.0, date(2026, 10, 14), 0.9, 1.0, delta=-0.48, iv=0.26))  # ~30 天：期限结构远端
    return _Snap(cs)



def _row():
    return sh.build_opportunity(inst="silver", sym="SLV", snap=_snap(), session=T, spot=58.12, atr=1.0,
                                wall_fn=lambda k: {"strike": 57.0 if k == "P" else 60.0, "oi": 9000, "buf_pct": 1.9,
                                                   "n_exp": 2, "oi_by_expiry": {"2026-09-16": 9000}},
                                labels={"base_date": "2026-09-11"}, identity={"mode": "prospective", "status": "provisional"})




def _bars(closes, lows=None, highs=None):
    ds = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    return [(d, (highs or closes)[i], (lows or closes)[i], closes[i]) for i, d in enumerate(ds)]







def test_prospective_requires_certified_and_before_open():
    from undertow.shadow_cli import prospective_ok
    base = {"session": "2026-09-14", "identity": {"mode": "prospective", "status": "certified"}}
    assert prospective_ok(dict(base, recorded_at="2026-09-14T12:00:00+00:00"))          # 08:00 ET
    assert not prospective_ok(dict(base, recorded_at="2026-09-14T14:00:00+00:00"))      # 10:00 ET 迟到
    assert not prospective_ok({**base, "identity": {"mode": "prospective", "status": "provisional"},
                               "recorded_at": "2026-09-14T12:00:00+00:00"})
    assert not prospective_ok({**base, "identity": {"mode": "replay", "status": "certified"},
                               "recorded_at": "2026-09-14T12:00:00+00:00"})


def test_cli_is_read_only():
    """影子账从不下单：编排代码里不得出现任何下单/撤单子命令。"""
    src = (ROOT / "undertow" / "shadow_cli.py").read_text("utf-8")
    import re
    assert not re.search(r'\[\s*"order"', src), "不得调用 longbridge order 子命令"
    for bad in ("order buy", "order sell", "order cancel", "order replace", "submit_order", "place_order"):
        assert bad not in src, bad
    assert "fetch_depth" in src and "_run(" not in src, "盘口只经 longbridge_quote 的只读接口取"



def _settled_row(day, flow, a_p, b_p, a_c, b_c):
    def o(p):
        b = sh.CONFIG["primary_basis"]
        return {"pnl": {b: p}, "pnl_bounds": {b: [p, p]}, "pnl_residual0": {b: p}, "max_risk": {b: 100.0}}
    return {"session": day, "identity": {"mode": "prospective"}, "decision": {"flow": {"call_direction": flow}},
            "legs": [{"leg_id": "P-B1", "same_as_A": False}, {"leg_id": "C-B1", "same_as_A": False}],
            "outcome": {"P-A": o(a_p), "P-B1": o(b_p), "C-A": o(a_c), "C-B1": o(b_c)}}


def test_paired_summary_side_and_flow_subsets():
    rows = [_settled_row(f"2026-10-{i:02d}", "偏多", 10, 0, -50, -40) for i in range(1, 8)]
    both = sh.paired_summary(rows, pool=None)
    put = sh.paired_summary(rows, sides=["P"], pool=None)
    aligned = sh.paired_summary(rows, flow_aligned_only=True, pool=None)
    assert both["coverage"]["pairs"] == 14 and put["coverage"]["pairs"] == 7
    assert put["A"]["mean"] == pytest.approx(0.10) and put["AminusB"]["mean"] == pytest.approx(0.10)
    assert aligned["coverage"]["pairs"] == 7 and aligned["A"]["mean"] == pytest.approx(0.10), "偏多 → 只取 put 侧"
    assert both["identity"] == "exploratory" and both["A_verdict"].startswith("探索·")


def test_risk_budget_draft():
    assert sh.contracts_allowed(90.0, 1000.0) == 2                      # 20% × 1000 = 200 → 2 张
    assert sh.contracts_allowed(90.0, 1000.0, cluster_used=150.0) == 0  # 同簇已用 150，剩 50 < 90
    assert sh.contracts_allowed(90.0, 0.05) == 0, "当前实盘净值 $0.05：一张都不能开"
    assert sh.CLUSTERS["贵金属"] == ("gold", "silver")


def test_versioned_ledger_paths_and_report_filters_versions():
    from undertow import shadow_cli as sc
    assert str(sc._path("silver", False)).endswith(f"shadow/{sh.CONFIG['version']}/silver.jsonl")
    assert str(sc._path("silver", True)).endswith(f"shadow/{sh.CONFIG['version']}/replay/silver.jsonl")
    src = (ROOT / "undertow" / "shadow_cli.py").read_text("utf-8")
    assert 'r.get("config_version") == sh.CONFIG["version"]' in src, "不同配置版本不得混入同一主检验"



def test_pin_ranksum_sanity():
    import importlib.util
    spec = importlib.util.spec_from_file_location("s10", ROOT / "scripts" / "step10_pin_test.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    assert m.ranksum_z([0.1] * 10, [1.0] * 10) < -3
    assert abs(m.ranksum_z([0.5, 0.6, 0.7, 0.8, 0.9], [0.5, 0.6, 0.7, 0.8, 0.9])) < 1e-9
    assert m.ranksum_z([0.1] * 3, [1.0] * 10) is None




# ═══════════════════ v3（Codex 005：R01/R02/R04/R05/R06/S00）═══════════════════



def test_same_dollar_width_on_irregular_grid():
    """R04：不规则网格下「往外数两档」会给出不同美元宽度；v3 所有臂同宽，找不到就不可配对。"""
    strikes = [85.0, 87.5, 90.0, 92.5, 95.0, 97.0, 97.5, 98.0, 99.0, 100.0]
    w = sh.spread_width(strikes, 100.0)
    assert w == 1.0, w                                  # ±5% 内最常见间隔 0.5 → 宽 1.0
    assert sh._buy_leg(strikes, 99.0, "P", w) == 98.0
    assert sh._buy_leg(strikes, 97.0, "P", w) is None   # 96 未挂牌 → 不可配对，不偷偷换成 95
    assert sh.pick_a({"strike": 97.0}, strikes, 100.0, "P", w) == (None, "no_same_width_protective")


def test_build_opportunity_all_arms_same_width():
    r = _row()
    for side in ("P", "C"):
        ws_ = {l["width_usd"] for l in r["legs"] if l["side"] == side and l["status"] == "candidate"}
        assert len(ws_) <= 1, ws_
    assert "windows" in r and "entry" not in r, "v3 只存原始观测"


def _q(bid, ask, bs=10, as_=10, err=None):
    return {"bid": bid, "ask": ask, "bid_size": bs, "ask_size": as_, "error": err, "quote_time": None}


def _att(wkey, quotes, phase="rth", minute=0):
    """窗口内第 minute 分钟开始、同分钟结束的一次尝试（ET 夏令时 = UTC−4）。"""
    day, w = wkey.split("|")
    lo = sh.window_bounds(date.fromisoformat(day), w)[0] + minute
    off = "-04:00" if date.fromisoformat(day) < date(2026, 11, 1) else "-05:00"   # EDT / EST
    t = f"{day}T{lo // 60:02d}:{lo % 60:02d}:10{off}"
    return {"started_at": t, "ended_at": t, "phase": phase, "underlying": None, "quotes": quotes}


def _leg():
    return {"leg_id": "P-A", "side": "P", "rule": "A", "expiry": "2026-09-16", "sell": 57.0, "buy": 56.0,
            "width_usd": 100.0, "status": "candidate", "snapshot_credit": None}


def _vrow(windows):
    return {"session": "2026-09-14", "windows": windows}


BARS3 = [(date(2026, 9, 14), 58.2, 57.8, 58.0), (date(2026, 9, 15), 58.2, 57.8, 58.0), (date(2026, 9, 16), 58.2, 57.8, 58.0)]
ENTRY_OK = {"P|57": _q(0.30, 0.32), "P|56": _q(0.08, 0.10)}          # 保守收 20


def _full_windows(cost_quotes):
    """cost_quotes: 标记窗口 → quotes；入场窗固定 ENTRY_OK。"""
    w = {"2026-09-14|open": {"attempts": [_att("2026-09-14|open", ENTRY_OK)]}}
    for k, q in cost_quotes.items():
        w[k] = {"attempts": [_att(k, q)]}
    return w


def _marks_all(q):
    return {k: q for k in ["2026-09-14|close", "2026-09-15|open", "2026-09-15|close", "2026-09-16|open", "2026-09-16|close"]}


def test_r01_absent_marks_are_unknown_not_held():
    """R01：全程无标记 → 止损口径未知（旧版显示盈利 16.8、gap=0）。"""
    o = sh.settle_leg(_leg(), _vrow(_full_windows({})), bars=BARS3)
    assert o["pnl"]["quote_entry_expiry_intrinsic"] == pytest.approx(20 - 3.2)
    assert o["pnl"]["stop1x_twice_daily"] is None and o["status"]["stop1x_twice_daily"] == "path_unknown"
    assert o["marks"] == {"expected": 5, "valid": 0, "not_run": 5, "missing": 0, "pending": 0}


def test_r01_one_day_missing_and_missing_before_trigger():
    cheap = {"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}
    dear = {"P|57": _q(0.70, 0.75), "P|56": _q(0.05, 0.06)}         # 成本 70 ≥ 2×20
    m = _marks_all(cheap); del m["2026-09-15|open"]; del m["2026-09-15|close"]    # 单日丢任务
    o = sh.settle_leg(_leg(), _vrow(_full_windows(m)), bars=BARS3)
    assert o["pnl"]["stop1x_twice_daily"] is None and o["status"]["stop1x_twice_daily"] == "path_unknown"
    m2 = _marks_all(cheap); del m2["2026-09-14|close"]; m2["2026-09-15|close"] = dear   # 先缺后触发
    o2 = sh.settle_leg(_leg(), _vrow(_full_windows(m2)), bars=BARS3)
    assert o2["pnl"]["stop1x_twice_daily"] is None and o2["status"]["stop1x_twice_daily"] == "path_unknown_before_trigger"
    m3 = _marks_all(cheap); m3["2026-09-15|close"] = dear            # 全部有效、触发
    o3 = sh.settle_leg(_leg(), _vrow(_full_windows(m3)), bars=BARS3)
    assert o3["pnl"]["stop1x_twice_daily"] == pytest.approx(20 - 70 - 3.2)
    assert o3["status"]["stop1x_twice_daily"] == "stopped@2026-09-15|close"
    o4 = sh.settle_leg(_leg(), _vrow(_full_windows(_marks_all(cheap))), bars=BARS3)
    assert o4["status"]["stop1x_twice_daily"] == "held_all_marks_valid"
    assert o4["pnl"]["stop1x_twice_daily"] == o4["pnl"]["quote_entry_expiry_intrinsic"]


def test_r02_zero_size_and_errors_are_not_valid_entries():
    for bad, why in ((_q(0.30, 0.32, bs=0), "zero_size"), (_q(None, 0.32), "price_missing"),
                     (_q(0.30, 0.32, err="timeout"), "leg_error")):
        w = {"2026-09-14|open": {"attempts": [_att("2026-09-14|open", {"P|57": bad, "P|56": _q(0.08, 0.10)})]}}
        x = sh.window_leg(_vrow(w), _leg(), "2026-09-14|open", "entry")
        assert x["status"] == "missing" and x["reasons"] == [why]


def test_r02_retry_takes_first_valid_attempt_not_best():
    w = {"2026-09-14|open": {"attempts": [
        _att("2026-09-14|open", {"P|57": _q(0.30, 0.32, bs=0), "P|56": _q(0.08, 0.10)}, minute=1),  # 失败
        _att("2026-09-14|open", {"P|57": _q(0.25, 0.27), "P|56": _q(0.08, 0.10)}, minute=6),        # 第一次有效：收 15
        _att("2026-09-14|open", {"P|57": _q(0.40, 0.42), "P|56": _q(0.08, 0.10)}, minute=11)]}}     # 更好但不许挑
    x = sh.window_leg(_vrow(w), _leg(), "2026-09-14|open", "entry")
    assert x["status"] == "valid" and x["at"].startswith("2026-09-14T10:06") and x["credit"]["conservative"] == pytest.approx(15.0)


def test_off_hours_quotes_never_count():
    w = {"2026-09-14|open": {"attempts": [_att("2026-09-14|open", ENTRY_OK, phase="off_hours")]}}
    assert sh.window_leg(_vrow(w), _leg(), "2026-09-14|open", "entry")["reasons"] == ["off_hours"]


def test_r05_exit_cost_above_width_kept_raw():
    wide = {"P|57": _q(1.40, 1.50), "P|56": _q(0.30, 0.35)}         # 成本 120 > 宽 100
    x = sh.window_leg(_vrow({"2026-09-15|open": {"attempts": [_att("2026-09-15|open", wide)]}}), _leg(), "2026-09-15|open", "exit")
    assert x["cost"] == pytest.approx(120.0) and x["exceeds_width"] is True
    m = _marks_all({"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}); m["2026-09-15|open"] = wide
    o = sh.settle_leg(_leg(), _vrow(_full_windows(m)), bars=BARS3)
    assert o["pnl"]["stop1x_twice_daily"] == pytest.approx(20 - 120 - 3.2), "超宽平仓不得变成到期盈利"


def test_r06_pre_expiry_exit_and_immature_expiry():
    cheap = {"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}
    o = sh.settle_leg(_leg(), _vrow(_full_windows({"2026-09-15|close": cheap})), bars=BARS3)
    assert o["pnl"]["pre_expiry_exit_policy"] == pytest.approx(20 - 10 - 3.2)      # 9/15 收盘窗平仓
    o2 = sh.settle_leg(_leg(), _vrow(_full_windows({})), bars=BARS3)
    assert o2["pnl"]["pre_expiry_exit_policy"] is None and o2["status"]["pre_expiry_exit_policy"] == "exit_not_run"
    o3 = sh.settle_leg(_leg(), _vrow(_full_windows({})), bars=BARS3[:2])
    assert o3["complete"] is False and o3["bars_missing"] == ["2026-09-16"], "到期日线未出：不完整、明确列出缺哪天"
    assert o3["pnl"]["quote_entry_expiry_intrinsic"] is None and o3["status"]["quote_entry_expiry_intrinsic"] == "immature"


def test_half_day_close_window_not_run_is_unknown_not_skipped():
    """半日市收盘窗未运行：记 not_run，止损口径未知 —— 不补、不当作未触发。"""
    m = _marks_all({"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}); del m["2026-09-15|close"]
    o = sh.settle_leg(_leg(), _vrow(_full_windows(m)), bars=BARS3)
    assert o["marks"]["not_run"] == 1 and o["pnl"]["stop2x_twice_daily"] is None


def test_no_valid_entry_makes_all_quote_bases_unknown():
    o = sh.settle_leg(_leg(), _vrow({}), bars=BARS3)
    for b in ("quote_entry_expiry_intrinsic", "pre_expiry_exit_policy", "close_beyond_next_open_exit", "stop1x_twice_daily"):
        assert o["pnl"][b] is None and o["status"][b].startswith("entry_"), b


def test_jsonl_ledger_freeze_update_and_quarantine_v3(tmp_path):
    p = tmp_path / "silver.jsonl"; r = _row(); r["recorded_at"] = "2026-09-14T10:00:00+00:00"
    assert jl.insert_frozen(p, r, key_field="key", frozen=sh.frozen_part) == "inserted"
    assert jl.update(p, lambda x: x["windows"].update({"k": {"attempts": []}}) or True,
                     key_field="key", frozen=sh.frozen_part) == 1, "追加原始观测不算改事前字段"
    with pytest.raises(jl.LedgerConflictError):
        jl.update(p, lambda x: x["legs"][0].update(sell=1.0) or True, key_field="key", frozen=sh.frozen_part)


def _quote_env(tmp_path, monkeypatch, depth_fn):
    """端到端：cmd_quote 在假盘口下的状态与重试。"""
    import argparse
    from undertow import shadow_cli as sc
    from undertow.collect import longbridge_quote as lq
    monkeypatch.setattr(sc, "DIR", tmp_path)
    monkeypatch.setattr(sc, "_phase_now", lambda: "rth")
    monkeypatch.setattr(sc, "_window_now", lambda w: True)
    monkeypatch.setattr(sc, "market_today", lambda: T)
    monkeypatch.setattr(sc, "_now_iso", lambda: f"{T.isoformat()}T10:03:00-04:00")
    monkeypatch.setattr(lq, "fetch_depth", depth_fn)
    monkeypatch.setattr(lq, "fetch_stock_quotes", lambda syms: {})
    r = _row(); r["recorded_at"] = "2026-09-14T10:00:00+00:00"
    jl.insert_frozen(sc._path("silver", False), r, key_field="key", frozen=sh.frozen_part)
    st = tmp_path / "st.json"
    run = lambda: sc.cmd_quote(argparse.Namespace(instruments=["silver"], window="open",
                                                  allow_off_hours=False, status_file=str(st)))
    return run, st


class _D:
    def __init__(self, bid, ask, bs=10, as_=10, error=""):
        self.bid, self.ask, self.bid_size, self.ask_size, self.error = bid, ask, bs, as_, error


def test_quote_partial_then_retry_then_complete(tmp_path, monkeypatch):
    state = {"n": 0}

    def depth(syms):
        state["n"] += 1
        # 第一次：一半合约零挂单量（失败）；第二次：全部正常
        out = {}
        for i, s in enumerate(syms):
            # 按行权价定价，保证卖腿（更靠近价内）比买腿贵 → 保守收权金为正
            import re as _re
            kind, k = _re.search(r"\d{6}([PC])(\d+)\.US$", s).groups()
            k = int(k) / 1000
            mid = max(0.02, (k - 50) * 0.05) if kind == "P" else max(0.02, (66 - k) * 0.05)
            bs = 0 if (state["n"] == 1 and i % 2 == 0) else 10
            out[s] = _D(round(mid - 0.01, 2), round(mid + 0.01, 2), bs=bs)
        return out
    run, st = _quote_env(tmp_path, monkeypatch, depth)
    rc1 = run(); s1 = json.loads(st.read_text())
    assert rc1 == 1 and s1["overall"] in ("partial", "failed"), "部分失败不得返回成功（否则调度层写 .ok 堵死当日）"
    rc2 = run(); s2 = json.loads(st.read_text())
    assert rc2 == 0 and s2["overall"] == "complete"
    from undertow import shadow_cli as sc
    row = jl.load(sc._path("silver", False), "key")[0]
    assert len(row["windows"]["2026-09-14|open"]["attempts"]) == 2, "两次尝试都保留"


def test_quote_total_api_failure_is_failed(tmp_path, monkeypatch):
    def boom(syms):
        from undertow.collect.longbridge_quote import LiveQuotesUnavailable
        raise LiveQuotesUnavailable("全部取数失败")
    run, st = _quote_env(tmp_path, monkeypatch, boom)
    assert run() == 1 and json.loads(st.read_text())["overall"] == "failed"




def test_late_or_wrong_day_attempts_do_not_count():
    """晚到（拖过窗口末）或日期对不上的尝试不计有效 —— 否则固定窗口会悄悄变成「任何时候」。"""
    late = _att("2026-09-14|open", ENTRY_OK); late["ended_at"] = "2026-09-14T10:21:30-04:00"
    x = sh.window_leg(_vrow({"2026-09-14|open": {"attempts": [late]}}), _leg(), "2026-09-14|open", "entry")
    assert x["reasons"] == ["outside_window"]
    other_day = _att("2026-09-15|open", ENTRY_OK)
    x2 = sh.window_leg(_vrow({"2026-09-14|open": {"attempts": [other_day]}}), _leg(), "2026-09-14|open", "entry")
    assert x2["reasons"] == ["outside_window"]
    edge = _att("2026-09-14|open", ENTRY_OK, minute=20)                 # 10:20:10 仍在窗口（含结束分钟）
    assert sh.window_leg(_vrow({"2026-09-14|open": {"attempts": [edge]}}), _leg(), "2026-09-14|open", "entry")["status"] == "valid"
    utc = dict(_att("2026-09-14|open", ENTRY_OK), started_at="2026-09-14T14:05:00+00:00", ended_at="2026-09-14T14:06:00+00:00")
    assert sh.in_window("2026-09-14|open", utc["started_at"], utc["ended_at"]), "UTC 时间戳换算到 ET 后判定"
    assert not sh.in_window("2026-09-14|open", None, None)


def test_quote_missing_opportunity_row_is_failure(tmp_path, monkeypatch):
    """开盘窗时没有当日机会行 = 盘前 capture 失败，不得报 unchanged 并写 .ok。"""
    import argparse
    from undertow import shadow_cli as sc
    monkeypatch.setattr(sc, "DIR", tmp_path)
    monkeypatch.setattr(sc, "_phase_now", lambda: "rth")
    monkeypatch.setattr(sc, "_window_now", lambda w: True)
    monkeypatch.setattr(sc, "market_today", lambda: T)
    st = tmp_path / "st.json"
    rc = sc.cmd_quote(argparse.Namespace(instruments=["silver"], window="open", allow_off_hours=False, status_file=str(st)))
    s_ = json.loads(st.read_text())
    assert rc == 1 and s_["overall"] == "failed" and s_["counts"]["missing_rows"] == 1
    rc2 = sc.cmd_quote(argparse.Namespace(instruments=["silver"], window="close", allow_off_hours=False, status_file=str(st)))
    assert rc2 == 0 and json.loads(st.read_text())["overall"] == "unchanged", "收盘窗无应有任务是正常的"


def test_status_breakdown_surfaces_unknowns():
    cheap = {"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}
    m = _marks_all(cheap); del m["2026-09-15|open"]
    row = dict(_vrow(_full_windows(m)), legs=[_leg()])
    row["outcome"] = {"P-A": sh.settle_leg(_leg(), row, bars=BARS3)}
    unsettled = dict(_vrow({}), legs=[_leg()], outcome=None)
    bd = sh.status_breakdown([row, unsettled], "stop1x_twice_daily")
    assert bd["status"] == {"path_unknown": 1, "unsettled": 1}
    assert bd["marks"] == {"expected": 5, "valid": 4, "not_run": 1, "missing": 0, "pending": 0}
    snap = sh.status_breakdown([row], "snapshot_model")
    assert snap["status"] == {"credit_missing": 1}, "快照无权利金不得标成正常的 stale_snapshot_quote"


def test_settle_rederive_recomputes_outcome_keeps_raw(tmp_path, monkeypatch):
    import argparse, types
    from undertow import shadow_cli as sc
    from undertow.collect import cboe_history as ch
    monkeypatch.setattr(sc, "DIR", tmp_path)
    ser = types.SimpleNamespace(dates=[b[0] for b in BARS3], highs=[b[1] for b in BARS3],
                                lows=[b[2] for b in BARS3], closes=[b[3] for b in BARS3])
    monkeypatch.setattr(ch.CboeHistorySource, "fetch_series", lambda self, inst: ser)
    r = _row(); r["recorded_at"] = "2026-09-14T10:00:00+00:00"
    for l in r["legs"]:
        l["expiry"] = "2026-09-16"
    r["windows"] = _full_windows({})
    r["outcome"] = {l["leg_id"]: {"pnl": {"x": 999}, "complete": True}
                    for l in r["legs"] if l["status"] == "candidate"}  # 旧派生（已全部成熟）
    p = sc._path("silver", False)
    jl.insert_frozen(p, r, key_field="key", frozen=sh.frozen_part)
    ns = lambda **k: argparse.Namespace(instruments=["silver"], status_file=None, **k)
    sc.cmd_settle(ns(rederive=False))
    assert jl.load(p, "key")[0]["outcome"] == r["outcome"], "不加 --rederive 时已全部成熟的行不动"
    sc.cmd_settle(ns(rederive=True))
    new = jl.load(p, "key")[0]
    assert new["windows"] == r["windows"] and new["rederived_at"]
    assert all("quote_entry_expiry_intrinsic" in o["pnl"] for o in new["outcome"].values())


def test_marks_after_expiry_are_ignored():
    """跨到期标记：到期后窗口里的尝试（哪怕成本极高）不参与止损判断。"""
    cheap = {"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}
    dear = {"P|57": _q(0.90, 0.95), "P|56": _q(0.05, 0.06)}
    m = _marks_all(cheap); m["2026-09-17|open"] = dear
    o = sh.settle_leg(_leg(), _vrow(_full_windows(m)), bars=BARS3 + [(date(2026, 9, 17), 58.2, 57.8, 58.0)])
    assert o["status"]["stop1x_twice_daily"] == "held_all_marks_valid" and o["marks"]["expected"] == 5



# ═══════════════════ v4（Codex 006：C01–C05、三个设计决定）═══════════════════
from undertow.core import market_calendar as mc   # noqa: E402


def test_config_v5_identity():
    c = sh.CONFIG
    assert c["version"] == "shadow-v5-20260926" and c["fee_round_trip"] == ws.FEE_PER_TRADE
    assert "short_only_policy" in c and c["prospective_start"] == "2026-09-28"
    assert c["primary_endpoint"] == "pre_expiry_exit_policy" == c["primary_basis"], "决定 1：唯一主终点"
    assert "quote_entry_expiry_intrinsic" in c["secondary_endpoints"] and "pre_expiry_exit_policy" not in c["secondary_endpoints"]
    assert c["primary_comparison"] == "A vs B1" and c["b_rules"]["B3"] == {"delta": 0.20}
    assert c["stats"]["block_days"] == 5 and c["stats"]["sensitivity_block_days"] == 10 and c["stats"]["min_full_blocks"] == 4
    assert c["formal_test"]["date"] == "2026-12-31"
    assert c["calendar"]["hash"] == mc.calendar_hash()
    pools = c["pools"]
    assert sorted(k for v in pools.values() for k in v) == sorted(c["instruments"]) and len(c["instruments"]) == 15
    assert "tqqq" not in pools["etf"] and "nvda" in pools["single_stock"] and c["primary_pool"] == "etf"


def test_calendar_holidays_early_close_and_unknown():
    assert mc.is_trading_day(date(2026, 11, 26)) is False and mc.is_trading_day(date(2026, 9, 7)) is False
    assert mc.close_time(date(2026, 11, 27)) == "13:00" and mc.close_time(date(2026, 12, 24)) == "13:00"
    assert mc.close_time(date(2026, 9, 28)) == "16:00"
    assert mc.is_trading_day(date(2027, 4, 1)) is None, "覆盖外 = 未知，不猜"
    assert mc.trading_days(date(2027, 3, 30), date(2027, 4, 2)) is None
    assert mc.prev_trading_day(date(2026, 11, 30)) == date(2026, 11, 27)
    assert mc.next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)


def test_close_window_follows_core_close():
    """决定 3：收盘窗 = 核心收市前 30~15 分钟。"""
    assert sh.window_bounds(date(2026, 9, 28), "close") == (930, 945)
    assert sh.window_bounds(date(2026, 11, 27), "close") == (750, 765)       # 12:30–12:45
    assert sh.window_bounds(date(2026, 11, 27), "open") == (600, 620)
    assert sh.window_bounds(date(2026, 11, 26), "close") is None             # 感恩节休市
    assert sh.window_bounds(date(2027, 4, 5), "open") is None                # 日历未知


# —— C01：出场必须两腿都合格 ——
SQ = {"bid": 0.9, "ask": 1.0, "bid_size": 10, "ask_size": 10}


def test_c01_protective_error_or_zero_size_never_earns_income():
    bad = {"bid": 0.8, "ask": 0.9, "bid_size": 0, "ask_size": 0, "error": "stale/error"}
    assert sh.exit_quality(SQ, bad) == "protective_leg_error"
    assert sh.exit_quality(SQ, dict(bad, error=None)) == "protective_zero_size"
    assert sh.exit_quality(SQ, None) == "protective_leg_missing"
    assert sh.exit_quality(SQ, {"bid": None, "ask": 0.1, "bid_size": 0, "ask_size": 5}) == "protective_price_missing"
    assert sh.exit_quality(dict(SQ, error="x"), {"bid": 0.1, "ask": 0.2, "bid_size": 1, "ask_size": 1}) == "sell_leg_error"


def test_c01_worthless_long_leg_is_named_short_only():
    worthless = {"bid": 0.0, "ask": 0.01, "bid_size": 0, "ask_size": 50}
    assert sh.exit_quality(SQ, worthless) is None
    assert sh.exit_mode(worthless) == "short_only" and sh.exit_cost_raw(SQ, worthless) == 100.0
    good = {"bid": 0.8, "ask": 0.85, "bid_size": 5, "ask_size": 5}
    assert sh.exit_mode(good) == "both_legs" and sh.exit_cost_raw(SQ, good) == pytest.approx(20.0)


def test_c01_window_with_bad_protective_is_missing_not_priced():
    bad = {"P|57": _q(0.90, 1.00), "P|56": dict(_q(0.80, 0.90, bs=0), error="stale")}
    x = sh.window_leg(_vrow({"2026-09-15|close": {"attempts": [_att("2026-09-15|close", bad)]}}),
                      _leg(), "2026-09-15|close", "exit")
    assert x == {"status": "missing", "reasons": ["protective_leg_error"]}


# —— C02：应有窗口来自日历，缺日线 ≠ 休市 ——
def test_c02_missing_bar_keeps_expected_windows():
    assert sh.expected_mark_windows(date(2026, 9, 14), date(2026, 9, 16)) == [
        "2026-09-14|close", "2026-09-15|open", "2026-09-15|close", "2026-09-16|open", "2026-09-16|close"]
    bars = [BARS3[0], BARS3[2]]                                           # 删掉 9/15 日线
    cheap = {"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}
    o = sh.settle_leg(_leg(), _vrow(_full_windows(_marks_all(cheap))), bars=bars)
    assert o["marks"]["expected"] == 5 and o["bars_missing"] == ["2026-09-15"] and o["complete"] is False
    assert o["pnl"]["pre_expiry_exit_policy"] == pytest.approx(20 - 10 - 3.2), "到期前一交易日仍是 9/15，不是 9/14"
    assert o["status"]["close_beyond_next_open_exit"] == "bars_incomplete"
    assert o["any_close_breach"] is None


def test_c02_holiday_is_skipped_by_calendar_not_by_bars():
    """周五 11/27 半日市入场、下周一到期…换成跨感恩节：11/25 入场、11/27 到期，11/26 休市。"""
    exp_windows = sh.expected_mark_windows(date(2026, 11, 25), date(2026, 11, 27))
    assert exp_windows == ["2026-11-25|close", "2026-11-27|open", "2026-11-27|close"]
    assert mc.prev_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)


def test_calendar_unknown_marks_everything_unknown():
    leg = dict(_leg(), expiry="2027-04-07")
    o = sh.settle_leg(leg, {"session": "2027-04-05", "windows": {}}, bars=[])
    assert set(o["status"].values()) == {"calendar_unknown"} and o["complete"] is False


# —— C03：入场日本身就是到期前最后交易日 ——
def _fri_mon(exit_quotes, exit_minute=0):
    leg = dict(_leg(), expiry="2026-09-21")
    w = {"2026-09-18|open": {"attempts": [_att("2026-09-18|open", ENTRY_OK)]},
         "2026-09-18|close": {"attempts": [_att("2026-09-18|close", exit_quotes, minute=exit_minute)]}}
    bars = [(date(2026, 9, 18), 58.2, 57.8, 58.0), (date(2026, 9, 21), 58.2, 57.8, 58.0)]
    return sh.settle_leg(leg, {"session": "2026-09-18", "windows": w}, bars=bars)


def test_c03_friday_entry_monday_expiry_exits_friday_close():
    o = _fri_mon({"P|57": _q(0.90, 1.00), "P|56": _q(0.90, 0.95)})
    assert o["status"]["pre_expiry_exit_policy"] == "ok"
    assert o["pnl"]["pre_expiry_exit_policy"] == pytest.approx(20 - 10 - 3.2)
    assert o["exit_mode"]["pre_expiry_exit_policy"] == "both_legs"


def test_c03_inconsistent_timestamps_never_priced():
    """退出须严格晚于实际入场。正常数据里收盘窗必在开盘窗之后，此规则是防御：
    构造一个时间戳早于入场的「退出」，确认它不产生损益（被窗口或先后规则拦下皆可）。"""
    leg = dict(_leg(), expiry="2026-09-21")
    ent = _att("2026-09-18|open", ENTRY_OK)
    ext = _att("2026-09-18|close", {"P|57": _q(0.90, 1.00), "P|56": _q(0.90, 0.95)})
    ent["started_at"] = "2026-09-18T10:00:00-04:00"; ent["ended_at"] = "2026-09-18T10:00:30-04:00"
    ext["started_at"] = ext["ended_at"] = "2026-09-18T10:00:20-04:00"       # 退出时间戳早于入场结束
    w = {"2026-09-18|open": {"attempts": [ent]}, "2026-09-18|close": {"attempts": [ext]}}
    bars = [(date(2026, 9, 18), 58.2, 57.8, 58.0), (date(2026, 9, 21), 58.2, 57.8, 58.0)]
    o = sh.settle_leg(leg, {"session": "2026-09-18", "windows": w}, bars=bars)
    # 10:00:20 不在收盘窗内 → 窗口判为缺失（outside_window）；无论哪条规则拦下，都不得产生损益
    assert o["pnl"]["pre_expiry_exit_policy"] is None


def test_endpoint_maturity_is_independent():
    """提前退出在其退出窗结束后即可结算，不等到期日（成熟性按终点）。"""
    now = datetime(2026, 9, 15, 16, 0, tzinfo=sh._TZ)
    o = sh.settle_leg(_leg(), _vrow(_full_windows({"2026-09-15|close": {"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}})),
                      bars=BARS3[:2], now=now)
    assert o["status"]["pre_expiry_exit_policy"] == "ok" and o["pnl"]["pre_expiry_exit_policy"] is not None
    assert o["status"]["quote_entry_expiry_intrinsic"] == "immature"
    assert o["status"]["stop1x_twice_daily"] in ("immature", "path_unknown")
    assert o["marks"]["pending"] == 2 and o["bars_missing"] == [] and o["complete"] is False


# —— C04：日历块 bootstrap、完整块下限、退化标志 ——
def _cal(n, start=date(2026, 9, 1)):
    return [d.isoformat() for d in mc.trading_days(start, date(2026, 12, 31))][:n]


def test_c04_constant_sample_is_degenerate_not_support():
    g = {d: [0.1] for d in _cal(20)}
    ci = sh.block_bootstrap(g, iters=1000)
    assert ci["status"] == "degenerate" and sh.judge(ci) == "退化（不判）"


def test_c04_full_block_floor():
    ds = _cal(19)
    g = {d: [0.1 * (i % 3) - 0.05] for i, d in enumerate(ds)}
    assert sh.block_bootstrap(g, iters=500)["status"] == "insufficient", "19 // 5 = 3 < 4"
    g20 = {d: [0.1 * (i % 3) - 0.05] for i, d in enumerate(_cal(20))}
    assert sh.block_bootstrap(g20, iters=500)["status"] == "ok"
    assert sh.block_bootstrap(g20, iters=500, block_days=10)["status"] == "insufficient", "10 日块需 40 个日期"


def test_c04_calendar_gaps_are_not_compressed():
    """有样本日之间隔着无机会交易日：块沿日历滑动，n_calendar_days 覆盖整个跨度。"""
    days = _cal(60)
    g = {d: [0.1 * (i % 4) - 0.1] for i, d in enumerate(days) if i % 3 == 0}   # 每 3 个交易日才有一笔
    ci = sh.block_bootstrap(g, iters=500)
    assert ci["n_dates"] == 20 and ci["n_calendar_days"] == len(days) - (len(days) - 1) % 3
    assert ci["status"] == "ok"
    assert sh.block_bootstrap({"2026-09-07": [0.1]})["status"] in ("date_not_trading_day", "insufficient")


def test_c04_serial_correlation_widens_interval():
    days = _cal(40)
    g = {d: [1.0 if (i // 5) % 2 == 0 else -0.6] for i, d in enumerate(days)}
    w1 = sh.block_bootstrap(g, iters=3000, block_days=1); w5 = sh.block_bootstrap(g, iters=3000, block_days=5)
    assert (w5["hi"] - w5["lo"]) > (w1["hi"] - w1["lo"]) * 1.3


def test_formal_identity_and_sample_restriction():
    assert sh.formal_identity(date(2026, 12, 31)) == "exploratory"
    assert sh.formal_identity(date(2027, 1, 4)) == "formal"
    rows = [_settled_row("2026-12-30", "偏多", 10, 0, -50, -40), _settled_row("2027-01-05", "偏多", 10, 0, -50, -40)]
    for r in rows:
        for l in r["legs"]:
            l.update(status="candidate", expiry=r["session"])
        r["legs"] += [{"leg_id": "P-A", "status": "candidate", "expiry": r["session"]}]
    sm = sh.paired_summary(rows, pool=None, sides=["P"], as_of=date(2027, 1, 10))
    assert sm["identity"] == "formal" and sm["coverage"]["opportunities"] == 1, "检验日之后入场的行不进正式样本"


def test_non_overlap_filter():
    rows = []
    for d, e in (("2026-09-14", "2026-09-16"), ("2026-09-15", "2026-09-17"), ("2026-09-17", "2026-09-18"),
                 ("2026-09-21", "2026-09-23")):
        r = _settled_row(d, "偏多", 10, 0, -50, -40); r["instrument"] = "silver"
        r["legs"].append({"leg_id": "P-A", "status": "candidate", "expiry": e})
        rows.append(r)
    kept = [r["session"] for r in sh._non_overlap(rows, "P")]
    assert kept == ["2026-09-14", "2026-09-17", "2026-09-21"]


def test_windows_command(monkeypatch, capsys):
    from undertow import shadow_cli as sc
    for d, want, rc in ((date(2026, 11, 27), "open 600 620\nclose 750 765\nchain 615 635\n", 0),
                        (date(2026, 11, 26), "", 0), (date(2027, 4, 5), "", 3)):
        monkeypatch.setattr(sc, "market_today", lambda d=d: d)
        assert sc.cmd_windows(None) == rc
        assert capsys.readouterr().out == want


def test_session_hooks_windows_from_calendar_and_ok_only_on_success():
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    body = src[src.index("shadow_window() {"):src.index("# ── ① 盘前简报")]
    assert 'if (( RC == 0 )); then' in body and ': > "$OKF"' in body
    assert "shadow windows" in body and "930" not in body, "收盘窗时刻只由 shadow windows 给出，不写死"
    assert src.index("shadow_window() {") < src.index('shadow_window open "$_LO"') < src.index("# ── ① 盘前简报")
    assert "|| IN_SHADOW == 1" in src[src.index("不在任何窗口"):]
    du = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    assert "shadow capture" in du and "shadow settle" in du


# ═══════════════════ v5（Codex 007：零买价身份、残腿区间、S02）═══════════════════

def test_v5_zero_bid_needs_identifiable_quote():
    assert sh.exit_quality(SQ, {"bid": 0.0, "ask": None, "bid_size": 0, "ask_size": 0}) == "protective_ask_missing"
    assert sh.exit_quality(SQ, {"bid": 0.0, "ask": 0.05, "bid_size": 0, "ask_size": 0}) == "protective_zero_bid_unverified"
    assert sh.exit_quality(SQ, {"bid": 0.0, "ask": 0.0, "bid_size": 0, "ask_size": 5}) == "protective_zero_bid_unverified"
    assert sh.exit_quality(SQ, {"bid": 0.0, "ask": 0.05, "bid_size": 0, "ask_size": 10}) is None


def _short_only_row(expiry_close):
    """9/15 收盘窗：短腿 ask 0.10，长腿 bid 0 / ask 0.02 → 只买回短腿；到期 9/16 收盘 = expiry_close。"""
    m = {"2026-09-15|close": {"P|57": _q(0.08, 0.10), "P|56": _q(0.0, 0.02, bs=0)}}
    bars = BARS3[:2] + ([(date(2026, 9, 16), 58.2, 55.0, expiry_close)] if expiry_close is not None else [])
    return sh.settle_leg(_leg(), _vrow(_full_windows(m)), bars=bars)


def test_v5_short_only_residual_expired_otm_is_point():
    o = _short_only_row(58.0)                                   # 长腿 56P 到期虚值
    b = sh.CONFIG["primary_basis"]
    assert o["exit_mode"][b] == "short_only" and o["status"][b] == "ok|short_only_expired_otm"
    assert o["pnl"][b] == pytest.approx(20 - 10 - 3.2) and o["pnl_bounds"][b] == [o["pnl"][b], o["pnl"][b]]
    assert o["residual"][b]["status"] == "expired_otm" and o["residual"][b]["strike"] == 56.0
    r = o["residual"][b]
    assert r["disposition_basis"].startswith("model") and r["actual_confirmed"] is None, "模型处置 ≠ 已确认现金流"
    row = dict(_vrow({}), legs=[_leg()], outcome={"P-A": o})
    assert sh.status_breakdown([row], b)["model_disposition_points"] == 1


def test_v5_short_only_residual_itm_has_no_lower_bound():
    o = _short_only_row(55.5)                                   # 长腿 56P 到期实值 0.50
    b = sh.CONFIG["primary_basis"]
    assert o["status"][b] == "ok|short_only_itm_unknown" and o["pnl"][b] is None
    assert o["pnl_bounds"][b] == [None, pytest.approx(20 - 10 - 3.2 + 50)]
    assert o["pnl_residual0"][b] == pytest.approx(20 - 10 - 3.2), "残值零情景单独保存，不冒充已实现"


def test_v5_short_only_waits_for_expiry_bar():
    o = _short_only_row(None)
    assert o["status"][sh.CONFIG["primary_basis"]] == "immature" and o["complete"] is False


def test_v5_lower_bound_difference_is_not_a_difference_bound():
    """Codex 007 反例：A=[10,10]、B=[5,25] → 零残值差 +5，但真实差可到 −15。区间传播后不得判「支持」。"""
    days = _cal(20)
    noise = lambda i: 0.01 * (i % 3 - 1)
    lo = {d: [10 / 100 - 25 / 100 + noise(i)] for i, d in enumerate(days)}
    hi = {d: [10 / 100 - 5 / 100 + noise(i)] for i, d in enumerate(days)}
    ci = sh.interval_ci(lo, hi, iters=500)
    assert ci["mean_bounds"][0] == pytest.approx(-0.15, abs=0.01) and ci["mean_bounds"][1] == pytest.approx(0.05, abs=0.01)
    assert ci["lo"] < -0.14 and ci["hi"] > 0.04 and sh.judge(ci) == "未决", "零残值差 +0.05 不得被判支持"
    const = sh.interval_ci({d: [-0.15] for d in days}, {d: [0.05] for d in days}, iters=500)
    assert const["status"] == "degenerate", "上下界各自恒定仍是常数样本：不判定"
    ci2 = sh.interval_ci(lo, hi, unbounded=1, iters=500)
    assert ci2["status"] == "residual_unknown" and sh.judge(ci2) == "未决（残腿处置未知）"


def _rowp(day, inst, a, b, a_mode="both_legs", b_mode="both_legs", b_bounds=None):
    k = sh.CONFIG["primary_basis"]

    def o(v, mode, bounds=None):
        bb = bounds or [v, v]
        return {"pnl": {k: v if bb[0] == bb[1] else None}, "pnl_bounds": {k: bb}, "pnl_residual0": {k: bb[0] if bb[0] is not None else v},
                "max_risk": {k: 100.0}, "credit": {k: 20.0}, "width_usd": 100.0, "exit_mode": {k: mode},
                "status": {k: "ok"}}
    return {"session": day, "instrument": inst, "identity": {"mode": "prospective"},
            "decision": {"flow": {"call_direction": "偏多"}},
            "legs": [{"leg_id": "P-A", "status": "candidate", "expiry": day},
                     {"leg_id": "P-B1", "status": "candidate", "expiry": day, "same_as_A": False},
                     {"leg_id": "C-A", "status": "no_candidate", "reason": "no_wall"}],
            "outcome": {"P-A": o(a, a_mode), "P-B1": o(b, b_mode, b_bounds)}}


def test_v5_estimates_conditional_and_scenario():
    rows = [_rowp("2026-09-28", "gold", 10, 5, b_mode="short_only", b_bounds=[5, 25])]
    s_b = sh.paired_summary(rows, pool=None, sides=["P"])
    s_c = sh.paired_summary(rows, pool=None, sides=["P"], estimate="both_legs_only")
    s_r = sh.paired_summary(rows, pool=None, sides=["P"], estimate="residual0")
    assert s_b["AminusB"]["mean_bounds"] == [pytest.approx(-0.15), pytest.approx(0.05)]
    assert s_c["coverage"]["pairs"] == 0 and s_c["coverage"]["excluded_conditional"] == 1
    assert s_r["AminusB"]["mean"] == pytest.approx(0.05)
    assert set(s_b) >= {"A", "A_paired", "B_paired", "AminusB"}, "S02：三个均值并列"


def test_s02_opportunity_ledger_every_cell_in_one_category():
    rows = [_rowp("2026-09-28", "gold", 10, 5), _rowp("2026-09-29", "gold", 10, 5)]
    rows[1]["outcome"]["P-B1"] = None
    led = sh.opportunity_ledger(rows, pool="etf", start=date(2026, 9, 28), end=date(2026, 9, 30),
                                high_vol={("gold", "2026-09-28")})
    t = led["totals"]
    assert led["cells"] == 7 * 3 * 2 == sum(t.values())
    assert t["pairable"] == 1 and t["priced_A_only"] == 1 and t["no_candidate"] == 2
    assert t["not_generated"] == 7 * 3 * 2 - 4, "没有机会行的交易日必须进分母，不能消失"
    g = led["by_instrument"]["gold"]
    assert g["cells"] == 6 and g["pairable_rate"] == pytest.approx(1 / 6, abs=1e-4) and g["weight_in_pool"] == 1.0
    assert led["no_candidate_reasons"] == {"no_wall": 2}
    assert led["missing_by_vol"]["high"]["cells"] == 2


def test_s02_metrics_table_descriptive():
    rows = [_rowp("2026-09-28", "gold", 10, 5), _rowp("2026-09-29", "gold", -30, 5, b_mode="short_only", b_bounds=[5, 25])]
    mt = sh.metrics_table(rows, pool="etf", sides=["P"])
    assert mt["A"]["n_point"] == 2 and mt["A"]["net_usd"] == pytest.approx(-10)
    assert mt["A"]["pnl_per_width"] == pytest.approx(-0.10) and mt["A"]["credit_per_width"] == pytest.approx(0.2)
    assert mt["A"]["fee_per_credit"] == pytest.approx(3.2 / 20)
    assert mt["B1"]["n_point"] == 1 and mt["B1"]["n_interval_only"] == 1


def test_formal_freeze_keeps_first_and_appends_revision(tmp_path, monkeypatch):
    from undertow import shadow_cli as sc
    monkeypatch.setattr(sc, "DIR", tmp_path)
    rows = [{"key": "k1", "outcome": {"x": 1}}]
    assert "首份正式结果已冻结" in sc._formal_freeze({"a": 1}, rows)
    assert sc._formal_freeze({"a": 1}, rows) == "正式结果与首份一致"
    assert "首份保留" in sc._formal_freeze({"a": 2}, rows)
    f = tmp_path / sh.CONFIG["version"] / "formal" / "formal_result.json"
    assert json.loads(f.read_text())["summaries"] == {"a": 1}
    assert len((f.parent / "formal_result.json.revisions.jsonl").read_text().splitlines()) == 1


def test_primary_pool_windows_inside_option_hours():
    """Codex 007：观察窗须落在各主池品种期权交易时段内（正常日、13:00 收市日都查）；扩展池未认证 → None。"""
    from undertow.core import option_products as op
    from undertow.core.config import load_config
    cfg = load_config()
    roots = [cfg.get(k).options.symbol for k in sh.CONFIG["pools"]["etf"]]
    assert sorted(roots) == sorted(r for r, c in op.ROOTS.items() if c is not None)
    for d in (date(2026, 9, 28), date(2026, 11, 27), date(2026, 12, 24)):
        for r in roots:
            assert sh.windows_inside_option_hours(r, d) is True, (r, d)
    assert sh.windows_inside_option_hours("TQQQ", date(2026, 9, 28)) is None
    assert sh.windows_inside_option_hours("GLD", date(2026, 11, 26)) is None      # 休市
    assert op.option_close_minutes("USO", early=False) == 960 and op.option_close_minutes("GLD", early=True) == 795


# ═══════════════════ S05：账户可执行账（Codex 008）═══════════════════

def _exec_row(inst="silver", session="2026-09-14"):
    r = _row(); r["instrument"] = inst; r["key"] = f"{session}|{inst}"; r["session"] = session
    r["windows"] = {f"{session}|open": {"attempts": [_att(f"{session}|open", {
        **{sh.qkey(l["side"], l["sell"]): _q(0.30, 0.32) for l in r["legs"] if l["status"] == "candidate"},
        **{sh.qkey(l["side"], l["buy"]): _q(0.08, 0.10) for l in r["legs"] if l["status"] == "candidate"}})]}}
    return r


def test_s05_budget_status_split():
    from undertow.analyze import shadow_exec as sx
    res = sx.evaluate([_exec_row()], session="2026-09-14", net_assets=2000.0, account_open_max_loss=0.0,
                      account_cluster_open={}, prior=[])
    a = next(x for x in res if x["leg_id"] == "P-A")
    assert a["max_loss"] == pytest.approx(a["width"] - a["credit"] + 3.2)
    assert a["budget_status"] == "pass" and a["n"] >= 1
    assert a["broker_status"] == "unverified" and a["selection_status"] == "independent_alternative", \
        "Codex 009 N03：理论预算通过 ≠ 可执行；互斥备选不可相加"
    assert "verdict" not in a


def test_s05_current_account_cannot_open():
    from undertow.analyze import shadow_exec as sx
    res = sx.evaluate([_exec_row()], session="2026-09-14", net_assets=0.05, account_open_max_loss=0.0,
                      account_cluster_open={}, prior=[])
    assert all(x["n"] == 0 and x["budget_status"] != "pass" for x in res)


def test_s05_unknown_account_risk_is_unknown():
    from undertow.analyze import shadow_exec as sx
    res = sx.evaluate([_exec_row()], session="2026-09-14", net_assets=10000.0, account_open_max_loss=None,
                      account_cluster_open=None, prior=[])
    priced = [x for x in res if x["budget_status"] != "no_candidate"]
    assert priced and all(x["budget_status"] == "unknown" and x["n"] == 0 for x in priced)


def test_s05_hypothetical_prior_reported_not_consumed():
    """此前候选的假设占用只列出、不扣额度；按 v5 退出日（到期前一交易日）释放。"""
    from undertow.analyze import shadow_exec as sx
    prior = [{"rule": "A", "budget_status": "pass", "expiry": "2026-09-16", "session": "2026-09-11",
              "cluster": "贵金属", "max_loss": 300.0, "n": 1}]
    fresh = sx.evaluate([_exec_row()], session="2026-09-14", net_assets=2000.0, account_open_max_loss=0.0,
                        account_cluster_open={}, prior=[])
    held = sx.evaluate([_exec_row()], session="2026-09-14", net_assets=2000.0, account_open_max_loss=0.0,
                       account_cluster_open={}, prior=prior)
    fa = next(x for x in fresh if x["leg_id"] == "P-A"); ha = next(x for x in held if x["leg_id"] == "P-A")
    assert ha["n"] == fa["n"] and ha["hypothetical_prior_occupancy"] == 300.0
    assert sx.hypothetical_prior_occupancy(prior, date(2026, 9, 15)) == {"贵金属": 300.0}
    assert sx.hypothetical_prior_occupancy(prior, date(2026, 9, 16)) == {}, "v5 在到期前一交易日退出后释放"
    real = sx.evaluate([_exec_row()], session="2026-09-14", net_assets=2000.0, account_open_max_loss=300.0,
                       account_cluster_open={"贵金属": 300.0}, prior=[])
    assert next(x for x in real if x["leg_id"] == "P-A")["n"] == 0, "真实持仓才扣额度"


def test_s05_no_entry_quote_is_unknown():
    from undertow.analyze import shadow_exec as sx
    r = _exec_row(); r["windows"] = {}
    res = sx.evaluate([r], session="2026-09-14", net_assets=10000.0, account_open_max_loss=0.0,
                      account_cluster_open={}, prior=[])
    assert all(x["budget_status"] in ("unknown", "no_candidate") and x["n"] == 0 for x in res)


def test_s05_exec_output_is_private():
    from undertow import shadow_cli as sc
    assert str(sc.EXEC_DIR).startswith("data/account/")
    import subprocess
    r = subprocess.run(["git", "check-ignore", "-q", "data/account/shadow_exec/x.json"], cwd=ROOT)
    assert r.returncode == 0, "可执行账含账户金额，必须 gitignore"



def test_open_chain_filter_and_schedule():
    """开盘后近价全链：只留 ≤14 天到期、±10% 内；现价缺失不猜；调度走 shadow windows 的 chain 行。"""
    from undertow.shadow_cli import filter_chain
    pl = {"data": {"current_price": 100.0, "options": [
        {"option": "SLV261002P00095000", "bid": 1}, {"option": "SLV261002P00085000", "bid": 1},   # 近期·近价 / 太远
        {"option": "SLV261120C00101000", "bid": 1}, {"option": "junk"}]}}
    f = filter_chain(pl, date(2026, 9, 28))
    assert [o["option"] for o in f["data"]["options"]] == ["SLV261002P00095000"]
    assert f["undertow_filter"]["n_full"] == 4 and f["undertow_filter"]["n_kept"] == 1
    assert pl["data"]["options"][1]["option"] == "SLV261002P00085000", "不改原 payload"
    with pytest.raises(ValueError):
        filter_chain({"data": {"options": []}}, date(2026, 9, 28))
    src = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert 'shadow chain --status-file' in src and 'shadow_window chain' in src


# —— Codex 009 N02：开盘后全链快照的数据完整性 ——
from undertow.collect.store import SnapshotStore as _REAL_STORE   # noqa: E402

def _chain_env(tmp_path, monkeypatch, payload_fn):
    import argparse
    from undertow import shadow_cli as sc
    from undertow.collect import cboe_options as co
    from undertow.collect import store as st_mod
    real = _REAL_STORE
    monkeypatch.setattr(st_mod, "SnapshotStore", lambda: real(root=tmp_path / "snap"))
    monkeypatch.setattr(sc, "_in_chain_window", lambda: True)
    monkeypatch.setattr(sc, "market_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr(co.CboeOptionsSource, "fetch_raw", lambda self, inst, use_cache=True: payload_fn())
    stf = tmp_path / "st.json"
    run = lambda: sc.cmd_chain(argparse.Namespace(instruments=["silver"], allow_off_hours=False, status_file=str(stf)))
    return run, stf, real(root=tmp_path / "snap")


def _pl(opts, ltt="2026-09-28T10:05:00"):
    return {"symbol": "SLV", "timestamp": "2026-09-28 14:20:00",
            "data": {"current_price": 58.0, "last_trade_time": ltt, "options": opts}}


GOOD_OPTS = [{"option": "SLV261002P00057000", "bid": 0.3}, {"option": "SLV261002C00060000", "bid": 0.2}]


@pytest.mark.parametrize("payload,frag", [
    ({"symbol": "SLV", "data": {"current_price": 58.0}}, "options"),           # 缺 options：不是 0 合约
    (_pl([]), "empty"),
    (_pl([{"option": "SLV270115P00030000"}]), "no_match"),
])
def test_n02_bad_or_empty_chain_is_not_success(tmp_path, monkeypatch, payload, frag):
    run, stf, store = _chain_env(tmp_path, monkeypatch, lambda: payload)
    assert run() == 1
    s_ = json.loads(stf.read_text())
    assert s_["overall"] == "failed" and frag in json.dumps(s_["issues"], ensure_ascii=False)
    assert not store.path_of("options_open", "SLV", date(2026, 9, 28)).exists()


def test_n02_certified_vs_uncertified_quote_time(tmp_path, monkeypatch):
    run, stf, store = _chain_env(tmp_path, monkeypatch, lambda: _pl(GOOD_OPTS))
    assert run() == 0 and json.loads(stf.read_text())["overall"] == "complete"
    f = store.load("options_open", "SLV", date(2026, 9, 28))["undertow_filter"]
    assert f["open_window_certified"] is True and f["n_kept"] == 2 and "有限近价链" in f["scope"]
    run2, stf2, store2 = _chain_env(tmp_path / "b", monkeypatch, lambda: _pl(GOOD_OPTS, ltt="2026-09-28T11:30:00"))
    assert run2() == 1 and json.loads(stf2.read_text())["overall"] == "partial"
    assert store2.load("options_open", "SLV", date(2026, 9, 28))["undertow_filter"]["open_window_certified"] is False
    run3, _, store3 = _chain_env(tmp_path / "c", monkeypatch, lambda: _pl(GOOD_OPTS, ltt=None))
    run3()
    assert store3.load("options_open", "SLV", date(2026, 9, 28))["undertow_filter"]["open_window_certified"] is None


def test_n02_existing_file_validated_not_just_exists(tmp_path, monkeypatch):
    run, stf, store = _chain_env(tmp_path, monkeypatch, lambda: _pl(GOOD_OPTS))
    assert run() == 0
    assert run() == 0 and json.loads(stf.read_text())["overall"] == "unchanged", "有效文件 → 跳过"
    p = store.path_of("options_open", "SLV", date(2026, 9, 28))
    p.write_bytes(b"not gzip")                                              # 损坏
    assert run() == 1 and json.loads(stf.read_text())["overall"] == "partial"
    assert list(p.parent.glob("*.corrupt-*")), "坏文件隔离保留"
    assert store.load("options_open", "SLV", date(2026, 9, 28))["undertow_filter"]["status"] == "ok", "已重抓"
