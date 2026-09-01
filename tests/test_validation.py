"""validation.REGISTRY 的行为锁。

2026-09-01 新建：该模块是「每条进入决策的判断都要有 n/hits/p_value」的登记簿，
本身却没有测试。当天在它上面出过两次事故：
  · 用索引切片更正条目时，误删了夹在中间的 tradeable_gate 整条；
  · 新增 p_value=None 的条目时，significant 属性直接崩溃。
"""
from undertow.analyze import validation


# ── 2026-09-01：p_value=None 是合法状态（「这条根本没法检验」）──────────
def test_p值为None时不崩溃且状态为无法检验():
    """三条核心闸门要历史逐行 OI，免费源拿不到 —— 无法检验是真实状态，
    不能崩溃，也不能硬塞一个假 p 值蒙混成「样本不足」。"""
    v = validation.Validation(key="k", label="l", n=36, hits=None,
                              p_value=None, baseline=None, note="")
    assert v.significant is False
    assert v.status == "无法检验"


def test_闸门净效果条目已注册且标为无法检验():
    v = validation.REGISTRY["gate_net_effect"]
    assert v.status == "无法检验"
    assert "从未被检验过" in v.note


def test_每个注册条目的status都能算出来():
    """回归锁：任何条目字段为 None 都不得让 status 抛异常。"""
    for k, v in validation.REGISTRY.items():
        assert isinstance(v.status, str) and v.status, k
