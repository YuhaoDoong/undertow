"""验证状态登记簿：每个会影响交易决策的判断，必须在这里登记它的实测成绩。

起因（用户 2026-08-31）：「不能每次出来一个，我想要照着做交易的时候，你又说
这其实是不准的」。根因是报告用「回测校准的加权投票」这种说法呈现结论，而
翻遍代码，八个投票因子里只有一个有完整的回测记录（样本量 + p 值），其余的
权重是拍的 —— outlook.py 开头那句「按回测校准的可信度加权」当时并不成立。

规则（写死在测试里）：
  1. 任何进入交易决策的判断，都要在 REGISTRY 里有一条，带 n / hits / p_value；
  2. 报告展示结论时，必须同时展示 n 与 p，不得只展示结论；
  3. 未验证的判断标 status="未验证"，不得给「可信度中/高」这类标签；
  4. p ≥ 0.05 时必须显示 samples_to_significance()，把「什么时候能信」
     变成一个具体数字，而不是含糊的「样本不足」。

⚠️ 全部数字来自 2026-08-31 的回测，样本区间 2026-06-25 ~ 2026-08-31。
   新增样本后需重跑并更新此表，不得手改。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def binom_p(k: int, n: int, p0: float = 0.5) -> float:
    """双侧二项检验：n 次里 k 次命中，与 p0 的差异有多容易被运气解释。

    采用 **equal-tailed** 定义（2 × 较近的那条单尾），不是常见统计软件里
    "累加所有 PMF ≤ 观测 PMF" 的 probability-ordering 精确 p 值。
    p0=0.5 时两者一致；p0 明显偏离 0.5 时可能不同 —— codex review 2026-09-04 指出。

    ⚠️ 每项用 lgamma 算 log-PMF 后在普通域求和，不是完整的 log-sum-exp：
    极小的尾部项会下溢为 0。判断是否 <0.05 足够，但**不保证任意极端参数下
    都给出精确的非零 p**。
    ⚠️ 不能直接 math.comb —— n 上千时组合数超出 float 范围会抛 OverflowError
    （2026-09-04 实测 n=5000 必崩）。
    """
    if n <= 0:
        return 1.0
    if not 0 <= k <= n:
        raise ValueError(f"k 必须在 [0, {n}]，收到 {k}")
    # 退化零假设：p0=0 时只有 k=0 可能发生，p0=1 时只有 k=n 可能发生。
    # 原来一律返回 1.0，等于说"不可能事件也不奇怪"。
    if p0 <= 0:
        return 1.0 if k == 0 else 0.0
    if p0 >= 1:
        return 1.0 if k == n else 0.0
    lp, lq = math.log(p0), math.log1p(-p0)
    lgn = math.lgamma(n + 1)

    def _pmf(i: int) -> float:
        lg = lgn - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
        return math.exp(lg) if lg > -745 else 0.0   # -745 是 exp 下溢阈值

    # ⚠️ 尾巴要挑对边。原来一律算 2×P(X≥k)，k **小于**均值时恒返回 1.0 ——
    # 于是「显著劣于基线」会被掩盖成「不显著」（2026-09-04 实测
    # binom_p(0,10,0.5) 给 1.0，正确答案 0.00195）。
    # 这不是理论洁癖：ta_indicators_direction 那条命中率 48.6% < 基线 53.9%，
    # 正是 k < 均值的情形。
    if k >= n * p0:
        tot = sum(_pmf(i) for i in range(k, n + 1))
    else:
        tot = sum(_pmf(i) for i in range(0, k + 1))
    return min(1.0, 2 * tot)


def samples_to_significance(hits: int, n: int, p0: float = 0.5,
                            alpha: float = 0.05, cap: int = 5000) -> int | None:
    """保持当前命中率，还需要多少个样本才能达到显著。

    回答的是「什么时候能信这个信号」—— 用户 2026-08-31 问的正是这个。
    返回 None = 当前命中率太接近 p0，在 cap 之内达不到（这本身就是答案：
    这个信号即使是真的，边缘也太薄，不值得等）。
    """
    if n <= 0:
        return None
    rate = hits / n
    if rate <= p0:
        return None
    if binom_p(hits, n, p0) < alpha:
        return 0        # 当前已显著，不必再等（原来会因 n>上界而误返回 None）
    # 判据必须用**精确**二项检验：小 n 时离散性让精确解显著早于正态近似
    # （实测 hits=17/n=26：精确要 +11，正态近似说要 +14）。
    # 正态近似只用来定搜索上界，免得在命中率贴近 p0 时白跑到 cap。
    z = 1.959963985                        # 双侧 α=0.05
    delta = rate - p0
    approx = math.ceil((z * math.sqrt(p0 * (1 - p0)) / delta) ** 2)
    if approx > cap:
        return None                        # 边缘太薄，cap 之内等不到
    limit = min(cap, approx + 50)
    m = n
    while m < limit:
        m += 1
        k = math.ceil(rate * m - 1e-9)
        if binom_p(k, m, p0) < alpha:
            return m - n
    return None


@dataclass(frozen=True)
class Validation:
    key: str
    label: str
    n: int
    # hits/p_value/baseline 允许 None：「这条根本没法检验」是本项目的真实状态
    # （三条核心闸门要历史逐行 OI，免费源拿不到）。见 status 的「无法检验」分支。
    hits: int | None
    p_value: float | None
    baseline: float | None   # 对照基准（无脑做多的胜率等）
    note: str
    caveat: str = ""
    cluster_n: int | None = None   # 日期簇数（跨品种同日相关，簇才是独立样本）
    # kind="hit" 命中率检验（hits/n + 二项检验）
    # kind="corr" 相关性检验（r/t + t 检验）—— 此时 hits 无意义，不得当命中率显示
    kind: str = "hit"
    r: float | None = None         # 相关型：相关系数
    r_control: float | None = None # 相关型：对照相关系数（如"对命中率"的 r）
    effect: str = ""               # 相关型：效应的人话描述

    @property
    def rate(self) -> float | None:
        # hits=None（「无法检验」类条目）返回 None，不得用 0.0 冒充命中率 ——
        # 那会在报告里显示成「0%」，把「没测过」说成「测了，一次没中」。
        if self.hits is None:
            return None
        return self.hits / self.n if self.n else 0.0

    @property
    def significant(self) -> bool:
        # p_value / hits 允许为 None：本项目里「这条根本没法检验」是真实状态
        # （三条核心闸门要历史逐行 OI，免费源拿不到）。没有 p 值就不算显著，
        # 而不是崩溃，也不是硬塞一个假 p 值蒙混过去。
        return self.p_value is not None and self.p_value < 0.05

    @property
    def cluster_corrected(self) -> bool:
        """p 值是否按日期簇算过。打印 cluster_n 不等于用了簇推断
        （codex 2026-08-31 P1-11）。"""
        return bool(self.cluster_n) and "未做日期簇" not in self.caveat

    @property
    def status(self) -> str:
        if self.n == 0:
            return "未验证"
        if self.p_value is None:
            return "无法检验"
        # codex 2026-08-31 P1-12：原实现只看原始 p<0.05 就标「已验证」，
        # 于是 expected_move 的 raw p=0.048（同批数据第二次找模式、极端档 n=6、
        # 未用簇推断）被显示成绿色已验证。凡未做簇修正的一律降级。
        if self.significant and self.cluster_corrected:
            return "已验证"
        if self.significant:
            return "待簇修正"
        return "样本不足"

    @property
    def need_more(self) -> int | None:
        if self.hits is None:
            return None
        return samples_to_significance(self.hits, self.n)

    def summary(self) -> str:
        if self.kind == "corr":
            # 相关型：hits 无意义，显示 r 与对照 r
            s = f"n={self.n}，r={self.r:+.3f}，p={self.p_value:.3f}"
            if self.r_control is not None:
                s += f"（对照 r={self.r_control:+.3f}）"
            if self.effect:
                s += f" · {self.effect}"
            return s + (" · 已验证" if self.significant else " · 未达显著")
        if self.hits is None:
            # 不访问 rate / need_more —— 这两个都要 hits。
            # 判据必须是 hits is None，不能是 status=="无法检验"：
            # 对照型条目（如 wall_edge_vs_placebo）有 p 值但没有"命中数"这个概念，
            # 它的 status 是「样本不足」，照样会崩（2026-09-02 实测）。
            tail = ("无法检验（缺可用的零假设或前瞻收益）" if self.p_value is None
                    else f"p={self.p_value:.3f}"
                         + (f"，{self.cluster_n} 个日期簇" if self.cluster_n else "")
                         + ("" if self.significant else " · 未达显著"))
            return f"n={self.n} · {tail} · 详见 note 与复现入口"
        # ⚠️ hits 有值但 p_value 为 None 是合法组合：用 bootstrap CI 判定的条目
        # （如 ta_indicators_direction）没有 p 值。2026-09-04 实测这里会崩 ——
        # 这是 hits is None 那条注释所说问题的第三个变体，别再假设 p 一定存在。
        s = f"{self.hits}/{self.n} = {self.rate:.0%}（基准 {self.baseline:.0%}）"
        s += (f"，p={self.p_value:.3f}" if self.p_value is not None
              else "，以 bootstrap 置信区间判定（见 note）")
        if self.cluster_n:
            s += f"，{self.cluster_n} 个日期簇"
        if self.significant:
            return s + " · 已验证"
        more = self.need_more
        # ⚠️ need_more 假设命中率原地不变，是【乐观】下界；把抽样波动算进去
        # （功效 80%）需要的样本更多 —— 强信号 65% 那条：乐观 +11，功效口径 +21。
        s += (f" · 样本不足，若命中率保持不变还需 {more} 个（乐观下界；"
              f"要在 80% 功效下确证需更多）"
              if more else " · 命中率贴近基准，再攒样本也难以证实")
        return s


# ── 登记簿：每条都必须能追到一次实际回测 ────────────────────────────────────
REGISTRY: dict[str, Validation] = {
    "strong_signal_dir": Validation(
        key="strong_signal_dir", label="强信号当日方向",
        n=26, hits=17, p_value=0.169, baseline=0.50,
        cluster_n=None,
        note="开火信号配当日 open→close。分波动看：横盘<0.5% 时 6/12=50%（掷硬币）、"
             "小动 7/10=70%、中动 3/3=100%、大动 1/1=100%。整体不显著是被横盘日稀释的。",
        caveat="⚠️ 2026-09-01 作废重估：baseline=0.50 是错的。样本期内 GLD/SLV/USO "
                "全程上涨（+7.4%/+9.7%/+26.8%），随机做空命中率只有 39~40%，而本表 26 "
                "条信号里绝大多数是看跌 —— 拿 50% 当零假设会把真信号判成噪音。"
                "改用同(品种,方向)随机取日的置换检验后，见 strong_signal_extreme_d1。"
                "另：本条用当日 open→close(D+0)，实测 D+1 才是有效窗口。"),
    "strong_signal_extreme_d1": Validation(
        key="strong_signal_extreme_d1", label="极强信号方向（正规台账口径）",
        n=16, hits=9, p_value=0.804, baseline=0.50, cluster_n=None,
        note="数据源：data/history/signals/*.json（record() 自动落盘，无遗漏）。"
             "口径：base=C[D−1]，forward_Nd=C[D−1+N]/C[D−1]−1，去趋势(减 N×drift_60d)、"
             "按品种抽稀成不重叠样本。"
             "分窗口：D+0 9/16=56.2% p=0.804；D+1 8/15=53.3% p=1.000；"
             "D+2 8/14=57.1% p=0.790；D+4 5/12=41.7% p=0.774。**全窗口不显著。**",
        caveat="⚠️ 2026-09-01 教训：当天先用 data/history/strong_signal_days.json（手工台账）"
               "算出 D+1 9/12=75%、p=0.027，差点写进 skill。核对发现该手工台账"
               "【漏记了 3 条有结果的极强信号，且 3 条全部不应验】"
               "（07-10 gold 看涨 −0.31%、07-30 wti 看涨 −1.42%、08-14 silver 看跌 +0.55%）"
               "—— 典型幸存者偏差。手工台账已废弃，一切统计只走 signal_ledger.load_all()。"
               "另：HORIZONS 原为 (1,3,5,10)，缺 2（即 D+1 次日），2026-09-01 补入。"),
    "tradeable_gate": Validation(
        key="tradeable_gate", label="可交易信息闸门（压力比 ≥2×）",
        n=62, hits=40, p_value=0.044, baseline=0.50, cluster_n=30,
        note="放行组 62 笔 65%、顺向 +0.46%；拦掉组 29 笔 41%、顺向 -0.12%。Fisher p=0.044。",
        caveat="① 共测 10 个阈值，Bonferroni 校正后 p=0.44 不再显著；"
               "② Fisher 检验把 91 个品种-日当独立观察，未做日期簇聚类 —— "
               "登记簿里写了 cluster_n=30 但推断没用它（codex 2026-08-31 P1-11）；"
               "③ 待样本外验证"),
    "gate_net_effect": Validation(
        key="gate_net_effect", label="闸门净效果（开火 vs 被拦）",
        n=79, hits=None, p_value=None, baseline=None, cluster_n=None,
        note="2026-09-01 首次检验。三条核心闸门（压力比/主翼比/净建仓规模）"
             "【从未被检验过】——检验需历史逐行权价 ΔOI，免费源拿不到，只能向前累积。"
             "唯一可做的是逆向价格闸门的双样本对比：\n"
             "  gate_contrast  开火均值 / 被拦均值 / Welch t\n"
             "    h=1 (D+0)   +0.36% / +0.24% / +0.15\n"
             "    h=2 (D+1)   -0.18% / +1.62% / -1.45\n"
             "    h=3 (D+2)   -0.66% / +2.08% / -1.75\n"
             "    h=5 (D+4)   -1.64% / +1.87% / -1.48\n"
             "另按压力比≥10× 手工分组（各 18 条）："
             "D+0 开火 56%/+0.28% vs 被拦 56%/−0.14%；"
             "D+1 开火 50%/−0.42% vs 被拦 67%/+0.82%。\n"
             "── 2026-09-01 逐条闸门检验（去趋势；复现入口 scripts/gate_analysis.py）──\n"
             "样本 79 条（压力比达标且 drift_60d 可算；含 drift 缺失则 89 条，"
             "但那 10 条无法去趋势、不进统计）。codex 独立复现确认："
             "无任何一行两个方向同时 pressure_ok，不存在一行两记的重复计数。\n"
             "① 主翼比：wing_ok=True 37条 D+1 51%/+0.29%；False 42条 D+1 69%/+0.79%\n"
             "② 净建仓规模：scale_ok=True 79条，False 0条 —— 【一次没拦过，是摆设】\n"
             "③ 逆向价格（前三条全过后）：contra_ok=True 27条 D+1 48%/−0.23%；False 10条 D+1 60%/+1.69%\n"
             "④ 合计：开火 27条 D+1 −0.23% vs 被拦 52条 D+1 +0.97%，"
             "Welch t=−1.828（抽稀后 −1.716），整体置换 p=0.071，主翼比单独置换 p=0.255。\n"
             "被拦掉的最好几条：07-24 wti +10.68%、08-02 wti +10.00%、07-10 wti +8.60%、"
             "08-19 silver +7.98% —— 多数被主翼比拦下。",
        caveat="⚠️ |t| 均未达 2、blocked_n 仅 8~10，按模块自身标准一条都不成立，"
               "【不构成「闸门有害」的结论】。但 h=2/3/5 三个窗口方向一致（被拦组更好），"
               "这个方向须持续监控，不得因不显著就当噪音丢弃。"
               "行动项（按确定性排序）：\n"
               "  a) 净建仓规模闸门 79/79 全过、0 次拦截 —— 这条【不需要显著性】"
               "     就能断定它在当前样本无增量作用（但不得用同一批数据调阈值后"
               "     再宣称已优化，codex 2026-09-01 P1）；"
               "     要么调紧阈值要么删掉，别再算作『三道闸门』之一；\n"
               "  b) 报告应把【被闸门拦下的信号】也显示出来（标注未过闸门及卡在哪一条），"
               "     现在用户完全看不到它们，无从判断闸门是否误伤；\n"
               "  c) 复现入口：scripts/gate_analysis.py（支持 --thin/--dedupe-row/--exclude）；\n"
               "  d) 样本到 n≥50（开火组）时重跑本条；在那之前"
               "     既不得以「闸门已验证」为由辩护，也不得据此拆闸门。"),
    "wall_edge_vs_placebo": Validation(
        key="wall_edge_vs_placebo", label="卖在墙上 vs 同距离非墙（安慰剂对照）",
        n=80, hits=None, p_value=None, baseline=None, cluster_n=37,
        note="复现入口：scripts/placebo_wall_value.py。配对设计：同到期、"
             "强制实际价差宽度相同、排除全部结构墙、按距离差最小确定性匹配。"
             "SLV/GLD/QQQ/USO put 侧，宽度 3%，到期 4~11 天。\n"
             "── 结果对匹配方式高度敏感，无法收敛 ──\n"
             "  距离差≤1.5pp  80对  墙缓冲5.24% 非墙4.88%  配对ROI差 −0.97%  双侧p=0.011\n"
             "  距离差≤1.0pp  79对  5.20%/4.86%            −0.97%  p=0.012\n"
             "  距离差≤0.5pp  31对  3.33%/3.19%            −0.70%  p=0.048\n"
             "  对照改取全部候选均值                        +0.08%（方向相反）\n"
             "每一档里墙都比非墙【远】0.14~0.36pp，远→权利金低→ROI 低，"
             "方向与观测到的『墙上更差』完全一致 —— 该显著性很可能是残余距离差的产物。",
        caveat="⛔ **本条不支持任何方向性结论**，三点原因：\n"
               "  ① 核心主张无法检验：两组均 0/80 破墙。未破墙时 pnl ≡ credit − fee，"
               "     配对差里只剩权利金差；配对率差的有效事件数为 0，"
               "     『墙上是否更难破』观测信息量为零（codex 2026-09-02 P0-1）。\n"
               "  ② 权利金维度的结果不稳健：单一最近匹配 −0.97%(p=0.011) 与"
               "     全候选均值 +0.08% 方向相反，且残余距离差可完整解释前者。\n"
               "  ③ 匹配精度不足：卡尺收到 0.2pp 时可用对数 <8，做不下去。\n"
               "→ 历史上本条曾被写成『证伪了墙位价值』（2026-09-02 上午），"
               "  那是过度解读，已作废。正确表述是：**当前样本对墙位价值给不出结论**，"
               "  要检验核心主张必须有破墙样本，即跌市数据。"),
    "ta_indicators_direction": Validation(
        key="ta_indicators_direction", label="技术面子模块的方向增量（配对差值检验）",
        n=60, hits=None, p_value=None, baseline=None, cluster_n=None,
        note="复现入口：scripts/validate_ta_paired.py。四品种×三周期×五指标 = "
             "**60 个 (品种,周期,指标) 组合，分别报告，不做任何跨周期汇总**。\n"
             "方法：在每个完全相同的可用时点算配对差值 "
             "d = 1[指标命中] − 1[同期一直做多命中]，对按时间排列的 d 序列做 "
             "moving-block bootstrap（块长 = max(4×horizon, 20)）。零假设 E[d]=0。\n"
             "注意 d 只在**指标做空**时非零 —— 做多时两者完全相同。\n"
             "结果：**57 个『与一直做多无区别』，3 个『劣于』**"
             "（美元日线的 UT持仓/MACD柱/Stoch-K）。\n"
             "但 60 次检验在 α=0.05 下**期望假阳性正好 3 个**，"
             "P(至少 3 个显著 | 全部无效) = 0.583；Bonferroni 校正后一个都不剩。\n"
             "→ **没有任何证据表明这些指标在任何品种任何周期上有效，也没有证据表明有害。**",
        caveat="⚠️ 这条结论被推翻过两次，过程本身比结论更该记住：\n"
               "  第一版：四品种×三周期的段混在一起 → 得出『三个指标显著劣于』\n"
               "  第二版：按品种拆开 → 发现那三个『显著』全是美元一个品种拖的，"
               "改口成『美元五个指标全部显著劣于』\n"
               "  第三版（codex review 2026-09-04 推翻）：按品种拆了仍然不够 —— "
               "**+3 根在 1h 是 3 小时、在 1d 是 3 个交易日，检验对象根本不是一回事**；"
               "4h 又是从 1h 聚合来的，两者高度重叠。且段级 bootstrap 检验的不是"
               "『相对一直做多的增量』：基线被当成无误差常数，而它同样是这批数据估出的"
               "随机量，观测集合也因 warm-up 而不重合。改用配对差值 + 时间块 bootstrap 后，"
               "『美元明确有害』**撤回** —— 五个里只剩三个，且落在假阳性期望内。\n"
               "⚠️ 这个检验回答的是**业务问题**（能否战胜趋势基准），不是"
               "『指标是否含方向信息』。一个与未来完全独立、一半做多一半做空的指标，"
               "在上涨率 54% 的市场里预期命中率约 50%，本来就会输给一直做多 —— "
               "输了不等于有负预测力。信息检验（块置换）见脚本里的 block_permute()，未跑。\n"
               "⚠️ 功效：SE≈3pp。先前写『只能检出 ≥5~6pp』用的是 1.96×SE，"
               "那只是『刚好越过显著门槛』；**80% power 的最小可检效应是 "
               "(1.96+0.84)×SE ≈ 8.4pp**。2~4pp 完全看不见，5~6pp 也只有一半机会检出。\n"
               "→ 结论：ta 包维持『不进研报、不进方向投票』。理由是**尚未用合格方法"
               "证明存在增量价值**，不是已经证伪 —— 这两句话不能混为一谈。"),
    "trend_as_filter": Validation(
        key="trend_as_filter", label="Supertrend 作为资金流的过滤器（同向 vs 反向）",
        n=153, hits=None, p_value=None, baseline=None, cluster_n=None,
        note="复现入口：scripts/trend_as_filter.py。用户 2026-09-04 提出"
             "「把超级趋势结合我们的模型系统，趋势作为辅助」—— 这与「进方向投票」"
             "是两件事，先例是 vol_surface_as_filter，故照搬那次的检验方式："
             "看资金流在『趋势同向 / 反向』两组的准确率差异。\n"
             "⚠️ Supertrend 方向取 **D−1 收盘**的值 —— 决策在 D 日盘前，"
             "那时能看到的最后一根完整日线是 D−1，取 D 当根就是未来函数。\n"
             "按品种拆开，Fisher 精确检验：\n"
             "  GLD D+0 同向 65%(11/17) vs 反向 63%(17/27)  差 +2pp  p=1.000\n"
             "  GLD D+1 同向 69%(11/16) vs 反向 67%(18/27)  差 +2pp  p=1.000\n"
             "  SLV D+1 同向 45%(10/22) vs 反向 60%(12/20)  差 −15pp p=0.374\n"
             "  USO D+1 同向 41%(7/17)  vs 反向 52%(13/25)  差 −11pp p=0.543\n"
             "  QQQ D+1 同向 14%(1/7)   vs 反向 47%(7/15)   差 −32pp p=0.193\n"
             "**8 个检验全部不显著（p 0.193~1.000），方向还不一致。**\n"
             "对照 vol_surface_as_filter：金银同向 87% vs 反向 71%，差 16pp —— "
             "那才是「有过滤价值」的量级。",
        caveat="⚠️ 有个方向性观察但**样本不足以支持**：四个品种里三个是"
               "『趋势反向时资金流反而更准』。一种可能的解释是资金流与趋势背离时"
               "说明有新的力量在介入、而趋势是滞后量 —— 但每组只有 7~27 个样本，"
               "p 值 0.193~1.000，现在说什么都是讲故事。留待样本积累。\n"
               "⚠️ 值得记住的对比：波动率面**也来自期权链**（同源数据的另一个维度），"
               "它有过滤价值；Supertrend 是外部的通用技术指标，全世界都在看，"
               "能提取的早被套利掉。这提示我们的过滤器应当从**期权链内部**去找，"
               "而不是嫁接外部指标。\n"
               "→ 结论：趋势作为辅助过滤器，当前数据**不支持接入**。"),
    "vol_surface_as_filter": Validation(
        key="vol_surface_as_filter", label="波动率面作为资金流的过滤器（同向 vs 反向）",
        n=60, hits=36, p_value=0.17, baseline=0.47, cluster_n=None,
        note="复现入口：scripts/vol_vs_flow.py（152 对相邻快照，GLD/SLV/USO/QQQ）。"
             "资金流有方向的 149 对按波动率面拆三组，资金流 D+0/D+1 命中率：\n"
             "  波面同向  n=60  60% / 64%（有向均值 +0.52% / +1.11%）\n"
             "  波面反向  n=45  47% / 45%\n"
             "  波面中性  n=44  43% / 44%\n"
             "只看 ≥10× 资金流：全部 64%/66%；同向 79%/79%(n=14)；反向 55%/50%(n=11)。\n"
             "反向时波面自身：原映射 53%/55%，严格映射 44%/45% —— 冲突时谁都不准，"
             "且结论对判读→方向的映射敏感（±10pp），两种映射下都接近抛硬币。",
        caveat="⚠️ 2026-09-03 用户追问后按品种拆开，四品种平均掩盖了金银的真实表现。"
               "**只看金银（GLD+SLV）**、按用户交易规则（当天应验即对，否则看次日）：\n"
               "  波面同向 26/30=87%（<10× 88%，≥10× 83%）\n"
               "  波面反向 20/28=71%（<10× 74%，≥10× 67%）\n"
               "金银上冲突时资金流 71% 对，**不是抛硬币** —— 47% 那个数是 USO/QQQ 拖的"
               "（8/21 起 USO 三次冲突两次大错：8/27 26.3× 看跌 +2.09%，9/1 4.9× 看跌 +5.46%）。"
               "但同向 87% vs 反向 71% 仍差 16pp，波面作为过滤器的价值在金银上依然成立。\n"
               "金银 ≥10× 冲突且错的只有三次：7/16 SLV 16.4× 看涨(−3.49%)、"
               "8/6 GLD 12.4× 看跌(+2.27%)、8/14 SLV 176.5× 看跌(+2.42%)；8/21 起 ≥10× 三中三。\n"
               "两日规则给了两次机会，比严格 D+0 口径宽松；四品种混算的原结论保留如下供对照：\n"
               "反向组资金流 47%，比中性组还低。数据支持的是另一句话：\n"
               "**波动率面不是独立信号，是过滤器** —— 资金流有方向时，只在波面同向时行动，"
               "命中率从 ~50% 提到 60~64%；≥10× 时提到 79%。\n"
               "同向 60% vs 反向 47% 的双比例 z≈1.36，p≈0.17，未达显著；"
               "≥10× 同向 79% 仅 n=14。跨品种同日相关未做簇修正。\n"
               "另：资金流比值 10~20× 命中 71%(n=17) 是甜点，≥20× 回落到 53~57%(n=15)，"
               "「越强越准」不成立。强看涨 n=5 / 极强看涨 n=4，D+1 分别 33%/25%，"
               "样本不足以支持「强/极强看涨准确率很高」。"),
    "wall_space_vote": Validation(
        key="wall_space_vote", label="Gamma 墙位空间投票",
        n=83, hits=41, p_value=1.000, baseline=0.51, cluster_n=43,
        note="近端口径 49%、混算口径 54%、基准（无脑做多）51%。四个变体 p 全为 1.000。",
        caveat="换到期口径、换阈值形式都试过，均无改善——该因子本身无预测力"),
    "surface_gate": Validation(
        key="surface_gate", label="固定Delta曲面闸门（逐腿与曲面矛盾即否决）",
        n=66, hits=43, p_value=0.169, baseline=0.50,
        note="2026-08-31 四口径对比（100 品种-日，≥2× 子集）："
             "① 现状一刀切否决 65%/+0.44%　② 去掉闸门 60%/+0.31%　"
             "③ 矛盾降半权 60%/+0.31%　④ 大单≥2000张豁免 64%/+0.37%。"
             "放松闸门能把可判定率从 52% 提到 57%，但命中率与收益都变差 —— "
             "被掐掉的不是被误伤的信号，是噪音。",
        caveat="低可判定率是这套方法在诚实说「不知道」，不是待修的缺陷；"
               "三种放松方案均已试过并更差，勿再改"),
    "ratio_spread": Validation(
        key="ratio_spread", label="比例价差检出（R15）",
        kind="corr", n=4, hits=0, p_value=0.99, baseline=0.0,
        r=0.0, r_control=None,
        effect="信噪比（真实检出/随机基线）：纯ΔOI口径 1.0~1.5x（TQQQ 1.0x=全是巧合），"
               "加当日成交量同步配对后 2.9~6.2x",
        note="零假设检验：把 ΔOI 与 volume 在行权价之间随机打乱 20 次重跑。"
             "GLD 2/0.7、SLV 4/1.1、QQQ 13/4.5、TQQQ 5/0.8。",
        caveat="只验证了「不是随机凑对」，【未】验证检出后是否有交易价值；"
               "QQQ 随机基线仍有 4.5 个，条目多时不可信"),
    # codex 2026-08-31 P1-12：真正决定仓位的 credit_wall 与 Kelly 此前根本没登记，
    # 违反本模块开头自己写的第 1 条规则。补登记，并如实写明未通过簇修正。
    # 2026-08-31 codex review 后重做回测：三档全部翻负，策略默认停用。
    # 保留登记是为了记住「82% 胜率」那个错误结论是怎么来的。
    "credit_wall_conservative": Validation(
        key="credit_wall_conservative", label="墙位卖方价差·稳健档",
        kind="corr", n=19, hits=0, p_value=0.949, baseline=0.0,
        r=-0.2571, r_control=None, cluster_n=14,
        effect="簇均 ROI -25.71%，95%CI [-53.9%, +0.2%] —— 未通过",
        note="每信号一笔(max_roi)、DTE 按执行日、到期日缺价即丢弃、按日期簇聚合。",
        caveat="最初报的「38 笔 82% 胜率 +2.84%/笔」来自四处方法论错误叠加："
               "①一个信号算多笔而报告只取年化最高者(策略定义漂移) "
               "②DTE 用 obs_day 错一天 ③到期日缺价向后取(look-ahead) "
               "④品种-日当独立样本。修正后为负"),
    "credit_wall_aggressive": Validation(
        key="credit_wall_aggressive", label="墙位卖方价差·激进档",
        kind="corr", n=18, hits=0, p_value=0.713, baseline=0.0,
        r=-0.0557, r_control=None, cluster_n=16,
        effect="簇均 ROI -5.57%，95%CI [-26.4%, +10.6%] —— 三档里最接近零",
        note="同稳健档口径。九个组合(3规则×3档)全部负簇均 ROI。",
        caveat="胜率 67% 但簇均 ROI 为负 —— 胜率高不等于赚钱，"
               "这正是不能用二项检验代替净收益检验的原因(codex P1-8)"),
    "kelly_sizing": Validation(
        key="kelly_sizing", label="Kelly 仓位（由平均盈亏反解）",
        kind="corr", n=0, hits=0, p_value=1.0, baseline=0.0, r=0.0,
        effect="未验证：二项 Kelly 假设每次只有两个固定结果，"
               "而实际分布是小赢/小亏/全损/指派尾部的混合",
        note="f*=(p·b−q)/b，b 由平均盈利÷平均亏损得到。",
        caveat="codex 2026-08-31 P1-9：正确做法是对逐笔收益分布直接最大化 "
               "Σlog(1+f·r_i)，并按日期簇重采样、对参数不确定性折扣。"
               "⚠️ 其输入（credit_wall 各档胜率/期望）现已证伪，"
               "在策略本身重新通过验证前，Kelly 结果无意义"),
    "expected_move": Validation(
        key="expected_move", label="可判定率预告波动幅度",
        kind="corr", n=66, hits=0, p_value=0.048, baseline=0.0,
        r=0.243, r_control=0.053,
        effect="预告的是幅度不是方向：高低两组命中率 66% vs 65%（一样），"
               "当日波动 1.09% vs 0.55%（翻倍）",
        note="相关性检验，非命中率检验：可判定率 vs 当日波动 r=+0.243 (t=2.01)；"
             "vs 是否命中 r=+0.053（对照）。",
        caveat="同批数据第二次找模式；≥80% 档仅 6 笔"),
}


def get(key: str) -> Validation | None:
    return REGISTRY.get(key)


def badge(key: str) -> str:
    """给报告用的一行状态串。找不到就明说未登记，不许沉默。"""
    v = REGISTRY.get(key)
    if v is None:
        return "⚠️ 未登记验证状态（不得作为交易依据）"
    icon = {"已验证": "✅", "待簇修正": "🟠",
            "样本不足": "🟡", "未验证": "⚠️"}.get(v.status, "⚠️")
    if v.kind == "corr":
        # 相关型同样要过簇修正才算已验证（codex 2026-08-31 P1-12）
        icon = "✅" if (v.significant and v.cluster_corrected) else (
            "🟠" if v.significant else "🟡")
    s = f"{icon} {v.status}：{v.summary()}"
    if v.caveat:
        s += f"　⚠️ {v.caveat}"
    return s
