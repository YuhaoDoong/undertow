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
    assert v.n == 60, "60 个 (品种,周期,指标) 组合，分别报告"
    assert "不做任何跨周期汇总" in v.note
    assert "期望假阳性正好 3 个" in v.note, "多重比较必须写明"
    assert "被推翻过两次" in v.caveat, "结论的修正过程比结论本身更该留下"
    assert "撤回" in v.caveat, "『美元明确有害』已撤回"
    assert "8.4pp" in v.caveat, "80% power 的功效说明"
    assert "尚未用合格方法" in v.caveat and "不是已经证伪" in v.caveat


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


def test_双尾检验必须挑对尾巴():
    """原实现一律算 2×P(X≥k)，k **小于**均值时恒返回 1.0，
    把「显著劣于基线」掩盖成「不显著」。
    ta_indicators_direction 那条 48.6% vs 基线 53.9% 正是这种情形。"""
    from undertow.analyze.validation import binom_p
    import pytest as _p
    assert binom_p(0, 10, 0.5) == _p.approx(2 * 0.5 ** 10), "k=0 该用左尾"
    assert binom_p(10, 10, 0.5) == _p.approx(2 * 0.5 ** 10), "两侧应对称"
    assert binom_p(5, 10, 0.5) == 1.0, "正中间不显著"
    # 显著劣于基线的真实数据不得被算成 p=1
    assert binom_p(1328, 2733, 0.539) < 1e-5


def test_已显著时所需样本为零而非None():
    """原来当 n 已超过正态近似上界时循环不执行，误返回 None（读作"永远等不到"）。"""
    from undertow.analyze.validation import samples_to_significance as f
    assert f(120, 200) == 0
    assert f(17, 26) == 11, "未显著的仍要给出增量"


def test_退化零假设下不可能事件的p值为零():
    """p0=0 时只有 k=0 可能发生。原来一律返回 1.0，
    等于说「不可能事件也不奇怪」（codex P2-1）。"""
    from undertow.analyze.validation import binom_p
    assert binom_p(0, 10, 0.0) == 1.0 and binom_p(3, 10, 0.0) == 0.0
    assert binom_p(10, 10, 1.0) == 1.0 and binom_p(3, 10, 1.0) == 0.0


def test_k越界必须抛错():
    from undertow.analyze.validation import binom_p
    import pytest as _p
    for bad in (-1, 11):
        with _p.raises(ValueError):
            binom_p(bad, 10, 0.5)


def test_双侧定义已在文档里写明():
    """equal-tailed 与 probability-ordering 在 p0≠0.5 时可能给出不同结果，
    采用哪种必须写清楚。"""
    from undertow.analyze.validation import binom_p
    import inspect
    doc = inspect.getdoc(binom_p)
    assert "equal-tailed" in doc and "probability-ordering" in doc
    assert "不保证任意极端参数下" in doc


def test_0DTE的pin措辞不得过度确定():
    """仅凭 OI 无法确证 pin 效应；能确定的只有「收盘后消失」。"""
    import pathlib
    src = pathlib.Path("undertow/analyze/gamma.py").read_text("utf-8")
    assert "可能" in src and "不能支撑跨日的墙位" in src
    assert "pin 效应是真的" not in src


def test_趋势作为过滤器的检验已登记且结论为不接入():
    from undertow.analyze.validation import REGISTRY
    v = REGISTRY["trend_as_filter"]
    assert "全部不显著" in v.note
    assert "D−1" in v.note, "必须写明取前一日收盘，否则是未来函数"
    assert "不支持接入" in v.caveat
    assert "期权链内部" in v.caveat, "留下方法论指向"
