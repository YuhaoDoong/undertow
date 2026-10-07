"""模拟仓台账统计（纯函数）：分类、按批次汇总、被修订替代不算失败、早期判断单列。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import paper_book as pb  # noqa: E402


def T(tid, state, batch="rule", pnl=None, reason=None, cont=None, ml=100.0):
    p = {"state": state, "batch": batch, "side": "C", "expiry": "2026-10-16", "k_sell": 390.0, "k_buy": 392.0,
         "entry_credit": 0.5, "economics": {"max_loss_usd": ml, "max_gain_usd": 46.8}, "pnl_usd": pnl, "skip_reason": reason,
         "events": []}
    if cont:
        p["continuation_of"] = cont
    return {"id": tid, "execution": "模拟", "instrument": "GLD", "paper": p}


def test_classify_and_summarize():
    ths = [T("a", "settled", pnl=46.8), T("b", "closed_stop", pnl=-60.0), T("c", "entered"),
           T("d", "skipped", reason="credit_low（x）"), T("e", "skipped", reason="superseded_by_revision：由 f 替代"),
           T("f", "planned", cont="e"), T("u", "closed_manual", batch="user", pnl=10.0, ml=250.0),
           {"id": "old", "execution": "模拟", "outcome": "对", "trade_pnl": 174.0}, {"id": "live", "execution": "实盘"}]
    c = pb.classify(ths)
    assert [r["id"] for r in c["closed"]] == ["a", "b", "u"] and [r["id"] for r in c["open"]] == ["c"]
    assert [r["id"] for r in c["not_entered"]] == ["d"] and [r["id"] for r in c["superseded"]] == ["e"]
    assert [r["id"] for r in c["planned"]] == ["f"] and [t["id"] for t in c["legacy_judgments"]] == ["old"]
    sm = pb.summarize(c)
    assert sm["rule"]["wins"] == 1 and sm["rule"]["losses"] == 1 and sm["rule"]["pnl_usd"] == -13.2
    assert sm["rule"]["max_loss_sum_usd"] == 200.0 and sm["rule"]["open"] == 1 and sm["rule"]["not_entered"] == 1
    assert sm["user"]["pnl_per_max_loss"] == 0.04 and sm["_reasons"] == {"credit_low": 1}
    md, html = pb.render("2026-09-30", c, sm, {}, "now")
    assert "| rule | 2 | 1 / 1 / 0 | $-13.20 |" in md and "<table>" in html and "被修订替代的版本（1，不算失败）" in md


def test_scheduled_in_daily_before_set_e_and_session_after_close():
    root = Path(__file__).resolve().parents[1]
    d = (root / "scripts" / "daily_update.sh").read_text("utf-8")
    import re
    i = d.index("PB_OUT=$(python3 scripts/paper_book.py")
    last = [m.group(1) for m in re.finditer(r"(?m)^set ([+-])e\s*$", d[:i])][-1]
    assert last == "+"                                                            # 在 set +e 区间内，失败不中断 daily
    h = (root / "scripts" / "session_hooks.sh").read_text("utf-8")
    assert h.index("paper_book() {") < h.index("then paper_book; fi")



def test_legacy_backfill_summary_and_render():
    rows = [{"id": "l1", "state": "settled", "pnl_usd_recorded": 27.0, "pnl_usd_with_fee_3_20": 23.8, "max_loss_usd_recorded": 73.0,
             "judgment": "q", "k_sell": 60, "k_buy": 61, "side": "C", "expiry": "2026-09-21", "underlying": "SLV", "entry_credit": 0.27},
            {"id": "l2", "state": "settled_theoretical", "pnl_usd_recorded": 1.0, "pnl_usd_with_fee_3_20": -2.2,
             "max_loss_usd_recorded": 142.0, "judgment": "s", "legs": [{"side": "buy", "sym": "P58"}], "underlying": "SLV"},
            {"id": "l3", "state": "fill_not_recorded", "judgment": "x"}]
    ls = pb.summarize_legacy(rows)
    assert ls["closed"] == 2 and ls["wins"] == 2 and ls["pnl_usd"] == 28.0 and ls["pnl_usd_with_fee"] == 21.6
    assert ls["incomplete"] == ["l3"] and ls["judgments"] == 2
    c = pb.classify([])
    md, _ = pb.render("d", c, pb.summarize(c), {}, "now", legacy=rows, shadow=["影子账 prospective：共 1 行"])
    assert "旧模拟仓（9/29 之前" in md and "fill_not_recorded" in md and "> 影子账 prospective" in md


# —— Codex 032 R2 / R5 / R7 ——

def test_missing_values_are_unknown_not_flat_and_assumed_exit_separate():
    import math
    nan = T("n", "settled", pnl=float("nan"))
    none = T("x", "settled", pnl=None); none["paper"]["economics"] = {}
    noml = T("m", "settled", pnl=20.0); noml["paper"]["economics"] = {}
    zero = T("z", "settled", pnl=0.0)                                         # 真实打平：仍算正式
    rev = T("r", "settled", pnl=30.0); rev["paper"]["result_under_review"] = True
    asm = T("s", "closed_tp", pnl=25.0); asm["paper"]["exit_assumption"] = "buy_bid_missing→按 0 计"
    ok = T("o", "settled", pnl=40.0)
    c = pb.classify([nan, none, noml, zero, rev, asm, ok])
    sm = pb.summarize(c)["rule"]
    assert sm["closed"] == 2 and sm["flat"] == 1 and sm["wins"] == 1 and sm["pnl_usd"] == 40.0
    assert sm["unknown"] == 3 and sm["under_review"] == 1 and sm["assumed"] == 1 and sm["assumed_pnl_usd"] == 25.0
    assert sm["max_loss_sum_usd"] == 200.0 and sm["pnl_per_max_loss"] == 0.2 and sm["complete"] is False
    md, _ = pb.render("2026-10-02", c, pb.summarize(c), {}, "now")              # 不崩、缺失显示「—」
    assert "（结果未知）" in md and "估算退出" in md and "（不完整）" in md and "| 3 / 1 / 1（$+25.00） |" in md


def test_legacy_missing_values_go_to_incomplete():
    rows = [{"id": "a", "state": "settled", "pnl_usd_recorded": None, "max_loss_usd_recorded": 50.0, "judgment": "j"},
            {"id": "b", "state": "settled", "pnl_usd_recorded": 10.0, "pnl_usd_with_fee_3_20": 6.8, "max_loss_usd_recorded": 90.0,
             "judgment": "k"}]
    s = pb.summarize_legacy(rows)
    assert s["closed"] == 1 and s["flat"] == 0 and s["incomplete"] == ["a"] and s["max_loss_sum_usd"] == 90.0


def test_open_valuation_multiplies_qty():
    r = T("q2", "entered"); r["paper"].update(qty=2, entry_credit=1.0,
                                             economics={"max_loss_usd": 806.4, "max_gain_usd": 193.6, "fee_total_usd": 6.4})
    r["paper"]["events"] = [{"action": "mark", "at": "2026-10-02T15:45", "value": 0.5}]
    c = pb.classify([r])
    md, _ = pb.render("2026-10-02", c, pb.summarize(c), {}, "now")
    assert "$+93.60" in md and "$+43.60" not in md


def test_atomic_write_keeps_previous_on_readback_failure(tmp_path, monkeypatch):
    f = tmp_path / "paper.md"
    pb._atomic_write(f, "v1")
    assert f.read_text("utf-8") == "v1"
    real = pb.Path.read_text
    monkeypatch.setattr(pb.Path, "read_text", lambda self, *a, **k: "corrupt" if self.name.endswith(".tmp") else real(self, *a, **k))
    import pytest
    with pytest.raises(RuntimeError):
        pb._atomic_write(f, "v2")
    assert real(f, "utf-8") == "v1" and not list(tmp_path.glob("*.tmp"))


def test_early_exit_vs_hold_to_expiry_section():
    a = T("t1", "closed_time", pnl=-10.2); a["paper"]["hold_to_expiry"] = {"pnl_usd": 11.8, "close": 55.13, "early_exit_effect_usd": -22.0}
    b = T("t2", "closed_tp", pnl=20.0)                                        # 尚未到期补记
    c = T("t3", "settled", pnl=40.0)
    cl = pb.classify([a, b, c])
    md, _ = pb.render("2026-10-07", cl, pb.summarize(cl), {}, "now")
    assert "$+11.80（到期收 55.13）；提前了结影响 $-22.00" in md and "待到期后补记" in md and "（到期结算，不适用）" in md
    assert "| rule | 规则时间出场 | 1（1） | $-10.20 | $+11.80 | $-22.00 |" in md


def test_audit_view_lists_voided_exit_without_double_counting():
    r = T("g", "entered"); r["paper"]["voided_exits"] = [{"state": "closed_stop", "pnl_usd": -288.2,
        "exit_event": {"action": "stop", "at": "2026-10-05T19:45"}, "assessment": {"data_integrity": ["B 缺 bid（缺失输入）"]},
        "user_authorization": "恢复，但是要严查错误", "observation_gap": {"missed_mark_slots": []}}]
    c = pb.classify([r]); sm = pb.summarize(c)
    md, _ = pb.render("2026-10-07", c, sm, {}, "now")
    assert "审计视图：被作废的退出" in md and "$-288.20" in md and "B 缺 bid" in md
    assert sm["rule"]["pnl_usd"] == 0.0 and sm["rule"]["open"] == 1                 # 不进合计、仍按在场计
