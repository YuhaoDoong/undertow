"""事件影响捕捉的确定性测试（纯函数，不依赖网络）。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undertow.analyze.event_impact import (EventSnapshot, InstrumentSnap,
                                           compare, render_compare_md, render_snap_md)


def _snap(label, spot_slv, iv_slv, stale=False, heat=1, fut_slv=None):
    return EventSnapshot(label=label, at=f"2026-08-26T{label[-2:]}:00:00Z",
                         event_name="Core PCE",
                         instruments=[
                             InstrumentSnap(key="silver", display_name="白银", spot=spot_slv,
                                            fut_symbol="SI=F", fut_price=fut_slv,
                                            atm_iv=iv_slv, iv_stale=stale, heat_score=heat),
                             InstrumentSnap(key="gold", display_name="黄金", spot=428.0,
                                            fut_symbol="GC=F", fut_price=4700.0,
                                            atm_iv=0.25, heat_score=5)])


def test_compare_computes_move_and_iv_delta():
    """价格变动% 与 ΔIV(pp) 正确计算，按变动幅度排序。"""
    b, a = _snap("12", 62.0, 0.47, heat=1), _snap("13", 63.24, 0.40, heat=3)
    c = compare(b, a)
    slv = [r for r in c["rows"] if r["key"] == "silver"][0]
    assert abs(slv["move_pct"] - 2.0) < 1e-6, slv          # 62 → 63.24 = +2%
    assert abs(slv["d_iv_pp"] - (-7.0)) < 1e-6, slv        # 47% → 40% = -7pp（IV crush）
    assert c["rows"][0]["key"] == "silver", "变动大的排前"
    print(f"PASS test_compare_computes_move_and_iv_delta → {slv['move_pct']:+.1f}% / {slv['d_iv_pp']:+.1f}pp")


def test_stale_iv_flagged():
    """任一侧 IV 陈旧（盘前）→ 对比里标出，避免误读为『事件未影响波动率』。"""
    b, a = _snap("08", 62.0, 0.47, stale=True), _snap("10", 62.5, 0.47)
    c = compare(b, a)
    slv = [r for r in c["rows"] if r["key"] == "silver"][0]
    assert slv["iv_stale"] is True
    md = render_compare_md(c)
    assert "陈旧" in md, md
    print("PASS test_stale_iv_flagged")


def test_render_snapshot():
    """单份快照渲染：期货价打头、ETF 场次并列、IV 陈旧标注。"""
    md = render_snap_md(_snap("12", 62.0, 0.47, stale=True, fut_slv=69.35))
    assert "白银" in md and "Core PCE" in md
    assert "69.35" in md, "期货价应打头"
    assert "⚠陈旧" in md, "夜盘 IV 应标陈旧"
    print("PASS test_render_snapshot")


def test_no_overlap_graceful():
    b = EventSnapshot(label="b", at="t1", instruments=[
        InstrumentSnap(key="wti", display_name="原油", spot=68.0)])
    a = _snap("13", 63.0, 0.40)
    c = compare(b, a)
    assert c["rows"] == []
    assert "无可对比" in render_compare_md(c)
    print("PASS test_no_overlap_graceful")


def test_futures_is_primary_price_source():
    """有期货价时用期货算变动（连续、实时），并在口径列标明。"""
    b = _snap("12", 62.0, 0.47, fut_slv=69.0)
    a = _snap("13", 62.1, 0.40, fut_slv=70.38)     # ETF 只动0.16%，期货动2%
    c = compare(b, a)
    slv = [r for r in c["rows"] if r["key"] == "silver"][0]
    assert abs(slv["move_pct"] - 2.0) < 1e-6, slv          # 用期货：69→70.38 = +2%
    assert "期货" in slv["price_source"] and "SI=F" in slv["price_source"], slv
    assert slv["etf_before"] == 62.0 and slv["etf_after"] == 62.1   # ETF 仍保留
    print(f"PASS test_futures_is_primary_price_source → {slv['price_source']} {slv['move_pct']:+.1f}%")


def test_falls_back_to_etf_without_futures():
    """无期货价时退回 ETF，并在口径列标出场次。"""
    b, a = _snap("12", 62.0, 0.47), _snap("13", 63.24, 0.40)   # fut_price=None
    c = compare(b, a)
    slv = [r for r in c["rows"] if r["key"] == "silver"][0]
    assert abs(slv["move_pct"] - 2.0) < 1e-6
    assert "ETF" in slv["price_source"], slv
    print("PASS test_falls_back_to_etf_without_futures")


def test_sessions_recorded_separately():
    """ETF 各场次分开记——非盘前时段 pre_market 可能是陈旧值，不能只留 freshest。"""
    i = InstrumentSnap(key="silver", display_name="白银", spot=62.55, spot_kind="夜盘",
                       etf_regular=62.32, etf_overnight=62.55, etf_pre=60.99)
    s = EventSnapshot(label="t", at="x", instruments=[i])
    md = render_snap_md(s)
    assert "62.32" in md and "62.55" in md and "60.99" in md, md   # 三个场次都在
    print("PASS test_sessions_recorded_separately")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
