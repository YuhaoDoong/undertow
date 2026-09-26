"""信用价差退出研究：收盘信号只能使用随后观察到的快照估价。

输入全部来自调用方，无 I/O。盘前链不是可成交盘口；返回值始终是模型情景，
不能作为实盘回报。已触发退出却缺后续报价时返回 pnl=None，不偷换为持有到期。
"""
from __future__ import annotations

import math
from datetime import date

from undertow.analyze.gamma import pick_sell_wall
from undertow.analyze.wall_spread import FEE_PER_TRADE, close_cost

RULES = {"hold", "break", "wallmove", "break_and_move", "profit50", "break_or_profit50"}


def _quote(snap, kind, strike, expiry):
    if snap is None:
        return None
    for c in snap.contracts:
        if c.kind == kind and c.strike == strike and c.expiry == expiry:
            if (c.bid is not None and c.ask is not None
                    and math.isfinite(c.bid) and math.isfinite(c.ask)
                    and 0 <= c.bid <= c.ask and c.ask > 0):
                return c
    return None


def evaluate_exit(*, kind: str, sell: float, buy: float, expiry: date,
                  entry_day: date, credit: float, initial_wall: float,
                  snapshots: dict, closes: dict[date, float], rule: str) -> dict:
    """固定入场后比较退出规则；收益只标为快照报价模型。

    每天先观察盘前快照，再观察当天收盘。首日收盘亦会触发；缺快照的日子
    仍检查收盘。退出意图一经触发保持待执行，不能因后来价格回来而忘记。
    FEE_PER_TRADE 是四个合约边的往返费用预算，不在提前退出时再乘二。
    """
    if rule not in RULES or kind not in ("P", "C"):
        raise ValueError("未知退出规则或期权类型")
    if expiry < entry_day or not (buy < sell if kind == "P" else buy > sell):
        raise ValueError("到期日或保护腿方向无效")
    width = abs(sell - buy) * 100
    days = sorted(d for d in set(closes) | set(snapshots) if entry_day <= d <= expiry)
    pending = None
    reason = "持有到期"
    missing_quotes = 0
    for day in days:
        snap = snapshots.get(day)
        sc, bc = _quote(snap, kind, sell, expiry), _quote(snap, kind, buy, expiry)
        cost = close_cost(sc, bc) if sc is not None and bc is not None else None
        if cost is not None and not (0 <= cost <= width):
            cost = None  # 不把异常价差报价裁剪成一个好看的损益
        prior = [d for d in closes if d < day]
        moved = False
        if snap is not None and prior and rule in ("wallmove", "break_and_move"):
            prior_day = max(prior)
            wall = pick_sell_wall(snap, day, closes[prior_day], kind)
            moved = wall is None or wall["strike"] != initial_wall
        if day > entry_day and pending is None:
            if rule == "wallmove" and moved:
                pending, reason = day, "换墙"
            elif rule in ("profit50", "break_or_profit50") and cost is not None and cost <= credit * .5:
                pending, reason = day, "浮盈50%"
        if pending is not None:
            if cost is not None:
                return {"pnl": credit - cost - FEE_PER_TRADE,
                        "exit_day": day, "trigger_day": pending, "exit_cost": cost,
                        "held": (day - entry_day).days, "why": reason, "early": True,
                        "status": "snapshot_model", "execution_verified": False,
                        "missing_quote_observations": missing_quotes}
            missing_quotes += 1
        px = closes.get(day)
        if pending is None and px is not None and day < expiry:
            broke = px < sell if kind == "P" else px > sell
            if broke and (rule in ("break", "break_or_profit50")
                          or (rule == "break_and_move" and moved)):
                pending, reason = day, "收盘破卖腿" if rule != "break_and_move" else "收盘破腿且换墙"
                # 本日快照已经在上面消费；不能回到该快照按过去的价格退出。
    settle = closes.get(expiry)
    if pending is not None or settle is None:
        return {"pnl": None, "exit_day": None, "trigger_day": pending, "exit_cost": None,
                "held": None, "why": reason if pending else "缺到期收盘",
                "early": False, "status": "unpriced_exit" if pending else "unsettled",
                "execution_verified": False, "missing_quote_observations": missing_quotes}
    intrinsic = min(width, max(0, (sell - settle if kind == "P" else settle - sell) * 100))
    return {"pnl": credit - intrinsic - FEE_PER_TRADE,
            "exit_day": expiry, "trigger_day": None, "exit_cost": intrinsic,
            "held": (expiry - entry_day).days, "why": reason, "early": False,
            "status": "expiry_model", "execution_verified": False,
            "missing_quote_observations": missing_quotes}
