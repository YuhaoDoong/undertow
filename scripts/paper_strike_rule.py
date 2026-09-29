"""模拟仓按现价自动选档规则 wall-dynamic-v1（草案，待用户与 Codex 审；未接入 paper_trades 状态机，不会自动执行）。

用户 2026-09-29：「price is changing， you should adjust your plan as well」；「以后更新期权墙后要重新判断，应该算延续」；
对「把按现价自动选档写进规则，先给你和 GPT 看再上线」答「ok」。

  python3 scripts/paper_strike_rule.py GLD 2026-09-30      # 只读演练：按此刻行情它会选哪一档（不登记、不入场）

规则（牛市看跌价差；每次取价时在入场窗口内重新选，第一份合格即入场；入场后不自动换档——换档是平仓再开，另需决定）：
1. 墙：该品种 0–14 天内所有到期的 put OI 按行权价加总（快照 = 前一交易日收盘结算，盘中不变）。只看现价下方 WALL_RANGE 内的行权价；
   OI ≥ 区间内最大值 × WALL_FRAC 的行权价算「墙」。取离现价最近的一道墙 W。
2. 卖腿：不高于 W、且离现价 ≥ MIN_BUFFER 的最高挂牌行权价（墙离价太近 → 卖在墙下方，让墙先挡一下）；须有有效买价。
3. 买腿：卖腿下方、宽度 ≤ MAX_WIDTH_FRAC × 现价的挂牌行权价，由窄到宽；须有有效卖价（买腿只需要 ask）。
   第一个满足 0 < 保守权利金（卖腿 bid − 买腿 ask）< 宽度 且 权利金/宽度 ≥ MIN_CREDIT_RATIO 的即选定。
4. 没有合格组合 → 不开，记录原因（无墙 / 卖腿无买价 / 权利金不够 / 宽度超限）。每次选档的全过程（现价、各墙、候选与报价）都留痕。
全部参数为未校准的设计值，写死在 PARAMS；改动即新版本。只读报价，从不下单。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RULE = "wall-dynamic-v1"
PARAMS = {"wall_range": 0.05, "wall_frac": 0.5, "min_buffer": 0.005, "max_width_frac": 0.015, "min_credit_ratio": 0.15}


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def select_put_spread(spot: float, put_oi: dict, listed: list, quotes: dict, params: dict = PARAMS) -> dict:
    """纯函数。put_oi：{行权价: 0–14 天 put OI 合计}；listed：目标到期挂牌行权价；quotes：{行权价: {bid, ask, error}}。"""
    tr = {"rule": RULE, "params": dict(params), "spot": spot}
    lo = spot * (1 - params["wall_range"])
    zone = {k: v for k, v in put_oi.items() if lo <= k < spot and v > 0}
    if not zone:
        return {**tr, "ok": False, "reason": "no_wall（现价下方区间内无 put OI）"}
    top = max(zone.values())
    walls = sorted((k for k, v in zone.items() if v >= top * params["wall_frac"]), reverse=True)
    W = walls[0]
    tr.update(walls=[(k, zone[k]) for k in walls], nearest_wall=W)
    sells = [k for k in sorted(listed, reverse=True) if k <= W and (spot - k) / spot >= params["min_buffer"]]
    if not sells:
        return {**tr, "ok": False, "reason": "no_sell_strike（墙下方无满足缓冲的挂牌行权价）"}
    ks = sells[0]
    tr.update(sell=ks, sell_buffer=round((spot - ks) / spot, 5), wall_buffer=round((spot - W) / spot, 5))
    qs = quotes.get(ks) or {}
    if qs.get("error") or not _ok(qs.get("bid")):
        return {**tr, "ok": False, "reason": f"sell_no_bid（{ks:g} 无有效买价）"}
    tried = []
    for kb in sorted((k for k in listed if k < ks), reverse=True):
        w = ks - kb
        if w > params["max_width_frac"] * spot:
            break
        qb = quotes.get(kb) or {}
        if qb.get("error") or not _ok(qb.get("ask")):
            tried.append((kb, None, "buy_no_ask")); continue
        cr = round(qs["bid"] - qb["ask"], 4)
        ok = 0 < cr < w and cr / w >= params["min_credit_ratio"]
        tried.append((kb, cr, round(cr / w, 4)))
        if ok:
            return {**tr, "ok": True, "buy": kb, "width": w, "credit": cr, "credit_ratio": round(cr / w, 4), "tried": tried}
    return {**tr, "ok": False, "reason": "no_qualifying_width（宽度上限内权利金/宽度均不达标或买腿无卖价）", "tried": tried}


def _dryrun(sym: str, expiry: str) -> int:
    """只读演练：此刻按规则会选哪一档。"""
    import json
    from datetime import date
    from collections import defaultdict
    from undertow.collect.store import SnapshotStore
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.longbridge_quote import fetch_depth, fetch_stock_quotes
    from undertow.core.clock import market_today
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    cfg, s = load_config(), SnapshotStore()
    key = {i.options.symbol: i.key for i in cfg.instruments.values() if i.options}[sym]
    today = market_today()
    cs = snapshot_from_payload(s.load("options", sym, session_index(s, sym)[today]), key, sym).contracts
    spot = fetch_stock_quotes([f"{sym}.US"])[f"{sym}.US"].last
    e = date.fromisoformat(expiry)
    oi = defaultdict(int)
    for c in cs:
        if c.kind == "P" and 0 <= (c.expiry - today).days <= 14:
            oi[c.strike] += c.open_interest
    listed = sorted({c.strike for c in cs if c.expiry == e and c.kind == "P" and spot * 0.93 <= c.strike < spot})
    d = fetch_depth([option_symbol(sym, expiry, "P", k) for k in listed])
    quotes = {k: {"bid": d[option_symbol(sym, expiry, "P", k)].bid, "ask": d[option_symbol(sym, expiry, "P", k)].ask,
                  "error": d[option_symbol(sym, expiry, "P", k)].error} for k in listed}
    print(json.dumps(select_put_spread(spot, dict(oi), listed, quotes), ensure_ascii=False, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_dryrun(sys.argv[1], sys.argv[2]))
