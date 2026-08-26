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
    # 维度一：偏离度
    stretch: float = 3.88
    stretch_pctile: float = 0.91
    ma20: float = 398.18
    atr: float = 7.19
    # 维度二：回撤度
    drawdown: float = -0.19
    drawdown_pct: float = -0.31
    dd_pctile: float = 0.92
    high_n: float = 429.42
    # 合成
    pctile: float = 0.915
    band: str = "强超买"
    regime: str = "牛"
    diverge: str = ""
    ma200: float = 414.09
    edge_pp: float = -0.293
    win_rate: float = 41.0
    n_hist: int = 1225
    t_stat: float = -1.81
    reliable: bool = False


def test_empty_when_no_data():
    assert render_technicals_section(None, None) == ""
    assert render_technicals_section(_Tech(ok=False), _Stretch(ok=False)) == ""
    print("PASS test_empty_when_no_data")


def test_edge_always_carries_t_and_verdict():
    """非中性档给出边缘时，必须同时给出 t 值和显著/不显著判定。"""
    html = render_technicals_section(_Tech(), _Stretch())
    assert "-0.29pp" in html, html[:400]
    assert "t=-1.81" in html
    assert "未达显著" in html and "仅参考" in html
    print("PASS test_edge_always_carries_t_and_verdict")


def test_reliable_cell_marked_significant():
    sr = _Stretch(band="极超卖", pctile=0.02, stretch=-2.9, stretch_pctile=0.03,
                  drawdown=-5.1, drawdown_pct=-8.4, dd_pctile=0.01,
                  edge_pp=1.364, win_rate=65.0, n_hist=471, t_stat=3.16, reliable=True)
    html = render_technicals_section(_Tech(heat="强超卖", heat_score=-5), sr)
    assert "+1.36pp" in html and "t=+3.16" in html and "显著" in html
    assert "未达显著" not in html
    print("PASS test_reliable_cell_marked_significant")


def test_neutral_band_shows_no_fake_edge():
    """中性档是基准桶，不能渲染出 '+0.00pp 边缘' 让人误读成有信号。"""
    sr = _Stretch(band="中性", pctile=0.47, stretch=-0.03, stretch_pctile=0.47,
                  drawdown=-1.2, drawdown_pct=-2.1, dd_pctile=0.47,
                  edge_pp=0.0, win_rate=47.0, n_hist=10274, t_stat=0.0, reliable=False)
    html = render_technicals_section(_Tech(heat="中性", heat_score=0), sr)
    assert "基准桶" in html
    assert "0.00pp" not in html and "+0.00" not in html
    print("PASS test_neutral_band_shows_no_fake_edge")


def test_conflict_banner_direction_matches_signal():
    """过热分喊超卖时，分歧告警必须说'跌得急'，不能说'涨得急'。"""
    sr = _Stretch(band="中性", pctile=0.32, stretch=-0.24, stretch_pctile=0.34,
                  drawdown=-1.1, drawdown_pct=-1.9, dd_pctile=0.30,
                  edge_pp=0.0, win_rate=47.0, n_hist=10274, t_stat=0.0)
    html = render_technicals_section(_Tech(heat="强超卖", heat_score=-4, trend="纠缠"), sr)
    assert "与过热分分歧" in html
    assert "跌得急" in html and "涨得急" not in html

    html2 = render_technicals_section(_Tech(heat="偏超买", heat_score=2), sr)
    assert "涨得急" in html2 and "跌得急" not in html2
    print("PASS test_conflict_banner_direction_matches_signal")


def test_no_conflict_banner_when_agreeing():
    """两者都说极端时不该报分歧。"""
    sr = _Stretch(band="极超买", pctile=0.97, stretch_pctile=0.97, dd_pctile=0.97,
                  edge_pp=-0.416, n_hist=1021, t_stat=-1.17, win_rate=40.0)
    html = render_technicals_section(_Tech(), sr)
    assert "与过热分分歧" not in html
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
    """只有超买超卖读数、没有传统指标时也应出卡（反之亦然）。"""
    a = render_technicals_section(None, _Stretch())
    b = render_technicals_section(_Tech(), None)
    assert "超买超卖" in a and "强超买" in a
    assert "超买超卖" in b and "多头排列" in b
    print("PASS test_renders_with_stretch_only")


def test_both_dimensions_rendered_with_own_pctiles():
    """两个维度必须各自带分位显示——分歧时读者要能看出是哪一维在说话。"""
    sr = _Stretch(stretch=-0.17, stretch_pctile=0.34, ma20=712.16, atr=8.41,
                  drawdown=-4.51, drawdown_pct=-5.07, dd_pctile=0.06, high_n=748.65,
                  pctile=0.20, band="偏超卖", regime="牛",
                  edge_pp=0.369, win_rate=55.0, n_hist=2250, t_stat=1.36)
    html = render_technicals_section(_Tech(heat="强超卖", heat_score=-4), sr)
    assert "偏离度" in html and "回撤度" in html
    assert "34%" in html and "6%" in html, "两维分位都要显示"
    assert "-0.17" in html and "-5.07%" in html
    assert "合并分位" in html and "20%" in html
    print("PASS test_both_dimensions_rendered_with_own_pctiles")


def test_diverge_banner_rendered():
    """两维分歧时必须出黄条告警；无分歧时不出。"""
    msg = "回撤深但偏离度不深：约为两维都超卖时的四成"
    sr = _Stretch(diverge=msg, pctile=0.20, band="偏超卖",
                  stretch_pctile=0.34, dd_pctile=0.06,
                  edge_pp=0.369, win_rate=55.0, n_hist=2250, t_stat=1.36)
    BANNER = "⚠️ <b>两维分歧</b>"          # 只认告警条本身；说明文案里也会提到这个词
    html = render_technicals_section(None, sr)
    assert BANNER in html and "四成" in html
    assert BANNER not in render_technicals_section(None, _Stretch(diverge=""))
    print("PASS test_diverge_banner_rendered")


def test_winrate_labelled_not_as_probability():
    """必须写明「跑赢率」不是「上涨概率」——否则 65% 会被读成六成半会涨。"""
    html = render_technicals_section(_Tech(), _Stretch())
    assert "跑赢率" in html
    assert "绝对方向准确率" in html
    print("PASS test_winrate_labelled_not_as_probability")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
