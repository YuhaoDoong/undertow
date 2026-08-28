"""方向裁决与弃权的确定性测试。

要守住的三件事：
  1. **硬弃权与软弃权必须可区分**——过期/无数据是逻辑约束，压力比不足是未校准阈值
  2. 弃权时 direction 必须为空，绝不能留个方向让下游误用
  3. 阈值必须**始终**标注未校准（实测没有任何门槛的 Wilson 下界超过 50%）
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.direction import decide, MIN_RATIO


def test_stale_data_is_hard_abstain():
    """过期数据必须硬弃权：可交易时点已过，判对也吃不到。

    2026-08-27 实测：SLV 因 OCC 未结算，管线仍拿 (8/25,8/26) 当最新，
    在 8/27 早晨弹出 ⚡极强看跌 —— 它本该在 8/26 开盘交易。
    """
    c = decide(up_pressure=1000, dn_pressure=90_000,
               trade_date="2026-08-26", today="2026-08-27")
    assert c.abstain and c.hard and c.direction == ""
    assert "已过期" in c.reasons[0]
    print("PASS test_stale_data_is_hard_abstain")


def test_unsettled_oi_is_hard_abstain():
    c = decide(up_pressure=0, dn_pressure=0, oi_changed=False)
    assert c.abstain and c.hard and "OCC" in c.reasons[0]
    print("PASS test_unsettled_oi_is_hard_abstain")


def test_no_prev_snapshot_is_hard_abstain():
    c = decide(up_pressure=99_999, dn_pressure=0, has_prev=False)
    assert c.abstain and c.hard and c.direction == ""
    print("PASS test_no_prev_snapshot_is_hard_abstain")


def test_low_ratio_soft_abstain_two_modes():
    """压力比不足 = 软弃权（未校准阈值）。默认走 shadow：不抑制，但标低置信。

    项目铁律：未校准的东西不得正式裁决。软弃权阈值全部未校准
    （实测没有任何门槛的 Wilson 95% 下界超过 50%），故默认 shadow。
    """
    sh = decide(up_pressure=1000, dn_pressure=900)                    # 默认 shadow
    assert not sh.abstain and sh.low_confidence and sh.direction == "偏多"
    assert any("未经校准" in r for r in sh.reasons)
    st = decide(up_pressure=1000, dn_pressure=900, shadow_soft=False)  # 正式执行
    assert st.abstain and not st.hard and st.direction == ""
    print("PASS test_low_ratio_soft_abstain_two_modes")


def test_gauge_conflict_two_modes():
    """两口径反向：默认 shadow（给方向但标低置信），strict 才真弃权。"""
    sh = decide(up_pressure=90_000, dn_pressure=1000, net_delta=-25_000)
    assert not sh.abstain and sh.low_confidence
    assert "两个口径反向" in sh.reasons[0]
    st = decide(up_pressure=90_000, dn_pressure=1000, net_delta=-25_000,
                shadow_soft=False)
    assert st.abstain and not st.hard and st.direction == ""
    print("PASS test_gauge_conflict_two_modes")


def test_range_friendly_when_direction_unclear():
    """方向不明或低置信 → 区间策略（铁鹰）的适用场景。

    用户 2026-08-27：「方向不明，墙明确的时候，也可以铁鹰」。
    弃权不等于不能交易 —— 铁鹰本就是中性结构，方向未知恰是它的正当理由。
    """
    assert decide(up_pressure=1000, dn_pressure=900).range_friendly          # 低置信
    assert decide(up_pressure=90_000, dn_pressure=1000,
                  net_delta=-25_000).range_friendly                          # 两口径反向
    assert decide(up_pressure=5, dn_pressure=5, oi_changed=False).range_friendly  # 硬弃权
    # 高置信方向 → 不是区间场景
    clear = decide(up_pressure=90_000, dn_pressure=1000, net_delta=+25_000)
    assert not clear.range_friendly and not clear.low_confidence
    print("PASS test_range_friendly_when_direction_unclear")


def test_agreement_gives_direction():
    c = decide(up_pressure=90_000, dn_pressure=1000, net_delta=+25_000,
               trade_date="2026-08-27", today="2026-08-27")
    assert not c.abstain and c.direction == "偏多"
    assert any("两口径同向" in r for r in c.reasons)
    print("PASS test_agreement_gives_direction")


def test_never_claims_calibrated():
    """任何裁决都不得声称已校准——实测没有门槛的 Wilson 下界超过 50%。"""
    for kw in ({"up_pressure": 90_000, "dn_pressure": 1000},
               {"up_pressure": 1000, "dn_pressure": 900},
               {"up_pressure": 1000, "dn_pressure": 90_000, "net_delta": 5},
               {"up_pressure": 1000, "dn_pressure": 900, "shadow_soft": False}):
        c = decide(**kw)
        assert c.calibrated is False
        if not c.hard:
            assert any("未经校准" in r for r in c.reasons), c.reasons
    print("PASS test_never_claims_calibrated")


def test_abstain_never_leaks_direction():
    """真弃权时 direction 必须为空、label 为"方向不明"；低置信则显式标注。"""
    for kw in ({"up_pressure": 1000, "dn_pressure": 900, "shadow_soft": False},
               {"up_pressure": 1000, "dn_pressure": 90_000, "net_delta": +9,
                "shadow_soft": False},
               {"up_pressure": 5, "dn_pressure": 5, "oi_changed": False}):
        c = decide(**kw)
        assert c.direction == "" and c.label == "方向不明"
    lc = decide(up_pressure=1000, dn_pressure=900)
    assert lc.direction == "偏多" and "低置信" in lc.label
    print("PASS test_abstain_never_leaks_direction")




def test_strong_signal_obeys_abstention():
    """裁决弃权时，⚡强信号不得独自开火——它不是第二份独立证据。

    强信号用的就是同一套 upside/downside_pressure，实测与 pressure 方向 100% 共线。
    2026-08-27 实测：接入裁决后 QQQ/WTI 的裁决是「方向不明（两口径反向）」，
    而 ⚡ 横幅仍在亮 —— 正是我们反复在修的「指标互相打架」，只不过这次是自己造的。
    """
    from dataclasses import dataclass, field
    from datetime import date as _d
    from undertow.analyze.flow import detect_strong_signal, FlowChange
    from undertow.analyze.direction import decide

    def _leg(kind, bias, delta):
        return FlowChange(expiry=_d(2026, 9, 18), strike=100.0, kind=kind,
                          prev_oi=1000, curr_oi=9000, d_oi=8000, delta=delta,
                          prev_iv=0.2, curr_iv=0.23, d_iv_pp=3.0, adj_iv_pp=2.0,
                          curr_volume=9000, moneyness=0.0, bias=bias,
                          judgment="x", on_wall="", note="", weight=1.0)

    @dataclass
    class _FA:
        changes: list
        upside_pressure: float
        downside_pressure: float
        net_call_doi: int = 0
        net_put_doi: int = 50_000
        vol: object = None
        prev_date: str = "2026-08-26"
        curr_date: str = "2026-08-27"
        spot: float = 100.0
        call: object = None

        def __post_init__(self):
            if self.call is None:
                self.call = decide(up_pressure=self.upside_pressure,
                                   dn_pressure=self.downside_pressure,
                                   net_delta=self.net_delta)
        net_delta: float = 0.0

    legs = [_leg("P", "bearish", -0.30) for _ in range(30)]
    # 推断口径压倒性偏空，但观测口径（净有效 Delta）指向偏多 → 裁决弃权
    fa = _FA(changes=legs, upside_pressure=6_576, downside_pressure=91_763,
             net_delta=+5_689)
    object.__setattr__(fa, "call",
                       decide(up_pressure=6_576, dn_pressure=91_763,
                              net_delta=+5_689, shadow_soft=False))
    assert fa.call.abstain, "前提：strict 模式下应因两口径反向而弃权"
    assert detect_strong_signal(fa) is None, "裁决弃权时 ⚡ 不得开火"

    # 两口径同向时正常开火
    fa2 = _FA(changes=legs, upside_pressure=6_576, downside_pressure=91_763,
              net_delta=-5_689)
    assert not fa2.call.abstain
    assert detect_strong_signal(fa2) is not None
    print("PASS test_strong_signal_obeys_abstention")




def test_downstream_must_not_parse_tilt_prose():
    """下游不得在 flow_tilt 散文里搜"多"/"空" —— 弃权文案同时含两个字。

    2026-08-27 codex review 实测：冲突文案是
      「方向不明（两个口径反向：推断口径（资金力 13.95×）指向偏空，
        观测口径（净有效 Delta +5,689）指向偏多）」
    同时含"空"和"多"。condor.py 旧代码先查"空"，于是把【方向不明】
    无条件当成偏空、给出"铁鹰宜整体下移半档"的建议。
    """
    from undertow.analyze.direction import decide
    c = decide(up_pressure=6_576, dn_pressure=91_763, net_delta=+5_689,
               shadow_soft=False)
    tilt = f"方向不明（{c.reasons[0]}）"
    assert "空" in tilt and "多" in tilt, "前提：弃权文案确实同时含两个方向字"
    # 结构化字段必须干净：弃权时 direction 为空
    assert c.abstain and c.direction == ""
    print("PASS test_downstream_must_not_parse_tilt_prose")


def test_condor_ignores_direction_when_abstaining():
    """裁决弃权时，铁鹰不得应用任何方向微调。"""
    from dataclasses import dataclass
    from undertow.analyze import condor as C
    from undertow.analyze.direction import decide

    @dataclass
    class _FA:
        call: object
        flow_tilt: str = "方向不明（两个口径反向：…指向偏空，…指向偏多）"

    abst = _FA(call=decide(up_pressure=6_576, dn_pressure=91_763, net_delta=+5_689,
                           shadow_soft=False))
    src = C.__file__
    txt = open(src, encoding="utf-8").read()
    assert '"空" in tilt' not in txt, "condor 不得再解析 flow_tilt 散文"
    assert 'getattr(_call, "direction"' in txt or '_call, "direction"' in txt
    assert abst.call.abstain and abst.call.direction == ""
    print("PASS test_condor_ignores_direction_when_abstaining")




def test_condor_promoted_when_direction_unclear_and_walls_present():
    """方向不明/低置信 + 墙明确 → 铁鹰应被**主动推荐**，而非仅"不阻止"。

    用户 2026-08-27：「方向不明，墙明确的时候，也可以铁鹰」。
    铁鹰是中性结构，赚区间内的时间价值，不需要方向判断 ——
    方向未知恰是它的正当理由（墙的前提由 assess_condor 门槛 3 把关）。
    """
    from undertow.analyze import condor as C
    txt = open(C.__file__, encoding="utf-8").read()
    assert "range_friendly" in txt, "condor 必须消费 call.range_friendly"
    assert "正是铁鹰" in txt, "方向不明时应给出正面理由，而不是沉默"
    # 方向明确时不得触发该分支
    from undertow.analyze.direction import decide
    clear = decide(up_pressure=90_000, dn_pressure=1000, net_delta=+25_000)
    assert not clear.range_friendly
    print("PASS test_condor_promoted_when_direction_unclear_and_walls_present")




def test_summary_and_banner_agree_on_downgrade():
    """报告横幅 / 索引页 / CLI 摘要 —— 三处对「降级」的口径必须一致。

    2026-08-28 实测：SPY 的 ⚡强看涨 在报告横幅里已正确标注「本告警已过期」
    （它描述 8/26 交易日、本该 8/27 开盘交易），**CLI 摘要却仍是满格 ⚡** ——
    与前一天修「低置信」时"渲染层改了、摘要层漏了"是同一类错。

    降级有两种，摘要必须都能体现：
      · 已过期（硬）：可交易日已过，判对也吃不到
      · 低置信（软）：方向裁决的软条件未过，阈值未校准
    """
    from pathlib import Path
    cli = (Path(__file__).resolve().parents[1] / "undertow" / "cli.py").read_text(encoding="utf-8")
    assert "(已过期)" in cli, "CLI 摘要缺过期降级标记"
    assert "(低置信)" in cli, "CLI 摘要缺低置信降级标记"
    # 三处口径同步的意图必须写在代码里，防止将来只改一处
    assert "三处口径必须同步" in cli
    html = (Path(__file__).resolve().parents[1] / "undertow" / "report" / "html.py").read_text(encoding="utf-8")
    assert "本告警已过期" in html and "本告警为「低置信」" in html
    print("PASS test_summary_and_banner_agree_on_downgrade")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
