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


# ── 渲染路径回归锁（codex 2026-09-01 P0）──────────────────────────────
# 只测 status 不测渲染，是 402 测试全绿却让整份研报崩掉的原因。
def test_无法检验条目不得让验证表崩溃():
    from undertow.report import html
    out = html.render_validation_table()
    assert isinstance(out, str) and len(out) > 100
    assert "未检验" in out


def test_无法检验条目的badge不得报错():
    from undertow.report import html
    b = html._val_badge("gate_net_effect")
    assert "读取失败" not in b, "badge 内部吞掉了异常"


def test_每个条目的summary与badge都能渲染():
    from undertow.report import html
    for k, v in validation.REGISTRY.items():
        assert isinstance(v.summary(), str), k
        assert "读取失败" not in html._val_badge(k), k


def test_rate为None时不得用0冒充():
    """hits=None 返回 None，不能返回 0.0 —— 那会把「没测过」显示成「一次没中」。"""
    v = validation.Validation(key="k", label="l", n=10, hits=None,
                              p_value=None, baseline=None, note="")
    assert v.rate is None
    assert v.need_more is None


def test_对照型条目_有p值但无命中数_也不得崩溃():
    """2026-09-02：wall_edge_vs_placebo 有 p=0.267 但 hits=None，
    status 是「样本不足」而非「无法检验」，昨天按 status 分支会走进命中率格式化。
    判据必须是 hits is None。"""
    v = validation.Validation(key="k", label="l", n=79, hits=None,
                              p_value=0.267, baseline=None, note="", cluster_n=37)
    assert v.status == "样本不足"
    assert "0.267" in v.summary()
    from undertow.report import html
    assert html.render_validation_table()


def test_安慰剂对照条目已注册且结论为无法区分():
    v = validation.REGISTRY["wall_edge_vs_placebo"]
    assert v.p_value == 0.267 and v.cluster_n == 37
    assert not v.significant
    assert "无法区分" in v.note
