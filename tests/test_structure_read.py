"""结构读数模块的确定性测试。

这个模块存在的理由：投票层把性质不同的东西（中期持仓/价格位置/历史分位/
IV 推断的主动方）统一翻译成 sign×weight 相加，假矛盾必然产生。
本模块只描述状态，**永不输出方向票**，因此与方向层正交、不会打架。

要守住的五件事：
  1. 噪音与低可靠度腿对汇总贡献**恒为 0**，不是 0.5/0.6
  2. 「高」可靠度目前恒不产生——日快照无法确认逐笔主动方，不许假装
  3. 纯净度 > 1 物理不可能 → 必须降级，不许因比值大就当最干净的新仓
  4. 证伪清单是**三态**：数据不足 ≠ 条件不满足
  5. 防守强度轴与方向正交——防守增强不等于看跌
"""
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze import structure_read as SR
from undertow.analyze.flow import FlowChange


def _ch(kind="P", strike=690.0, d_oi=5000, vol=8000, prev_iv=0.20,
        d_iv_pp=1.0, adj_iv_pp=0.8, delta=-0.25, spread_note=""):
    return FlowChange(expiry=date(2026, 9, 18), strike=strike, kind=kind,
                      prev_oi=1000, curr_oi=1000 + d_oi, d_oi=d_oi, delta=delta,
                      prev_iv=prev_iv, curr_iv=prev_iv + d_iv_pp / 100,
                      d_iv_pp=d_iv_pp, adj_iv_pp=adj_iv_pp, curr_volume=vol,
                      moneyness=-0.04, bias="bearish", judgment="x",
                      on_wall="", note="", weight=1.0, spread_note=spread_note)


def test_noise_and_low_contribute_zero():
    """噪音与低可靠度必须 counts=False —— 不是给个小权重继续投票。"""
    noise = SR.grade_leg(_ch(adj_iv_pp=0.1))                    # 在噪音带内
    low_unknown = SR.grade_leg(_ch(prev_iv=0.0))                # 昨日无 IV
    low_cut = SR.grade_leg(_ch(d_oi=-5000))                     # 减仓
    ok = SR.grade_leg(_ch())
    assert noise.reliability == "噪音" and not noise.counts
    assert low_unknown.reliability == "低" and not low_unknown.counts
    assert low_cut.reliability == "低" and not low_cut.counts
    assert ok.reliability == "中" and ok.counts
    print("PASS test_noise_and_low_contribute_zero")


def test_high_reliability_never_produced():
    """日快照无法确认逐笔主动方，也无前瞻校准 → 不许出现「高」。"""
    grades = {SR.grade_leg(_ch(kind=k, d_oi=o, adj_iv_pp=a, delta=d)).reliability
              for k in ("C", "P") for o in (5000, -5000)
              for a in (-3.0, -0.8, 0.1, 0.8, 3.0) for d in (0.3, -0.3)}
    assert "高" not in grades, f"不该出现「高」，实得 {grades}"
    print("PASS test_high_reliability_never_produced")


def test_purity_above_one_is_downgraded():
    """纯净度 > 1 物理不可能（每张成交最多产生一张 OI）→ 成交量没统计全。

    绝不能因为比值大就当成最干净的新仓——那恰好是反的。
    """
    impossible = SR.grade_leg(_ch(d_oi=1736, vol=117))          # 14.84
    assert impossible.reliability == "低" and not impossible.counts
    assert any("物理不可能" in w for w in impossible.excluded_why)
    churn = SR.grade_leg(_ch(d_oi=500, vol=5000))               # 0.10 换手
    assert churn.reliability == "低"
    print("PASS test_purity_above_one_is_downgraded")


def test_contradictory_iv_direction_is_noise():
    """相对 IV 与 Delta 修正后方向矛盾 = 随市波动被相对化放大 → 噪音。"""
    g = SR.grade_leg(_ch(d_iv_pp=+2.0, adj_iv_pp=-1.5))
    assert g.reliability == "噪音" and not g.counts
    print("PASS test_contradictory_iv_direction_is_noise")


def test_spread_protection_leg_excluded():
    """已识别价差的保护腿方向含义由整体结构决定，不单独计票。"""
    g = SR.grade_leg(_ch(spread_note="熊市看涨价差·长腿(保护)"))
    assert g.reliability == "噪音" and not g.counts
    print("PASS test_spread_protection_leg_excluded")


def test_effective_delta_is_pure_arithmetic():
    """有效 Delta = ΔOI×delta，不依赖任何主动方推断。"""
    g = SR.grade_leg(_ch(d_oi=10_000, delta=-0.25))
    assert abs(g.effective_delta - (-2500.0)) < 1e-6
    print("PASS test_effective_delta_is_pure_arithmetic")


@dataclass
class _VR:
    atm_iv_pp: float = 18.0
    skew25_pp: float = 4.5
    skew10_pp: float = 9.0


@dataclass
class _VS:
    prev: object = None
    curr: object = None
    d_atm_pp: float = 0.0
    d_skew25_pp: float = 0.0
    d_skew10_pp: float = 0.0
    d_spot_pct: float = 0.0


@dataclass
class _FA:
    changes: list
    vol: object
    spot: float = 719.77
    prev_date: str = "2026-08-26"
    curr_date: str = "2026-08-27"
    total_call_volume: int = 500_000
    total_put_volume: int = 450_000


def _fa(changes, **vk):
    return _FA(changes=changes, vol=_VS(prev=_VR(), curr=_VR(), **vk))


def test_checklist_is_three_state_not_boolean():
    """数据不足 ≠ 条件不满足。把"测不了"记成 ❌ 是把无知包装成结论。"""
    r = SR.analyze_structure(_fa([_ch()]), [], [], recent_volumes=None)
    labels = {c[0]: c[1] for c in r.checklist}
    assert labels["成交明显放量"] is None, "无近期均值时必须是 None（测不了）"
    assert "测不了" in r.state_summary or "不下转折结论" in r.state_summary
    assert r.trend_break is False
    print("PASS test_checklist_is_three_state_not_boolean")


def test_trend_break_needs_all_three():
    """趋势转折必须三项同时满足；缺一即不下结论。"""
    r = SR.analyze_structure(
        _fa([_ch()], d_atm_pp=+2.0), [], [], recent_volumes=[100_000] * 5)
    assert r.trend_break is False          # 只满足 ATM 一项
    print("PASS test_trend_break_needs_all_three")


def test_never_emits_direction():
    """本模块**永不**输出方向票——防守强度轴与多空正交。

    ⚠️ 断言的对象是【结论字段】（defense / state_summary），不是整份 Markdown：
    正文里出现"看跌"是合法的，因为那些是**否定句**与证伪条件说明
    （如"远虚 Put 加保护 ≠ 看跌到那个价位"）——那正是本模块要传达的判读规则，
    不是方向票。把整份文本一刀切地禁词，会把正确的解释也一并禁掉。
    """
    for kw in ({"d_skew25_pp": +2.0}, {"d_skew25_pp": -2.0}, {"d_atm_pp": +2.0}):
        r = SR.analyze_structure(_fa([_ch()], **kw), [], [])
        assert r.defense in SR.DEFENSE_AXIS, r.defense
        for banned in ("偏多", "偏空", "看涨", "看跌", "可空", "综合分"):
            assert banned not in r.defense, f"防守等级不得含方向词「{banned}」"
            assert banned not in r.state_summary, f"状态描述不得含方向词「{banned}」"
    print("PASS test_never_emits_direction")


def test_defense_axis_is_orthogonal_to_direction():
    """防守增强与"看跌"是两回事：偏斜走陡只抬防守等级，不产生任何方向结论。"""
    calm = SR.analyze_structure(_fa([_ch()]), [], [])
    tense = SR.analyze_structure(_fa([_ch()], d_skew25_pp=+2.0, d_atm_pp=+2.0), [], [])
    assert SR.DEFENSE_AXIS.index(tense.defense) > SR.DEFENSE_AXIS.index(calm.defense)
    assert not hasattr(tense, "bias") and not hasattr(tense, "direction")
    print("PASS test_defense_axis_is_orthogonal_to_direction")


def test_needs_prev_snapshot():
    r = SR.analyze_structure(None, [], [])
    assert not r.ok
    r2 = SR.analyze_structure(_FA(changes=[_ch()], vol=None), [], [])
    assert not r2.ok
    print("PASS test_needs_prev_snapshot")


def test_render_says_no_signal_when_no_usable_legs():
    """没有可用腿时必须明说「今日无方向性信息」，而不是留空表。"""
    r = SR.analyze_structure(_fa([_ch(adj_iv_pp=0.05)]), [], [])
    assert not r.usable_legs
    assert "无可用腿" in SR.render_md(r)
    print("PASS test_render_says_no_signal_when_no_usable_legs")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
