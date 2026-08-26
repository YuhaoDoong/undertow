"""计划交易（planned trades）——把"等到位再动手"写成可监控、可执行的完整计划。

解决的真实问题：用户在新加坡时区，美股 21:30–04:00 SGT 正在睡觉，盯不了盘。
解法是【两段式】，而不是让 AI 代下单：
  **方案 B（本模块）**：记录完整计划 → 定时抓实时价 → 触发/破线就告警 → 算出该挂什么单；
  **方案 A（券商端）**：把这些单**预先挂成条件单**（LO/LIT/MIT/移动止损），
                        睡觉时由**券商**自动执行——毫秒级、不掉线、有交易所背书。

**硬边界（不可越）**：本模块**只读行情、只做计划与告警**，
**绝不下单/撤单/改单**——下单永远是用户自己在券商端的动作。
这既是 undertow 全项目的铁律，也保住了"执行前有一次人类停顿"这道防线
（用户档案里的冷静期/单日上限等规则，价值正在于此）。

计划落 gitignore 的 `data/soul/plans.json`，不进公开仓库。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

DEFAULT_PATH = Path("data/soul/plans.json")


@dataclass(frozen=True)
class Leg:
    symbol: str                # 长桥合约代码，如 SLV260918P60000.US
    action: str                # buy / sell（开仓动作）
    qty: int
    limit: float | None = None # 计划限价（每股）


@dataclass(frozen=True)
class Exits:
    """出场四问——缺一不可（对应档案里的 exit_first 铁律）。"""
    target: str = ""           # 盈利了结条件
    stop: str = ""             # 止损条件
    time: str = ""             # 时间了结
    edge: str = ""             # 边际了结
    # 持仓窗口内的【已知日程事件】——不是出场条件，是必须事先知道的跳空风险。
    # 分开列而不是塞进 stop：止损是价格触发，事件是日历触发，两者的应对完全不同
    # （事件可以选择减仓过、对冲过、或明确接受跳空；止损没得选）。
    # 不计入 complete 判定——出场四问齐全与否，跟有没有事件无关。
    event: str = ""            # 如「8/28 ET10:00 Fed主席讲话 + 非农基准修正」

    @property
    def complete(self) -> bool:
        return all([self.target, self.stop, self.time])


@dataclass(frozen=True)
class TradePlan:
    id: str
    underlying: str            # SLV / GLD / QQQ
    structure: str             # 人读的结构描述
    level: float               # 进场触发价（标的价）
    direction: str             # below（跌到）/ above（涨到）
    legs: list = field(default_factory=list)      # list[Leg]
    exits: Exits = field(default_factory=Exits)
    gate: str = ""             # 到位后仍须过的闸门
    size_note: str = ""        # 规模依据（张数 vs 净资产%）
    status: str = "waiting"    # waiting / active / done / cancelled
    created: str = ""
    note: str = ""

    def triggered(self, spot: float | None) -> bool | None:
        """标的现价是否已触发。无价返回 None。"""
        if spot is None or spot <= 0:
            return None
        return spot <= self.level if self.direction == "below" else spot >= self.level


def load_plans(path: Path | None = None) -> list[TradePlan]:
    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for r in raw.get("plans", []):
        out.append(TradePlan(
            id=r.get("id", ""), underlying=r.get("underlying", ""),
            structure=r.get("structure", ""), level=float(r.get("level", 0) or 0),
            direction=r.get("direction", "below"),
            legs=[Leg(**l) for l in r.get("legs", [])],
            exits=Exits(**r.get("exits", {})), gate=r.get("gate", ""),
            size_note=r.get("size_note", ""), status=r.get("status", "waiting"),
            created=r.get("created", ""), note=r.get("note", "")))
    return out


def save_plans(plans: list[TradePlan], path: Path | None = None) -> Path:
    p = Path(path) if path else DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"plans": [asdict(x) for x in plans]},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ————————————————————————————————————————— 监控


@dataclass(frozen=True)
class PlanAlert:
    plan_id: str
    kind: str            # 触发 / 接近 / 提示
    detail: str


NEAR_PCT = 1.5           # 距触发价 ≤ 此% 算"接近"


def check_plans(plans: list[TradePlan], spots: dict) -> list[PlanAlert]:
    """用实时标的价核每个 waiting 计划：已触发 / 接近触发。纯函数。"""
    out: list[PlanAlert] = []
    for p in plans:
        if p.status != "waiting":
            continue
        spot = spots.get(p.underlying)
        t = p.triggered(spot)
        if t is None:
            continue
        gap = (spot - p.level) / p.level * 100.0
        if t:
            out.append(PlanAlert(p.id, "触发",
                                 f"{p.underlying} {spot:.2f} 已{'跌破' if p.direction=='below' else '站上'}"
                                 f" {p.level:.2f} → {p.structure}（仍须过闸门：{p.gate}）"))
        elif abs(gap) <= NEAR_PCT:
            out.append(PlanAlert(p.id, "接近",
                                 f"{p.underlying} {spot:.2f}，距触发价 {p.level:.2f} 还有 {abs(gap):.1f}%"))
    return out


# ————————————————————————————————————————— 订单参数（供用户自己执行）


def render_orders(plan: TradePlan) -> str:
    """把计划翻译成**可照抄的下单参数**。

    ⚠️ 只输出参数文本供用户在券商端自行下单——本模块不执行任何交易。
    """
    L = [f"### {plan.id} — {plan.structure}",
         f"触发：{plan.underlying} {'跌到' if plan.direction=='below' else '涨到'} {plan.level:g}",
         f"闸门：{plan.gate or '—'}　规模：{plan.size_note or '—'}", ""]
    L.append("**进场腿（到位后自行下单）**：")
    L.append("")
    L.append("| 合约 | 方向 | 张数 | 限价 | 长桥单型 |")
    L.append("|---|---|---:|---:|---|")
    for lg in plan.legs:
        px = f"{lg.limit:.2f}" if lg.limit is not None else "市价"
        typ = "LO（限价）" if lg.limit is not None else "MO（市价）"
        L.append(f"| `{lg.symbol}` | {'买入' if lg.action=='buy' else '卖出'} | {lg.qty} | {px} | {typ} |")
    L.append("")
    if plan.exits.target or plan.exits.stop:
        L.append("**出场单（进场成交后立刻挂上，GTC）**：")
        if plan.exits.target:
            L.append(f"- 止盈：{plan.exits.target}")
        if plan.exits.stop:
            L.append(f"- 止损：{plan.exits.stop}")
        if plan.exits.time:
            L.append(f"- 时间：{plan.exits.time}")
        if plan.exits.edge:
            L.append(f"- 边际：{plan.exits.edge}")
        if plan.exits.event:
            L.append(f"- ⚠️ 窗口内事件：{plan.exits.event}")
        L.append("")
    if not plan.exits.complete:
        L.append("> ⚠️ 出场三要素（目标/止损/时间）未写全——按你的 `exit_first` 铁律，**不应进场**。")
        L.append("")
    L.append("> 以上为**参数参考**，下单由你自己在长桥端完成；本工具只读、不执行交易。")
    return "\n".join(L)


def render_plans_md(plans: list[TradePlan], alerts: list[PlanAlert] | None = None) -> str:
    L = ["# 计划交易（只读监控 · 执行由你自己完成）", ""]
    if alerts:
        L.append("## 🔔 当前告警")
        for a in alerts:
            icon = "🎯" if a.kind == "触发" else "⏳"
            L.append(f"- {icon} **[{a.kind}]** {a.detail}")
        L.append("")
    if not plans:
        L.append("- 暂无计划。用 `undertow plan --add` 或直接编辑 `data/soul/plans.json`。")
        return "\n".join(L)
    L.append("| 状态 | ID | 标的 | 触发 | 结构 | 出场齐全 |")
    L.append("|---|---|---|---|---|---|")
    for p in plans:
        arrow = "≤" if p.direction == "below" else "≥"
        L.append(f"| {p.status} | `{p.id}` | {p.underlying} | {arrow}{p.level:g} | {p.structure} | "
                 f"{'✅' if p.exits.complete else '❌ 缺'} |")
    L.append("")
    L.append("> 本模块**只读行情、只做计划与告警，绝不下单/撤单/改单**——"
             "下单永远是你自己在券商端的动作。")
    return "\n".join(L)
