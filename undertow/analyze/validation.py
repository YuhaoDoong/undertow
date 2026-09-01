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
    hits: int
    p_value: float
    baseline: float          # 对照基准（无脑做多的胜率等）
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
        return self.p_value < 0.05

    @property
    def status(self) -> str:
        if self.n == 0:
            return "未验证"
        if self.significant:
            return "已验证"
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
        caveat="样本区间仅 2026-06-25 起，且横盘日占比近半"),
    "tradeable_gate": Validation(
        key="tradeable_gate", label="可交易信息闸门（压力比 ≥2×）",
        n=62, hits=40, p_value=0.044, baseline=0.50, cluster_n=30,
        note="放行组 62 笔 65%、顺向 +0.46%；拦掉组 29 笔 41%、顺向 -0.12%。Fisher p=0.044。",
        caveat="共测 10 个阈值，Bonferroni 校正后 p=0.44 不再显著，待样本外验证"),
    "wall_space_vote": Validation(
        key="wall_space_vote", label="Gamma 墙位空间投票",
        n=83, hits=41, p_value=1.000, baseline=0.51, cluster_n=43,
        note="近端口径 49%、混算口径 54%、基准（无脑做多）51%。四个变体 p 全为 1.000。",
        caveat="换到期口径、换阈值形式都试过，均无改善——该因子本身无预测力"),
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
    icon = {"已验证": "✅", "样本不足": "🟡", "未验证": "⚠️"}[v.status]
    if v.kind == "corr":
        icon = "✅" if v.significant else "🟡"
    s = f"{icon} {v.status}：{v.summary()}"
    if v.caveat:
        s += f"　⚠️ {v.caveat}"
    return s
