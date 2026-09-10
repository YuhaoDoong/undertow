"""滚动收租的硬闸门 —— 把「利润确定，风险递增」从一句自知之明变成可执行的停止条件。

## 为什么需要它

外部实盘 SOP 的第 9 条是「滚雪球式收租：平旧卖新、行权价跟着标的挪」，
作者自己在图上标了 **「利润确定，风险递增」**，风险线里也写了「别把虚值压没」。
**他知道这件事危险。但「知道」和「有硬约束」是两回事** —— 那两句都是提示，
没有说虚值压到多少必须停、累计多少轮必须重置，所以它拦不住任何一次具体的滚动。

这正是本项目自己踩过的坑：墙位卖方价差全品种停用（wall-spread-suspended），
死因不是胜率不够高，而是**胜率很高、单次亏损吃掉几十次盈利**。
捡钢镚类结构的账不能按"这一轮赚不赚"算，要按"要滚多少轮才赚得回一次被打穿"算。

## 本模块回答的三个问题

1. **垫子还剩多厚**：卖腿离现价还有多远（%）、|delta| 到哪了。
   行权价跟着标的挪 = 每一轮都在把安全边际换成确定的小额收入。
2. **滚了多少轮、漂了多远**：累计轮次、行权价相对首轮漂移。
3. **要滚多少轮才赚得回一次被打穿**（`rounds_to_recover`）——
   **这是整个模块最该看的一个数**。它把"捡钢镚"从比喻变成一个整数。

## 设计边界

* **纯函数，无 I/O**：滚动历史由调用方喂入（手工记录或从成交回报推断），
  与 condor / calendar_spread 同构。本模块不读账户、不落盘。
* **不预测、不择时**：只回答"按当前状态该不该继续滚"，不回答"下一轮卖哪里"。
* 闸门是**风险约束**不是策略优化 —— 触发时给出的一律是"停/减/重置"，
  从不建议"再滚一轮试试"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# —— 垫子（虚值厚度）——
# 卖腿离现价的距离，占现价的百分比。跌破 → 这一轮已经不是"收租"而是"赌方向"。
CUSHION_STOP_PCT = 1.5    # 硬停：垫子薄于此，不得再滚
CUSHION_WARN_PCT = 3.0    # 预警：进入危险区
# 卖腿 |delta| 上限。delta 比距离更能反映"被打穿的概率"，因为它含了波动率与剩余时间。
DELTA_STOP = 0.35
DELTA_WARN = 0.28

# —— 轮次 ——
# 滚动本身会累积路径依赖：每轮的行权价都锚在上一轮的结果上，
# 连滚多轮后，结构离最初那个"观点"已经很远了。
ROUNDS_WARN = 6
ROUNDS_STOP = 10

# —— 捡钢镚判据 ——
# 累计收租 / 一次被打穿的亏损。低于此 → 赔率结构本身不成立。
RECOVER_RATIO_WARN = 0.5   # 累计收租还不到一次亏损的一半
_MULT = 100.0


@dataclass(frozen=True)
class RollRound:
    """一轮滚动。金额一律【每股】口径，正=收入。"""
    on: date
    strike: float          # 该轮卖腿行权价
    spot: float            # 该轮开仓时标的价
    credit: float          # 该轮净收（新卖腿收 − 旧卖腿平仓付），每股
    kind: str = "C"        # C / P
    delta: float | None = None   # 该轮开仓时卖腿 |delta|
    closed_cost: float = 0.0     # 平旧腿的实付（已含在 credit 里，单列供审计）


@dataclass(frozen=True)
class RollVerdict:
    action: str                  # 继续 / 预警 / 停止
    headline: str
    rounds: int = 0
    cum_credit: float = 0.0      # 累计收租，每股
    cum_credit_usd: float = 0.0
    cushion_pct: float | None = None      # 当前垫子（%）
    cur_delta: float | None = None
    strike_drift_pct: float | None = None # 行权价相对首轮漂移（%）
    max_loss_usd: float | None = None     # 一次被打穿的亏损（$/组合）
    rounds_to_recover: float | None = None
    reasons: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)


def assess(rounds: list[RollRound], *, spot: float,
           breach_loss: float | None = None) -> RollVerdict:
    """对一串滚动历史做体检。

    spot          当前标的价
    breach_loss   一次被打穿的亏损（每股）。**必须由调用方给**：
                  它取决于是否有保护腿、保护腿多远 —— 裸卖时理论无上限，
                  本模块不替使用者假设。给 None 则跳过 rounds_to_recover。
    """
    if not rounds:
        return RollVerdict("继续", "尚无滚动记录")
    rounds = sorted(rounds, key=lambda r: r.on)
    cur = rounds[-1]
    n = len(rounds)
    cum = sum(r.credit for r in rounds)

    # 垫子：卖 call 时行权价在上方为正，卖 put 时在下方为正
    if cur.kind == "C":
        cushion = (cur.strike - spot) / spot * 100
    else:
        cushion = (spot - cur.strike) / spot * 100
    drift = (cur.strike - rounds[0].strike) / rounds[0].strike * 100

    reasons: list[str] = []
    stops: list[str] = []

    # ① 垫子
    if cushion < CUSHION_STOP_PCT:
        stops.append(f"垫子仅剩 {cushion:.2f}%（< {CUSHION_STOP_PCT}%）——"
                     "这一轮已经不是收租，是赌方向")
    elif cushion < CUSHION_WARN_PCT:
        reasons.append(f"垫子 {cushion:.2f}% 已进入危险区（< {CUSHION_WARN_PCT}%）")

    # ② delta（比距离更可靠：含了波动率与剩余时间）
    d = abs(cur.delta) if cur.delta is not None else None
    if d is not None:
        if d >= DELTA_STOP:
            stops.append(f"卖腿 |Δ| {d:.3f} ≥ {DELTA_STOP}——被打穿的概率已经不是尾部事件")
        elif d >= DELTA_WARN:
            reasons.append(f"卖腿 |Δ| {d:.3f} 偏高（≥ {DELTA_WARN}）")

    # ③ 轮次：滚动累积路径依赖，越滚离最初那个观点越远
    if n >= ROUNDS_STOP:
        stops.append(f"已滚 {n} 轮（≥ {ROUNDS_STOP}）——结构离最初的观点已经很远，"
                     "该重置而不是再挪一次行权价")
    elif n >= ROUNDS_WARN:
        reasons.append(f"已滚 {n} 轮（≥ {ROUNDS_WARN}），注意路径依赖")

    # ④ 捡钢镚判据 —— 本模块最该看的一个数
    r2r = None
    max_loss_usd = None
    if breach_loss is not None and breach_loss > 0:
        max_loss_usd = breach_loss * _MULT
        avg = cum / n
        r2r = (breach_loss / avg) if avg > 0 else float("inf")
        ratio = cum / breach_loss
        if ratio < RECOVER_RATIO_WARN:
            reasons.append(
                f"累计收租 {cum * _MULT:,.0f}$ 仅为一次被打穿亏损 "
                f"{max_loss_usd:,.0f}$ 的 {ratio * 100:.0f}%")

    # 行权价漂移方向：跟着标的挪 = 把安全边际换成了确定的小额收入
    if abs(drift) > 0.01:
        reasons.append(f"行权价已从首轮 {rounds[0].strike:g} 挪到 {cur.strike:g}"
                       f"（{drift:+.1f}%）——每一次挪动都是拿垫子换租金")

    if stops:
        action, head = "停止", "⛔ 停止滚动：" + "；".join(stops)
    elif reasons:
        action, head = "预警", "⚠️ 可继续但已亮灯：" + "；".join(reasons[:2])
    else:
        action, head = "继续", f"垫子 {cushion:.2f}%、已滚 {n} 轮，暂无触发"

    if r2r is not None and r2r != float("inf"):
        head += f"｜按当前均租，要再滚 **{r2r:.1f} 轮**才赚得回一次被打穿"

    return RollVerdict(
        action=action, headline=head, rounds=n,
        cum_credit=round(cum, 4), cum_credit_usd=round(cum * _MULT, 2),
        cushion_pct=round(cushion, 3), cur_delta=round(d, 4) if d is not None else None,
        strike_drift_pct=round(drift, 3),
        max_loss_usd=round(max_loss_usd, 2) if max_loss_usd else None,
        rounds_to_recover=round(r2r, 2) if r2r not in (None, float("inf")) else None,
        reasons=reasons, stops=stops)


def render_md(v: RollVerdict) -> str:
    icon = {"停止": "⛔", "预警": "⚠️", "继续": "✅"}[v.action]
    L = [f"### 滚动收租体检 · {icon} {v.action}", "",
         f"- 已滚 **{v.rounds}** 轮，累计收租 **{v.cum_credit_usd:,.0f}$** / 组合"]
    if v.cushion_pct is not None:
        L.append(f"- 当前垫子 **{v.cushion_pct:.2f}%**"
                 + (f"，卖腿 |Δ| **{v.cur_delta:.3f}**" if v.cur_delta is not None else ""))
    if v.strike_drift_pct is not None:
        L.append(f"- 行权价累计漂移 **{v.strike_drift_pct:+.1f}%**")
    if v.rounds_to_recover is not None:
        L.append(f"- 一次被打穿 **−{v.max_loss_usd:,.0f}$**；"
                 f"按当前均租需再滚 **{v.rounds_to_recover:.1f} 轮**才赚得回")
    L.append("")
    for s in v.stops:
        L.append(f"- ⛔ {s}")
    for r in v.reasons:
        L.append(f"- ⚠️ {r}")
    L += ["",
          "> 「利润确定，风险递增」是这类结构的定义，不是它的缺点描述。",
          "> 本模块只给风险约束（停/减/重置），从不建议再滚一轮。"]
    return "\n".join(L)
