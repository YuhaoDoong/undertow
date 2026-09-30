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
