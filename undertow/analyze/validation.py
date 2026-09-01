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
    """双侧二项检验：n 次里 k 次命中，与 p0 的差异有多容易被运气解释。"""
    if n <= 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i)
                            for i in range(k, n + 1)))


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
    m = n
    while m < cap:
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
    def rate(self) -> float:
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
        s = f"{self.hits}/{self.n} = {self.rate:.0%}（基准 {self.baseline:.0%}），p={self.p_value:.3f}"
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
        n=36, hits=None, p_value=None, baseline=None, cluster_n=None,
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
             "── 2026-09-01 逐条闸门检验（89 条压力比达标样本，去趋势）──\n"
             "① 主翼比：wing_ok=True 42条 D+1 51%/+0.29% ；False 47条 D+1 69%/+0.79%\n"
             "② 净建仓规模：scale_ok=True 89条，False 0条 —— 【89 次一次没拦过，是摆设】\n"
             "③ 逆向价格：contra_ok=True 31条 D+1 48%/−0.23%；False 11条 D+1 60%/+1.69%\n"
             "④ 合计：开火 27条 D+1 −0.23% vs 被拦 52条 D+1 +0.97%，"
             "Welch t=−1.828（抽稀后 −1.716），主翼比单独置换 p=0.255。\n"
             "被拦掉的最好几条：07-24 wti +10.68%、08-02 wti +10.00%、07-10 wti +8.60%、"
             "08-19 silver +7.98% —— 多数被主翼比拦下。",
        caveat="⚠️ |t| 均未达 2、blocked_n 仅 8~10，按模块自身标准一条都不成立，"
               "【不构成「闸门有害」的结论】。但 h=2/3/5 三个窗口方向一致（被拦组更好），"
               "这个方向须持续监控，不得因不显著就当噪音丢弃。"
               "行动项（按确定性排序）：\n"
               "  a) 净建仓规模闸门 89/89 全过 —— 这条【不需要显著性】就能断定是摆设，"
               "     要么调紧阈值要么删掉，别再算作『三道闸门』之一；\n"
               "  b) 报告应把【被闸门拦下的信号】也显示出来（标注未过闸门及卡在哪一条），"
               "     现在用户完全看不到它们，无从判断闸门是否误伤；\n"
               "  c) 样本到 n≥50（开火组）时重跑本条；在那之前"
               "     既不得以「闸门已验证」为由辩护，也不得据此拆闸门。"),
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
