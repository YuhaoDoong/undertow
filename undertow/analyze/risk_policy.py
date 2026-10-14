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
# 10% ⇔ 打平胜率 ≤ 1/1.1 ≈ 90.9%，参照此前估算的卖墙价差约需 89–91% 胜率打平；【未校准】，只是把明显不值的单挡掉。
# 不放进 POLICY_B / evaluate_budget：影子账 v5 的预算按其预登记版本复现，不能中途加闸门。
# ════════════════════════════════════════════════════════════════════════════════════

PAYOFF_FLOOR_VERSION = "payoff-floor-v1-20261014"
PAYOFF_FLOOR = {"version": PAYOFF_FLOOR_VERSION, "min_net_gain_to_max_loss": 0.10}
PAYOFF_FLOOR_META = {"status": "已决定（用户 2026-10-14「两件事都可以」）；数值未校准",
                     "applies_to": "模拟仓用户批次与规则批次的新开仓（贷记与借记价差）；实盘开仓前同样检查",
                     "on_fail": "不开；有备选到期则改试更远到期，否则提示改用更远到期"}


def payoff_floor(net_max_gain, max_loss, rule: dict = PAYOFF_FLOOR) -> dict:
    """含费最大收益 / 含费最大亏损 ≥ 下限才放行。任一金额缺失/非有限/最大亏损 ≤ 0 → incomplete（不是通过）。
    返回 {"status", "ratio", "limit", "breakeven_win_rate", "version", "note"}；打平胜率 = 最大亏损 / (最大收益 + 最大亏损)。"""
    lim = rule["min_net_gain_to_max_loss"]
    out = {"status": "incomplete", "ratio": None, "limit": lim, "breakeven_win_rate": None, "version": rule["version"], "note": ""}
    if not (_num(net_max_gain) and _num(max_loss) and max_loss > 0):
        out["note"] = "最大收益或最大亏损未知/非正 → 无法判定"
        return out
    ratio = net_max_gain / max_loss
    bw = max_loss / (net_max_gain + max_loss) if net_max_gain > -max_loss else None
    out.update(ratio=round(ratio, 4), breakeven_win_rate=None if bw is None else round(bw, 4))
    if ratio + 1e-12 >= lim:
        out["status"] = "pass"
    else:
        out["status"] = "fail"
        out["note"] = (f"含费最大收益 ${net_max_gain:,.2f} / 最大亏损 ${max_loss:,.2f} = {ratio:.1%} < {lim:.0%}"
                       + (f"（打平需胜率 {bw:.1%}）" if bw is not None else "") + "；建议改用更远到期")
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


def evaluate_budget(*, candidate: dict, open_positions: list | None, capital: dict | None, pause: dict | None,
                    policy: dict = POLICY_B, max_qty: int = 20) -> dict:
    """B 政策下，一个候选最多可开几组（只读预算）。

    candidate：{"id", "underlying", "unit_stop_loss", "unit_payoff_max_loss", ["unit_occupancy"], ["qty_requested"]}
               —— 每组含费金额；整笔 = 每组 × 组数（同一笔多组仍是一笔逻辑持仓）。
    open_positions：在场与已选入待执行的逻辑持仓（整笔）：{"id", "underlying", "stop_loss", "payoff_max_loss", "state"}；
                    state ∈ open / selected_pending（已选入待执行：暂占额度）。同 id 重复导入只算一次。
                    None = 现有持仓未知 → incomplete。成员任一金额未知 → incomplete（不当 0）。
    capital：{"equity"(R), "asof", ["buying_power"], ["stale"]}；pause：{"latched": bool, ["reason"]}，None = 状态未知。
    检查顺序：暂停 → 数据完整 → 单笔止损 → 单笔理论最大亏损 → 类别合计 → 全场合计 → 大仓数量；资金能力另列 operation_status。
    金银多空不互相抵扣；类别合计是该类所有品种的合计，不是每品种额度。"""
    reasons, checks = [], []
    out = {"policy_version": policy["version"], "profile_id": policy.get("profile_id"),
           "asof": (capital or {}).get("asof"), "policy_basis": "R = 当前可审计权益（Codex 043 推荐口径）",
           "checks": checks, "allowed_units": 0, "budget_status": "incomplete", "operation_status": "unverified",
           "reasons": reasons}

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
    if not (_num(us) and us > 0 and _num(um) and um > 0):
        chk("候选经济量", "incomplete", note="每组止损损失/理论最大亏损未知或非正 → 不开")
    else:
        chk("候选经济量", "pass")
    seen, opens = set(), []
    if open_positions is None:
        chk("现有持仓", "incomplete", note="现有持仓风险未传入/未知 → 不能当作空仓")
    else:
        miss = []
        for p in open_positions:
            if p.get("id") in seen:
                continue                                   # 幂等：重复导入不重复占用
            seen.add(p.get("id"))
            if p.get("state", "open") not in ("open", "selected_pending"):
                continue
            if not (_num(p.get("payoff_max_loss")) and p["payoff_max_loss"] >= 0) or \
               not (_num(p.get("stop_loss")) and p["stop_loss"] >= 0):
                miss.append(str(p.get("id")))
            opens.append(p)
        chk("现有持仓", "incomplete" if miss else "pass", note=("缺金额：" + "、".join(miss[:5])) if miss else "")
    bad = [c for c in checks if c["status"] != "pass"]
    if bad:                                                # 已暂停 = 确定的 0（fail）；其余缺口 = incomplete（不是通过）
        out["budget_status"] = "fail" if checks[0]["status"] == "fail" else "incomplete"
        reasons += [f"{c['check']}：{c['note'] or c['status']}" for c in bad]
        return out

    lim_stop, lim_max = limit_usd(policy["stop_loss"], R), limit_usd(policy["max_loss"], R)
    cat = category_of(candidate.get("underlying"))
    lim_cat, lim_acct = limit_usd(policy["categories"][cat], R), limit_usd(policy["account"], R)
    big_thr, big_max = policy["big_position"]["threshold_usd"], policy["big_position"]["max_count"]
    open_cat = sum(p["payoff_max_loss"] for p in opens if category_of(p.get("underlying")) == cat)
    open_all = sum(p["payoff_max_loss"] for p in opens)
    open_big = sum(1 for p in opens if p["payoff_max_loss"] > big_thr)
    want = candidate.get("qty_requested") or max_qty

    def ok_at(k):
        s, m = us * k, um * k
        return [("单笔计划止损", s <= lim_stop + 1e-9, s, lim_stop),
                ("单笔理论最大亏损", m <= lim_max + 1e-9, m, lim_max),
                (f"类别合计（{cat}）", open_cat + m <= lim_cat + 1e-9, open_cat + m, lim_cat),
                ("全场合计", open_all + m <= lim_acct + 1e-9, open_all + m, lim_acct),
                ("大仓数量", not (m > big_thr and open_big >= big_max), open_big + (1 if m > big_thr else 0), big_max)]
    n = 0
    for k in range(1, int(want) + 1):
        if all(ok for _, ok, _, _ in ok_at(k)):
            n = k
        else:
            break
    for name, ok, v, lim in ok_at(max(n, 1)):             # 0 组时报告 1 组卡在哪；≥1 组时报告该组数下的各项
        chk(name, "pass" if ok else "fail", value=round(v, 2), limit=lim)
    out["allowed_units"] = n
    out["budget_status"] = "pass" if n >= 1 else "fail"
    if n == 0:
        reasons += [f"{c['check']} {c['value']} > {c['limit']}" for c in checks if c["status"] == "fail"]
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
    每笔：计划止损、理论最大亏损；合计：金银、其它、全场；大仓数量。任一金额未知 → incomplete。"""
    checks = []
    R = (capital or {}).get("equity")
    if open_positions is None or not _num(R):
        checks.append({"check": "现有持仓/权益", "status": "incomplete", "value": None, "limit": None,
                       "note": "持仓或权益未知"})
        return {"policy_version": policy["version"], "checks": checks, "overall": "incomplete"}
    seen, rows = set(), []
    for p in open_positions:
        if p.get("id") in seen or p.get("state", "open") not in ("open", "selected_pending"):
            continue
        seen.add(p.get("id")); rows.append(p)
    ls, lm = limit_usd(policy["stop_loss"], R), limit_usd(policy["max_loss"], R)
    for p in rows:
        for name, key, lim in (("单笔计划止损", "stop_loss", ls), ("单笔理论最大亏损", "payoff_max_loss", lm)):
            v = p.get(key)
            st = "incomplete" if not _num(v) else ("pass" if v <= lim + 1e-9 else "fail")
            checks.append({"check": f"{p.get('id')}·{name}", "status": st, "value": v, "limit": lim, "note": ""})
    known = all(_num(p.get("payoff_max_loss")) for p in rows)
    for cat in ("metals", "other"):
        tot = sum(p["payoff_max_loss"] for p in rows if category_of(p.get("underlying")) == cat) if known else None
        lim = limit_usd(policy["categories"][cat], R)
        checks.append({"check": f"类别合计（{cat}）", "status": "incomplete" if tot is None else ("pass" if tot <= lim + 1e-9 else "fail"),
                       "value": tot, "limit": lim, "note": "金银多空不抵扣"})
    tot = sum(p["payoff_max_loss"] for p in rows) if known else None
    lim = limit_usd(policy["account"], R)
    checks.append({"check": "全场合计", "status": "incomplete" if tot is None else ("pass" if tot <= lim + 1e-9 else "fail"),
                   "value": tot, "limit": lim, "note": ""})
    nb = sum(1 for p in rows if _num(p.get("payoff_max_loss")) and p["payoff_max_loss"] > policy["big_position"]["threshold_usd"])
    checks.append({"check": "大仓数量", "status": "incomplete" if not known else ("pass" if nb <= policy["big_position"]["max_count"] else "fail"),
                   "value": nb, "limit": policy["big_position"]["max_count"], "note": f"理论最大亏损 > ${policy['big_position']['threshold_usd']:.0f}"})
    sts = {c["status"] for c in checks}
    return {"policy_version": policy["version"], "checks": checks,
            "overall": "fail" if "fail" in sts else ("incomplete" if "incomplete" in sts else "pass")}
