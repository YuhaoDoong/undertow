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





def test_bootstrap_and_bounds():
    assert sh.date_block_bootstrap({})[0] is None
    m, lo, hi = sh.date_block_bootstrap({f"d{i}": [0.1, 0.1] for i in range(4)})
    assert m == pytest.approx(0.1) and lo is None, "少于 5 个日期不给区间"
    m, lo, hi = sh.date_block_bootstrap({f"d{i}": [i / 10] for i in range(20)})
    assert lo < m < hi
    assert sh.zero_event_upper(50) == pytest.approx(0.0582, abs=1e-3), "50 笔零事件的 95% 上界 5.8%"
    assert sh.judge(0.01, 0.2) == "支持" and sh.judge(-0.2, -0.01) == "不支持" and sh.judge(-0.1, 0.1) == "未决"



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
        return {"pnl": {sh.CONFIG["primary_basis"]: p}, "max_risk": {sh.CONFIG["primary_basis"]: 100.0}}
    return {"session": day, "identity": {"mode": "prospective"}, "decision": {"flow": {"call_direction": flow}},
            "legs": [{"leg_id": "P-B1", "same_as_A": False}, {"leg_id": "C-B1", "same_as_A": False}],
            "outcome": {"P-A": o(a_p), "P-B1": o(b_p), "C-A": o(a_c), "C-B1": o(b_c)}}


def test_paired_summary_side_and_flow_subsets():
    rows = [_settled_row(f"2026-10-{i:02d}", "偏多", 10, 0, -50, -40) for i in range(1, 8)]
    both = sh.paired_summary(rows)
    put = sh.paired_summary(rows, sides=["P"])
    aligned = sh.paired_summary(rows, flow_aligned_only=True)
    assert both["coverage"]["pairs"] == 14 and put["coverage"]["pairs"] == 7
    assert put["A_mean_norm"] == pytest.approx(0.10) and put["AminusB_mean_norm"] == pytest.approx(0.10)
    assert aligned["coverage"]["pairs"] == 7 and aligned["A_mean_norm"] == pytest.approx(0.10), "偏多 → 只取 put 侧"


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

def test_config_v3_identity_and_fee_single_source():
    c = sh.CONFIG
    assert c["version"] == "shadow-v3-20260926" and c["fee_round_trip"] == ws.FEE_PER_TRADE
    assert c["primary_comparison"] == "A vs B1" and c["primary_endpoint"] == "quote_entry_expiry_intrinsic"
    assert c["b_rules"] == {"B1": {"atr": 1.0}, "B2": {"atr": 2.0}, "B3": {"delta": 0.20}}
    assert c["stats"]["block_days"] == 5 and c["formal_test_date"] == "2026-12-31"
    assert "googl" in c["instruments"] and "dxy" not in c["instruments"]


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
    hh, mm = map(int, sh.CONFIG["quote"]["windows"][w][0].split(":"))
    t = f"{day}T{hh:02d}:{mm + minute:02d}:10-04:00"
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
    assert o["marks"] == {"expected": 5, "valid": 0, "not_run": 5, "missing": 0}


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
    assert o["pnl"]["pre_expiry_close_exit"] == pytest.approx(20 - 10 - 3.2)      # 9/15 收盘窗平仓
    o2 = sh.settle_leg(_leg(), _vrow(_full_windows({})), bars=BARS3)
    assert o2["pnl"]["pre_expiry_close_exit"] is None and o2["status"]["pre_expiry_close_exit"] == "exit_not_run"
    assert sh.settle_leg(_leg(), _vrow(_full_windows({})), bars=BARS3[:2]) is None, "到期 bar 未出不结算"


def test_half_day_close_window_not_run_is_unknown_not_skipped():
    """半日市收盘窗未运行：记 not_run，止损口径未知 —— 不补、不当作未触发。"""
    m = _marks_all({"P|57": _q(0.10, 0.12), "P|56": _q(0.02, 0.03)}); del m["2026-09-15|close"]
    o = sh.settle_leg(_leg(), _vrow(_full_windows(m)), bars=BARS3)
    assert o["marks"]["not_run"] == 1 and o["pnl"]["stop2x_twice_daily"] is None


def test_no_valid_entry_makes_all_quote_bases_unknown():
    o = sh.settle_leg(_leg(), _vrow({}), bars=BARS3)
    for b in ("quote_entry_expiry_intrinsic", "pre_expiry_close_exit", "close_beyond_next_open_exit", "stop1x_twice_daily"):
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


def test_session_hooks_only_mark_ok_on_success_and_run_first():
    sh_ = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    body = sh_[sh_.index("shadow_window() {"):sh_.index('shadow_window open 600 620')]
    assert 'if (( RC == 0 )); then' in body and ': > "$OKF"' in body.split("else")[0]
    assert sh_.index("shadow_window open 600 620") < sh_.index("# ── ① 盘前简报"), "必须在可能 exit 0 的旧窗口之前"
    assert "ET_MIN >= 930 && ET_MIN <= 945" in sh_[sh_.index("不在任何窗口"):]
    du = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    assert "shadow capture" in du and "shadow settle" in du


def test_block_bootstrap_widens_under_serial_correlation():
    """连续 5 日同号的序列：5 日块区间应比逐日块宽（逐日块低估不确定性）。"""
    g = {f"2026-10-{i + 1:02d}": [1.0 if (i // 5) % 2 == 0 else -0.6] for i in range(30)}
    m, lo1, hi1 = sh.date_block_bootstrap(g, iters=4000, block_days=1)
    _, lo5, hi5 = sh.date_block_bootstrap(g, iters=4000, block_days=5)
    assert (hi5 - lo5) > (hi1 - lo1) * 1.3
    assert sh.CONFIG["stats"]["block_days"] == 5 and sh.CONFIG["stats"]["sensitivity_block_days"] == 10
    s = sh.paired_summary([])
    assert s["sensitivity"]["block_days"] == 10 and s["stats_version"] == "stats-v1"


def test_block_bootstrap_refuses_too_few_blocks():
    """R08 同类退化：7 个日期配 10 日块 → 每次抽到全样本 → 零宽区间。必须报样本不足。"""
    g = {f"2026-10-{i + 1:02d}": [0.1 * i - 0.2] for i in range(7)}
    m, lo, hi = sh.date_block_bootstrap(g, block_days=10)
    assert m is not None and lo is None and hi is None
    assert sh.date_block_bootstrap(g, block_days=5)[1] is None       # 需 ≥16 个日期
    g16 = {f"d{i:02d}": [0.1 * (i % 3)] for i in range(16)}
    assert sh.date_block_bootstrap(g16, iters=2000, block_days=5)[1] is not None


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
