"""账户风控政策：唯一来源、带版本（Codex 008 G07 / S05）。

此前同一条规则散在三处：AGENTS.md 写「单笔止损风险 ≤10%、单笔最大亏损 ≤20%」；
sizing.size 只看 Kelly 与「一组是否超过购买力」，总组数不受限（复现：净资产 1000、购买力 100 → 5 组、占用 500）；
shadow.contracts_allowed 又自己写了一遍 20%。现在只在这里定义，所有调用方读它。

原则：
- Kelly 只能在这些上限【之内】进一步减少组数，不能成为放宽上限的理由。
- 期权价差 1 组不可分：算出来不到 1 组就是 0 组，不上调风险去凑整数张。
- 任何输入非有限、缺失、≤0 → 0 组并说明原因；未知不是通过。
- 全账户同时未平仓最大亏损的上限【用户尚未设定】：None = 只报告合计、不判定，并在输出里明说。
改任何数值 = 升 VERSION。
"""
from __future__ import annotations

import math

VERSION = "risk-policy-v1-20260926"
POLICY = {
    "version": VERSION,
    "source": "AGENTS.md 五、金融语义：仓位双层限额",
    "max_stop_risk_frac": 0.10,        # 单笔【止损情景损失】≤ 净资产 10%（正常行情，定仓位）
    "max_loss_frac": 0.20,             # 单笔【最大亏损】≤ 净资产 20%（跳空硬上限）
    "cluster_max_loss_frac": 0.20,     # 同簇（金银、股指…）同时未平仓最大亏损合计 ≤ 20%（草案，沿用影子账 P5）
    "account_max_loss_frac": None,     # 全账户同时未平仓最大亏损合计：未设定 → 只报告，不判定
    "kelly_role": "只能在上述上限内减少组数，不能放宽",
}


# 每条限额的状态（Codex 033 C2：「当前实现」与「用户已确认」必须分得清；未决项显示未决，不伪称硬性用户规则）。
# 数值与执行行为不变（既有 10%/20% 不擅自放宽）；改任何状态或数值 = 升 VERSION。
POLICY_META = {
    "max_stop_risk_frac": {"status": "生效", "source": "AGENTS.md 五节；用户私有档案 size_cap",
                           "effective_from": "2026-09-26", "confirmed_by": "用户（档案 size_cap 条）"},
    "max_loss_frac": {"status": "生效", "source": "AGENTS.md 五节；用户私有档案 size_cap",
                      "effective_from": "2026-09-26", "confirmed_by": "用户（档案 size_cap 条）"},
    "cluster_max_loss_frac": {"status": "草案（实现中参与组数计算，但用户未确认）", "source": "影子账 P5 沿用",
                              "effective_from": "2026-09-26", "confirmed_by": None},
    "account_max_loss_frac": {"status": "未决（用户未设定）", "source": None, "effective_from": None, "confirmed_by": None},
    "_pending_conflicts": ["conflict_pct_sizing_vs_jit_funding（按净资产百分比 vs 用多少入多少，2026-10-06 登记，待讨论）",
                           "conflict_refill_vs_trading（亏损后不追加入金的字面含义，2026-10-06 登记，待讨论）"],
}


def policy_status_lines(policy: dict = POLICY, meta: dict = POLICY_META) -> list[str]:
    """给研报/体检显示用：每条限额的现值与状态；未决与草案显式标出。"""
    out = []
    for k, m in meta.items():
        if k.startswith("_"):
            continue
        v = policy.get(k)
        out.append(f"{k} = {('%.0f%%' % (v * 100)) if isinstance(v, (int, float)) else '未设定'}：{m['status']}"
                   + (f"（确认：{m['confirmed_by']}）" if m.get("confirmed_by") else ""))
    out += [f"待讨论：{c}" for c in meta.get("_pending_conflicts", [])]
    return out


def _finite_pos(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def max_units(*, net_assets, unit_max_loss, unit_stop_loss=None, buying_power=None, unit_occupancy=None,
              cluster_open_max_loss: float = 0.0, account_open_max_loss: float = 0.0,
              policy: dict = POLICY) -> tuple[int, list[str]]:
    """在政策与资金约束下最多可开几组。返回 (组数, 约束说明列表)；组数可为 0。

    unit_max_loss：每组最大亏损（必填；未知 → 0 组）。unit_stop_loss：每组止损情景损失（缺省按最大亏损，保守）。
    buying_power / unit_occupancy：给了就同时受购买力约束。cluster/account_open_max_loss：同簇/全账户已占用的
    最大亏损（跨日未平仓也算）。
    """
    notes: list[str] = []
    if not (isinstance(net_assets, (int, float)) and math.isfinite(net_assets)):
        return 0, ["净资产未知或非有限数 → 0 组（未知不是通过）"]
    if net_assets <= 0:
        return 0, [f"净资产 ${net_assets:,.2f} ≤ 0 → 0 组"]
    if not _finite_pos(unit_max_loss):
        return 0, ["每组最大亏损未知/非正 → 0 组（风险未封顶或算不出，不开）"]
    stop = unit_stop_loss if unit_stop_loss is not None else unit_max_loss
    if not _finite_pos(stop):
        return 0, ["每组止损情景损失未知/非正 → 0 组"]
    caps = {
        "最大亏损 ≤{:.0%}".format(policy["max_loss_frac"]): int((policy["max_loss_frac"] * net_assets) // unit_max_loss),
        "止损风险 ≤{:.0%}".format(policy["max_stop_risk_frac"]): int((policy["max_stop_risk_frac"] * net_assets) // stop),
    }
    if policy.get("cluster_max_loss_frac") is not None:
        room = policy["cluster_max_loss_frac"] * net_assets - max(0.0, cluster_open_max_loss)
        caps["同簇合计 ≤{:.0%}（草案，用户未确认）".format(policy["cluster_max_loss_frac"])] = max(0, int(max(0.0, room) // unit_max_loss))
    if policy.get("account_max_loss_frac") is not None:
        room = policy["account_max_loss_frac"] * net_assets - max(0.0, account_open_max_loss)
        caps["全账户合计 ≤{:.0%}".format(policy["account_max_loss_frac"])] = max(0, int(max(0.0, room) // unit_max_loss))
    else:
        notes.append("全账户同时未平仓最大亏损上限未设定：只报告合计，不判定")
    if buying_power is not None or unit_occupancy is not None:
        if not (isinstance(buying_power, (int, float)) and math.isfinite(buying_power)) or not _finite_pos(unit_occupancy):
            return 0, notes + ["购买力或每组占用未知/非有限 → 0 组"]
        caps["购买力"] = max(0, int(buying_power // unit_occupancy))
    n = max(0, min(caps.values()))
    binding = [k for k, v in caps.items() if v == n]
    notes.insert(0, f"{n} 组；受限于：{'、'.join(binding)}（{policy['version']}）")
    return n, notes


# ════════════════════════════════════════════════════════════════════════════════════
# 实盘试行政策 B（Codex 043 WP2）。上面的 POLICY（v1，10%/20%）冻结，只供历史研究记录与影子账研究预算按原版本复现；
# 面向真实账户的只读入口（风险汇总、账户可执行账）显式传 POLICY_B。改任何数值 = 升 VERSION_B。
# 用户 2026-10-14 原话（选项 B）：「单笔亏损，止损按 200 ，最大按 300 如何？」「B 吧」；下限的理由：
# 「不然反而会因为风险控制，选择风险更大的价格……当本金增长后，比例超过了最低，则按比例来。」
# ════════════════════════════════════════════════════════════════════════════════════

VERSION_B = "risk-policy-v2-live-trial-B-20261014"
POLICY_B = {
    "version": VERSION_B,
    "profile_id": "live-trial-B",
    "planned_trial_capital_usd": 500.0,                       # 计划情景，不是到账事实
    "stop_loss": {"frac": 0.15, "floor_usd": 200.0},          # 整笔、含费的计划止损损失 ≤ max(15%·R, $200)
    "max_loss": {"frac": 0.30, "floor_usd": 300.0},           # 整笔、含费的理论最大亏损 ≤ max(30%·R, $300)
    "big_position": {"threshold_usd": 150.0, "max_count": 1},  # 理论最大亏损 > $150 的逻辑持仓同时最多 1 笔
    "categories": {"metals": {"frac": 0.40, "floor_usd": 300.0}, "other": {"frac": 0.20, "floor_usd": 150.0}},
    "account": {"frac": 0.60, "floor_usd": 500.0},
    "take_profit_gain_frac": 0.90,
    "pause": {"drawdown_frac": 0.50, "consecutive_losses": 4},
    "structures": "只做限定风险的借记/贷记价差；不裸卖、不做期货、不用融资杠杆",
}
METALS = frozenset({"gold", "silver", "GLD", "SLV", "GC", "SI", "XAU", "XAG", "PAXG", "IAU", "SIVR"})

POLICY_B_META = {
    "planned_trial_capital_usd": {"status": "已决定（计划情景）", "source": "用户 2026-10-14", "confirmed_by": "用户"},
    "stop_loss": {"status": "已决定", "source": "用户 2026-10-14 选项 B", "confirmed_by": "用户"},
    "max_loss": {"status": "已决定", "source": "用户 2026-10-14 选项 B", "confirmed_by": "用户"},
    "big_position": {"status": "已决定", "source": "用户 2026-10-14 选项 B", "confirmed_by": "用户"},
    "categories": {"status": "已决定", "source": "用户 2026-10-14 选项 B", "confirmed_by": "用户"},
    "account": {"status": "已决定", "source": "用户 2026-10-14", "confirmed_by": "用户"},
    "take_profit_gain_frac": {"status": "已决定", "source": "用户 2026-10-14", "confirmed_by": "用户"},
    "pause": {"status": "已决定", "source": "用户 2026-10-14 选项 B", "confirmed_by": "用户"},
    "R_definition": {"status": "Codex 043 推荐口径", "source": "R = 当前可审计权益；初始试行资本另行冻结",
                     "confirmed_by": None},
    "_open": ["固定 20 个美股交易日的操作试行（Codex 建议、未校准，用户未确认）",
              "峰值回撤阈值、固定现金储备（未决定，只报告不判定）"],
}


# ════════════════════════════════════════════════════════════════════════════════════
# 开仓收益风险比下限（入场闸门，独立于额度政策；改数值 = 升 PAYOFF_FLOOR_VERSION）。
# 起因：2026-10-08 用户批次 GLD 370/365P 当天到期收 0.07，含费最大收益 $3.8 对最大亏损 $496（要赢 99.2% 才打平），
# 当时唯一的检查是「权利金扣费后 > 0」，于是照开。用户 2026-10-14：「权利金 0.07，就不该开仓吧」；
# 对「含费最大收益 < 最大亏损的 10% 就不开、提示改更远到期」答「两件事都可以」。
# 10% ⇔ 理论满收益/满亏损两点模型的打平胜率 ≤ 1/1.1 ≈ 90.9%；赢时按 90% 止盈拿 0.9G、亏时吃满 L，则要 91.7%（Codex 044 E3）。
# 两者都不是实际胜率门槛：止损、提前退出、费用滑点会继续改变平均赢亏。【未校准】，只是把明显不值的单挡掉。
# 不放进 POLICY_B / evaluate_budget：影子账 v5 的预算按其预登记版本复现，不能中途加闸门。
# ════════════════════════════════════════════════════════════════════════════════════

PAYOFF_FLOOR_VERSION = "payoff-floor-v1-20261014"
PAYOFF_FLOOR = {"version": PAYOFF_FLOOR_VERSION, "min_net_gain_to_max_loss": 0.10}
PAYOFF_FLOOR_META = {"status": "已决定（用户 2026-10-14「两件事都可以」）；数值未校准",
                     "applies_to": "模拟仓用户批次与规则批次的新开仓（贷记与借记价差）；实盘开仓前同样检查",
                     "on_fail": "不开；有备选到期则改试更远到期，否则提示改用更远到期"}


def payoff_floor(net_max_gain, max_loss, rule: dict = PAYOFF_FLOOR) -> dict:
    """含费最大收益 / 含费最大亏损 ≥ 下限才放行。任一金额缺失/非有限/最大亏损 ≤ 0 → incomplete（不是通过）。
    返回 {"status", "ratio", "limit", "breakeven_win_rate", "breakeven_win_rate_tp90", "breakeven_basis", "version", "note"}。
    breakeven_win_rate = L/(G+L)：每次赢满净最大收益 G、每次亏满净最大亏损 L 的两点模型；
    breakeven_win_rate_tp90 = L/(0.9G+L)：赢时按 90% 止盈。都不是实际打平胜率（平均赢亏另算）。"""
    lim = rule["min_net_gain_to_max_loss"]
    out = {"status": "incomplete", "ratio": None, "limit": lim, "breakeven_win_rate": None, "breakeven_win_rate_tp90": None,
           "breakeven_basis": "理论两点模型：赢满 G / 亏满 L（tp90：赢 0.9G）", "version": rule["version"], "note": ""}
    if not (_num(net_max_gain) and _num(max_loss) and max_loss > 0):
        out["note"] = "最大收益或最大亏损未知/非正 → 无法判定"
        return out
    ratio = net_max_gain / max_loss
    bw = max_loss / (net_max_gain + max_loss) if net_max_gain > -max_loss else None
    tp = POLICY_B["take_profit_gain_frac"]
    bw90 = max_loss / (tp * net_max_gain + max_loss) if tp * net_max_gain > -max_loss else None
    out.update(ratio=round(ratio, 4), breakeven_win_rate=None if bw is None else round(bw, 4),
               breakeven_win_rate_tp90=None if bw90 is None else round(bw90, 4))
    if ratio + 1e-12 >= lim:
        out["status"] = "pass"
    else:
        out["status"] = "fail"
        out["note"] = (f"含费最大收益 ${net_max_gain:,.2f} / 最大亏损 ${max_loss:,.2f} = {ratio:.1%} < {lim:.0%}"
                       + (f"（理论满收益/满亏损打平胜率 {bw:.1%}，按 90% 止盈 {bw90:.1%}）" if bw is not None and bw90 is not None else "")
                       + "；建议改用更远到期")
    return out


def category_of(underlying: str) -> str:
    """金银 vs 其它。underlying 可以是品种键（gold）、期权根（GLD）或带后缀（GLD.US）。"""
    u = (underlying or "").split(".")[0]
    return "metals" if (u in METALS or u.upper() in METALS) else "other"


def limit_usd(rule: dict, R) -> float | None:
    """max(frac × R, floor)。R 未知/非有限 → None（未知不是通过）；R ≤ 0 时只剩下限。"""
    if not (isinstance(R, (int, float)) and not isinstance(R, bool) and math.isfinite(R)):
        return None
    return max(rule["frac"] * max(R, 0.0), rule["floor_usd"])


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _book_rows(open_positions: list) -> tuple[list[dict], list[str]]:
    """在场/已选入账本 → (计入的逻辑持仓, 问题清单)。问题非空 = 账本不完整（调用方给 incomplete，不是通过）。

    Codex 044 P1：旧版按 id 去重且 first-wins，而 id 是「标的·策略名」——同标签不同到期的三笔只算一笔。现在：
    - id 必须是稳定逻辑身份（risk_aggregate.combo_id：标的/到期/腿合约/张数），缺 id → 问题（不能都被 None 去重）；
    - 同 id 且内容完全一致 → 真重复，只算一次（幂等）；同 id 内容冲突 → 问题（不挑哪条）；
    - 金额必须是 ≥0 的有限数（负风险会抵消别的风险 → 拒绝）；state 只认 open / selected_pending。"""
    rows, problems, seen = [], [], {}
    keys = ("underlying", "stop_loss", "payoff_max_loss", "state")
    for p in open_positions:
        pid = p.get("id") if isinstance(p, dict) else None
        if not isinstance(pid, str) or not pid.strip():
            problems.append("持仓缺稳定 id（无法判断是否重复）")
            continue
        sig = tuple(p.get(k, "open" if k == "state" else None) for k in keys)
        if pid in seen:
            if seen[pid] != sig:
                problems.append(f"{pid}：同 id 两条内容冲突")
            continue
        seen[pid] = sig
        st = p.get("state", "open")
        if st not in ("open", "selected_pending"):
            if st not in ("closed", "cancelled"):
                problems.append(f"{pid}：未知状态 {st!r}")
            continue
        if not all(_num(p.get(k)) and p[k] >= 0 for k in ("stop_loss", "payoff_max_loss")):
            problems.append(f"{pid}：缺金额或金额为负")
            continue
        rows.append(p)
    return rows, problems


def _book_checks(rows: list[dict], R: float, policy: dict) -> list[dict]:
    """整本账（含拟新增的那一笔）在 B 下的全部检查：每笔止损/最大亏损、两类合计、全场合计、大仓数量。
    evaluate_book 与 evaluate_budget 共用这一处，两个入口对同一本账不会给出相反结论（Codex 044 P1-5）。"""
    ls, lm = limit_usd(policy["stop_loss"], R), limit_usd(policy["max_loss"], R)
    out = []
    for p in rows:
        for name, key, lim in (("单笔计划止损", "stop_loss", ls), ("单笔理论最大亏损", "payoff_max_loss", lm)):
            out.append({"check": f"{p['id']}·{name}", "status": "pass" if p[key] <= lim + 1e-9 else "fail",
                        "value": round(p[key], 2), "limit": lim, "note": "", "position": p["id"]})
    for cat in ("metals", "other"):
        tot = sum(p["payoff_max_loss"] for p in rows if category_of(p.get("underlying")) == cat)
        lim = limit_usd(policy["categories"][cat], R)
        out.append({"check": f"类别合计（{cat}）", "status": "pass" if tot <= lim + 1e-9 else "fail",
                    "value": round(tot, 2), "limit": lim, "note": "金银多空不抵扣"})
    tot = sum(p["payoff_max_loss"] for p in rows)
    lim = limit_usd(policy["account"], R)
    out.append({"check": "全场合计", "status": "pass" if tot <= lim + 1e-9 else "fail", "value": round(tot, 2),
                "limit": lim, "note": ""})
    thr, mx = policy["big_position"]["threshold_usd"], policy["big_position"]["max_count"]
    nb = sum(1 for p in rows if p["payoff_max_loss"] > thr)
    out.append({"check": "大仓数量", "status": "pass" if nb <= mx else "fail", "value": nb, "limit": mx,
                "note": f"理论最大亏损 > ${thr:.0f}"})
    return out


def evaluate_budget(*, candidate: dict, open_positions: list | None, capital: dict | None, pause: dict | None,
                    policy: dict = POLICY_B, max_qty: int = 20) -> dict:
    """B 政策下，一个候选最多可开几组（只读预算）。

    candidate：{"id", "underlying", "unit_stop_loss", "unit_payoff_max_loss", ["unit_occupancy"], ["qty_requested"]}
               —— 每组含费金额；整笔 = 每组 × 组数（同一笔多组仍是一笔逻辑持仓）。qty_requested 缺省 = max_qty；
               给了就必须是正整数（bool/小数/0/负数 → incomplete）。
    open_positions：在场与已选入待执行的逻辑持仓（整笔）：{"id"(稳定身份), "underlying", "stop_loss", "payoff_max_loss", "state"}；
                    state ∈ open / selected_pending（暂占额度）/ closed / cancelled。真重复只算一次；缺 id、同 id 冲突、
                    金额缺失或为负 → incomplete（不当 0、不挑一条）。None = 现有持仓未知 → incomplete。
    capital：{"equity"(R), "asof", ["buying_power"], ["stale"]}；pause：{"latched": bool, ["reason"]}，None = 状态未知。
    判定对象是【拟建后的整本账】（现有 + 本笔 k 组），与 evaluate_book 同一套检查：现有账本已超限（任一类别、单笔或大仓数）
    → 新增 0 组并点名超限项；不自动平旧仓、不换仓。资金能力另列 operation_status。"""
    reasons, checks = [], []
    out = {"policy_version": policy["version"], "profile_id": policy.get("profile_id"),
           "asof": (capital or {}).get("asof"), "policy_basis": "R = 当前可审计权益（Codex 043 推荐口径）",
           "checks": checks, "allowed_units": 0, "policy_allowed_units": 0, "budget_status": "incomplete",
           "operation_status": "unverified", "reasons": reasons}

    def chk(name, status, value=None, limit=None, note=""):
        checks.append({"check": name, "status": status, "value": value, "limit": limit, "note": note})

    # 1. 暂停
    if pause is None or not isinstance(pause.get("latched"), bool):
        chk("暂停状态", "incomplete", note="暂停状态未知或损坏 → 冻结新增")
    elif pause["latched"]:
        chk("暂停状态", "fail", note=f"已暂停：{pause.get('reason') or ''}；固定美元下限不覆盖暂停")
    else:
        chk("暂停状态", "pass")
    # 2. 数据完整
    R = (capital or {}).get("equity")
    if not _num(R):
        chk("当前权益 R", "incomplete", note="权益未知/非有限 → 无法定额度")
    elif (capital or {}).get("stale"):
        chk("当前权益 R", "incomplete", value=R, note="资金读数过期或与持仓不一致 → 未核实")
    else:
        chk("当前权益 R", "pass", value=R)
    us, um = candidate.get("unit_stop_loss"), candidate.get("unit_payoff_max_loss")
    want = candidate.get("qty_requested", max_qty)
    if not (_num(us) and us > 0 and _num(um) and um > 0):
        chk("候选经济量", "incomplete", note="每组止损损失/理论最大亏损未知或非正 → 不开")
    elif not (isinstance(want, int) and not isinstance(want, bool) and want >= 1):
        chk("候选经济量", "incomplete", note=f"请求组数 {want!r} 不是正整数")
    elif not (isinstance(candidate.get("underlying"), str) and candidate["underlying"].strip()):
        chk("候选经济量", "incomplete", note="候选缺标的 → 无法归类")
    else:
        chk("候选经济量", "pass")
    rows = []
    if open_positions is None:
        chk("现有持仓", "incomplete", note="现有持仓风险未传入/未知 → 不能当作空仓")
    else:
        rows, probs = _book_rows(open_positions)
        chk("现有持仓", "incomplete" if probs else "pass", note="；".join(probs[:5]))
    bad = [c for c in checks if c["status"] != "pass"]
    if bad:                                                # 已暂停 = 确定的 0（fail）；其余缺口 = incomplete（不是通过）
        out["budget_status"] = "fail" if checks[0]["status"] == "fail" else "incomplete"
        reasons += [f"{c['check']}：{c['note'] or c['status']}" for c in bad]
        return out

    cid = candidate.get("id") or "候选"

    def book_at(k):
        return _book_checks(rows + [{"id": f"{cid}（本笔 ×{k}）", "underlying": candidate["underlying"],
                                     "stop_loss": us * k, "payoff_max_loss": um * k}], R, policy)
    n = 0
    for k in range(1, want + 1):
        if all(c["status"] == "pass" for c in book_at(k)):
            n = k
        else:
            break
    final = book_at(max(n, 1))                             # 0 组时报告 1 组卡在哪；≥1 组时报告该组数下的各项
    pre = [c for c in _book_checks(rows, R, policy) if c["status"] == "fail"]
    checks += final
    out["allowed_units"] = out["policy_allowed_units"] = n
    out["budget_status"] = "pass" if n >= 1 else "fail"
    if pre:
        reasons.append("现有账本已超限（新增冻结，已有仓位按原计划管理、不自动平仓）：" +
                       "、".join(f"{c['check']} {c['value']} > {c['limit']}" for c in pre))
    if n == 0:
        reasons += [f"{c['check']} {c['value']} > {c['limit']}" for c in final if c["status"] == "fail"
                    and c not in pre and not any(c["check"] == q["check"] for q in pre)]
    # 资金能力：独立于政策；美元下限不产生资金或保证金能力
    bp, occ = (capital or {}).get("buying_power"), candidate.get("unit_occupancy")
    if n >= 1 and um * n > R:                              # 美元下限只是政策额度，不产生资金：权益不够亏这一笔就开不了
        out["operation_status"] = "insufficient_equity"
        reasons.append(f"权益 ${R:,.2f} < 本笔理论最大亏损 ${um * n:,.2f}（政策下限不产生资金）")
    elif _num(bp) and _num(occ) and occ > 0 and n >= 1:
        if occ * n > bp:
            out["operation_status"] = "insufficient_buying_power"
            reasons.append(f"购买力 ${bp:,.2f} < 占用 ${occ * n:,.2f}（政策通过 ≠ 能开）")
        else:
            out["operation_status"] = "capital_ok_permissions_unverified"
    reasons.append("政策预算通过不等于券商可开：价差权限、组合占用、到期/指派处置均未核实")
    return out


def evaluate_book(open_positions: list | None, capital: dict | None, policy: dict = POLICY_B) -> dict:
    """现有持仓本身是否在 B 的限额内（状态核查，不涉及新增；暂停只冻结新增，不在这里判）。
    与 evaluate_budget 共用 _book_rows / _book_checks。任一成员缺 id、冲突、金额未知 → incomplete。"""
    R = (capital or {}).get("equity")
    if open_positions is None or not _num(R):
        return {"policy_version": policy["version"], "overall": "incomplete",
                "checks": [{"check": "现有持仓/权益", "status": "incomplete", "value": None, "limit": None,
                            "note": "持仓或权益未知"}]}
    rows, probs = _book_rows(open_positions)
    checks = _book_checks(rows, R, policy)
    if probs:                                              # 合计缺成员：已知部分超限仍是确定的 fail；没超限 ≠ pass
        for c in checks:
            if "position" not in c and c["status"] == "pass":
                c["status"], c["note"] = "incomplete", (c["note"] + "；" if c["note"] else "") + "仅已知部分"
        checks.append({"check": "账本完整性", "status": "incomplete", "value": len(probs), "limit": None,
                       "note": "；".join(probs[:5])})
    sts = {c["status"] for c in checks}
    return {"policy_version": policy["version"], "checks": checks,
            "overall": "fail" if "fail" in sts else ("incomplete" if "incomplete" in sts else "pass")}
