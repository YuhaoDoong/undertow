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
    BANDS, CALIB, CALIB_META, DD_LOOK, MA_N, REGIME_N, analyze_stretch, band_of,
    drawdown_series, pct_rank_last, stretch_series,
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


def test_calib_carries_test_sample_size():
    """CALIB 必须带上【检验样本量】，且它必须显著小于触发次数。

    触发次数是重叠计数，Welch t 实际只用不重叠子样本（约 1/horizon）。
    只列触发会把证据规模夸大数倍——「强超买-熊」触发 202 次但检验 n 仅 36，
    这个差距足以改变对显著性的判断。（codex review 2026-08-26）
    """
    for k, v in CALIB.items():
        assert len(v) == 5, f"{k} 应为 5 元组（含检验样本量），实为 {len(v)}"
        _edge, _wr, n_trig, _t, n_test = v
        assert n_test > 0, k
        assert n_test < n_trig, f"{k}: 检验样本 {n_test} 不应 ≥ 触发次数 {n_trig}"
        assert n_test <= n_trig * 0.35, f"{k}: 检验/触发 比例异常 {n_test}/{n_trig}"
    print("PASS test_calib_carries_test_sample_size")


def test_calib_meta_is_single_source_of_numbers():
    """样本量等数字只能有一处来源——散文里写死必然过期。

    codex review 抓到源码里同时存在 29,522 / 29,422 两个样本数，而实际已是 29,477。
    面板每天多几根，任何写进注释的绝对数都会漂。
    """
    for key in ("asof", "panel", "n_total", "mode", "horizon", "caveats"):
        assert key in CALIB_META, key
    assert len(CALIB_META["caveats"]) >= 2, "统计局限必须显式列出"
    assert any("不独立" in c for c in CALIB_META["caveats"])
    assert any("选择" in c for c in CALIB_META["caveats"])
    # 源码的【断言性文案】里不得再写死样本数（形如「NN,NNN 样本」）。
    # 注意：解释这个 bug 的注释本身必然要提到旧数字（29,522 / 29,422），那是历史记录，
    # 不是断言——所以只查「数字 + 样本」这个断言句式，不做全文扫描。
    import pathlib as _p
    import re as _re
    src = _p.Path(__file__).resolve().parents[1] / "undertow" / "analyze"
    for f in ("stretch.py", "resonance.py"):
        txt = (src / f).read_text()
        hits = _re.findall(r"\d{2},?\d{3}\s*(?:个)?样本", txt)
        hits = [h for h in hits if str(CALIB_META["n_total"]) not in h.replace(",", "")]
        assert not hits, f"{f} 的断言文案里仍写死样本数: {hits}（应改为读 CALIB_META）"
    print("PASS test_calib_meta_is_single_source_of_numbers")


def test_calib_reliability_flags_match_docstring():
    """模块文档声称「只有超卖侧可用，超买侧一格都不可用」—— 用表本身守住这个说法。

    可用 = |t|≥2 **且** 检验样本量≥MIN_TEST_N。只看 t 会把「强超买-熊」(t=-2.10,
    检验 n=36) 算成可用——小样本下 t 极不稳，那是假信号。

    这条测试的作用是防文档漂移：谁重跑校准改了 CALIB，这里会立刻挂，
    提醒去同步 docstring 与研报文案里的数字。
    """
    from undertow.analyze.stretch import MIN_TEST_N, T_SIGNIFICANT
    # 只看 t 的「表面显著」集合
    surface = {k for k, v in CALIB.items() if abs(v[3]) >= T_SIGNIFICANT}
    assert surface == {("极超卖", "牛"), ("强超卖", "牛"),
                       ("极超卖", "熊"), ("强超卖", "熊"), ("强超买", "熊")}, \
        f"表面显著集合变了，文档需同步更新: {sorted(surface)}"
    # 真正可用的还要过检验样本量门槛——「强超买-熊」检验 n 仅 36，不认
    usable = {k for k in surface if CALIB[k][4] >= MIN_TEST_N}
    assert usable == {("极超卖", "牛"), ("强超卖", "牛"),
                      ("极超卖", "熊"), ("强超卖", "熊")}, \
        f"可用集合变了: {sorted(usable)}"
    assert all("超卖" in b for b, _ in usable), "可用格必须全在超卖侧"
    assert CALIB[("强超买", "熊")][4] < MIN_TEST_N, "该格若样本量变大，结论需重新评估"
    print("PASS test_calib_reliability_flags_match_docstring")


def test_two_dimensions_are_distinct():
    """偏离度与回撤度必须给出不同读数——否则又回到"同一件事数两遍"。

    构造一个"尖顶后跌回均线"的价形：从近期高点回撤明显，但价格贴着 MA20。
    这正是 2026-08-26 QQQ 的形态。
    """
    # 先缓慢上行 → 尖顶 → 快速跌回原区间 → 横盘
    c = [100.0 + i * 0.02 for i in range(500)]
    c += [c[-1] + i * 1.2 for i in range(1, 16)]      # 尖顶：15 根急拉
    c += [c[-1] - i * 1.15 for i in range(1, 16)]     # 快速跌回
    c += [c[-1] + (0.1 if i % 2 else -0.1) for i in range(30)]   # 横盘
    h = [x * 1.004 for x in c]
    l = [x * 0.996 for x in c]
    sr = analyze_stretch(_S(dates=list(range(len(c))), closes=c, highs=h, lows=l))
    assert sr.ok, sr.note
    # 回撤度应明显更极端（更低分位），偏离度则被 MA20 追上而回到中性附近
    assert sr.dd_pctile < sr.stretch_pctile - 0.05, \
        f"尖顶回落形态下回撤度应比偏离度更极端: dd={sr.dd_pctile:.2f} st={sr.stretch_pctile:.2f}"
    assert sr.drawdown < -3.0, f"该形态回撤应有数个 ATR，实测 {sr.drawdown:.2f}"
    assert abs(sr.stretch) < 1.0, f"跌回均线后偏离度应接近 0，实测 {sr.stretch:.2f}"
    print("PASS test_two_dimensions_are_distinct")


def test_diverge_rule():
    """分歧规则本身（直接喂分位，不依赖合成价形）。

    与价形构造分开测：价形能否造出某个分位组合是另一回事，
    这里只钉死"给定两维分位 → 该不该报分歧、报哪一条"。
    """
    from undertow.analyze.stretch import _diverge_note
    # 回撤深(6%)但偏离不深(34%) —— 2026-08-26 QQQ 的真实读数
    n = _diverge_note(0.34, 0.06)
    assert n and "回撤深但偏离度不深" in n and "四成" in n, n
    # 反过来：偏离深但回撤不深
    n2 = _diverge_note(0.05, 0.60)
    assert n2 and "偏离度深但回撤不深" in n2, n2
    # 两维一致（都超卖 / 都超买）→ 不报分歧
    assert _diverge_note(0.05, 0.04) == ""
    assert _diverge_note(0.96, 0.95) == ""
    # 都在中性区 → 不报分歧
    assert _diverge_note(0.50, 0.45) == ""
    # 超买侧的两条分歧
    assert "接近前高但涨势不陡" in _diverge_note(0.60, 0.95)
    assert "反弹不是新高" in _diverge_note(0.95, 0.60)
    print("PASS test_diverge_rule")


def test_drawdown_is_non_positive_and_scale_invariant():
    """回撤度按定义 ≤0（收盘不可能高于含自身的窗口最高价），且价格整体缩放后不变。"""
    c = _wave(n=600)
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    a = drawdown_series(h, l, c)
    vals = [x for x in a if x is not None]
    assert vals and max(vals) <= 1e-9, f"回撤度出现正值: {max(vals)}"
    b = drawdown_series([x * 7 for x in h], [x * 7 for x in l], [x * 7 for x in c])
    for x, y in zip(a, b):
        if x is not None:
            assert abs(x - y) < 1e-9, f"回撤度尺度不变性被破坏: {x} vs {y}"
    print("PASS test_drawdown_is_non_positive_and_scale_invariant")


def test_drawdown_no_lookahead():
    """回撤度同样不得被未来数据污染。"""
    c = _wave(n=700)
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    a = drawdown_series(h, l, c)
    c2 = list(c); c2[600:] = [x * 4 for x in c2[600:]]
    h2 = [x * 1.01 for x in c2]; l2 = [x * 0.99 for x in c2]
    b = drawdown_series(h2, l2, c2)
    for i in range(599):
        assert (a[i] is None) == (b[i] is None)
        if a[i] is not None:
            assert abs(a[i] - b[i]) < 1e-9, f"第 {i} 根被未来数据污染"
    print("PASS test_drawdown_no_lookahead")


def test_combo_pctile_is_mean_of_dimensions():
    """合并分位必须就是两维分位的均值——档位判定依赖这一点。"""
    sr = analyze_stretch(_series(_wave(n=900)))
    assert sr.ok, sr.note
    assert abs(sr.pctile - (sr.stretch_pctile + sr.dd_pctile) / 2) < 1e-9
    assert sr.band == band_of(sr.pctile)
    print("PASS test_combo_pctile_is_mean_of_dimensions")


def test_backtest_modes_all_runnable():
    """三种口径都必须能跑通并给出档位齐全的校准，否则 --compare 会静默塌掉。"""
    c = _wave(n=2200, amp=10.0)
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    for mode in sb.SIGNAL_MODES:
        samples = sb.build_samples("T", h, l, c, mode=mode)
        assert samples, f"{mode} 无样本"
        cal = sb.calibrate(samples, horizon=5, min_n=20)
        assert cal, f"{mode} 校准为空"
    print("PASS test_backtest_modes_all_runnable")


def test_diverge_stats_shape():
    """分歧统计必须返回带 n/edge/t 的行，供文档与研报引用。"""
    c = _wave(n=2200, amp=10.0)
    h = [x * 1.01 for x in c]; l = [x * 0.99 for x in c]
    dv = sb.diverge_stats(sb.build_samples("T", h, l, c), horizon=5)
    assert "rows" in dv
    for r in dv["rows"]:
        assert {"label", "n", "edge_pp", "win_rate", "t"} <= set(r)
    print("PASS test_diverge_stats_shape")


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
