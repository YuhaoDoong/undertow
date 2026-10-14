"""垂直价差经济量的唯一来源（Codex 043 WP1）。纯函数，不依赖任何执行脚本。

此前同一组量在三处各算一遍，而且口径互相截断：
  scripts/paper_trades.economics   含费最大收益 / 最大亏损（正确，但只有理论到期口径）；
  healthcheck.stop_risk            2 倍权利金止损情景，却用 min(…, max_loss) 截断：宽 5、收 2.70、往返费 3.20 时
                                   理论最大亏损 233.20、按 2C 清算的情景损失 273.20，却报 230（既截断又漏费）。
现在只在这里算，并把下列口径分开给，互不替代：
  entry_net_max_gain            入场时含费最大收益（到期全额收下 / 价差打满）
  entry_net_payoff_max_loss     入场时含费理论最大亏损（到期价差打满 / 借记价值归零）
  planned_stop_loss_usd         按 stop_rule 了结时的计划损失（含往返费）；可以【超过】理论最大亏损——
                                2C 止损在价差宽度不够时就是这样，这是成本/操作状态，不是坏数据，不得截断
  liquidation_scenario_loss     loss_at_value(v)：按任意清算价值 v 了结的含费损失
  remaining_loss_from_current_value   从当前清算价值再走到最坏到期结果的追加损失（不含已发生盈亏）

符号约定（与模拟仓一致）：entry_credit = 卖腿收入 − 买腿支出（每股）；贷记 > 0，借记 < 0（= −付出的权利金）。
清算价值 value = 买回整组的每股成本：贷记 ∈ [0, width]，借记 ∈ [−width, 0]（负数 = 卖出能收回的钱）。
含费盈亏 = ((entry_credit − value) × 乘数 − 往返费) × 组数。

费用模型：fee_round_trip 是【每组】往返费用的模型假设（长桥 0.80/张/笔 × 2 腿 × 2 笔 = 3.20），不是已核验的全部现实费用；
指派/行权处置费用未知 → 恒为 None，并在 fee_assumption 里写明。fee_round_trip 缺失 → 所有含费量为 None（不折零）。
"""
from __future__ import annotations

import math

MULT = 100
FEE_ASSUMPTION = "每组往返费用为模型假设（入场一半、退出一半）；指派/行权处置费用未知、未计入"


def _fin(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def is_debit(side: str, k_sell: float, k_buy: float) -> bool:
    """借记价差：call 买低卖高、put 买高卖低。"""
    return (k_buy < k_sell) if side == "C" else (k_buy > k_sell)


def vertical(*, side: str, k_sell: float, k_buy: float, entry_credit: float, qty: int = 1,
             fee_round_trip: float | None = None, stop_rule: dict | None = None, mult: int = MULT) -> dict:
    """一组垂直价差的全部入场经济量。任何输入不合法 → {"complete": False, "error": …}，数值全为 None。

    stop_rule：None（不设止损）、{"kind": "credit_multiple", "mult": 2.0}（价值 ≥ mult × 入场权利金即了结，只用于贷记）、
    {"kind": "usd", "amount": 200.0}（整笔含费亏损达到该美元数即了结；整笔，不是每组/每腿）。
    """
    base = {"side": side, "k_sell": k_sell, "k_buy": k_buy, "entry_credit": entry_credit, "qty": qty,
            "fee_round_trip": fee_round_trip, "fee_assumption": FEE_ASSUMPTION, "assignment_fee_usd": None,
            "stop_rule": stop_rule, "mult": mult}
    nones = dict.fromkeys(("structure", "width", "entry_net_max_gain", "entry_net_payoff_max_loss", "planned_stop_loss_usd",
                           "stop_value", "stop_exceeds_payoff_max_loss", "fee_entry_usd", "fee_exit_usd", "fee_total_usd",
                           "breakeven"))
    if side not in ("C", "P") or not _fin(k_sell, k_buy, entry_credit) or k_sell == k_buy:
        return {**base, **nones, "complete": False, "error": "腿或权利金不合法"}
    if not (isinstance(qty, int) and not isinstance(qty, bool) and qty > 0) or not (isinstance(mult, int) and mult > 0):
        return {**base, **nones, "complete": False, "error": "组数/乘数必须是正整数"}
    width = abs(k_sell - k_buy)
    debit = is_debit(side, k_sell, k_buy)
    if (debit and not entry_credit < 0) or (not debit and not entry_credit > 0) or abs(entry_credit) >= width:
        return {**base, **nones, "complete": False, "error": "权利金符号与结构不符或不在 (0, 宽度) 内"}
    out = {**base, **nones, "structure": "debit" if debit else "credit", "width": width, "error": None}
    fee_ok = fee_round_trip is not None and _fin(fee_round_trip) and fee_round_trip >= 0
    if fee_ok:
        out.update(fee_entry_usd=round(fee_round_trip / 2 * qty, 4), fee_exit_usd=round(fee_round_trip / 2 * qty, 4),
                   fee_total_usd=round(fee_round_trip * qty, 4))
        worst = width if not debit else 0.0                   # 最坏到期清算价值
        best = 0.0 if not debit else -width
        out["entry_net_payoff_max_loss"] = round(loss_at_value(out, worst), 2)
        out["entry_net_max_gain"] = round(-loss_at_value(out, best), 2)
        fee_ps = fee_round_trip / mult
        c = abs(entry_credit)
        if debit:
            out["breakeven"] = round((k_buy + c + fee_ps) if side == "C" else (k_buy - c - fee_ps), 4)
        else:
            out["breakeven"] = round((k_sell - c + fee_ps) if side == "P" else (k_sell + c - fee_ps), 4)
    sr = stop_rule
    if sr is None or not fee_ok:
        pass
    elif sr.get("kind") == "credit_multiple" and not debit and _fin(sr.get("mult")) and sr["mult"] > 1:
        v = sr["mult"] * entry_credit
        out["stop_value"] = round(v, 4)
        out["planned_stop_loss_usd"] = round(loss_at_value(out, v), 2)
    elif sr.get("kind") == "usd" and _fin(sr.get("amount")) and sr["amount"] > 0:
        out["planned_stop_loss_usd"] = round(float(sr["amount"]), 2)
        # 达到该亏损时的清算价值：((v − credit) × 乘数 + 往返费) × 组数 = amount
        out["stop_value"] = round(entry_credit + (sr["amount"] / qty - fee_round_trip) / mult, 4)
    else:
        out["error"] = f"stop_rule 不适用于该结构或参数不合法：{sr}"
    if out["planned_stop_loss_usd"] is not None and out["entry_net_payoff_max_loss"] is not None:
        out["stop_exceeds_payoff_max_loss"] = out["planned_stop_loss_usd"] > out["entry_net_payoff_max_loss"] + 1e-9
    out["complete"] = fee_ok and out["error"] is None
    return out


def loss_at_value(e: dict, value: float) -> float | None:
    """按清算价值 value（每股）了结整笔的含费损失（亏为正、赚为负）。不截断到理论区间——越界本身就是要报告的事实。"""
    if not _fin(value) or e.get("fee_round_trip") is None or not _fin(e.get("fee_round_trip"), e.get("entry_credit")):
        return None
    return ((value - e["entry_credit"]) * e["mult"] + e["fee_round_trip"]) * e["qty"]


def remaining_loss_from_current_value(e: dict, value: float) -> float | None:
    """从当前清算价值再走到最坏到期结果的追加损失（美元，≥0）。不含已实现/已浮动的盈亏，也不含退出费
    （到期结算可能不发生退出费）。value 越界（贷记 > 宽度 / 借记 < −宽度 或 > 0）→ None：报价不成立，不硬算。"""
    if e.get("width") is None or not _fin(value):
        return None
    w = e["width"]
    if e["structure"] == "credit":
        return None if not 0 <= value <= w else round((w - value) * e["mult"] * e["qty"], 2)
    return None if not -w <= value <= 0 else round((0 - value) * e["mult"] * e["qty"], 2)


def credit_range_for_stop_cap(*, width: float, stop_mult: float, cap_usd: float, max_loss_cap_usd: float | None,
                              fee_round_trip: float, qty: int = 1, mult: int = MULT) -> tuple[float, float] | None:
    """贷记价差在「mult×C 止损」下，同时满足 计划止损损失 ≤ cap_usd 与 理论最大亏损 ≤ max_loss_cap_usd 的权利金区间 [lo, hi]。
    止损损失 = ((m−1)·C·乘数 + F)·q ≤ cap → C ≤ (cap/q − F) / ((m−1)·乘数)
    最大亏损 = ((W−C)·乘数 + F)·q ≤ cap2 → C ≥ W − (cap2/q − F)/乘数
    无交集 → None。只是算术区间，不代表有真实报价能成交。"""
    if not _fin(width, stop_mult, cap_usd, fee_round_trip) or stop_mult <= 1 or width <= 0:
        return None
    hi = min((cap_usd / qty - fee_round_trip) / ((stop_mult - 1) * mult), width)
    lo = 0.0
    if max_loss_cap_usd is not None:
        lo = max(lo, width - (max_loss_cap_usd / qty - fee_round_trip) / mult)
    return (round(lo, 4), round(hi, 4)) if lo <= hi and hi > 0 else None
