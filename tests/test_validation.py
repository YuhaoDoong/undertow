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


def test_安慰剂对照的结论口径不得退回_证伪():
    """2026-09-02 自查：两组都 0 破墙 → 配对差里只剩权利金差，
    这个检验【无力】检验破墙率。把它写成「证伪了墙位价值」是过度解读。
    本测试锁住修正后的口径，防止以后又被简化回去。"""
    v = validation.REGISTRY["wall_edge_vs_placebo"]
    assert v.cluster_n == 37
    assert v.p_value is None, "结果不稳健，不得挂单一 p 值冒充结论"
    assert v.status == "无法检验"
    assert "不支持任何方向性结论" in v.caveat
    assert "有效事件数为 0" in v.caveat, "必须说明零事件"
    assert "不稳健" in v.caveat, "必须说明对匹配方式敏感"
    assert "证伪" not in v.note


def test_hits有值但p为None时summary不崩():
    """用 bootstrap CI 判定的条目没有 p 值。这是 p_value=None 的第三个变体：
    前两个是 hits=None（2026-09-02 codex P0）与对照型条目有 p 无 hits。"""
    from undertow.analyze.validation import Validation
    v = Validation(key="x", label="x", n=100, hits=52, p_value=None,
                   baseline=0.5, cluster_n=20, note="", caveat="")
    s = v.summary()
    assert "bootstrap" in s and "52/100" in s
    assert not v.significant, "p 为 None 时不得判为显著"


def test_ta方向检验条目已记录且结论为不进投票():
    from undertow.analyze.validation import REGISTRY
    v = REGISTRY["ta_indicators_direction"]
    assert v.cluster_n == 397 and v.baseline == 0.539
    assert "全部与基线无区别" in v.note and "全部显著劣于" in v.note
    assert "统计必须按品种拆" in v.caveat, "合并样本得出过错误结论，必须留下这条"
    assert "功效不足，不是证明无效" in v.caveat
    assert "不进方向投票" in v.caveat


def test_二项检验在大n下不溢出():
    """math.comb(5000, 2500) 超出 float 范围会直接抛 OverflowError。
    改用 lgamma 在对数域求和（2026-09-04 实测触发）。"""
    from undertow.analyze.validation import binom_p
    assert 0 <= binom_p(2600, 5000) <= 1
    assert 0 <= binom_p(2500, 5000) <= 1
    assert binom_p(4000, 5000) < 1e-6, "极端偏离应给出极小 p"


def test_所需样本量必须用精确二项而非正态近似():
    """小 n 时离散性让精确检验显著得更早：
    hits=17/n=26 精确要 +11，正态近似会说要 +14。"""
    from undertow.analyze.validation import samples_to_significance as f
    assert f(17, 26) == 11


def test_命中率贴近基线时返回None而不是硬算到上限():
    from undertow.analyze.validation import samples_to_significance as f
    assert f(510, 1000) is None, "边缘太薄，cap 之内等不到"
    assert f(50, 100) is None, "命中率等于基线"
