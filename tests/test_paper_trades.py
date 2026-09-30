"""模拟仓状态机 v2（Codex 025-1/2）：规格与报价校验、第一份合格报价入场、错过窗口终态、常规时段内才执行止损、
到期日收盘结算与补结、手续费口径统一。只读报价、只写私有日志，从不下单。"""
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import paper_trades as pt  # noqa: E402

ET = ZoneInfo("America/New_York")


def _p(**kw):
    p = {"underlying": "GLD.US", "expiry": "2026-09-30", "side": "P", "sell": "S", "buy": "B", "k_sell": 375.0,
         "k_buy": 373.0, "qty": 1, "entry_date": "2026-09-29", "entry_window_et": ["10:00", "10:20"],
         "min_credit_ratio": 0.15, "stop_mult": 2.0, "fee_round_trip": 3.2, "mark_slots_et": ["09:45", "15:45"],
         "state": "planned", "events": []}
    p.update(kw)
    return p


def q(sb, sa, bb, ba):
    return lambda syms: {"S": {"bid": sb, "ask": sa, "error": None}, "B": {"bid": bb, "ask": ba, "error": None}}


def at(d, h, m):
    return datetime(2026, 9, d, h, m, tzinfo=ET)


def test_economics_single_source():
    e = pt.economics("P", 375, 373, 0.41, 1, 3.2)
    assert e["max_loss_usd"] == 162.2 and e["max_gain_usd"] == 37.8 and e["breakeven"] == pytest.approx(374.622)
    e2 = pt.economics("P", 375, 373, 0.41, 2, 3.2)                      # 费用为每组，整单 × qty
    assert e2["max_loss_usd"] == 324.4 and e2["fee_total_usd"] == 6.4
    assert pt.economics("C", 400, 402, 0.3, 1, 3.2)["breakeven"] == pytest.approx(400.268)


def test_entry_first_qualifying_quote_and_rejects():
    p = _p()
    assert pt.step(p, at(29, 9, 55), depth=q(1.4, 1.5, 0.9, 1.0)) is None
    assert pt.step(p, at(29, 10, 1), depth=q(1.0, 1.1, 0.8, 0.9)) == "retry"            # 门槛不够：窗口内继续
    assert pt.step(p, at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03)) == "enter"
    assert p["entry_credit"] == 0.41 and p["economics"]["max_loss_usd"] == 162.2 and p["stop_value"] == 0.82


def test_credit_at_or_above_width_is_anomaly_not_entry():
    """Codex 025 probe：宽度 1、权利金 1.9 以前被接受、最大亏损为负。"""
    p = _p(k_sell=100.0, k_buy=99.0)
    assert pt.step(p, at(29, 10, 1), depth=q(2.0, 2.1, 0.05, 0.1)) == "retry" and p["state"] == "planned"
    assert "quote_anomaly" in p["last_reject"]
    assert pt.step(p, at(29, 10, 21), depth=q(2.0, 2.1, 0.05, 0.1)) == "skipped" and "quote_anomaly" in p["skip_reason"]


@pytest.mark.parametrize("bad", [{"k_sell": float("nan")}, {"qty": 0}, {"qty": 1.5}, {"k_buy": 376.0},
                                 {"side": "X"}, {"k_buy": 375.0}])
def test_invalid_spec(bad):
    p = _p(**bad)
    assert pt.step(p, at(29, 10, 1), depth=q(1.4, 1.5, 0.9, 1.0)) == "invalid_spec" and p["state"] == "invalid_spec"


def test_invalid_quotes_nan_and_crossed():
    p = _p()
    assert pt.step(p, at(29, 10, 1), depth=q(float("nan"), 1.5, 0.9, 1.0)) == "retry"
    assert pt.step(p, at(29, 10, 2), depth=q(1.6, 1.5, 0.9, 1.0)) == "retry"            # bid > ask
    assert p["state"] == "planned"


def test_missed_entry_day_is_terminal():
    """Codex 025 probe：错过入场日到次日仍 planned。"""
    p = _p()
    assert pt.step(p, at(30, 11, 0), depth=q(1.4, 1.5, 0.9, 1.0)) == "missed" and "missed_window" in p["skip_reason"]


def _entered():
    p = _p(); pt.step(p, at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03)); return p


def test_stop_only_inside_rth_and_marks_are_sparse():
    p = _entered()
    p["mark_slots_et"] = ["09:45", "16:35"]
    assert pt.step(p, at(29, 16, 36), depth=q(1.5, 1.7, 0.7, 0.8)) == "mark"            # 16:35 盘后：只估值
    assert p["state"] == "entered" and p["events"][-1]["action"] == "mark_offhours"
    assert pt.step(p, at(30, 9, 46), depth=q(1.5, 1.7, 0.7, 0.8)) == "stop"             # 常规时段内触发
    assert p["pnl_usd"] == round((0.41 - 1.0) * 100 - 3.2, 2)


@pytest.mark.parametrize("close,val", [(376.0, 0.0), (374.0, 1.0), (370.0, 2.0)])
def test_settle_uses_expiry_day_close(close, val):
    p = _entered()
    sc = lambda u, d: {"close": close, "source": "t", "bar_date": d.isoformat()} if d == date(2026, 9, 30) else None
    assert pt.step(p, at(30, 16, 21), session_close=sc) == "settle"
    assert p["settle_value"] == val and p["pnl_usd"] == round((0.41 - val) * 100 - 3.2, 2)
    assert "不模拟" in p["settlement_model"]


def test_settlement_pending_then_recovered_next_day():
    """Codex 025 probe：错过到期日到次日仍 entered。现在：取不到 → pending；次日补结用到期日那根。"""
    p = _entered()
    assert pt.step(p, at(30, 16, 21), session_close=lambda u, d: None) == "settlement_pending"
    assert pt.step(p, at(30, 16, 26), session_close=lambda u, d: None) is None           # 不重复刷事件
    seen = []
    sc = lambda u, d: seen.append(d) or {"close": 376.0, "source": "t", "bar_date": d.isoformat()}
    assert pt.step(p, datetime(2026, 10, 1, 10, 0, tzinfo=ET), session_close=sc) == "settle"
    assert seen == [date(2026, 9, 30)] and p["state"] == "settled"


def test_never_places_orders():
    src = (Path(__file__).resolve().parents[1] / "scripts/paper_trades.py").read_text("utf-8")
    for bad in ("order buy", "order sell", "order cancel", "order replace", '"order"', "submit_order"):
        assert bad not in src


def test_session_hook_runs_paper_tick_each_trading_wake():
    src = (Path(__file__).resolve().parents[1] / "scripts/session_hooks.sh").read_text("utf-8")
    assert src.index("paper_tick() {") < src.index('[[ -n "$SHW" ]] && paper_tick')
    assert src.index('[[ -n "$SHW" ]] && paper_tick') < src.index("while read -r _W _LO _HI")    # ⑫ 先于影子窗口与采样
    assert "scripts/paper_trades.py tick" in src


def test_quote_with_explicit_error_rejected():
    """Codex 026 probe：带 stale 错误、挂单量 0 的报价曾被接受。"""
    assert not pt.quote_ok({"bid": 1, "ask": 1.1, "error": "stale", "bid_size": 0, "ask_size": 0})
    assert pt.quote_ok({"bid": 1, "ask": 1.1, "error": "", "bid_size": 0, "ask_size": 0})


def test_entry_quote_labels_size_and_gap():
    p = _p()
    dq = lambda syms: {"S": {"bid": 1.44, "ask": 1.6, "bid_size": 0, "ask_size": 5, "error": "",
                             "fetched_at": "2026-09-29T14:06:00+00:00"},
                       "B": {"bid": 0.95, "ask": 1.03, "bid_size": 3, "ask_size": 9, "error": "",
                             "fetched_at": "2026-09-29T14:06:00.600000+00:00"}}
    assert pt.step(p, at(29, 10, 6), depth=dq) == "enter"
    lab = p["entry_quote_labels"]
    assert lab["size_sufficient"] is False and lab["leg_fetch_gap_s"] == pytest.approx(0.6)
    assert "source_ts_unknown" in lab["time_alignment"]


def test_ledger_is_idempotent_projection_after_crash(tmp_path):
    """journal 已记入场、台账尚未追加（崩溃窗口）→ 下次补齐；重复调用不重复写。"""
    p = _entered()
    j = {"theses": [{"id": "T1", "execution": "模拟", "paper": p}]}
    led = tmp_path / "led.jsonl"
    assert pt.sync_ledger(j, led) == 1
    assert pt.sync_ledger(j, led) == 0
    pt.step(p, at(30, 16, 21), session_close=lambda u, d: {"close": 376.0, "source": "t", "bar_date": d.isoformat()})
    assert pt.sync_ledger(j, led) == 1
    rows = [__import__("json").loads(x) for x in led.read_text().splitlines()]
    assert [r["action"] for r in rows] == ["enter", "settle"] and len({r["event_id"] for r in rows}) == 2
    led.write_text(led.read_text() + "{broken\n")
    with pytest.raises(RuntimeError):
        pt.sync_ledger(j, led)


def _settled(close=376.0):
    p = _entered()
    pt.step(p, at(30, 16, 21), session_close=lambda u, d: {"close": close, "source": "t", "bar_date": d.isoformat()})
    return p


def test_settlement_audit_match_mismatch_unavailable():
    p = _settled(376.0)
    assert pt.audit_settlement(p, at(30, 16, 30), cboe_close=lambda u, d: None) == "source_unavailable"
    assert pt.audit_settlement(p, at(30, 16, 40), cboe_close=lambda u, d: None) is None       # 一小时内不重查
    assert pt.audit_settlement(p, at(30, 18, 0), cboe_close=lambda u, d: None) is None        # 仍缺：不重复记事件
    assert pt.audit_settlement(p, at(30, 19, 30), cboe_close=lambda u, d: {"close": 376.005, "source": "c"}) == "source_match"
    assert p["settle_audit"]["result_under_review"] is False and p["pnl_usd"] == round(41 - 3.2, 2)
    assert pt.audit_settlement(p, at(30, 23, 0), cboe_close=lambda u, d: {"close": 1.0, "source": "c"}) is None  # 终态


def test_settlement_audit_flip_marks_under_review_without_overwrite():
    p = _settled(375.02)                                      # 长桥：价外，全额收权利金
    pt.audit_settlement(p, at(30, 20, 0), cboe_close=lambda u, d: {"close": 374.90, "source": "c"})
    a = p["settle_audit"]
    assert a["status"] == "source_mismatch" and a["result_under_review"] and p["result_under_review"]
    assert p["settle_value"] == 0 and p["pnl_usd"] == round(41 - 3.2, 2)                    # 原记账不覆盖
    assert a["secondary_value"] == pytest.approx(0.1)


def test_size_report_three_classes_unknown_kept_separate():
    th = [{"execution": "模拟", "paper": {"entered_at": "x", "pnl_usd": 10.0, "entry_quote_labels": {"size_sufficient": True}}},
          {"execution": "模拟", "paper": {"entered_at": "x", "pnl_usd": -5.0, "entry_quote_labels": {"size_sufficient": False}}},
          {"execution": "模拟", "paper": {"entered_at": "x", "entry_quote_labels": {"size_sufficient": None}}},
          {"execution": "模拟", "paper": {"state": "skipped", "skip_reason": "credit_low"}},
          {"execution": "实盘", "paper": {"entered_at": "x"}}]
    r = pt.size_report(th)
    assert r["all"]["n"] == 4 and r["all"]["entered"] == 3 and sum(r["all"]["pnl"]) == 5.0
    assert r["sufficient"]["n"] == 1 and r["insufficient"]["n"] == 1
    assert r["unknown"]["n"] == 2 and r["unknown"]["not_entered"] == {"credit_low": 1}      # 未入场/无标签 → unknown


# —— 版本链与入场锁定（Codex 028/029；用户：「盘中已成交，那么就不应该乱动了，换挡应该是开没开仓的时候」）——
def _th(tid, batch, cont=None, **kw):
    p = _p(**kw); p["batch"] = batch
    if cont:
        p["continuation_of"] = cont
    return {"id": tid, "execution": "模拟", "paper": p}


def _env(tmp_path, monkeypatch, theses):
    import json as _j
    monkeypatch.setattr(pt, "JOURNAL", tmp_path / "journal.json")
    monkeypatch.setattr(pt, "LOCK", tmp_path / "journal.lock")
    monkeypatch.setattr(pt, "LEDGER", tmp_path / "ledger.jsonl")
    (tmp_path / "journal.json").write_text(_j.dumps({"theses": theses}, ensure_ascii=False))
    return lambda: {t["id"]: t for t in _j.loads((tmp_path / "journal.json").read_text())["theses"]}


def test_second_candidate_in_chain_never_enters_after_first(tmp_path, monkeypatch):
    root = _th("R", "legacy"); root["paper"]["state"] = "skipped"
    load = _env(tmp_path, monkeypatch, [root, _th("A", "claude", "R"), _th("B", "claude", "A")])
    acts = pt.tick(at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03))
    st = load()
    assert st["A"]["paper"]["state"] == "entered" and st["B"]["paper"]["state"] == "skipped"
    assert "superseded_by_entry" in st["B"]["paper"]["skip_reason"] and "B:skipped" in acts


def test_other_batch_is_independent(tmp_path, monkeypatch):
    root = _th("R", "legacy"); root["paper"]["state"] = "skipped"
    load = _env(tmp_path, monkeypatch, [root, _th("A", "claude", "R"), _th("U", "user", "R")])
    pt.tick(at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03))
    st = load()
    assert st["A"]["paper"]["state"] == "entered" and st["U"]["paper"]["state"] == "entered"


def test_register_revision_supersedes_planned_and_refuses_after_entry(tmp_path, monkeypatch):
    root = _th("R", "legacy"); root["paper"]["state"] = "skipped"
    load = _env(tmp_path, monkeypatch, [root, _th("A", "claude", "R")])
    r = pt.register_revision(_th("A2", "claude", "A", k_sell=376.0, k_buy=374.0), at(29, 9, 50))
    st = load()
    assert r["ok"] and r["superseded"] == ["A"] and st["A"]["paper"]["state"] == "skipped"
    assert "superseded_by_revision" in st["A"]["paper"]["skip_reason"] and st["A"]["paper"]["continued_by"] == "A2"
    pt.tick(at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03))
    assert load()["A2"]["paper"]["state"] == "entered"
    r2 = pt.register_revision(_th("A3", "claude", "A2"), at(29, 10, 30))
    assert not r2["ok"] and "入场后不换档" in r2["why"] and "A3" not in load()


def test_revision_waits_for_running_tick_then_sees_entry(tmp_path, monkeypatch):
    """旧任务已在取价（持锁）时用户登记新版本：新版本等锁，拿到锁后看到已成交 → 拒绝，不覆盖。"""
    import threading
    import time as _t
    root = _th("R", "legacy"); root["paper"]["state"] = "skipped"
    load = _env(tmp_path, monkeypatch, [root, _th("A", "claude", "R")])
    started, release = threading.Event(), threading.Event()

    def slow_depth(syms):
        started.set(); release.wait(5)
        return q(1.44, 1.6, 0.95, 1.03)(syms)
    th = threading.Thread(target=lambda: pt.tick(at(29, 10, 6), depth=slow_depth)); th.start()
    assert started.wait(5)
    res = {}
    rv = threading.Thread(target=lambda: res.update(pt.register_revision(_th("A2", "claude", "A"), at(29, 10, 7))))
    rv.start(); _t.sleep(0.2)
    assert rv.is_alive()                                            # 被锁挡住，没有插进旧任务的取价与写入之间
    release.set(); th.join(5); rv.join(5)
    assert load()["A"]["paper"]["state"] == "entered" and not res["ok"] and "A2" not in load()


def test_events_carry_scheduler_version(tmp_path, monkeypatch):
    root = _th("R", "legacy"); root["paper"]["state"] = "skipped"
    load = _env(tmp_path, monkeypatch, [root, _th("A", "claude", "R")])
    monkeypatch.setenv("PAPER_SCHED", "session-hook-v2")
    pt.tick(at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03))
    assert load()["A"]["paper"]["events"][-1]["sched"] == "session-hook-v2"


# —— 自动选档接入状态机（wall-dynamic-v1；用户 2026-09-30 金银熊市看涨价差）——
def _dyn(**kw):
    p = {"underlying": "GLD.US", "expiry": "2026-10-16", "side": "C", "qty": 1, "entry_date": "2026-09-30",
         "entry_window_et": ["10:00", "10:30"], "min_credit_ratio": 0.15, "stop_mult": 2.0, "fee_round_trip": 3.2,
         "mark_slots_et": ["09:45", "15:45"], "state": "planned", "events": [], "strike_rule": "wall-dynamic-v1"}
    p.update(kw)
    return p


def _sel(ok=True, sell=390.0, buy=392.0, credit=0.4, sha="a", wall=390.0):
    e = economics("C", sell, buy, credit, 1, 3.2) if ok else None
    return {"ok": ok, "rule": "wall-dynamic-v1", "sell": sell, "buy": buy, "credit": credit, "nearest_wall": wall,
            "economics": e, "reason": None if ok else "no_qualifying_width",
            "inputs": {"oi_snapshot_sha256": sha, "params_hash": "p",
                       "symbols": {str(sell): "GLD261016C390000.US", str(buy): "GLD261016C392000.US"},
                       "quotes": {str(sell): {"bid": 1.3, "ask": 1.4, "bid_size": 5, "ask_size": 5, "error": ""},
                                  str(buy): {"bid": 0.8, "ask": 0.9, "bid_size": 5, "ask_size": 5, "error": ""}}}}


from scripts.paper_trades import economics  # noqa: E402


def test_dynamic_selection_retries_then_enters_and_locks_strikes():
    p = _dyn()
    d = lambda d_, h, mi: datetime(2026, 9, d_, h, mi, tzinfo=ET)
    assert pt.step(p, d(30, 9, 55), selector=lambda p, n: _sel()) is None                # 窗口前不选
    assert pt.step(p, d(30, 10, 1), selector=lambda p, n: _sel(ok=False)) == "retry"
    assert p["events"][-1]["wall_change_reason"] == "first_selection" and p["last_reject"] == "no_qualifying_width"
    assert pt.step(p, d(30, 10, 6), selector=lambda p, n: _sel(wall=395.0, sell=395.0, buy=397.0)) == "enter"
    ev = [e for e in p["events"] if e["action"] == "selection"]
    assert ev[-1]["wall_change_reason"].startswith("spot_move")
    assert p["state"] == "entered" and p["k_sell"] == 395.0 and p["k_buy"] == 397.0 and p["sell"].endswith("C390000.US")
    assert p["economics"]["breakeven"] == pytest.approx(395 + 0.4 - 0.032)
    assert pt.step(p, d(30, 10, 11), selector=lambda p, n: 1 / 0) is None                 # 入场后不再选档


def test_dynamic_window_end_skipped_or_missed_and_oi_update_reason():
    p = _dyn()
    pt.step(p, datetime(2026, 9, 30, 10, 1, tzinfo=ET), selector=lambda p, n: _sel(ok=False, sha="a"))
    pt.step(p, datetime(2026, 9, 30, 10, 6, tzinfo=ET), selector=lambda p, n: _sel(ok=False, sha="b"))
    assert p["events"][-1]["wall_change_reason"].startswith("oi_update")
    assert pt.step(p, datetime(2026, 9, 30, 10, 31, tzinfo=ET), selector=lambda p, n: _sel()) == "skipped"
    q = _dyn()
    assert pt.step(q, datetime(2026, 10, 1, 10, 5, tzinfo=ET), selector=lambda p, n: _sel()) == "missed"
    assert pt.validate_spec(_dyn(strike_rule="other")) and pt.validate_spec(_dyn()) is None
