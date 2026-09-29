"""模拟仓按现价自动选档规则 wall-dynamic-v1（草案，待用户与 Codex 审；未接入 paper_trades 状态机，不会自动执行）。

用户 2026-09-29：「price is changing， you should adjust your plan as well」；「以后更新期权墙后要重新判断，应该算延续」；
对「把按现价自动选档写进规则，先给你和 GPT 看再上线」答「ok」。

  python3 scripts/paper_strike_rule.py GLD 2026-09-30      # 只读演练：按此刻行情它会选哪一档（不登记、不入场）

规则（牛市看跌价差；每次取价时在入场窗口内重新选，第一份合格即入场；入场后不自动换档——换档是平仓再开，另需决定）：
1. 墙：该品种 0–14 天内所有到期的 put OI 按行权价加总（快照 = 前一交易日收盘结算，盘中不变）。只看现价下方 WALL_RANGE 内的行权价；
   OI ≥ 区间内最大值 × WALL_FRAC 的行权价算「墙」。取离现价最近的一道墙 W。
2. 卖腿：只锚定一档 ——「不高于 W、且离现价 ≥ MIN_BUFFER 的最高挂牌行权价」（墙离价太近 → 卖在墙下方；「墙先挡一下」是待验证假设）。
   它没有有效买价就不开，不向下继续找（向下枚举另立版本）。
3. 买腿：卖腿下方、宽度 ≤ MAX_WIDTH_FRAC × 现价，由窄到宽；只需有效卖价。任一腿两侧都有且 bid > ask（交叉）→ 拒绝。
   合格 = 0 < 保守权利金（卖 bid − 买 ask）< 宽度，且含费最大收益 > 0（economics 唯一口径），且 权利金/宽度 ≥ MIN_CREDIT_RATIO。
4. 没有合格组合 → 不开，逐档记录拒绝原因。同一候选全集同时算「不设比例门槛」的影子对照（select_with_shadow），主结果仍含 15%。
5. 留痕：现价与抓取时刻、OI 快照路径与 sha256、全部报价（买卖价、挂单量、抓取时刻）、参数哈希、ATR14 与 ATR 缓冲（只描述）、DTE、
   含费最大收益/最大亏损、收益/亏损比。到期由外部指定（未自动选到期）。
6. 入场后不换档（用户：「盘中已成交，那么就不应该乱动了，换挡应该是开没开仓的时候」）；换档只作用于未入场候选，
   由 paper_trades.register_revision 与 tick 共用锁执行。
全部参数为未校准的探索设计值（15% 为历史设计基线），写死在 PARAMS；改动即新版本。只读报价，从不下单。
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


def _crossed(q: dict) -> bool:
    """两侧都有且 bid > ask → 异常报价（缺一侧可以，交叉不行）。"""
    return _ok(q.get("bid")) and _ok(q.get("ask")) and q["bid"] > q["ask"]


def params_hash(params: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]


def select_put_spread(spot: float, put_oi: dict, listed: list, quotes: dict, params: dict = PARAMS, *,
                      fee_round_trip: float = 3.2, qty: int = 1, target_oi: dict | None = None) -> dict:
    """纯函数。put_oi：{行权价: 0–14 天 put OI 合计}；target_oi：目标到期自身 put OI（只展示，不参与选档）；
    listed：目标到期挂牌行权价；quotes：{行权价: {bid, ask, error, …}}。
    卖腿语义（Codex 028 要求写明）：只锚定一档——「不高于最近墙、离价 ≥ 缓冲的最高挂牌行权价」；它没有有效买价就不开，
    不向下继续找（向下枚举是另一版规则）。买腿只需有效卖价；任何一腿两侧都有且交叉 → 异常，拒绝。
    合格 = 0 < 保守权利金 < 宽度、权利金/宽度 ≥ min_credit_ratio、且含费最大收益 > 0（economics 唯一口径）。"""
    from scripts.paper_trades import economics
    tr = {"rule": RULE, "params": dict(params), "params_hash": params_hash(params), "spot": spot}
    if not _ok(spot):
        return {**tr, "ok": False, "reason": "bad_spot（现价非有限正数）"}
    lo = spot * (1 - params["wall_range"])
    zone = {k: v for k, v in put_oi.items() if lo <= k < spot and v > 0}
    if not zone:
        return {**tr, "ok": False, "reason": "no_wall（现价下方区间内无 put OI）"}
    top = max(zone.values())
    walls = sorted((k for k, v in zone.items() if v >= top * params["wall_frac"]), reverse=True)
    W = walls[0]
    tr.update(walls=[(k, zone[k], (target_oi or {}).get(k)) for k in walls], nearest_wall=W)
    sells = [k for k in sorted(listed, reverse=True) if k <= W and (spot - k) / spot >= params["min_buffer"]]
    if not sells:
        return {**tr, "ok": False, "reason": "no_sell_strike（墙下方无满足缓冲的挂牌行权价）"}
    ks = sells[0]
    tr.update(sell=ks, sell_buffer=round((spot - ks) / spot, 5), wall_buffer=round((spot - W) / spot, 5))
    qs = quotes.get(ks) or {}
    if qs.get("error") or not _ok(qs.get("bid")):
        return {**tr, "ok": False, "reason": f"sell_no_bid（{ks:g} 无有效买价；锚定一档，不向下找）"}
    if _crossed(qs):
        return {**tr, "ok": False, "reason": f"sell_crossed（{ks:g} bid>ask）"}
    tried = []
    for kb in sorted((k for k in listed if k < ks), reverse=True):
        w = round(ks - kb, 6)
        if w > params["max_width_frac"] * spot:
            break
        qb = quotes.get(kb) or {}
        if qb.get("error") or not _ok(qb.get("ask")):
            tried.append({"buy": kb, "reject": "buy_no_ask"}); continue
        if _crossed(qb):
            tried.append({"buy": kb, "reject": "buy_crossed"}); continue
        cr = round(qs["bid"] - qb["ask"], 4)
        row = {"buy": kb, "width": w, "credit": cr, "ratio": round(cr / w, 4)}
        if not 0 < cr < w:
            tried.append({**row, "reject": "credit_outside_0_width"}); continue
        e = economics("P", ks, kb, cr, qty, fee_round_trip)
        row.update(max_gain_usd=e["max_gain_usd"], max_loss_usd=e["max_loss_usd"], breakeven=e["breakeven"],
                   gain_per_loss=round(e["max_gain_usd"] / e["max_loss_usd"], 4) if e["max_loss_usd"] > 0 else None)
        if e["max_gain_usd"] <= 0:
            tried.append({**row, "reject": "fee_exceeds_credit（含费最大收益 ≤ 0）"}); continue
        if cr / w < params["min_credit_ratio"]:
            tried.append({**row, "reject": "credit_ratio_low"}); continue
        tried.append({**row, "reject": None})
        return {**tr, "ok": True, "buy": kb, "width": w, "credit": cr, "credit_ratio": row["ratio"],
                "economics": e, "tried": tried}
    return {**tr, "ok": False, "reason": "no_qualifying_width（宽度上限内无合格买腿）", "tried": tried}


def select_with_shadow(spot, put_oi, listed, quotes, params=PARAMS, **kw) -> dict:
    """主规则（含 15% 门槛）与不设门槛的影子对照，在同一候选全集、同一时刻一起算（Codex 028）；主结果仍是含门槛的那个。"""
    return {"main": select_put_spread(spot, put_oi, listed, quotes, params, **kw),
            "shadow_no_ratio": select_put_spread(spot, put_oi, listed, quotes, {**params, "min_credit_ratio": 0.0}, **kw)}


def _dryrun(sym: str, expiry: str) -> int:
    """只读演练：此刻按规则会选哪一档；输出完整留痕包（输入引用与哈希、全部报价与抓取时刻、候选与拒绝原因）。"""
    import hashlib
    import json
    from datetime import date, datetime, timezone
    from collections import defaultdict
    from undertow.collect.store import SnapshotStore
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.longbridge_quote import fetch_stock_quotes
    from undertow.core.clock import market_today
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    from scripts.paper_trades import _depth
    cfg, s = load_config(), SnapshotStore()
    key = {i.options.symbol: i.key for i in cfg.instruments.values() if i.options}[sym]
    today = market_today()
    f = session_index(s, sym)[today]
    path = s.path_of("options", sym, f)
    cs = snapshot_from_payload(s.load("options", sym, f), key, sym).contracts
    sq = fetch_stock_quotes([f"{sym}.US"])[f"{sym}.US"]
    spot, spot_asof = sq.last, datetime.now(timezone.utc).isoformat()
    e = date.fromisoformat(expiry)
    oi, toi = defaultdict(int), defaultdict(int)
    for c in cs:
        if c.kind == "P" and 0 <= (c.expiry - today).days <= 14:
            oi[c.strike] += c.open_interest
        if c.kind == "P" and c.expiry == e:
            toi[c.strike] += c.open_interest
    listed = sorted({c.strike for c in cs if c.expiry == e and c.kind == "P" and spot * 0.93 <= c.strike < spot})
    syms = {k: option_symbol(sym, expiry, "P", k) for k in listed}
    d = _depth(list(syms.values()))
    quotes = {k: d[v] for k, v in syms.items()}
    atr = None                                                  # ATR14 只用今天之前的日线；取不到就记 None（不折 0）
    try:
        from undertow.analyze.technicals import _atr
        from undertow.collect.cboe_history import CboeHistorySource
        ps = CboeHistorySource().fetch_series(cfg.instruments[key])
        idx = [i for i, x in enumerate(ps.dates) if x < today]
        if len(idx) >= 15 and ps.highs:
            atr = _atr(ps.highs[:idx[-1] + 1], ps.lows[:idx[-1] + 1], ps.closes[:idx[-1] + 1], 14)
    except Exception as ex:
        atr = None
        print(f"⚠️ ATR14 取不到：{type(ex).__name__}: {ex}"[:160], file=sys.stderr)
    out = {"inputs": {"oi_snapshot": str(path.relative_to(ROOT)), "oi_snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "oi_asof": "前一交易日收盘结算（快照认证到 %s）" % today, "spot_asof": spot_asof, "expiry": expiry,
                      "dte": (e - today).days, "expiry_choice": "外部指定（未自动选到期）", "atr14": atr},
           "quotes": {str(k): v for k, v in quotes.items()},
           **select_with_shadow(spot, dict(oi), listed, quotes, target_oi=dict(toi))}
    for part in ("main", "shadow_no_ratio"):                    # ATR 缓冲只作描述，不参与选档（Codex 028：先不加 ATR 闸门）
        r = out[part]
        if atr and r.get("sell") is not None:
            r["sell_buffer_atr"] = round((spot - r["sell"]) / atr, 3)
            r["wall_buffer_atr"] = round((spot - r["nearest_wall"]) / atr, 3)
    print(json.dumps(out, ensure_ascii=False, default=str, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(_dryrun(sys.argv[1], sys.argv[2]))
