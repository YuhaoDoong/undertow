"""交易日记的确定性测试（函数式，不依赖 pytest / 网络）。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.soul.journal import (Trade, JournalEntry, capture_trades, load_journal,
                                   save_journal, render_entry_md, render_journal_md)

_EX = [{"time": "2026-08-25T13:47:00Z", "symbol": "SLV260826P61000.US", "side": "Buy",
        "quantity": "4", "price": "0.6"},
       {"time": "2026-08-24T10:00:00Z", "symbol": "OLD.US", "side": "Sell",
        "quantity": "1", "price": "1.0"}]
_CF = [{"time": "2026-08-25T13:47:00Z", "flow_name": "Option Buy Transaction",
        "balance": "-240.00", "symbol": "SLV260826P61000.US"},
       {"time": "2026-08-25T10:45:00Z", "flow_name": "Option Buy Fee",
        "balance": "-2.42", "symbol": "SLV260826P61000.US"}]


def test_capture_filters_day_and_fills_amount_fee():
    ts = capture_trades(_EX, _CF, day="2026-08-25")
    assert len(ts) == 1, ts                    # 只抓当日
    t = ts[0]
    assert t.symbol.endswith("P61000.US") and t.side == "buy" and t.qty == 4
    assert t.amount == -240.0 and t.fee == 2.42, t
    print(f"PASS test_capture_filters_day_and_fills_amount_fee → {t}")


def test_capture_without_cashflow():
    ts = capture_trades(_EX, None, day="2026-08-25")
    assert len(ts) == 1 and ts[0].amount == 0.0 and ts[0].fee == 0.0
    print("PASS test_capture_without_cashflow")


def test_roundtrip_and_render():
    e = JournalEntry(date="2026-08-25", title="测试", trades=capture_trades(_EX, _CF, day="2026-08-25"),
                     realized_pnl=-164.0, fees=2.42, net_assets_before=630.0, net_assets_after=439.68,
                     analysis="复盘内容", verdict="盖棺定论", mood="难受", tags=["主动止损"])
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "j.json"
        save_journal([e], fp)
        got = load_journal(fp)
    assert len(got) == 1 and got[0].verdict == "盖棺定论" and got[0].mood == "难受"
    md = render_entry_md(got[0])
    assert "盖棺定论" in md and "心情" in md and "-164.00" in md and "439.68" in md, md
    print("PASS test_roundtrip_and_render")


def test_empty_journal():
    assert "暂无记录" in render_journal_md([])
    print("PASS test_empty_journal")


def test_thesis_stats_small_sample_warns():
    """样本不足时明确警告『不足以区分运气与能力』。"""
    from undertow.soul.journal import Thesis, thesis_stats
    ts=[Thesis(id="a",date="2026-08-01",instrument="SLV",direction="看涨",outcome="对",trade_pnl=-80.0),
        Thesis(id="b",date="2026-08-02",instrument="GLD",direction="看跌",outcome="错",trade_pnl=+20.0)]
    st=thesis_stats(ts)
    assert st["scored"]==2 and st["enough_sample"] is False
    assert "不足以区分运气与能力" in st["note"], st["note"]
    assert st["hit_rate"]==0.5
    print("PASS test_thesis_stats_small_sample_warns")


def test_thesis_divergence_flagged():
    """判断对但亏钱 / 判断错但赚钱 → 计入背离（这是关键诊断）。"""
    from undertow.soul.journal import Thesis, thesis_stats, render_theses_md
    ts=[Thesis(id="a",date="2026-08-01",instrument="SLV",direction="看涨",outcome="对",trade_pnl=-80.0),
        Thesis(id="b",date="2026-08-02",instrument="GLD",direction="看跌",outcome="错",trade_pnl=+20.0),
        Thesis(id="c",date="2026-08-03",instrument="SLV",direction="看涨",outcome="对",trade_pnl=+50.0)]
    st=thesis_stats(ts)
    assert st["diverge_count"]==2, st          # a(对但亏) 与 b(错但赚)
    assert abs(st["diverge_pnl"]-(-60.0))<1e-9, st
    md=render_theses_md(ts)
    assert "背离" in md and "问题不在判断力" in md, md
    print(f"PASS test_thesis_divergence_flagged → 背离 {st['diverge_count']} 笔 {st['diverge_pnl']:+.0f}")


def test_unscored_thesis_not_counted():
    """未验证的判断不计入命中率样本。"""
    from undertow.soul.journal import Thesis, thesis_stats
    st=thesis_stats([Thesis(id="x",date="2026-08-24",instrument="SLV",direction="看涨")])
    assert st["total"]==1 and st["scored"]==0 and st["hit_rate"] is None
    print("PASS test_unscored_thesis_not_counted")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
