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


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """选档取回时刻 = max(请求时刻, _clock())。测试里的请求时刻是写死的 2026-09-30 等日期；若用真实时钟，
    真实时间一过这些日期的入场窗口，所有自动选档测试都会变成 skipped。固定成远古时刻 → 取回时刻 = 请求时刻；
    需要模拟慢取回的测试自己再 monkeypatch。"""
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2000, 1, 1, tzinfo=ET))


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



def test_pick_protective_width_near_target_and_liquidity():
    """用户批次：卖腿固定，保护腿宽度 5 左右「看成交」——离 5 最近、并列取更宽；无 ask 的跳过。"""
    Q = lambda b, a: {"bid": b, "ask": a, "error": ""}
    quotes = {380: Q(4.4, 4.6), 385: Q(1.8, 1.9), 384: Q(2.1, 2.2), 386: Q(1.5, 1.6), 390: Q(0.5, 0.6)}
    r = pt.pick_protective("C", 380, 5, [384, 385, 386, 390], quotes, 1, 3.2)
    assert r["ok"] and r["buy"] == 385 and r["credit"] == 2.5 and r["economics"]["max_loss_usd"] == 253.2
    quotes[385] = {"bid": 1.8, "ask": None, "error": ""}
    r = pt.pick_protective("C", 380, 5, [384, 385, 386, 390], quotes, 1, 3.2)
    assert r["buy"] == 386 and r["tried"][0] == {"buy": 385, "reject": "buy_no_ask_or_crossed"}   # 4 与 6 并列 → 更宽
    assert not pt.pick_protective("C", 380, 5, [390], quotes, 1, 3.2)["ok"]                          # 宽度 10 超出 5±1


def test_prep_selection_before_window_does_not_enter():
    p = _dyn(prep_from_et="09:40")
    a = pt.step(p, datetime(2026, 9, 30, 9, 45, tzinfo=ET), selector=lambda p, n: _sel())
    assert a == "prep" and p["state"] == "planned" and p["events"][-1]["action"] == "prep_selection"
    assert pt.step(p, datetime(2026, 9, 30, 10, 1, tzinfo=ET), selector=lambda p, n: _sel()) == "enter"
    assert [e for e in p["events"] if e["action"] == "selection"][-1]["wall_change_reason"] == "unchanged"


def test_close_value_missing_buy_bid_is_conservative_and_labelled():
    p = {"sell": "S", "buy": "B"}
    assert pt.close_value({"S": {"bid": 1.0, "ask": 1.1, "error": ""}, "B": {"bid": 0.4, "ask": 0.5, "error": ""}}, p) == (0.7, None)
    v, why = pt.close_value({"S": {"bid": 1.0, "ask": 1.1, "error": ""}, "B": {"bid": None, "ask": 0.02, "error": ""}}, p)
    assert v == 1.1 and "按 0 计" in why
    assert pt.close_value({"S": {"bid": 1.2, "ask": 1.1, "error": ""}, "B": {"bid": 0.4, "ask": 0.5, "error": ""}}, p) is None


def test_manual_close_user_initiated(tmp_path, monkeypatch):
    p = _entered(); p["events"] = p["events"]
    load = _env(tmp_path, monkeypatch, [{"id": "M", "execution": "模拟", "paper": p}])
    r = pt.manual_close("M", "用户：平掉", datetime(2026, 9, 30, 11, 0, tzinfo=ET), depth=q(1.0, 1.1, 0.5, 0.6))
    st = load()["M"]["paper"]
    assert r["ok"] and st["state"] == "closed_manual" and st["close_value"] == 0.6
    assert st["pnl_usd"] == round((0.41 - 0.6) * 100 - 3.2, 2) and st["events"][-1]["user_note"] == "用户：平掉"
    assert not pt.manual_close("M", "再平", depth=q(1.0, 1.1, 0.5, 0.6))["ok"]            # 已平仓不能再平



def test_selection_returned_after_window_is_skipped_not_backdated(monkeypatch):
    """Codex 031：10:29 开始选档、10:35 才取回报价 → 不能记 10:29 入场。"""
    p = _dyn()
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 30, 10, 35, tzinfo=ET))
    assert pt.step(p, datetime(2026, 9, 30, 10, 29, tzinfo=ET), selector=lambda p, n: _sel()) == "skipped"
    assert "selection_returned_after_window" in p["skip_reason"] and p.get("entered_at") is None
    sel = [e for e in p["events"] if e["action"] == "selection"][-1]
    assert sel["requested_at"].startswith("2026-09-30T10:29") and sel["returned_at"].startswith("2026-09-30T10:35")
    q = _dyn()
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 30, 10, 3, tzinfo=ET))
    assert pt.step(q, datetime(2026, 9, 30, 10, 1, tzinfo=ET), selector=lambda p, n: _sel()) == "enter"
    assert q["entered_at"].startswith("2026-09-30T10:03")                          # 入场时刻 = 取回时刻


def test_eight_slots_independent_and_revision_cannot_change_slot(tmp_path, monkeypatch):
    """Codex 031：2 批 × 2 品种 × 2 到期 = 8 个槽位互不阻挡；修订不得换到期来绕过锁定。"""
    ths = []
    for b in ("rule", "user"):
        for u in ("GLD.US", "SLV.US"):
            for e in ("2026-10-05", "2026-10-16"):
                ths.append({"id": f"{b}-{u[:3]}-{e[5:7]}{e[8:]}", "execution": "模拟",
                            "paper": _p(batch=b, underlying=u, expiry=e, side="P")})
    load = _env(tmp_path, monkeypatch, ths)
    pt.tick(at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03))
    st = load()
    assert all(v["paper"]["state"] == "entered" for v in st.values())               # 8 个槽位都能入场
    new = {"id": "rule-GLD-1005-r2", "execution": "模拟",
           "paper": _p(batch="rule", underlying="GLD.US", expiry="2026-10-16", side="P", continuation_of="rule-GLD-1005")}
    r = pt.register_revision(new, at(29, 10, 30))
    assert not r["ok"] and "不得改变仓位槽位" in r["why"]


def test_fixed_path_short_needs_bid_long_needs_ask():
    """Codex 031：固定档位也按腿校验——保护腿没有 bid 可以入场；交叉报价不行。"""
    p = _p()
    dq = lambda syms: {"S": {"bid": 1.44, "ask": 1.6, "error": ""}, "B": {"bid": None, "ask": 1.03, "error": ""}}
    assert pt.step(p, at(29, 10, 6), depth=dq) == "enter"
    p2 = _p()
    dq2 = lambda syms: {"S": {"bid": 1.44, "ask": 1.3, "error": ""}, "B": {"bid": 0.9, "ask": 1.0, "error": ""}}
    assert pt.step(p2, at(29, 10, 6), depth=dq2) == "retry"


def test_manual_close_outside_rth_becomes_request_then_executes(tmp_path, monkeypatch):
    p = _entered()
    load = _env(tmp_path, monkeypatch, [{"id": "M", "execution": "模拟", "paper": p}])
    r = pt.manual_close("M", "用户：平掉", datetime(2026, 9, 29, 20, 0, tzinfo=ET), depth=q(1.0, 1.1, 0.5, 0.6))
    assert r["pending"] and load()["M"]["paper"]["state"] == "entered"
    pt.tick(datetime(2026, 9, 30, 9, 35, tzinfo=ET), depth=q(1.0, 1.2, 0.5, 0.6))
    st = load()["M"]["paper"]
    assert st["state"] == "closed_manual" and st["close_value"] == 0.7 and st["events"][-1]["user_note"] == "用户：平掉"


def test_claude_exit_rule_take_profit_and_time_exit():
    p = _entered(); p.update(exit_rule="claude-exit-v1", expiry="2026-10-16", batch="rule_dynamic")
    assert pt.step(p, at(30, 9, 46), depth=q(0.2, 0.25, 0.1, 0.12)) == "take_profit"    # 0.25−0.10=0.15 ≤ 0.5×0.41
    assert p["state"] == "closed_tp"
    p2 = _entered(); p2.update(exit_rule="claude-exit-v1", expiry="2026-10-16", batch="rule_dynamic")
    r = pt.step(p2, datetime(2026, 10, 13, 9, 46, tzinfo=ET), depth=q(0.5, 0.55, 0.2, 0.25))
    assert r == "time_exit" and p2["state"] == "closed_time"                             # DTE=3
    p3 = _entered(); p3["expiry"] = "2026-10-16"                                          # 用户批次：没有 exit_rule，不自动出场
    assert pt.step(p3, at(30, 9, 46), depth=q(0.2, 0.25, 0.1, 0.12)) == "mark" and p3["state"] == "entered"


# —— 借记价差（用户 2026-09-30 原油：「牛市看涨价差……干脆就买方」）——

def _debit(**kw):
    """牛市看涨价差：买 144C（buy/k_buy）、卖 154C（sell/k_sell）。"""
    return _p(**{"underlying": "USO.US", "expiry": "2026-10-30", "side": "C", "structure": "debit",
                 "k_sell": 154.0, "k_buy": 144.0, "stop_mult": None, "min_credit_ratio": None,
                 "entry_date": "2026-09-30", "entry_window_et": ["10:00", "10:30"], **kw})


def test_debit_economics_and_direction_check():
    e = pt.economics("C", 154, 144, -3.5, 1, 3.2)
    assert e["structure"] == "debit" and e["debit"] == 3.5
    assert e["max_loss_usd"] == 353.2 and e["max_gain_usd"] == 646.8 and e["breakeven"] == pytest.approx(147.532)
    assert pt.economics("P", 140, 150, -4.0, 1, 3.2)["breakeven"] == pytest.approx(150 - 4 - 0.032)    # 熊市看跌价差
    assert pt.validate_spec(_debit()) is None
    assert "腿方向" in pt.validate_spec(_debit(k_sell=140.0))                  # 声明借记、腿却是贷记方向
    assert "腿方向" in pt.validate_spec(_p(side="C", k_sell=154.0, k_buy=144.0))   # 未声明借记（默认贷记）
    assert pt.validate_spec(_debit(stop_mult=2.0))                            # 借记止损倍数只能 (0,1) 或 None
    assert pt.validate_spec(_debit(structure="x"))


def test_debit_entry_marks_no_auto_stop_and_settles():
    p = _debit()
    # 卖腿(S=154C) bid 2.1、买腿(B=144C) ask 5.6 → 付 3.5
    assert pt.step(p, at(30, 10, 2), depth=q(2.1, 2.3, 5.4, 5.6)) == "enter"
    assert p["entry_credit"] == -3.5 and p["stop_value"] is None and p["economics"]["max_loss_usd"] == 353.2
    p["mark_slots_et"] = ["15:45"]
    assert pt.step(p, at(30, 15, 46), depth=q(0.2, 0.3, 0.8, 0.9)) == "mark" and p["state"] == "entered"   # 大跌也不自动止损
    assert p["events"][-1]["value"] == pytest.approx(0.3 - 0.8)
    for close, pnl in ((160.0, (10 - 3.5) * 100 - 3.2), (149.0, (5 - 3.5) * 100 - 3.2), (140.0, -353.2)):
        x = _debit(); pt.step(x, at(30, 10, 2), depth=q(2.1, 2.3, 5.4, 5.6))
        sc = lambda u, d, c=close: {"close": c, "source": "t", "bar_date": d.isoformat()}
        assert pt.step(x, datetime(2026, 10, 30, 16, 21, tzinfo=ET), session_close=sc) == "settle"
        assert x["pnl_usd"] == pytest.approx(pnl)
    t = {"paper": x}; pt._outcome(t); assert t["outcome"] == "错"
    y = _debit(); pt.step(y, at(30, 10, 2), depth=q(2.1, 2.3, 5.4, 5.6))
    pt.step(y, datetime(2026, 10, 30, 16, 21, tzinfo=ET), session_close=lambda u, d: {"close": 170.0, "source": "t"})
    t = {"paper": y}; pt._outcome(t); assert t["outcome"] == "对"                    # 到期全额价内 = 最大收益


def test_debit_debit_outside_width_is_anomaly_and_optional_stop():
    p = _debit()
    assert pt.step(p, at(30, 10, 2), depth=q(0.1, 0.2, 10.5, 10.6)) == "retry" and "quote_anomaly" in p["last_reject"]
    s = _debit(stop_mult=0.5)
    pt.step(s, at(30, 10, 2), depth=q(2.1, 2.3, 5.4, 5.6))
    assert s["stop_value"] == -1.75
    s["mark_slots_et"] = ["15:45"]
    assert pt.step(s, at(30, 15, 46), depth=q(0.2, 0.3, 1.5, 1.6)) == "stop"        # 持有价值 1.2 ≤ 1.75


def test_pick_debit_atm_and_short_leg():
    Q = lambda b, a: {"bid": b, "ask": a, "error": ""}
    assert pt.atm_strike(143.35, [142.0, 143.0, 144.0], side="C") == 143.0
    assert pt.atm_strike(143.5, [143.0, 144.0], side="C") == 143.0 and pt.atm_strike(143.5, [143.0, 144.0], side="P") == 144.0
    quotes = {143.0: Q(5.8, 6.0), 153.0: Q(2.3, 2.4), 152.0: Q(2.6, 2.7), 154.0: Q(2.0, 2.1)}
    r = pt.pick_debit("C", 143.0, 10, [152.0, 153.0, 154.0], quotes, 1, 3.2)
    assert r["ok"] and (r["buy"], r["sell"]) == (143.0, 153.0) and r["debit"] == 3.7 and r["credit"] == -3.7
    assert r["economics"]["max_loss_usd"] == 373.2
    quotes[153.0] = Q(None, 2.4)
    r = pt.pick_debit("C", 143.0, 10, [152.0, 153.0, 154.0], quotes, 1, 3.2)
    assert r["sell"] == 154.0 and r["tried"][0]["reject"] == "sell_no_bid_or_crossed"          # 9 与 11 并列 → 更宽
    assert not pt.pick_debit("C", 143.0, 10, [152.0], {143.0: Q(5.8, None)}, 1, 3.2)["ok"]       # 买腿无 ask


def test_debit_rule_spec_and_slot_separate_from_credit():
    d = _debit(k_sell=None, k_buy=None, strike_rule="user-debit-atm-v1", target_width=10.0, batch="user_subjective")
    assert pt.validate_spec(d) is None
    assert pt.validate_spec({**d, "structure": "credit"})                     # 借记规则只配借记结构
    assert pt.validate_spec(_dyn(structure="debit", stop_mult=None))          # 借记不能用墙规则
    c = {"paper": {**d, "structure": "credit"}}
    assert pt.slot_key({"paper": d}) != pt.slot_key(c) and len(pt.slot_key(c)) == 4


def test_debit_dynamic_entry_uses_selection():
    d = _debit(k_sell=None, k_buy=None, sell=None, buy=None, strike_rule="user-debit-atm-v1", target_width=10.0)
    sel = {"ok": True, "rule": "user-debit-atm-v1", "sell": 153.0, "buy": 143.0, "credit": -3.7, "debit": 3.7,
           "economics": pt.economics("C", 153.0, 143.0, -3.7, 1, 3.2), "nearest_wall": None,
           "inputs": {"params_hash": "atm+target_width=10.0",
                      "symbols": {"153.0": "USO261030C153000.US", "143.0": "USO261030C143000.US"},
                      "quotes": {"153.0": {"bid": 2.3, "ask": 2.4}, "143.0": {"bid": 5.8, "ask": 6.0}}}}
    assert pt.step(d, at(30, 10, 1), selector=lambda p, n: sel) == "enter"
    assert d["k_buy"] == 143.0 and d["k_sell"] == 153.0 and d["buy"].endswith("C143000.US")
    assert d["entry_credit"] == -3.7 and d["stop_value"] is None


def test_weekend_marks_are_offhours_only():
    """日历失效时 session 仍会在工作日 tick；状态机自身也不在周末执行止损（周六 09:46 只估值）。"""
    p = _entered()
    p["expiry"] = "2026-10-16"
    sat = datetime(2026, 10, 3, 9, 46, tzinfo=ET)
    assert pt.step(p, sat, depth=q(1.5, 1.7, 0.7, 0.8)) == "mark"
    assert p["state"] == "entered" and p["events"][-1]["action"] == "mark_offhours"


def test_write_journal_accepts_tuples_in_selection(tmp_path, monkeypatch):
    """选档结果里的 walls 是元组；旧的回读比对用对象相等，元组≠列表 → 每次 tick 都失败（2026-09-30 ET 09:40–09:52）。"""
    import json
    monkeypatch.setattr(pt, "JOURNAL", tmp_path / "journal.json")
    j = {"theses": [{"id": "x", "paper": {"events": [{"selection": {"walls": [(400.0, 17445)]}}]}}]}
    pt._write_journal(j)
    assert json.loads((tmp_path / "journal.json").read_text("utf-8"))["theses"][0]["paper"]["events"][0]["selection"]["walls"] == [[400.0, 17445]]


# —— Codex 032 R1：所有执行路径按报价取回时刻判断资格；R3：出场规则只授权给规则批 ——

def test_fixed_entry_returned_after_window_is_skipped(monkeypatch):
    p = _p()
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 29, 10, 25, tzinfo=ET))   # 窗口 10:00–10:20
    assert pt.step(p, at(29, 10, 19), depth=q(1.44, 1.6, 0.95, 1.03)) == "skipped"
    assert p.get("entered_at") is None and "quote_returned_after_window" in p["skip_reason"]
    p2 = _p()
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 29, 10, 7, tzinfo=ET))
    assert pt.step(p2, at(29, 10, 6), depth=q(1.44, 1.6, 0.95, 1.03)) == "enter"
    assert p2["entered_at"].startswith("2026-09-29T10:07") and p2["events"][-1]["requested_at"].startswith("2026-09-29T10:06")


def test_exit_returned_after_close_only_marks(monkeypatch):
    p = _entered(); p.update(mark_slots_et=["15:45"], expiry="2026-10-16")
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 29, 16, 1, tzinfo=ET))   # 15:59 请求、16:01 取回
    assert pt.step(p, at(29, 15, 59), depth=q(1.5, 1.7, 0.7, 0.8)) == "mark"             # 已到止损线也不执行
    assert p["state"] == "entered" and p["events"][-1]["action"] == "mark_offhours"


def test_close_request_kept_when_quote_fails_or_returns_after_close(monkeypatch):
    p = _entered(); p["close_request"] = {"at": "2026-09-29T20:00:00-04:00", "note": "平"}
    assert pt.step(p, at(30, 10, 0), depth=lambda s: {}) == "retry" and p.get("close_request")      # 报价不可用：请求保留
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 30, 16, 2, tzinfo=ET))
    assert pt.step(p, at(30, 15, 59), depth=q(1.5, 1.7, 0.7, 0.8)) == "retry" and p.get("close_request")
    assert p["state"] == "entered"


def test_user_batch_with_exit_rule_never_auto_exits():
    p = _entered(); p.update(exit_rule="claude-exit-v1", expiry="2026-10-05", batch="user_subjective")
    r = pt.step(p, datetime(2026, 10, 2, 9, 46, tzinfo=ET), depth=q(0.2, 0.25, 0.1, 0.12))
    assert r == "exit_rule_config_error" and p["state"] == "entered"
    assert pt.step(p, datetime(2026, 10, 2, 15, 46, tzinfo=ET), depth=q(0.2, 0.25, 0.1, 0.12)) == "mark"
    assert p["state"] == "entered"
    planned = _p(exit_rule="claude-exit-v1", batch="user_subjective")
    assert "未授权" in pt.validate_spec(planned)
    assert "未知出场规则" in pt.validate_spec(_p(exit_rule="x", batch="rule_dynamic"))
    assert pt.validate_spec(_p(exit_rule="claude-exit-v1", batch="rule_dynamic")) is None


def test_manual_close_quote_failure_or_late_return_keeps_request(tmp_path, monkeypatch):
    p = _entered()
    load = _env(tmp_path, monkeypatch, [{"id": "M", "execution": "模拟", "paper": p}])
    r = pt.manual_close("M", "平", datetime(2026, 9, 30, 11, 0, tzinfo=ET), depth=lambda s: {})
    st = load()["M"]["paper"]
    assert r["pending"] and st["state"] == "entered" and st["close_request"]["note"] == "平"
    p2 = _entered()
    load = _env(tmp_path, monkeypatch, [{"id": "N", "execution": "模拟", "paper": p2}])
    monkeypatch.setattr(pt, "_clock", lambda: datetime(2026, 9, 30, 16, 1, tzinfo=ET))
    r = pt.manual_close("N", "平", datetime(2026, 9, 30, 15, 59, tzinfo=ET), depth=q(1.0, 1.1, 0.5, 0.6))
    assert r["pending"] and load()["N"]["paper"]["state"] == "entered"


def test_tick_uses_per_position_request_time(tmp_path, monkeypatch):
    """整批 tick：排在后面的仓位按自己的请求时刻判断窗口（032 R1）。"""
    a, b = _p(), _p()
    b["expiry"] = "2026-10-01"                                  # 不同槽位，互不阻挡
    load = _env(tmp_path, monkeypatch, [{"id": "A", "execution": "模拟", "paper": a},
                                        {"id": "B", "execution": "模拟", "paper": b}])
    clock = iter([datetime(2026, 9, 29, 10, 10, tzinfo=ET), datetime(2026, 9, 29, 10, 10, 5, tzinfo=ET),
                  datetime(2026, 9, 29, 10, 25, tzinfo=ET), datetime(2026, 9, 29, 10, 25, 5, tzinfo=ET)])
    monkeypatch.setattr(pt, "_clock", lambda: next(clock))
    out = pt.tick(datetime(2026, 9, 29, 10, 9, tzinfo=ET), depth=q(1.44, 1.6, 0.95, 1.03))
    st = load()
    assert st["A"]["paper"]["state"] == "entered"
    assert st["B"]["paper"]["state"] == "missed" and st["B"]["paper"].get("entered_at") is None   # B 请求时已过窗口，不倒填成 10:09
    assert "A:enter" in out and "B:missed" in out


def test_paper_has_its_own_scheduler_and_session_no_longer_ticks():
    """Codex 032 O1：模拟仓独立调度（60 秒），session 不再调用，避免两个调度器同时主控。"""
    root = Path(__file__).resolve().parents[1]
    sess = (root / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert "paper_tick\n" not in sess and "&& paper_tick" not in sess and "then paper_tick" not in sess
    sh = (root / "scripts" / "paper_tick.sh").read_text("utf-8")
    assert 'lockf -t 0' in sh and 'PAPER_SCHED="$SCHED"' in sh and 'SCHED="paper-launchd-v1"' in sh
    assert ".paper_alive" in sh and ".status_paper.json" in sh and "FAILURE_" in sh
    assert 'PDIR="data/paper"' in sh and 'LOG="$PDIR/' in sh               # 含仓位 id 的日志只进私有目录
    pl = (root / "scripts" / "launchd" / "com.yuhaodoong.undertow.paper.plist").read_text("utf-8")
    assert "<integer>60</integer>" in pl and "paper_tick.sh" in pl


def test_explicit_judgment_id_spans_slots_and_revisions_keep_it(tmp_path, monkeypatch):
    """Codex 032 R4：同一观点跨到期计一个判断；不同观点各自计数；修订沿用判断身份、不能改；成交锁不变。"""
    a = _th("A", "rule", judgment_id="J1"); b = _th("B", "rule", judgment_id="J1"); c = _th("C", "rule", judgment_id="J2")
    b["paper"]["expiry"] = "2026-10-16"; c["paper"]["expiry"] = "2026-10-23"
    j = {"theses": [a, b, c]}
    assert pt.judgment_id(j, a) == pt.judgment_id(j, b) == "J1" and pt.judgment_id(j, c) == "J2"
    assert pt.judgment_id(j, _th("L", "rule")) == "L"                        # 旧记录无显式身份：退回修订链根，不推断
    _env(tmp_path, monkeypatch, [a])
    r = pt.register_revision(_th("A2", "rule", cont="A", judgment_id="J9"))
    assert not r["ok"] and "判断身份" in r["why"]
    r = pt.register_revision(_th("A3", "rule", cont="A"))
    assert r["ok"]
    import json
    st = {t["id"]: t for t in json.loads(pt.JOURNAL.read_text())["theses"]}
    assert st["A3"]["paper"]["judgment_id"] == "J1"


def test_missing_protective_bid_with_real_ask_never_triggers_exit():
    """2026-10-05 GLD 10/16 380/385C 假止损：385C 瞬时无买价（卖价 3.2），按 0 计估值 5.3 ≥ 止损 4.9 被平。"""
    p = _entered(); p.update(mark_slots_et=["15:45"], expiry="2026-10-16")
    S = lambda syms: {"S": {"bid": 1.5, "ask": 1.6, "error": None}, "B": {"bid": None, "ask": 0.9, "error": None}}
    assert pt.step(p, at(29, 15, 46), depth=S) == "retry"                          # 估值 1.6 ≥ 止损 0.82，但不执行
    assert p["state"] == "entered" and p["events"][-1]["action"] == "mark_incomplete"
    assert pt.step(p, at(29, 15, 47), depth=q(1.5, 1.6, 0.85, 0.9)) == "mark"      # 同一时点窗口内重取到完整报价 → 正常估值 0.75 < 0.82
    W = lambda syms: {"S": {"bid": 1.5, "ask": 1.6, "error": None}, "B": {"bid": None, "ask": 0.05, "error": None}}
    p2 = _entered(); p2.update(mark_slots_et=["15:45"], expiry="2026-10-16")
    assert pt.step(p2, at(29, 15, 46), depth=W) == "stop"                          # 保护腿确实一文不值 → 仍按 0 计执行


# —— 2026-10-06 用户：「恢复，但是要严查错误……这种错误不要影响模拟仓」——

def Q2(s, b):
    return lambda syms: {"S": {"error": None, **s}, "B": {"error": None, **b}}


def test_out_of_range_value_or_wide_leg_never_auto_exits_but_manual_allows_wide():
    p = _entered(); p.update(mark_slots_et=["15:45"], expiry="2026-10-16")      # 宽 2，止损 0.82
    assert pt.step(p, at(29, 15, 46), depth=Q2({"bid": 2.4, "ask": 2.6}, {"bid": 0.1, "ask": 0.2})) == "retry"
    assert p["events"][-1]["action"] == "mark_incomplete" and "越出" in p["events"][-1]["why"]    # 2.5 > 宽 2
    assert pt.step(p, at(29, 15, 47), depth=Q2({"bid": 0.5, "ask": 1.6}, {"bid": 0.6, "ask": 0.7})) == "retry"
    assert "过宽" in p["events"][-1]["why"] and p["state"] == "entered"                          # 卖腿买卖差 1.1
    assert pt.exit_problem({"S": {"bid": 0.5, "ask": 1.6}, "B": {"bid": 0.6, "ask": 0.7}}, p, 1.0, None, automatic=False) is None


def test_debit_long_leg_missing_bid_with_value_is_not_a_valuation():
    d = _debit(); pt.step(d, at(30, 10, 2), depth=q(2.1, 2.3, 5.4, 5.6))
    d["mark_slots_et"] = ["15:45"]
    r = pt.step(d, at(30, 15, 46), depth=Q2({"bid": 4.55, "ask": 4.75}, {"bid": None, "ask": 9.25}))
    assert r == "retry" and d["events"][-1]["action"] == "mark_incomplete"                        # 10/1 USO 那种 +5.05 的假估值


def test_void_exit_only_for_bad_data_and_keeps_history(tmp_path, monkeypatch):
    bad = _entered(); bad.update(k_sell=380.0, k_buy=385.0, side="C", expiry="2026-10-16", batch="user_subjective")
    good = _entered(); good.update(expiry="2026-10-16")
    load = _env(tmp_path, monkeypatch, [{"id": "BAD", "execution": "模拟", "paper": bad}, {"id": "GOOD", "execution": "模拟", "paper": good}])
    import json
    j = json.loads(pt.JOURNAL.read_text())
    b, g = j["theses"][0]["paper"], j["theses"][1]["paper"]
    qb = {"S": {"bid": 5.2, "ask": 5.3}, "B": {"bid": None, "ask": 3.2}}
    b.update(state="closed_stop", close_value=5.3, pnl_usd=-288.2, closed_at="t", exit_assumption="buy_bid_missing")
    b["events"] += [{"at": "t", "action": "mark", "quotes": qb, "value": 5.3}, {"at": "t", "action": "stop", "value": 5.3}]
    g.update(state="closed_stop", close_value=1.0, pnl_usd=-62.2, closed_at="t")
    g["events"] += [{"at": "t", "action": "mark", "quotes": {"S": {"bid": 1.4, "ask": 1.45}, "B": {"bid": 0.45, "ask": 0.5}}, "value": 1.0},
                    {"at": "t", "action": "stop", "value": 1.0}]
    pt._write_journal(j)
    assert not pt.void_exit("GOOD", "恢复", "x")["ok"]                                     # 真实止损不能恢复
    r = pt.void_exit("BAD", "恢复，但是要严查错误", "保护腿瞬时无买价")
    st = load()["BAD"]
    assert r["ok"] and st["paper"]["state"] == "entered" and st["outcome"] == "未验证"
    assert st["paper"]["voided_exits"][0]["pnl_usd"] == -288.2 and st["paper"]["events"][-1]["action"] == "exit_voided"
    assert any(json.loads(l)["action"] == "exit_voided" for l in (tmp_path / "ledger.jsonl").read_text().splitlines())


def test_early_exit_records_hold_to_expiry_counterfactual():
    """用户 2026-10-07：提前平仓的仓位到期后补记「假设持有到期」盈亏，便于评估提前平仓规则。"""
    p = _entered(); p.update(mark_slots_et=["09:45"], expiry="2026-10-05", exit_rule="claude-exit-v1", batch="rule_dynamic")
    assert pt.step(p, datetime(2026, 10, 2, 9, 46, tzinfo=ET), depth=q(0.5, 0.55, 0.2, 0.25)) == "time_exit"
    actual = p["pnl_usd"]
    sc = lambda u, d: {"close": 376.0, "source": "t"} if d == date(2026, 10, 5) else None
    assert pt.hold_to_expiry(p, datetime(2026, 10, 5, 16, 0, tzinfo=ET), session_close=sc) is None      # 到期日收盘前不算
    assert pt.hold_to_expiry(p, datetime(2026, 10, 5, 16, 21, tzinfo=ET), session_close=sc) == "hold_to_expiry"
    h = p["hold_to_expiry"]
    assert h["pnl_usd"] == round(0.41 * 100 - 3.2, 2) and h["actual_pnl_usd"] == actual        # 376 在卖腿 375 上方 → 全额
    assert h["early_exit_effect_usd"] == round(actual - h["pnl_usd"], 2) and p["state"] == "closed_time"   # 实际状态不变
    assert pt.hold_to_expiry(p, datetime(2026, 10, 6, 10, 0, tzinfo=ET), session_close=sc) is None       # 只记一次
    s = _p(); assert pt.hold_to_expiry(s, datetime(2026, 10, 6, 10, 0, tzinfo=ET), session_close=sc) is None  # 未提前了结的不记
