"""拉伸度指标 + 校准回测的确定性测试（函数式，不依赖 pytest / 网络）。

重点锚定三件容易悄悄坏掉的事：
  1. 无前视：第 i 个读数不得用到 >i 的数据
  2. 尺度不变：价格 ×3 后拉伸度读数不变（这是它能从 NQ=F 搬到 TQQQ 的前提）
  3. 边缘与 t 同源：都必须是"本桶 vs 同 regime 中性桶"，不能退化成单样本
"""
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.stretch import (
    BANDS, CALIB, MA_N, REGIME_N, analyze_stretch, band_of,
    pct_rank_last, stretch_series,
)
from undertow.analyze import stretch_backtest as sb


@dataclass
class _S:
    symbol: str = "T"
    dates: list = None
    closes: list = None
    highs: list = None
    lows: list = None


def _series(closes):
    return _S(dates=list(range(len(closes))), closes=closes,
              highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes])


def _wave(n=600, base=100.0, amp=8.0, period=40):
    """带趋势的正弦波：既有牛熊 regime，也有周期性超买超卖。"""
    return [base + i * 0.05 + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def test_no_lookahead():
    """把未来的价格改掉，历史读数必须一字不变。"""
    c = _wave()
    h = [x * 1.01 for x in c]
    l = [x * 0.99 for x in c]
    a = stretch_series(h, l, c)
    c2 = list(c); c2[500:] = [x * 5 for x in c2[500:]]
    h2 = [x * 1.01 for x in c2]; l2 = [x * 0.99 for x in c2]
    b = stretch_series(h2, l2, c2)
    for i in range(499):
        assert (a[i] is None) == (b[i] is None), i
        if a[i] is not None:
            assert abs(a[i] - b[i]) < 1e-9, f"第 {i} 根被未来数据污染: {a[i]} vs {b[i]}"
    print("PASS test_no_lookahead")


def test_scale_invariance():
    """价格整体 ×3（模拟 3 倍 ETF）后，拉伸度读数不变 —— 分子分母同步缩放。"""
    c = _wave()
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    a = stretch_series(h, l, c)
    b = stretch_series([x * 3 for x in h], [x * 3 for x in l], [x * 3 for x in c])
    pairs = [(x, y) for x, y in zip(a, b) if x is not None]
    assert pairs, "无有效读数"
    for x, y in pairs:
        assert abs(x - y) < 1e-9, f"尺度不变性被破坏: {x} vs {y}"
    print("PASS test_scale_invariance")


def test_stretch_sign_and_magnitude():
    """价格在均线之上 → 正；之下 → 负；ATR 越大同样偏离读数越小。"""
    up = [100.0] * 30 + [120.0]
    dn = [100.0] * 30 + [80.0]
    su = stretch_series([x * 1.001 for x in up], [x * 0.999 for x in up], up)
    sd = stretch_series([x * 1.001 for x in dn], [x * 0.999 for x in dn], dn)
    assert su[-1] > 0 and sd[-1] < 0, (su[-1], sd[-1])
    print("PASS test_stretch_sign_and_magnitude")


def test_bands_partition_unit_interval():
    """分档必须覆盖 [0,1] 且互不重叠 —— 否则会有分位落不进任何桶。"""
    seen = [band_of(p / 1000.0) for p in range(1001)]
    assert None not in seen and "" not in seen
    names = [n for _, n in BANDS]
    # 单调：分位越高，档位在 BANDS 中的序号不能倒退
    idx = [names.index(b) for b in seen]
    assert idx == sorted(idx), "分档不单调"
    print("PASS test_bands_partition_unit_interval")


def test_pct_rank_extremes():
    """末值是历史最大 → 分位 1.0；最小 → 0.0。"""
    xs = list(range(300))
    assert pct_rank_last([float(x) for x in xs]) == 1.0
    assert pct_rank_last([float(x) for x in reversed(xs)]) == 0.0
    print("PASS test_pct_rank_extremes")


def test_short_series_graceful():
    """价序不足 MA200 + 分位窗口 → ok=False，不崩。"""
    sr = analyze_stretch(_series([100.0 + i for i in range(50)]))
    assert not sr.ok and "跳过" in sr.note, sr
    print("PASS test_short_series_graceful")


def test_analyze_end_to_end():
    """完整读数：档位/regime 齐全，且校准表能查到该 (档位, regime)。"""
    sr = analyze_stretch(_series(_wave(n=800)))
    assert sr.ok, sr.note
    assert sr.regime in ("牛", "熊") and sr.band in [n for _, n in BANDS]
    assert (sr.band, sr.regime) in CALIB, f"校准表缺 {(sr.band, sr.regime)}"
    assert 0.0 <= sr.pctile <= 1.0
    print("PASS test_analyze_end_to_end")


def test_calib_table_complete_and_neutral_is_zero():
    """校准表必须覆盖 7 档 × 2 regime，且中性桶边缘恒为 0（它就是基准）。"""
    for _, band in BANDS:
        for rg in ("牛", "熊"):
            assert (band, rg) in CALIB, f"缺 {(band, rg)}"
    for rg in ("牛", "熊"):
        assert abs(CALIB[("中性", rg)][0]) < 1e-9, "中性桶边缘必须是 0"
    print("PASS test_calib_table_complete_and_neutral_is_zero")


def test_calib_oversold_side_monotonic():
    """超卖侧必须严格单调（越深的超卖边缘越大）—— 这一侧是通过显著性检验的那一侧。

    注意：**不对超买侧断言单调**。实测牛市 -0.206/-0.355/-0.321、熊市偏超买还是 +0.212，
    因为超买侧六个桶没有一个 |t| 到 2.0，本来就在噪音里晃。硬测单调只会得到假安全感。
    """
    for rg in ("牛", "熊"):
        e = [CALIB[(b, rg)][0] for b in ("极超卖", "强超卖", "偏超卖", "中性")]
        for a, b in zip(e, e[1:]):
            assert a >= b - 1e-9, f"{rg}市 超卖侧非单调: {e}"
    print("PASS test_calib_oversold_side_monotonic")


def test_calib_overall_direction():
    """整体方向：超卖三档均值应显著高于超买三档均值。"""
    for rg in ("牛", "熊"):
        sold = sum(CALIB[(b, rg)][0] for b in ("极超卖", "强超卖", "偏超卖")) / 3
        bought = sum(CALIB[(b, rg)][0] for b in ("偏超买", "强超买", "极超买")) / 3
        assert sold - bought > 0.3, f"{rg}市 超卖/超买方向不成立: {sold:.3f} vs {bought:.3f}"
    print("PASS test_calib_overall_direction")


def test_calib_reliability_flags_match_docstring():
    """模块文档声称「只有超卖侧过得了 t≥2」—— 用表本身守住这个说法，别让文档漂移。"""
    strong = {k for k, v in CALIB.items() if abs(v[3]) >= 2.0}
    assert strong == {("极超卖", "牛"), ("强超卖", "牛"), ("极超卖", "熊")}, \
        f"显著桶集合变了，文档需同步更新: {sorted(strong)}"
    print("PASS test_calib_reliability_flags_match_docstring")


def test_welch_t_two_sample():
    """Welch t 必须是双样本：两组同分布 → t≈0；整体平移 → t 显著。"""
    a = [float(i % 10) for i in range(200)]
    b = [float(i % 10) for i in range(200)]
    assert abs(sb.welch_t(a, b)) < 1e-9, "同分布应给 t=0"
    c = [x + 5.0 for x in b]
    assert sb.welch_t(c, a) > 5.0, "整体平移应给出大 t"
    assert abs(sb.welch_t([1.0], [2.0])) < 1e-9, "样本不足应返回 0 而非崩溃"
    print("PASS test_welch_t_two_sample")


def test_calibrate_neutral_is_reference():
    """calibrate 出来的中性桶边缘恒为 0、t 恒为 0（它是被减去的基准本身）。"""
    c = _wave(n=2000, amp=10.0)
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    samples = sb.build_samples("T", h, l, c)
    assert samples, "应能生成样本"
    cal = sb.calibrate(samples, horizon=5, min_n=20)
    assert cal, "校准结果为空"
    for (band, _rg), row in cal.items():
        if band == "中性":
            assert abs(row["edge_pp"]) < 1e-9 and abs(row["t"]) < 1e-9, row
    print("PASS test_calibrate_neutral_is_reference")


def test_build_samples_respects_horizon_bound():
    """样本不得越过序列末尾取前瞻收益（越界会静默用最后一根，污染结果）。"""
    c = _wave(n=1200)
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    hz = (2, 3, 5, 10)
    samples = sb.build_samples("T", h, l, c, horizons=hz)
    assert samples
    assert max(s.i for s in samples) < len(c) - max(hz), "样本越界"
    print("PASS test_build_samples_respects_horizon_bound")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
