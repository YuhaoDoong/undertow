"""研报技术面卡片的渲染测试（函数式，不依赖 pytest / 网络）。

锚定三件事：
  1. 未校准的裸标签不得单独出现——每个非中性档必须带上 t 值与显著性判定
  2. 过热分与拉伸度分歧时必须告警，且措辞方向要跟着信号走（超卖别说"涨得急"）
  3. 缺数据时返回空串，不能把半张卡片塞进报告
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.report.html import render_technicals_section


@dataclass
class _Tech:
    ok: bool = True
    trend: str = "多头排列"
    heat: str = "强超买"
    heat_score: int = 5
    rsi6: float = 83.0
    rsi14: float = 80.0
    kdj: tuple = (90.0, 88.0, 95.0)
    cci: float = 140.0
    boll: tuple = (100.0, 110.0, 90.0, 0.89)
    macd: tuple = (1.0, 0.5, 0.5)


@dataclass
class _Stretch:
    ok: bool = True
    stretch: float = 3.88
    pctile: float = 0.88
    band: str = "偏超买"
    regime: str = "牛"
    atr: float = 77.76
    edge_pp: float = -0.206
    win_rate: float = 42.0
    n_hist: int = 3080
    t_stat: float = -1.56
    reliable: bool = False


def test_empty_when_no_data():
    assert render_technicals_section(None, None) == ""
    assert render_technicals_section(_Tech(ok=False), _Stretch(ok=False)) == ""
    print("PASS test_empty_when_no_data")


def test_edge_always_carries_t_and_verdict():
    """非中性档给出边缘时，必须同时给出 t 值和显著/不显著判定。"""
    html = render_technicals_section(_Tech(), _Stretch())
    assert "-0.21pp" in html, html[:400]
    assert "t=-1.56" in html
    assert "未达显著" in html and "仅参考" in html
    print("PASS test_edge_always_carries_t_and_verdict")


def test_reliable_cell_marked_significant():
    sr = _Stretch(band="极超卖", pctile=0.02, stretch=-2.9,
                  edge_pp=1.059, win_rate=62.0, n_hist=735, t_stat=2.42, reliable=True)
    html = render_technicals_section(_Tech(heat="强超卖", heat_score=-5), sr)
    assert "+1.06pp" in html and "t=+2.42" in html and "显著" in html
    assert "未达显著" not in html
    print("PASS test_reliable_cell_marked_significant")


def test_neutral_band_shows_no_fake_edge():
    """中性档是基准桶，不能渲染出 '+0.00pp 边缘' 让人误读成有信号。"""
    sr = _Stretch(band="中性", pctile=0.47, stretch=-0.03,
                  edge_pp=0.0, win_rate=47.0, n_hist=9712, t_stat=0.0, reliable=False)
    html = render_technicals_section(_Tech(heat="中性", heat_score=0), sr)
    assert "基准桶" in html
    assert "0.00pp" not in html and "+0.00" not in html
    print("PASS test_neutral_band_shows_no_fake_edge")


def test_conflict_banner_direction_matches_signal():
    """过热分喊超卖时，分歧告警必须说'跌得急'，不能说'涨得急'。"""
    sr = _Stretch(band="中性", pctile=0.32, stretch=-0.24,
                  edge_pp=0.0, win_rate=47.0, n_hist=9712, t_stat=0.0)
    html = render_technicals_section(_Tech(heat="强超卖", heat_score=-4, trend="纠缠"), sr)
    assert "两个指标分歧" in html
    assert "跌得急" in html and "涨得急" not in html

    html2 = render_technicals_section(_Tech(heat="偏超买", heat_score=2), sr)
    assert "涨得急" in html2 and "跌得急" not in html2
    print("PASS test_conflict_banner_direction_matches_signal")


def test_no_conflict_banner_when_agreeing():
    """两者都说极端时不该报分歧。"""
    sr = _Stretch(band="极超买", pctile=0.97, edge_pp=-0.321,
                  n_hist=1515, t_stat=-1.66, win_rate=43.0)
    html = render_technicals_section(_Tech(), sr)
    assert "两个指标分歧" not in html
    print("PASS test_no_conflict_banner_when_agreeing")


def test_heat_marked_as_downgraded():
    """过热分必须显式标注已降级，否则读者会把它当同等权重的第二意见。"""
    html = render_technicals_section(_Tech(), _Stretch())
    assert "已降级为参考" in html
    print("PASS test_heat_marked_as_downgraded")


def test_boundary_caveats_present():
    """'仅日线成立' 与 '超买侧不显著' 是硬边界，任何一版渲染都不能丢。"""
    html = render_technicals_section(_Tech(), _Stretch())
    assert "仅日线成立" in html
    assert "1H/4H" in html
    assert "不可读作" in html
    print("PASS test_boundary_caveats_present")


def test_renders_with_stretch_only():
    """只有拉伸度、没有传统指标时也应出卡（反之亦然）。"""
    a = render_technicals_section(None, _Stretch())
    b = render_technicals_section(_Tech(), None)
    assert "超买超卖" in a and "偏超买" in a
    assert "超买超卖" in b and "多头排列" in b
    print("PASS test_renders_with_stretch_only")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
