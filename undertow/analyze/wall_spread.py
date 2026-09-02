"""墙位卖方价差 v3 —— 三步法定版（2026-09-02）

取代 v1(credit_wall.py) 与 v2。v2 的参数与绩效全部作废，原因见 git 历史：
回测用 snapshot.spot 当开仓价，而 46 个快照里 34 个的 spot 不是当天的价。

═══════════════════════════════════════════════════════════════════════
用户 2026-09-02 定的三步法。每一步单独测、单独定，不混在一个回测里。
完整数据与推导见 docs/wall_spread_3steps.md
═══════════════════════════════════════════════════════════════════════

① 选墙　gamma.pick_sell_wall()
    最大墙 = 最近墙 → 卖最大墙
    最大墙 ≠ 最近墙 → 朝现价挪一层；挪过去缓冲 <3% 则退回最大墙
    最大墙自己也 <3% → 弃权
    实测（SLV 43 个可交易日，判定=用 D−1 结构算的墙、D 当日收盘破没破）：
        put 覆盖 98% 破墙 0.0%；call 覆盖 84% 破墙 0.0%

② 建仓　DTE 2~4、宽 2~3 档、卖在墙上或墙内 1 档
    · 短 DTE 是双赢不是权衡：theta 临近到期最快 + 暴露时间最短。
      DTE2 净日均 $2.14(破7%) ≈ DTE14 $2.07(破45%)，收益相同风险差六倍。
    · 宽 2~3 档的"平衡破墙率"最高（最耐打）；宽 1 档被手续费吃掉
      （DTE1 手续费占权利金 93%），宽 5 档破满亏放大更快。
    · 墙内 1 档：**2026-09-02 撤回「免费」的结论**。按【到期】判定它的破墙率
      确实与墙上相同，但配上第三步的破卖腿平仓规则（逐日判定）就不成立：
          持有期内曾破卖腿的触发率   墙上 0%   墙内1档 7%~23%
      触发的那几笔全是「擦破又回来」（跌破 $0.11 后恢复），平仓即平错：
          put 墙内1档 DTE7  破卖腿即平 +$10.7  vs  持有到期 +$14.1（0对/3错）
          put 墙上   DTE7  两者完全相同 +$10.6（从不触发）
      墙内 1 档多收的 24% 权利金，抵不上被假破墙误伤的损失。故只卖墙上。

③ 出场　只在【收盘越过卖腿】时平仓；换墙不平、浮盈不平
    两侧对照（SLV 墙上 宽3档 DTE7，call=逆势 / put=顺势 / 合计）：
        持有到期      −329 / +249 / −80
        破卖腿即平     −80 / +249 / +169   ← 唯一两侧都不吃亏
        换墙即平      −302 / +113 / −189   ← 坏规则
        破墙或浮盈50%  −21 / +116 / +95
    破卖腿即平：逆势砍掉 76% 亏损，顺势一次都不触发。
    换墙即平平掉 27~75% 仓位却几乎不减亏，还把最差单笔从 −$2 恶化到 −$45。

═══════════════════════════════════════════════════════════════════════
⚠️⚠️ 未通过的生死线 —— 每次输出候选都必须一并显示
═══════════════════════════════════════════════════════════════════════
平衡破墙率（DTE 2~4、宽 2~3 档）  2.9% ~ 4.8%
call 侧持有到期实测破墙率          7%  ~ 8%

即：按期权定价，破墙率超过 ~4% 这套就不赚钱；而唯一的逆势样本
（call 侧，样本期白银 +9.7%）实测是 7~8%。第三步的破墙平仓把亏损
砍掉 76%，是把结果拉回平衡线的关键，但它是补救不是余量。

put 侧全程 0 破墙、年化 +400%~580%，那是样本期单边上涨送的，不是策略挣的。
**整套东西未经跌市验证。** 输出候选 ≠ 建议下单。

券商约束见 docs/broker/longbridge_margin.md：长桥的组合保证金只认
Covered Call/Put，价差与铁鹰不减免 —— 铁鹰收两份保证金，
资金效率腰斩（年化 +197% → +107%），且即便按标准一份也只有单边 put 的 41%。
故本模块**不出铁鹰候选**，只出单边 put / call，由用户自己决定做哪边或都做。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from undertow.analyze.gamma import pick_sell_wall

FEE_PER_LEG = 0.80
FEE_PER_TRADE = FEE_PER_LEG * 4          # 4 腿；提前平仓再收一次

#: 第二步定的建仓参数。逐品种，未列出的不出候选。
PARAMS: dict[str, dict] = {
    "silver": {
        "dte": (2, 4),
        "width_n": (2, 3),
        # 2026-09-02 由 (0,1) 收窄为 (0,)：墙内1档会被假破墙误伤，见文件头 ②
        "offsets": (0,),                 # 只卖墙上
        "min_buf": 0.03,
        "min_credit_mult": 2.0,          # 权利金至少是手续费的 2 倍才有意义
        # 第 2.1 步实测的平衡破墙率区间与逆势实测破墙率，随候选一起显示
        "breakeven_rate": (0.029, 0.048),
        "adverse_rate": (0.07, 0.08),
    },
}

#: 2026-09-02 用户定：目前只激活白银。
ACTIVE = {"silver"}


@dataclass(frozen=True)
class Candidate:
    kind: str                 # "P" / "C"
    expiry: date
    dte: int
    sell: float
    buy: float
    wall: float
    offset: int               # 0=墙上、1=墙内一档
    width_n: int
    credit: float             # 每张净收权利金（$，按组合单中价让 25% 点差）
    width: float              # 实际宽度（$）
    spot: float
    wall_rule: str            # 基准 / 上挪一层 / 退回最大墙

    @property
    def occupancy(self) -> float:
        """单边一份保证金。铁鹰在长桥要两份，见 docs/broker/longbridge_margin.md。"""
        return self.width - self.credit

    @property
    def max_loss(self) -> float:
        return self.occupancy + FEE_PER_TRADE

    @property
    def net_credit(self) -> float:
        return self.credit - FEE_PER_TRADE

    @property
    def roi(self) -> float:
        return self.net_credit / self.occupancy if self.occupancy > 0 else 0.0

    @property
    def net_daily(self) -> float:
        return self.net_credit / self.dte if self.dte else 0.0

    @property
    def buffer_pct(self) -> float:
        return abs(self.sell / self.spot - 1) * 100

    @property
    def fee_share(self) -> float:
        return FEE_PER_TRADE / self.credit if self.credit > 0 else float("inf")

    @property
    def breakeven_rate(self) -> float:
        """盈亏平衡所需的最高破墙率：守住赚 ÷ (守住赚 + 破满亏)。"""
        win, loss = self.net_credit, self.width - self.credit + FEE_PER_TRADE
        return win / (win + loss) if win + loss > 0 else 0.0

    @property
    def label(self) -> str:
        pos = "墙上" if self.offset == 0 else f"墙内{self.offset}档"
        return (f"{'卖put' if self.kind == 'P' else '卖call'} "
                f"{self.sell:g}/{self.buy:g} {self.expiry} ({self.dte}天, {pos})")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    puts: list[Candidate] = field(default_factory=list)
    calls: list[Candidate] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    @property
    def all(self) -> list[Candidate]:
        return self.puts + self.calls


def _fill(sell, buy, give: float = 0.25) -> float:
    """开仓成交价：组合单中价往不利方向让 give。

    「卖腿吃 bid、买腿吃 ask」对窄价差是毁灭性假设 —— 它把 SLV 55/54
    一周价差的 $7 权利金算成 $0，进而得出「手续费比权利金大」的荒谬结论。
    """
    sb, sa = sell.bid or 0, sell.ask or 0
    bb, ba = buy.bid or 0, buy.ask or 0
    mid = ((sb + sa) / 2 - (bb + ba) / 2) * 100
    worst = (sb - ba) * 100
    return mid + (worst - mid) * give


def close_cost(sell, buy, give: float = 0.25) -> float:
    """平仓成本（正数=要付出）：买回卖腿吃 ask 方向、卖出买腿吃 bid 方向。"""
    sb, sa = sell.bid or 0, sell.ask or 0
    bb, ba = buy.bid or 0, buy.ask or 0
    mid = ((sb + sa) / 2 - (bb + ba) / 2) * 100
    worst = (sa - bb) * 100
    return mid + (worst - mid) * give


def propose(snap, instrument: str, obs: date, execution_date: date,
            spot: float) -> Verdict:
    """给出该品种当日的卖方价差候选（put 与 call 分别列出，不合成铁鹰）。

    ⚠️ spot 必填且必须是【决策时已知】的价格（真实日线 C[D−1] 或实时报价）。
    绝不可回退 snap.spot —— 46 个快照里 34 个的 spot 不是当天的价。
    """
    if instrument not in ACTIVE:
        return Verdict(False, f"{instrument} 未激活（当前只激活：{'、'.join(sorted(ACTIVE)) or '无'}）")
    p = PARAMS[instrument]
    lo_dte, hi_dte = p["dte"]
    out: dict[str, list[Candidate]] = {"P": [], "C": []}
    walls: dict[str, dict] = {}

    for kind in ("P", "C"):
        w = pick_sell_wall(snap, obs, spot, kind, min_buf=p["min_buf"])
        if w is None:
            continue
        walls[kind] = w
        W = w["strike"]
        legs: dict[date, dict[float, object]] = defaultdict(dict)
        for c in snap.contracts:
            if c.kind != kind or c.bid is None or not c.ask:
                continue
            if lo_dte <= (c.expiry - execution_date).days <= hi_dte:
                legs[c.expiry][c.strike] = c
        for exp in sorted(legs):
            ks = sorted(legs[exp])
            if W not in ks:
                continue
            i = ks.index(W)
            for off in p["offsets"]:
                si = i + off if kind == "P" else i - off
                if si < 0 or si >= len(ks):
                    continue
                S = ks[si]
                if (kind == "P" and S >= spot) or (kind == "C" and S <= spot):
                    continue                      # 不卖实值
                for wn in p["width_n"]:
                    bi = si - wn if kind == "P" else si + wn
                    if bi < 0 or bi >= len(ks):
                        continue
                    B = ks[bi]
                    credit = _fill(legs[exp][S], legs[exp][B])
                    width = abs(B - S) * 100
                    if width <= credit:
                        continue
                    if credit <= FEE_PER_TRADE * p["min_credit_mult"]:
                        continue
                    out[kind].append(Candidate(
                        kind=kind, expiry=exp,
                        dte=(exp - execution_date).days,
                        sell=S, buy=B, wall=W, offset=off, width_n=wn,
                        credit=credit, width=width, spot=spot,
                        wall_rule=w["rule"]))

    for k in out:
        out[k].sort(key=lambda c: -c.net_daily)
    if not out["P"] and not out["C"]:
        return Verdict(False, _diagnose(snap, walls, spot, execution_date, p),
                       params=p)
    return Verdict(True,
                   f"DTE {lo_dte}~{hi_dte}、宽 {'/'.join(map(str, p['width_n']))} 档、"
                   f"墙上或墙内 1 档；出场只认「收盘越过卖腿」",
                   puts=out["P"], calls=out["C"], params=p)


def _diagnose(snap, walls, spot, execution_date, p) -> str:
    """没有候选时说清楚卡在哪 —— 干巴巴一句「无候选」没有信息量。

    2026-09-02 实测暴露的结构性矛盾：第一步选出的墙缓冲越厚越安全，
    但缓冲厚 + DTE 短 = 时间价值几乎为零。第二步测出「DTE 2~4 最优」时
    样本均缓冲是 6.8%；缓冲到 8%+ 时短期权根本收不到钱。
    **DTE 的最优区间隐含了缓冲条件，两者不独立。**
    所以这里要报告：放宽到多少天才够权利金，以及那个 DTE 的逆势破墙率。
    """
    lo_dte, hi_dte = p["dte"]
    thr = FEE_PER_TRADE * p["min_credit_mult"]
    parts = []
    for kind, nm in (("P", "put"), ("C", "call")):
        if kind not in walls:
            parts.append(f"{nm} 侧无合格墙（缓冲 <{p['min_buf']:.0%} 或无结构墙）")
            continue
        W = walls[kind]["strike"]
        buf = walls[kind]["buf_pct"]
        legs: dict[date, dict[float, object]] = defaultdict(dict)
        for c in snap.contracts:
            if c.kind == kind and c.bid is not None and c.ask:
                legs[c.expiry][c.strike] = c
        need = None
        for exp in sorted(legs):
            d = (exp - execution_date).days
            if d < lo_dte or W not in legs[exp]:
                continue
            ks = sorted(legs[exp])
            i = ks.index(W)
            best = 0.0
            for wn in p["width_n"]:
                bi = i - wn if kind == "P" else i + wn
                if 0 <= bi < len(ks):
                    best = max(best, _fill(legs[exp][W], legs[exp][ks[bi]]))
            if best > thr:
                need = (d, best)
                break
        if need:
            parts.append(
                f"{nm} 墙 {W:g}（缓冲 {buf:.1f}%）在 {lo_dte}~{hi_dte} 天内"
                f"权利金不足 ${thr:.1f}；要放宽到 DTE {need[0]} 才收得到 "
                f"${need[1]:.1f} —— 但那个持有期的逆势破墙率远超平衡率，不建议")
        else:
            parts.append(f"{nm} 墙 {W:g}（缓冲 {buf:.1f}%）各到期权利金均不足 ${thr:.1f}")
    return "；".join(parts)


def should_exit(kind: str, sell_strike: float, close_px: float) -> tuple[bool, str]:
    """第三步定版出场规则：**只**在收盘越过卖腿时平仓。

    换墙不平、浮盈不平 —— 两者在顺势侧都白白让出利润，逆势侧又几乎不减亏。
    用 close_px（收盘价），不是盘中价：破墙口径一律以收盘为准。
    """
    breached = (close_px > sell_strike) if kind == "C" else (close_px < sell_strike)
    if not breached:
        return False, f"收盘 {close_px:g} 未越过卖腿 {sell_strike:g} → 持有"
    gap = abs(close_px / sell_strike - 1) * 100
    return True, (f"收盘 {close_px:g} 已越过卖腿 {sell_strike:g}"
                  f"（{gap:.1f}%）→ 平仓")
