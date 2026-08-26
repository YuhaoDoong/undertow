"""新仓纯净度（转化率 = |ΔOI| ÷ 成交量）与超买超卖标签的确定性测试。

纯净度要守住的三件事：
  1. 同一个 ΔOI，成交量不同 → 可靠度必须不同（这正是引入该指标的理由）
  2. 转化率 > 1 在物理上不可能（每张成交最多产生一张 OI）→ 必须判「存疑」，
     绝不能因为比值大就当成最干净的新仓（那恰好是反的：成交量没统计全）
  3. 无成交量数据时返回 None / 「未知」，不许猜
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.flow import (
    PURITY_CLEAN, PURITY_IMPLAUSIBLE, PURITY_MIXED, FlowChange,
    purity_label, purity_reliability,
)
from undertow.report.html import stretch_pill
from datetime import date


def _fc(d_oi, vol, **kw):
    base = dict(expiry=date(2026, 9, 18), strike=414.0, kind="P",
                prev_oi=1126, curr_oi=1126 + d_oi, d_oi=d_oi, delta=-0.115,
                prev_iv=0.25, curr_iv=0.28, d_iv_pp=3.0, adj_iv_pp=3.19,
                curr_volume=vol, moneyness=-0.03, bias="bearish",
                judgment="买方保护", on_wall="", note="")
    base.update(kw)
    return FlowChange(**base)


def test_conversion_ratio_math():
    assert abs(_fc(202, 203).oi_conversion - 202 / 203) < 1e-9
    assert abs(_fc(-247, 3029).oi_conversion - 247 / 3029) < 1e-9   # 减仓也用绝对值
    assert _fc(100, 0).oi_conversion is None                        # 无成交 → 不猜
    print("PASS test_conversion_ratio_math")


def test_same_doi_different_reliability():
    """引入该指标的核心理由：同样 +200 手，含义可以天差地别。"""
    clean = _fc(200, 205)      # 成交几乎全变成新仓
    dirty = _fc(200, 4000)     # 大量日内换手
    assert purity_reliability(clean.oi_conversion) == "高"
    assert purity_reliability(dirty.oi_conversion) == "低"
    assert purity_label(clean.oi_conversion) == "干净新仓"
    assert purity_label(dirty.oi_conversion) == "多为日内换手"
    print("PASS test_same_doi_different_reliability")


def test_ratio_above_one_is_flagged_not_praised():
    """转化率 > 1 物理上不可能 → 判「存疑」，不是「高」。"""
    weird = _fc(-961, 333)     # 2.89，实盘真实出现过（GLD 375P）
    r = weird.oi_conversion
    assert r > PURITY_IMPLAUSIBLE
    assert purity_reliability(r) == "存疑", purity_reliability(r)
    assert "口径错配" in purity_label(r)
    assert purity_reliability(r) != "高"
    print("PASS test_ratio_above_one_is_flagged_not_praised")


def test_thresholds_ordered():
    assert 0 < PURITY_MIXED < PURITY_CLEAN < 1.0 < PURITY_IMPLAUSIBLE
    print("PASS test_thresholds_ordered")


def test_label_and_reliability_agree():
    """标签与可靠度必须同步分档，不能出现「干净新仓/低」这种自相矛盾。"""
    pairs = {"干净新仓": "高", "掺换手": "中", "多为日内换手": "低"}
    for r in (0.05, 0.29, 0.30, 0.50, 0.69, 0.70, 0.95, 1.05):
        lab, rel = purity_label(r), purity_reliability(r)
        assert pairs.get(lab) == rel, f"{r}: {lab} vs {rel}"
    assert purity_label(None) == "" and purity_reliability(None) == "未知"
    print("PASS test_label_and_reliability_agree")


# ── 超买超卖标签 ───────────────────────────────────────────────────

@dataclass
class _SR:
    ok: bool = True
    band: str = "偏超买"
    pctile: float = 0.87
    stretch_pctile: float = 0.88
    dd_pctile: float = 0.85
    reliable: bool = False
    diverge: str = ""


def test_pill_marks_significance():
    """显著与否必须压进标签本身——否则看到「强超买」就会脑补方向。"""
    weak = stretch_pill(_SR(band="强超买", reliable=False))
    strong = stretch_pill(_SR(band="极超卖", pctile=0.03, reliable=True))
    assert "强超买 ~" in weak, weak
    assert "极超卖 ✅" in strong, strong
    print("PASS test_pill_marks_significance")


def test_pill_neutral_has_no_mark():
    """中性档不带 ✅/~ ——它本来就没有方向可言。"""
    p = stretch_pill(_SR(band="中性", pctile=0.5, reliable=False))
    assert "中性" in p and "~" not in p and "✅" not in p
    print("PASS test_pill_neutral_has_no_mark")


def test_pill_shows_both_dimensions():
    """完整版必须给出两维分位，否则分歧时看不出是哪一维在说话。"""
    p = stretch_pill(_SR(stretch_pctile=0.32, dd_pctile=0.04, pctile=0.18))
    assert "偏离 32%" in p and "回撤 4%" in p and "18%" in p
    print("PASS test_pill_shows_both_dimensions")


def test_pill_compact_for_index():
    """索引页用紧凑版：只留档位 + 显著标记 + 合并分位。"""
    p = stretch_pill(_SR(band="偏超卖", pctile=0.18), compact=True)
    assert "偏超卖" in p and "18%" in p
    assert "偏离" not in p and "回撤" not in p
    print("PASS test_pill_compact_for_index")


def test_pill_flags_divergence():
    p = stretch_pill(_SR(diverge="回撤深但偏离度不深"))
    assert "两维分歧" in p
    assert "两维分歧" not in stretch_pill(_SR(diverge=""))
    print("PASS test_pill_flags_divergence")


def test_pill_empty_without_data():
    assert stretch_pill(None) == ""
    assert stretch_pill(_SR(ok=False)) == ""
    print("PASS test_pill_empty_without_data")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
