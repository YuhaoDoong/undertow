"""当日决策研判：总纲标签 ↔ 详细结论 的一致性（穷举全部输入组合）。

**为什么需要这组测试**

verdict 早先把「详细结论」和「总纲标签」写成两套并行的 if 链，于是必然漂移。
2026-08-26 穷举出三处自相矛盾：

  1. 强信号方向与摆动腿不符时 → 详细「回调进行中·别顺短腿追空」，标签却「短线跟势」
  2. up_leg 且非腿顶       → 详细「短线持多·可续持」，标签却「短线观望」
  3. 无盈亏比数据时         → 详细「无结构依据·观望为上」，标签却断言「追不划算」

第 3 条最危险：我们根本没算出盈亏比，标签却给了一个具体判断。

修法是把标签并进各自的分支，本测试则穷举 576 种输入，逐条核对标签与结论同源。
标签措辞若要改，必须同时改这里的映射——这正是想要的摩擦。
"""
import itertools
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.verdict import build_verdict


@dataclass
class _O:
    near_bias: str = ""
    mid_bias: str = ""


@dataclass
class _Setup:
    kind: str = "chase"
    grade: str = "中"
    rr: float = 1.0
    entry_label: str = "0.5区"


@dataclass
class _RR:
    ok: bool = True
    setups: list = field(default_factory=list)


@dataclass
class _Fib:
    ok: bool = True
    direction: str = "up"
    current_zone: str = "0.5 区"


@dataclass
class _SS:
    direction: str = "看跌"
    level: str = "强"
    diverges: bool = False


@dataclass
class _FA:
    flow_tilt: str = ""


_RRV = {"差": 0.6, "中": 1.2, "优": 2.5}


def _plan(grade):
    return _RR(setups=[_Setup("chase", grade, _RRV[grade]),
                       _Setup("pullback", "中", 1.2, "0.5区")])


BIAS = ["偏多", "偏空", "中性", ""]
ZONE = {True: "摆动高", False: "0.5 区"}


def _all_verdicts():
    for near, mid, cg, d, top, ss in itertools.product(
            BIAS, BIAS, ["差", "中", "优"], ["up", "down"], [True, False],
            [None, _SS("看跌"), _SS("看涨")]):
        yield (near, mid, cg, d, top, ss), build_verdict(
            _O(near, mid), _FA(), ss, _Fib(direction=d, current_zone=ZONE[top]), _plan(cg))


# 标签 → 详细结论里必须出现的关键词（任一命中即可）
SWING_MAP = {
    "短线跟多": ("跟多",), "短线跟空": ("跟空",),
    "短线等回调": ("回调进行中",), "短线等反抽": ("反抽进行中",),
    "短线止盈/收紧": ("止盈", "了结"),
    "短线持多": ("短线持多",), "短线持空": ("短线持空",),
    "短线观望": ("观望",),
}
CHASE_MAP = {
    "回调买·不追": ("回调买、不追",), "反抽卖·不追": ("反抽卖、不追",),
    "追势无依据": ("无结构依据",), "别追·等回调": ("别追",),
    "追不划算": ("追不划算",), "现价结构占优": ("结构占优",),
}
SHORT_MAP = {
    "可空": ("可考虑波段空", "做空有结构支持"),
    "轻仓短空": ("可轻仓短空",),
    "不做空": ("不是做空位置", "无明确做空依据"),
}


def test_swing_tag_matches_action():
    bad = []
    for inp, v in _all_verdicts():
        tag = v.headline.split(" · ")[2]
        keys = SWING_MAP.get(tag)
        assert keys is not None, f"未知短线标签「{tag}」——新增标签须同步本测试的映射表"
        if not any(k in v.swing_action for k in keys):
            bad.append((inp, tag, v.swing_action[:40]))
    assert not bad, f"标签与详细结论不符 {len(bad)} 例，首例: {bad[0]}"
    print("PASS test_swing_tag_matches_action")


def test_chase_tag_matches_answer():
    bad = []
    for inp, v in _all_verdicts():
        tag = v.headline.split(" · ")[1]
        keys = CHASE_MAP.get(tag)
        assert keys is not None, f"未知追势标签「{tag}」"
        if not any(k in v.chase_answer for k in keys):
            bad.append((inp, tag, v.chase_answer[:40]))
    assert not bad, f"标签与详细结论不符 {len(bad)} 例，首例: {bad[0]}"
    print("PASS test_chase_tag_matches_answer")


def test_short_tag_matches_answer():
    bad = []
    for inp, v in _all_verdicts():
        tag = v.headline.split(" · ")[0]
        keys = SHORT_MAP.get(tag)
        assert keys is not None, f"未知做空标签「{tag}」"
        if not any(k in v.short_answer for k in keys):
            bad.append((inp, tag, v.short_answer[:40]))
    assert not bad, f"标签与详细结论不符 {len(bad)} 例，首例: {bad[0]}"
    print("PASS test_short_tag_matches_answer")


def test_no_verdict_without_data_asserts_a_grade():
    """缺盈亏比数据时不得输出「追不划算」这类具体判断——那是无中生有。"""
    v = build_verdict(_O("偏多", "偏多"), _FA(), None, None, None)
    tag = v.headline.split(" · ")[1]
    assert tag == "追势无依据", tag
    assert "追不划算" not in v.headline
    print("PASS test_no_verdict_without_data_asserts_a_grade")


def test_headline_space_is_not_collapsed():
    """研判必须有区分度：576 种输入至少要产出 40 种以上不同总纲。

    这条是防「模板化」的护栏——如果哪次改动把分支压塌了（比如所有品种
    每天都输出同一句），这里会直接挂。2026-08-26 实测 69 种。
    """
    heads = {v.headline for _, v in _all_verdicts()}
    assert len(heads) >= 40, f"总纲只剩 {len(heads)} 种，研判可能已塌成模板"
    slots = [set(), set(), set(), set()]
    for h in heads:
        for j, p in enumerate(h.split(" · ")):
            slots[j].add(p)
    assert len(slots[0]) >= 3, slots[0]
    assert len(slots[1]) >= 5, slots[1]
    assert len(slots[2]) >= 7, slots[2]     # 修复前只有 5
    assert len(slots[3]) >= 3, slots[3]
    print(f"PASS test_headline_space_is_not_collapsed（{len(heads)} 种总纲）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n{len(fns)} tests passed.")
