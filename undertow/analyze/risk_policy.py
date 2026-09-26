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
        caps["同簇合计 ≤{:.0%}".format(policy["cluster_max_loss_frac"])] = max(0, int(max(0.0, room) // unit_max_loss))
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
