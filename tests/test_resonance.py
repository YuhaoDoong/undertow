"""共振层 + 卖方口径「不突破率」的确定性测试（函数式，不依赖 pytest / 网络）。

锚定的核心事实：
  1. 共振层【未校准】——任何输出都必须带上警示，且不得暗示可据此放大仓位
  2. 只有【极超卖/强超卖】与【极超买/强超买】参与共振；偏超卖/偏超买触发率各 15%
     且实测无方向价值（偏超卖 5 日涨率 54.3% < 基准 55.5%），必须被排除
  3. 不突破率的两种口径不可混淆：路径口径必然 ≤ 终值口径
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.resonance import (
    CAVEAT, OB_BANDS, OS_BANDS, assess_resonance, render_md, snapshot_row,
)
from undertow.analyze import stretch_backtest as sb


@dataclass
class _SR:
    ok: bool = True
    band: str = "极超卖"
    regime: str = "牛"
    stretch: float = -2.9
    stretch_pctile: float = 0.03
    drawdown: float = -5.1
    dd_pctile: float = 0.02
    pctile: float = 0.025


def test_resonance_states():
    """四象限：共振看多/共振看空/背离/单边。"""
    assert assess_resonance("偏多", _SR(band="极超卖")).state == "共振看多"
    assert assess_resonance("偏空", _SR(band="极超买")).state == "共振看空"
    assert assess_resonance("偏空", _SR(band="极超卖")).state == "背离"
    assert assess_resonance("偏多", _SR(band="极超买")).state == "背离"
    assert assess_resonance("中性", _SR(band="极超卖")).state == "仅超卖"
    assert assess_resonance("偏多", _SR(band="中性")).state == "仅结构"
    assert assess_resonance("中性", _SR(band="中性")).state == "无信号"
    print("PASS test_resonance_states")


def test_weak_bands_excluded_from_resonance():
    """偏超卖/偏超买不得参与共振——它们触发率各 15% 且实测无方向价值。"""
    assert "偏超卖" not in OS_BANDS and "偏超买" not in OB_BANDS
    assert assess_resonance("偏多", _SR(band="偏超卖")).state == "仅结构"
    assert assess_resonance("偏空", _SR(band="偏超买")).state == "仅结构"
    print("PASS test_weak_bands_excluded_from_resonance")


def test_caveat_always_present():
    """未校准的东西必须每次都带警示，且必须写明「不作为入场理由」。"""
    for bias in ("偏多", "偏空", "中性"):
        for band in ("极超卖", "极超买", "中性"):
            rr = assess_resonance(bias, _SR(band=band))
            assert rr.caveat == CAVEAT
            assert "不作为入场理由" in rr.caveat and "不据此放大仓位" in rr.caveat
    print("PASS test_caveat_always_present")


def test_overbought_resonance_is_explicitly_hedged():
    """共振看空的文案必须自带反证——超买后 5 日下跌率 42.2% < 基准 44.2%。"""
    rr = assess_resonance("偏空", _SR(band="极超买"))
    assert "无效" in rr.headline and "42.2%" in rr.headline
    assert "不构成看空依据" in rr.headline
    print("PASS test_overbought_resonance_is_explicitly_hedged")


def test_only_extreme_oversold_flagged_strong():
    """strong 只给【极超卖】——它是唯一通过方向准确率检验的档位。"""
    assert assess_resonance("偏多", _SR(band="极超卖")).strong
    assert not assess_resonance("偏多", _SR(band="强超卖")).strong
    assert not assess_resonance("偏空", _SR(band="极超买")).strong
    print("PASS test_only_extreme_oversold_flagged_strong")


def test_snapshot_row_leaves_forward_blank():
    """落盘行必须留空 forward_*，由日后校准脚本回填——不能在当天写入任何未来数据。"""
    rr = assess_resonance("偏多", _SR())
    row = snapshot_row("qqq", "2026-08-26", rr, _SR(), spot=710.72)
    for k in ("forward_5d", "forward_10d", "forward_20d"):
        assert row[k] is None, f"{k} 不该在落盘时有值"
    assert row["instrument"] == "qqq" and row["spot"] == 710.72
    # 两维读数都要存下来，否则日后没法按维度切
    assert row["stretch_pctile"] is not None and row["dd_pctile"] is not None
    print("PASS test_snapshot_row_leaves_forward_blank")


def test_graceful_without_stretch():
    rr = assess_resonance("偏多", None)
    assert not rr.ok and "跳过" in rr.caveat
    assert "跳过" in render_md(rr)
    print("PASS test_graceful_without_stretch")


# ── 卖方口径：不突破率 ──────────────────────────────────────────────

def _ramp(n=1200, drift=0.05, amp=6.0, period=50):
    import math
    return [100.0 + i * drift + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def _panel():
    c = _ramp()
    h = [x * 1.008 for x in c]
    l = [x * 0.992 for x in c]
    return h, l, c


def test_containment_path_never_exceeds_end():
    """路径口径必然 ≤ 终值口径：期间没碰过 ⇒ 到期也一定在里面，反之不然。"""
    h, l, c = _panel()
    s = sb.build_samples("T", h, l, c)
    acc = sb.containment_stats("T", h, l, c, s, horizon=5)
    assert acc, "无结果"
    for band, r in acc.items():
        for k_end, k_path in (("up_end", "up_path"), ("dn_end", "dn_path")):
            for d in r[k_end]:
                assert r[k_path][d] <= r[k_end][d] + 1e-9, \
                    f"{band} {d}σ 路径({r[k_path][d]:.1f}) > 终值({r[k_end][d]:.1f})"
    print("PASS test_containment_path_never_exceeds_end")


def test_containment_monotonic_in_distance():
    """行权价越远，不突破率必然越高。"""
    h, l, c = _panel()
    acc = sb.containment_stats("T", h, l, c, sb.build_samples("T", h, l, c), horizon=5)
    for band, r in acc.items():
        for k in ("up_end", "up_path", "dn_end", "dn_path"):
            ds = sorted(r[k])
            vals = [r[k][d] for d in ds]
            for a, b in zip(vals, vals[1:]):
                assert b >= a - 1e-9, f"{band} {k} 随距离非单调: {list(zip(ds, vals))}"
    print("PASS test_containment_monotonic_in_distance")


def test_containment_rates_are_percentages():
    h, l, c = _panel()
    acc = sb.containment_stats("T", h, l, c, sb.build_samples("T", h, l, c), horizon=5)
    for r in acc.values():
        for k in ("up_end", "up_path", "dn_end", "dn_path"):
            for v in r[k].values():
                assert 0.0 <= v <= 100.0, v
        assert r["atr_pct"] > 0
    print("PASS test_containment_rates_are_percentages")


def test_merge_containment_weights_by_n():
    """多品种合并必须按样本数加权，不能简单平均。"""
    a = {"中性": {"n": 100, "atr_pct": 1.0,
                  "up_end": {1.0: 60.0}, "up_path": {1.0: 40.0},
                  "dn_end": {1.0: 70.0}, "dn_path": {1.0: 50.0}}}
    b = {"中性": {"n": 300, "atr_pct": 2.0,
                  "up_end": {1.0: 80.0}, "up_path": {1.0: 60.0},
                  "dn_end": {1.0: 90.0}, "dn_path": {1.0: 70.0}}}
    m = sb.merge_containment([a, b])["中性"]
    assert m["n"] == 400
    assert abs(m["up_end"][1.0] - 75.0) < 1e-9, m["up_end"]      # (60*100+80*300)/400
    assert abs(m["atr_pct"] - 1.75) < 1e-9
    print("PASS test_merge_containment_weights_by_n")


def test_containment_md_states_edge_not_level():
    """表格必须点明「提升才是 edge」——绝对值高只是因为虚值远，最易误读。"""
    h, l, c = _panel()
    acc = sb.containment_stats("T", h, l, c, sb.build_samples("T", h, l, c), horizon=5)
    md = sb.render_containment_md(acc, horizon=5)
    assert "提升才是 edge" in md
    assert "卖 call" in md and "卖 put" in md
    assert "终值" in md and "路径" in md
    print("PASS test_containment_md_states_edge_not_level")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
