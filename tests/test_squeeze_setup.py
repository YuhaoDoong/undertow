"""逼空蓄势判据的确定性测试（函数式，不依赖 pytest）。

锚定：高集中度净空堆积 + 投机资金同步押多（双方净变化近似对冲、在高位一起建新仓），
应识别为【逼空蓄势·上行风险】，纠正"把方向性对冲净空误读成分歧/看空"的稀释；
集中度不极端或只有单边加空时，不得误报。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.core.models import CategoryChange, CotReport, TraderCategory
from undertow.analyze.positioning import analyze
from undertow.analyze.signals import generate_signals, net_bias


def _mk(d, *, mm_net, swap_net, mm_ch, swap_ch, c8_short, c8_long=25.0):
    """构造一期 COT：只关心 managed_money / swap_dealers 的净仓、周变化与前8大集中度。"""
    flat = TraderCategory(0, 0, 0)
    return CotReport(
        instrument="test", report_date=d, market_name="TEST",
        open_interest=400_000, open_interest_change=0,
        managed_money=TraderCategory(max(mm_net, 0), max(-mm_net, 0), 0),
        other_reportables=flat,
        swap_dealers=TraderCategory(max(swap_net, 0), max(-swap_net, 0), 0),
        producer_merchant=flat, nonreportable=flat,
        changes={"managed_money": mm_ch, "swap_dealers": swap_ch,
                 "other_reportables": CategoryChange(0, 0, 0),
                 "producer_merchant": CategoryChange(0, 0, 0),
                 "nonreportable": CategoryChange(0, 0, 0)},
        conc_net_4_long=c8_long * 0.6, conc_net_4_short=c8_short * 0.6,
        conc_net_8_long=c8_long, conc_net_8_short=c8_short,
    )


def _history(c8_short_last, *, mm_ch, swap_ch):
    """一段历史：前几期净空集中度较低，最后一期抬到 c8_short_last（→ 高历史分位）。"""
    base = date(2026, 6, 2)
    reps = []
    for i, cs in enumerate([40.0, 41.0, 42.0, 43.0, 44.0]):  # 低位铺底 → 末期跳高才是高分位
        reps.append(_mk(date(base.year, base.month, base.day + i * 0 + 1 + i),
                        mm_net=120_000, swap_net=-200_000,
                        mm_ch=CategoryChange(0, 0, 0), swap_ch=CategoryChange(0, 0, 0),
                        c8_short=cs))
    reps.append(_mk(date(2026, 8, 18), mm_net=141_648, swap_net=-228_657,
                    mm_ch=mm_ch, swap_ch=swap_ch, c8_short=c8_short_last))
    return reps


def test_squeeze_fires_on_concentrated_short_wall():
    """复刻黄金 8/18：净空集中度历史高位 + 投机加多 & Swap 加空近似对冲 → 逼空蓄势、综合偏多。"""
    hist = _history(54.8, mm_ch=CategoryChange(long=5961, short=1975, spread=0),
                    swap_ch=CategoryChange(long=-388, short=3564, spread=-1114))
    sigs = generate_signals(analyze(hist))
    codes = {s.code for s in sigs}
    assert "SHORT_SQUEEZE_SETUP" in codes, codes
    assert "SWAP_DIR_SHORT" not in codes, "逼空成立时不得再朴素看空同一批净空"
    sq = next(s for s in sigs if s.code == "SHORT_SQUEEZE_SETUP")
    assert sq.direction == "risk-up", sq.direction
    assert net_bias(sigs) == "偏多", net_bias(sigs)
    print(f"PASS test_squeeze_fires_on_concentrated_short_wall → {net_bias(sigs)}")


def test_no_squeeze_when_concentration_normal():
    """集中度不高（历史分位低、绝对占比 < 45%）→ 不报逼空，退回朴素方向性净空读法。"""
    hist = _history(38.0, mm_ch=CategoryChange(long=5961, short=1975, spread=0),
                    swap_ch=CategoryChange(long=-388, short=3564, spread=-1114))
    codes = {s.code for s in generate_signals(analyze(hist))}
    assert "SHORT_SQUEEZE_SETUP" not in codes, codes
    assert "SWAP_DIR_SHORT" in codes, "集中度不极端时应保留朴素方向性空头压力信号"
    print("PASS test_no_squeeze_when_concentration_normal")


def test_no_squeeze_when_mm_not_pressing_long():
    """空头集中但投机资金没同步押多（净减仓）→ 不构成"高位对赌"，不报逼空。"""
    hist = _history(54.8, mm_ch=CategoryChange(long=-2000, short=3000, spread=0),
                    swap_ch=CategoryChange(long=-388, short=3564, spread=-1114))
    codes = {s.code for s in generate_signals(analyze(hist))}
    assert "SHORT_SQUEEZE_SETUP" not in codes, codes
    print("PASS test_no_squeeze_when_mm_not_pressing_long")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
