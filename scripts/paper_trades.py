"""模拟仓自动执行（用户 2026-09-29：「今天开盘后，一个是模拟仓开仓，一个是继续记录数据验证。自动定时进行」）。

  python3 scripts/paper_trades.py tick          # session 钩子每 5 分钟调用：到点才做事，没到点什么都不写
  python3 scripts/paper_trades.py status

只读行情、只写私有 data/soul/（journal.json 里 execution=模拟 且带 paper 规格的事前判断 + paper_discretionary.jsonl 研究台账）；
**从不下单**（AGENTS 第一条）。规则版本 RULE_VERSION；每条 paper 规格记下它按哪一版执行。

v2（Codex 025-1/2 修订，入场前生效）：
- 规格校验：数值有限、qty 为正整数、宽度 > 0、腿方向正确（put 价差 卖腿行权价 > 买腿；call 相反）。不合格 → invalid_spec，不执行。
- 报价校验：两腿 bid/ask 有限、> 0、bid ≤ ask；权利金 = 卖腿 bid − 买腿 ask 必须 0 < 权利金 < 宽度。
  不合格 → 记下原始报价（entry_quote_invalid）并在窗口内继续取；**不截断报价制造可行成交**。
- 入场政策：窗口内【第一份】合格且权利金/宽度 ≥ 门槛的报价即入场（不看完窗口再择优）；窗口已过仍未入场 →
  missed（从未取得报价）或 skipped(no_valid_quote / credit_low)。错过入场日也终态标记，不会一直 planned。
- 盯市：只在常规交易时段内的时点（默认 09:45、15:45 ET）取盘口估值；这是**稀疏检查，不是连续止损**。
  时点落在常规时段之外 → 只记估值（mark_offhours），不执行止损。
- 结算：**理论到期记账**（按到期日常规收盘价的内在价值现金化；不模拟提前行权、指派、到期处置与实物交割）。
  收盘价取长桥日线中【日期 = 到期日】的那根，且只在到期日 16:20 ET 之后；取不到 → settlement_pending，之后每次唤醒重试；
  到期后才恢复运行也能补结（用到期日那根，不用恢复当天的价）。
- 风险单位：每笔固定 1 组（qty=1），收益与风险按每组比较；两笔金银同日不是两份独立证据。
- 结算口径 provisional（长桥日线）；CBOE 日线到后异步核对（audit_settlement），两源原值都留、不择优不平均。
- 手续费：fee_round_trip 为【每组】往返费用，整单 = 每组 × qty；最大亏损、最大收益、盈亏平衡都由 economics() 同一函数算。
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import sys
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
JOURNAL = ROOT / "data/soul/journal.json"
LOCK = ROOT / "data/soul/journal.json.lock"
LEDGER = ROOT / "data/soul/paper_discretionary.jsonl"
ET = ZoneInfo("America/New_York")
RULE_VERSION = "paper-sim-v2-20260929"
RTH = (time(9, 30), time(16, 0))
TERMINAL = ("settled", "closed_stop", "closed_manual", "skipped", "missed", "invalid_spec")
STRIKE_RULES = ("wall-dynamic-v1", "user-sell-fixed-v1")


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def _finite(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def economics(side: str, k_sell: float, k_buy: float, credit: float, qty: int, fee_round_trip: float) -> dict:
    """价差经济量的唯一来源（Codex 025-2）。fee_round_trip = 每组往返费用；整单 = 每组 × qty。"""
    width = abs(k_sell - k_buy)
    fee_ps = fee_round_trip / 100.0                      # 每股摊到的费用（1 组 = 100 股）
    be = (k_sell - credit + fee_ps) if side == "P" else (k_sell + credit - fee_ps)
    return {"width": width, "credit": credit,
            "max_gain_usd": round((credit * 100 - fee_round_trip) * qty, 2),
            "max_loss_usd": round(((width - credit) * 100 + fee_round_trip) * qty, 2),
            "breakeven": round(be, 4), "fee_total_usd": round(fee_round_trip * qty, 2)}


def validate_spec(p: dict) -> str | None:
    if p.get("strike_rule") and p.get("state") == "planned":          # 自动选档：入场前没有固定档位
        if p["strike_rule"] not in STRIKE_RULES:
            return f"未知选档规则 {p['strike_rule']}"
        if p["strike_rule"] == "user-sell-fixed-v1" and not (_finite(p.get("k_sell"), p.get("target_width"))
                                                              and p["target_width"] > 0):
            return "user-sell-fixed-v1 需要 k_sell 与正的 target_width"
        if p.get("side") not in ("P", "C") or not (isinstance(p.get("qty"), int) and p["qty"] > 0):
            return "side 须为 P/C、qty 须为正整数"
        if not _finite(p.get("min_credit_ratio"), p.get("stop_mult"), p.get("fee_round_trip")):
            return "数值非有限或缺失"
        return None
    if not _finite(p.get("k_sell"), p.get("k_buy"), p.get("min_credit_ratio"), p.get("stop_mult"), p.get("fee_round_trip")):
        return "数值非有限或缺失"
    if not (isinstance(p.get("qty"), int) and p["qty"] > 0):
        return "qty 须为正整数"
    if p.get("side") not in ("P", "C"):
        return "side 须为 P/C"
    if p["k_sell"] == p["k_buy"]:
        return "宽度为 0"
    if (p["side"] == "P") != (p["k_sell"] > p["k_buy"]):
        return "腿方向错误（put 价差卖腿行权价须高于买腿，call 相反）"
    if p["fee_round_trip"] < 0 or p["stop_mult"] <= 1 or not 0 <= p["min_credit_ratio"] < 1:
        return "费用/止损倍数/门槛不在合理范围"
    return None


def quote_ok(q: dict) -> bool:
    """报价可用：无显式错误、bid/ask 有限、0 < bid ≤ ask（Codex 026：带 error 的报价一律拒绝）。"""
    return q is not None and not q.get("error") and _finite(q.get("bid"), q.get("ask")) and 0 < q["bid"] <= q["ask"]


def quote_labels(q: dict, sell: str, buy: str, qty: int) -> dict:
    """研究模式按报价假设成交；另标挂单量是否够、两腿抓取时差、源时戳未知 —— 只标注，不据此称可执行。"""
    s, b = q.get(sell) or {}, q.get(buy) or {}
    sizes = (s.get("bid_size"), b.get("ask_size"))
    size_ok = None if any(x is None for x in sizes) else all(x >= qty for x in sizes)
    gap = None
    try:
        gap = abs((datetime.fromisoformat(b["fetched_at"]) - datetime.fromisoformat(s["fetched_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    return {"quote_valid": True, "size_sufficient": size_ok, "leg_fetch_gap_s": gap,
            "time_alignment": "source_ts_unknown（长桥 depth 不给源时戳）"}


def _depth(symbols):
    """逐腿取盘口。长桥 depth 接口不给源时戳，只能记逐腿抓取时刻（两腿时差由此可算）与一档挂单量。"""
    from undertow.collect.longbridge_quote import fetch_depth
    out = {}
    for s in symbols:
        d = fetch_depth([s]).get(s)
        out[s] = {"bid": getattr(d, "bid", None), "ask": getattr(d, "ask", None),
                  "bid_size": getattr(d, "bid_size", None), "ask_size": getattr(d, "ask_size", None),
                  "error": getattr(d, "error", None), "fetched_at": datetime.now(timezone.utc).isoformat(),
                  "source_ts": None}
    return out


def _session_close(underlying: str, day: date):
    """到期日常规收盘：长桥日线里日期 = day 的那根；没有 → None（settlement_pending）。
    取数根数按目标日期推算（长时间停机后恢复也能取到旧到期日，Codex 026）。结果是 provisional，CBOE 到后另行核对。"""
    from undertow.collect.longbridge_kline import fetch_bars
    count = min(1000, max(10, (datetime.now(ET).date() - day).days + 5))
    for b in fetch_bars(underlying, period="day", count=count):
        if b["ts"].astimezone(ET).date() == day and _finite(b["close"]):
            return {"close": b["close"], "source": "longbridge kline day", "bar_date": day.isoformat(),
                    "status": "provisional", "bar": {k: (v.isoformat() if k == "ts" else v) for k, v in b.items()},
                    "fetched_at": datetime.now(timezone.utc).isoformat()}
    return None


def _cboe_close(underlying: str, day: date):
    """CBOE 公开日线里日期 = day 的收盘（通常滞后 1–3 天）；没有 → None。第二来源不一定独立，也不是交易所官方真值。"""
    from undertow.collect.base import http_get_json
    from undertow.collect.cboe_history import CBOE_HIST_URL
    d = http_get_json(CBOE_HIST_URL.format(symbol=underlying.split(".")[0]))
    for r in d.get("data") or []:
        if r.get("date") == day.isoformat():
            try:
                c = float(r["close"])
            except (KeyError, TypeError, ValueError):
                return None
            return {"close": c, "source": "cboe daily", "row": r} if _finite(c) else None
    return None


def _spread_value(p: dict, s: float) -> float:
    return (max(0.0, p["k_sell"] - s) - max(0.0, p["k_buy"] - s)) if p["side"] == "P" else \
           (max(0.0, s - p["k_sell"]) - max(0.0, s - p["k_buy"]))


AUDIT_TOL = 0.01          # 两源收盘差 ≤ 1 美分算一致（ETF 报价精度 0.01）
AUDIT_RETRY_S = 3600      # 第二来源未到时最多每小时查一次


def audit_settlement(p: dict, now: datetime, *, cboe_close=_cboe_close) -> str | None:
    """已结算仓位的异步交叉核对（Codex 026-4）：追加 source_match / source_mismatch / source_unavailable，
    两源原值都保留；不改原记账、不择优、不平均。不一致且改变价内外或盈亏符号 → result_under_review。"""
    if p.get("state") != "settled":
        return None
    a = p.get("settle_audit") or {}
    if a.get("status") in ("source_match", "source_mismatch"):
        return None
    if a.get("last_try") and (now - datetime.fromisoformat(a["last_try"])).total_seconds() < AUDIT_RETRY_S:
        return None
    exp = date.fromisoformat(p["expiry"])
    c = cboe_close(p["underlying"], exp)
    a = {"last_try": now.isoformat(), "primary": p["settle"]}
    if c is None:
        a["status"] = "source_unavailable"
        first = (p.get("settle_audit") or {}).get("status") != "source_unavailable"
        p["settle_audit"] = a
        if first:
            p["events"].append({"at": now.isoformat(), "action": "settle_audit", "status": "source_unavailable"})
            return "source_unavailable"
        return None
    v2 = _spread_value(p, c["close"])
    pnl2 = round(((p["entry_credit"] - v2) * 100 - p["fee_round_trip"]) * p["qty"], 2)
    same = abs(c["close"] - p["settle"]["close"]) <= AUDIT_TOL
    flip = (v2 > 0) != (p["settle_value"] > 0) or (pnl2 >= 0) != (p["pnl_usd"] >= 0)
    a.update(status="source_match" if same else "source_mismatch", secondary=c, secondary_value=round(v2, 4),
             secondary_pnl_usd=pnl2, result_under_review=bool(not same and flip))
    p["settle_audit"] = a
    if a["result_under_review"]:
        p["result_under_review"] = True
    p["events"].append({"at": now.isoformat(), "action": "settle_audit", "status": a["status"],
                        "primary_close": p["settle"]["close"], "secondary_close": c["close"],
                        "result_under_review": a["result_under_review"]})
    return a["status"]


def _dynamic_select(p: dict, now: datetime) -> dict:
    """wall-dynamic-v1 的一次选档（用户认可的规则，2026-09-29；call 侧为镜像）。输入全部留痕：
    OI 取今天认证快照（前一交易日结算）里该侧 0…max(14, 目标 DTE) 天的合计；现价取长桥实时；报价取目标到期候选行权价的盘口。"""
    import hashlib
    from collections import defaultdict
    from scripts.paper_strike_rule import PARAMS, params_hash, select_with_shadow
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.longbridge_quote import fetch_stock_quotes
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    root = p["underlying"].split(".")[0]
    today = now.astimezone(ET).date()
    exp = date.fromisoformat(p["expiry"])
    cfg, st = load_config(), SnapshotStore()
    key = next(k for k, i in cfg.instruments.items() if i.options and i.options.symbol == root)
    f = session_index(st, root).get(today)
    if f is None:
        return {"ok": False, "reason": "no_snapshot（今天的认证快照未到）", "inputs": {}}
    path = st.path_of("options", root, f)
    snap = snapshot_from_payload(st.load("options", root, f), key, root)
    horizon = max(14, (exp - today).days)
    oi, toi = defaultdict(int), defaultdict(int)
    for c in snap.contracts:
        if c.kind == p["side"] and 0 <= (c.expiry - today).days <= horizon:
            oi[c.strike] += c.open_interest
        if c.kind == p["side"] and c.expiry == exp:
            toi[c.strike] += c.open_interest
    sq = fetch_stock_quotes([p["underlying"]])[p["underlying"]]
    spot, spot_at = sq.last, datetime.now(timezone.utc).isoformat()
    span = PARAMS["wall_range"] + PARAMS["max_width_frac"] + 0.01
    lo, hi = (spot, spot * (1 + span)) if p["side"] == "C" else (spot * (1 - span), spot)
    listed = sorted({c.strike for c in snap.contracts if c.expiry == exp and c.kind == p["side"] and lo <= c.strike <= hi})
    syms = {k: option_symbol(root, p["expiry"], p["side"], k) for k in listed}
    d = _depth(list(syms.values())) if syms else {}
    quotes = {k: d[v] for k, v in syms.items()}
    sel = select_with_shadow(spot, dict(oi), listed, quotes, side=p["side"], target_oi=dict(toi),
                             fee_round_trip=p["fee_round_trip"], qty=p["qty"])
    main = sel["main"]
    return {**main, "shadow_no_ratio": {k: sel["shadow_no_ratio"].get(k) for k in ("ok", "sell", "buy", "credit", "reason")},
            "inputs": {"oi_snapshot": str(path), "oi_snapshot_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                       "oi_window_days": horizon, "spot": spot, "spot_fetched_at": spot_at,
                       "quotes": {str(k): v for k, v in quotes.items()}, "symbols": {str(k): v for k, v in syms.items()},
                       "params_hash": params_hash(PARAMS)}}


def wall_change_reason(prev_sel: dict | None, sel: dict) -> str:
    """墙为什么变了（Codex 028）：新结算 OI / 现价移动导致最近墙切换 / 参数变化 / 未变 / 首次选档。"""
    if not prev_sel:
        return "first_selection"
    pi, ci = prev_sel.get("inputs") or {}, sel.get("inputs") or {}
    if pi.get("params_hash") != ci.get("params_hash"):
        return "params_change"
    if pi.get("oi_snapshot_sha256") != ci.get("oi_snapshot_sha256"):
        return "oi_update（新的结算 OI）"
    if prev_sel.get("nearest_wall") != sel.get("nearest_wall"):
        return "spot_move（现价移动导致最近墙切换）"
    if (prev_sel.get("sell"), prev_sel.get("buy")) != (sel.get("sell"), sel.get("buy")):
        return "quotes_or_spot（墙未变，报价或现价使选档变化）"
    return "unchanged"


def _user_sell_select(p: dict, now: datetime) -> dict:
    """user-sell-fixed-v1（用户 2026-09-30：「档位我选卖腿黄金380，白银卖腿55，保护腿你来挑吧，价差5左右看成交」）：
    卖腿固定为用户指定行权价；保护腿在宽度 [target−1, target+1] 内、有有效 ask 的挂牌行权价里取最接近 target 的
    （并列取更宽的，保护更远）。不设权利金比例门槛（用户批次），但仍要 0 < 权利金 < 宽度、含费最大收益 > 0、无交叉报价。"""
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.longbridge_quote import fetch_stock_quotes
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    root = p["underlying"].split(".")[0]
    today = now.astimezone(ET).date()
    cfg, st = load_config(), SnapshotStore()
    key = next(k for k, i in cfg.instruments.items() if i.options and i.options.symbol == root)
    f = session_index(st, root).get(today) or session_index(st, root).get(max(d for d in session_index(st, root) if d <= today))
    snap = snapshot_from_payload(st.load("options", root, f), key, root)
    ks, tw, up = float(p["k_sell"]), float(p["target_width"]), p["side"] == "C"
    listed = sorted({c.strike for c in snap.contracts if c.expiry.isoformat() == p["expiry"] and c.kind == p["side"]})
    cands = [k for k in listed if (k > ks if up else k < ks) and tw - 1 <= abs(k - ks) <= tw + 1]
    syms = {k: option_symbol(root, p["expiry"], p["side"], k) for k in [ks] + cands}
    d = _depth(list(syms.values()))
    quotes = {k: d[v] for k, v in syms.items()}
    sq = fetch_stock_quotes([p["underlying"]])[p["underlying"]]
    base = {"rule": "user-sell-fixed-v1", "sell": ks, "nearest_wall": None,
            "inputs": {"spot": sq.last, "spot_fetched_at": datetime.now(timezone.utc).isoformat(),
                       "quotes": {str(k): v for k, v in quotes.items()}, "symbols": {str(k): v for k, v in syms.items()},
                       "oi_snapshot": str(st.path_of("options", root, f)), "params_hash": f"target_width={tw}"}}
    return {**base, **pick_protective(p["side"], ks, tw, cands, quotes, p["qty"], p["fee_round_trip"])}


def pick_protective(side: str, ks: float, tw: float, cands: list, quotes: dict, qty: int, fee: float) -> dict:
    """纯函数：卖腿 ks 固定；保护腿在宽度 [tw−1, tw+1] 的候选里按「离 tw 最近、并列取更宽」依次试，
    第一个有有效 ask、无交叉、0 < 权利金 < 宽度、含费最大收益 > 0 的即选定。卖腿须有有效 bid。"""
    from scripts.paper_strike_rule import _crossed, _ok
    qs = quotes.get(ks) or {}
    if qs.get("error") or not _ok(qs.get("bid")) or _crossed(qs):
        return {"ok": False, "reason": f"sell_no_bid_or_crossed（{ks:g}）"}
    tried = []
    for kb in sorted((k for k in cands if tw - 1 <= abs(k - ks) <= tw + 1), key=lambda k: (abs(abs(k - ks) - tw), -abs(k - ks))):
        qb = quotes.get(kb) or {}
        if qb.get("error") or not _ok(qb.get("ask")) or _crossed(qb):
            tried.append({"buy": kb, "reject": "buy_no_ask_or_crossed"}); continue
        w = abs(kb - ks)
        cr = round(qs["bid"] - qb["ask"], 4)
        if not 0 < cr < w:
            tried.append({"buy": kb, "credit": cr, "reject": "credit_outside_0_width"}); continue
        e = economics(side, ks, kb, cr, qty, fee)
        if e["max_gain_usd"] <= 0:
            tried.append({"buy": kb, "credit": cr, "reject": "fee_exceeds_credit"}); continue
        tried.append({"buy": kb, "credit": cr, "reject": None})
        return {"ok": True, "buy": kb, "width": w, "credit": cr, "credit_ratio": round(cr / w, 4), "economics": e, "tried": tried}
    return {"ok": False, "reason": "no_protective_leg（宽度 target±1 内无可成交保护腿）", "tried": tried}


_SELECTORS = {"wall-dynamic-v1": "_dynamic_select", "user-sell-fixed-v1": "_user_sell_select"}


def step(p: dict, now: datetime, *, depth=_depth, session_close=_session_close, selector=None) -> str | None:
    """推进一步；返回动作名（None = 没到点）。纯逻辑 + 注入的取数函数，便于测试。"""
    et = now.astimezone(ET)
    today = et.date()
    ev = p.setdefault("events", [])
    st = p["state"]
    if st in TERMINAL:
        return None
    bad = validate_spec(p)
    if bad:
        p["state"] = "invalid_spec"; p["invalid_reason"] = bad
        ev.append({"at": now.isoformat(), "action": "invalid_spec", "why": bad})
        return "invalid_spec"
    p.setdefault("rule_version", RULE_VERSION)
    if st == "planned" and p.get("strike_rule"):
        return _step_dynamic(p, now, selector or globals()[_SELECTORS[p["strike_rule"]]])
    width = abs(p["k_sell"] - p["k_buy"])
    if st == "planned":
        lo, hi = (_hm(x) for x in p["entry_window_et"])
        ed = date.fromisoformat(p["entry_date"])
        if today < ed or (today == ed and et.time() < lo):
            return None
        if today > ed or et.time() >= hi:                  # 窗口已过（含错过入场日）→ 终态
            tried = [e for e in ev if e.get("action", "").startswith("entry_quote")]
            if not tried:
                p["state"] = "missed"; p["skip_reason"] = "missed_window（窗口内未运行）"
            else:
                p["state"] = "skipped"; p["skip_reason"] = p.get("last_reject") or "no_valid_quote"
            ev.append({"at": now.isoformat(), "action": p["state"], "why": p["skip_reason"]})
            return p["state"]
        q = depth([p["sell"], p["buy"]])
        s, b = q.get(p["sell"]), q.get(p["buy"])
        if not (quote_ok(s) and quote_ok(b)):
            p["last_reject"] = "no_valid_quote（报价缺失/非有限/bid>ask）"
            ev.append({"at": now.isoformat(), "action": "entry_quote_invalid", "quotes": q, "why": p["last_reject"]})
            return "retry"
        credit = round(s["bid"] - b["ask"], 4)
        if not 0 < credit < width:
            p["last_reject"] = f"quote_anomaly（权利金 {credit} 不在 (0, 宽度 {width:g})）"
            ev.append({"at": now.isoformat(), "action": "entry_quote_invalid", "quotes": q, "why": p["last_reject"]})
            return "retry"
        if credit / width < p["min_credit_ratio"]:
            p["last_reject"] = f"credit_low（{credit:.2f}/{width:g} < {p['min_credit_ratio']:.0%}）"
            ev.append({"at": now.isoformat(), "action": "entry_quote", "quotes": q, "why": p["last_reject"]})
            return "retry"                                 # 窗口内继续取；第一份合格即入场
        econ = economics(p["side"], p["k_sell"], p["k_buy"], credit, p["qty"], p["fee_round_trip"])
        lab = quote_labels(q, p["sell"], p["buy"], p["qty"])
        p.update(state="entered", entered_at=now.isoformat(), entry_credit=credit, economics=econ,
                 stop_value=round(p["stop_mult"] * credit, 4), entry_quote_labels=lab)
        ev.append({"at": now.isoformat(), "action": "enter", "quotes": q, "credit": credit, "economics": econ,
                   "quote_labels": lab})
        return "enter"
    # —— entered ——
    exp = date.fromisoformat(p["expiry"])
    if today > exp or (today == exp and et.time() >= time(16, 20)):
        c = session_close(p["underlying"], exp)
        if c is None:
            if p.get("settlement_status") != "pending":
                p["settlement_status"] = "pending"
                ev.append({"at": now.isoformat(), "action": "settlement_pending", "why": "到期日收盘价尚不可得"})
                return "settlement_pending"
            return None
        s = c["close"]
        val = _spread_value(p, s)
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        p.update(state="settled", settled_at=now.isoformat(), settlement_status="done", settle=c,
                 settle_value=round(val, 4), pnl_usd=pnl,
                 settlement_model="理论到期记账：按到期日常规收盘价的内在价值现金化；不模拟提前行权/指派/实物交割")
        ev.append({"at": now.isoformat(), "action": "settle", "close": c, "value": val, "pnl": pnl})
        return "settle"
    done = {e.get("slot") for e in ev if e.get("action") in ("mark", "mark_offhours")}
    for slot in p["mark_slots_et"]:
        key = f"{today.isoformat()} {slot}"
        t0 = _hm(slot)
        if key in done or today > exp or et.time() < t0 or (et.hour * 60 + et.minute) > t0.hour * 60 + t0.minute + 15:
            continue
        q = depth([p["sell"], p["buy"]])
        v = close_value(q, p)
        if v is None:
            ev.append({"at": now.isoformat(), "action": "mark_retry", "slot": key, "quotes": q})
            return "retry"
        val, assume = v
        in_rth = RTH[0] <= et.time() < RTH[1]
        ev.append({"at": now.isoformat(), "action": "mark" if in_rth else "mark_offhours", "slot": key,
                   "quotes": q, "value": val, "valuation_assumption": assume,
                   "note": "稀疏检查（非连续止损）" + ("" if in_rth else "；非常规时段只估值、不执行")})
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        if in_rth and val >= p["stop_value"]:
            p.update(state="closed_stop", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl)
            ev.append({"at": now.isoformat(), "action": "stop", "value": val, "pnl": pnl})
            return "stop"
        return "mark"
    return None


def close_value(q: dict, p: dict):
    """平仓估值 = 卖腿 ask − 买腿 bid。卖腿必须有有效 ask；保护腿常无买盘 → 买腿 bid 缺失时按 0 计（保守：平仓收不回它），
    并返回估值假设（Codex 028：缺保护腿 bid 不能静默折零、须单列假设）。报价不可用 → None。"""
    s, b = q.get(p["sell"]), q.get(p["buy"])
    s_ok = s is not None and not s.get("error") and _finite(s.get("ask")) and s["ask"] > 0 and \
        not (_finite(s.get("bid")) and s["bid"] > s["ask"])
    b_bid = b.get("bid") if b else None
    b_ok = b is not None and not b.get("error") and (b_bid is None or (_finite(b_bid) and b_bid >= 0)) and \
        not (_finite(b_bid) and _finite(b.get("ask")) and b_bid > b["ask"])
    if not (s_ok and b_ok):
        return None
    assume = None if _finite(b_bid) and b_bid > 0 else "buy_bid_missing→按 0 计（保守）"
    return round(s["ask"] - (b_bid if assume is None else 0.0), 4), assume


def manual_close(tid: str, note: str, now: datetime | None = None, *, depth=_depth) -> dict:
    """用户主动提前平仓（用户 2026-09-30：「提前平仓可以由我来主动提。暂时可以先不加规则」）：
    按当时保守报价（卖腿 ask、买腿 bid，缺买盘按 0 并标注）平仓，记 closed_manual 与用户原话。与 tick 共用锁。"""
    now = now or datetime.now(timezone.utc)
    with open(LOCK, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        j = json.loads(JOURNAL.read_text("utf-8"))
        t = next((x for x in j.get("theses", []) if x["id"] == tid), None)
        if t is None or (t.get("paper") or {}).get("state") != "entered":
            return {"ok": False, "why": f"{tid} 不存在或不在场"}
        p = t["paper"]
        q = depth([p["sell"], p["buy"]])
        v = close_value(q, p)
        if v is None:
            p["events"].append({"at": now.isoformat(), "action": "manual_close_retry", "quotes": q, "note": note})
            _write_journal(j)
            return {"ok": False, "why": "报价不可用，未平仓（已留痕）"}
        val, assume = v
        in_rth = RTH[0] <= now.astimezone(ET).time() < RTH[1]
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        p.update(state="closed_manual", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl)
        p["events"].append({"at": now.isoformat(), "action": "manual_close", "quotes": q, "value": val, "pnl": pnl,
                            "valuation_assumption": assume, "in_rth": in_rth, "user_note": note})
        _outcome(t)
        _write_journal(j)
        sync_ledger(j)
        return {"ok": True, "value": val, "pnl_usd": pnl, "valuation_assumption": assume, "in_rth": in_rth}


def _step_dynamic(p: dict, now: datetime, selector) -> str | None:
    """自动选档的入场：窗口内每次唤醒按规则重选；第一份合格即入场并锁定档位（入场后不换档）。"""
    et = now.astimezone(ET)
    today = et.date()
    ev = p.setdefault("events", [])
    lo, hi = (_hm(x) for x in p["entry_window_et"])
    ed = date.fromisoformat(p["entry_date"])
    prep = _hm(p["prep_from_et"]) if p.get("prep_from_et") else None
    if today == ed and prep is not None and prep <= et.time() < lo:
        # 入场前预取盘面与数据（用户 2026-09-30：「10点前就要获取盘面和数据了」）：只记录预选，不入场
        done_prep = [e for e in ev if e.get("action") == "prep_selection" and e.get("at", "")[:16] == now.isoformat()[:16]]
        if done_prep:
            return None
        sel = selector(p, now)
        ev.append({"at": now.isoformat(), "action": "prep_selection", "selection": sel})
        return "prep"
    if today < ed or (today == ed and et.time() < lo):
        return None
    if today > ed or et.time() >= hi:
        tried = [e for e in ev if e.get("action") == "selection"]
        p["state"] = "missed" if not tried else "skipped"
        p["skip_reason"] = "missed_window（窗口内未运行）" if not tried else (p.get("last_reject") or "no_qualifying_selection")
        ev.append({"at": now.isoformat(), "action": p["state"], "why": p["skip_reason"]})
        return p["state"]
    sel = selector(p, now)
    prev = next((e["selection"] for e in reversed(ev) if e.get("action") in ("selection", "prep_selection")), None)
    reason = wall_change_reason(prev, sel)
    ev.append({"at": now.isoformat(), "action": "selection", "selection": sel, "wall_change_reason": reason})
    if not sel.get("ok"):
        p["last_reject"] = sel.get("reason")
        return "retry"
    syms = (sel.get("inputs") or {}).get("symbols") or {}
    qs = (sel.get("inputs") or {}).get("quotes") or {}
    p.update(sell=syms.get(str(sel["sell"])), buy=syms.get(str(sel["buy"])), k_sell=float(sel["sell"]), k_buy=float(sel["buy"]))
    q = {p["sell"]: qs.get(str(sel["sell"])), p["buy"]: qs.get(str(sel["buy"]))}
    econ = sel["economics"]
    lab = quote_labels(q, p["sell"], p["buy"], p["qty"])
    p.update(state="entered", entered_at=now.isoformat(), entry_credit=sel["credit"], economics=econ,
             stop_value=round(p["stop_mult"] * sel["credit"], 4), entry_quote_labels=lab)
    ev.append({"at": now.isoformat(), "action": "enter", "quotes": q, "credit": sel["credit"], "economics": econ,
               "quote_labels": lab, "selection_rule": sel.get("rule"), "wall_change_reason": reason})
    return "enter"


def _outcome(t: dict) -> None:
    p = t["paper"]
    if p["state"] in ("settled", "closed_stop", "closed_manual"):
        t["trade_pnl"] = f"{p['pnl_usd']:.2f}"
        full = p["state"] == "settled" and p.get("settle_value", 1) == 0
        t["outcome"] = "对" if full else ("错" if p["pnl_usd"] < 0 else "部分")
        t["scored_at"] = datetime.now(ET).date().isoformat()
    elif p["state"] in ("skipped", "missed", "invalid_spec"):
        t["outcome"] = "未执行"
        t["review"] = (t.get("review") or "") + f"【模拟仓未入场】{p.get('skip_reason') or p.get('invalid_reason')}"


OUTCOME_ACTIONS = ("enter", "skipped", "missed", "invalid_spec", "stop", "settle")
MANUAL_ACTIONS = ("manual_close",)
LEDGER_ACTIONS = OUTCOME_ACTIONS + ("settle_audit",) + MANUAL_ACTIONS


def event_id(thesis_id: str, e: dict) -> str:
    return f"{thesis_id}|{e['action']}|{e['at']}"


def sync_ledger(j: dict, ledger: Path | None = None) -> int:
    """paper-discretionary 研究台账 = journal 关键事件的只追加投影（Codex 025/026）。
    在 journal 原子替换【之后】调用；按稳定 event_id 去重 —— 中途崩溃后下次唤醒补齐，重复运行不重复写。
    台账某行解析失败 → 抛错（不静默跳过、不覆盖）。返回本次追加条数。"""
    ledger = ledger or LEDGER
    seen = set()
    if ledger.exists():
        for i, line in enumerate(ledger.read_text("utf-8").splitlines(), 1):
            if line.strip():
                try:
                    seen.add(json.loads(line)["event_id"])
                except (ValueError, KeyError) as e:
                    raise RuntimeError(f"{ledger.name} 第 {i} 行损坏：{e}")
    new = []
    for t in j.get("theses", []):
        p = t.get("paper")
        if t.get("execution") != "模拟" or not p:
            continue
        for e in p.get("events") or []:
            if e.get("action") not in LEDGER_ACTIONS or event_id(t["id"], e) in seen:
                continue
            new.append({"event_id": event_id(t["id"], e), "thesis_id": t["id"],
                        "rule_version": p.get("rule_version", RULE_VERSION), "action": e["action"], "at": e["at"],
                        "state_now": p["state"],
                        "spec": {k: p.get(k) for k in ("underlying", "expiry", "side", "sell", "buy", "k_sell", "k_buy",
                                                       "qty", "entry_window_et", "min_credit_ratio", "stop_mult",
                                                       "fee_round_trip", "mark_slots_et")},
                        "evidence": e,
                        "result": {k: p.get(k) for k in ("entry_credit", "economics", "pnl_usd", "settle", "skip_reason")}})
    if new:
        with open(ledger, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush(); os.fsync(f.fileno())
    return len(new)


def chain_key(j: dict, t: dict) -> tuple:
    """判断 × 批次：沿 continuation_of 追到根记录（同一事前判断），批次缺省为 legacy。"""
    by = {x["id"]: x for x in j.get("theses", [])}
    root, seen = t, set()
    while (root.get("paper") or {}).get("continuation_of") in by and root["id"] not in seen:
        seen.add(root["id"])
        root = by[root["paper"]["continuation_of"]]
    return root["id"], (t.get("paper") or {}).get("batch", "legacy")


def entered_in_chain(j: dict, key: tuple, exclude: str | None = None) -> str | None:
    """同一判断 × 批次里已有模拟成交的记录 id（有 entered_at 即算，含已结算/已止损）。"""
    for x in j.get("theses", []):
        if x["id"] != exclude and x.get("execution") == "模拟" and (x.get("paper") or {}).get("entered_at") \
                and chain_key(j, x) == key:
            return x["id"]
    return None


def _supersede(p: dict, now: datetime, why: str) -> None:
    p.update(state="skipped", skip_reason=why)
    p.setdefault("events", []).append({"at": now.isoformat(), "action": "skipped", "why": why})


def _write_journal(j: dict) -> None:
    body = json.dumps(j, ensure_ascii=False, indent=2)
    fd, name = tempfile.mkstemp(dir=JOURNAL.parent, prefix=".journal.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body); f.flush(); os.fsync(f.fileno())
    if json.loads(Path(name).read_text("utf-8")) != j:
        raise RuntimeError("journal 回读校验失败，未替换")
    os.replace(name, JOURNAL)


def register_revision(new: dict, now: datetime | None = None) -> dict:
    """登记新版本（用户 2026-09-29：墙/价格更新后重新判断算延续；「盘中已成交，那么就不应该乱动了，换挡应该是开没开仓的时候」）。
    与 tick 共用同一把锁：同一判断 × 批次已有模拟成交 → 拒绝（入场后行权价/到期/数量固定，不自动平旧开新）；
    否则把同链上仍 planned 的旧版本记 superseded（原因留痕），再追加新版本。返回 {"ok", "why", "superseded"}。"""
    now = now or datetime.now(timezone.utc)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        j = json.loads(JOURNAL.read_text("utf-8"))
        if any(x["id"] == new["id"] for x in j.get("theses", [])):
            return {"ok": False, "why": f"id 已存在：{new['id']}", "superseded": []}
        j.setdefault("theses", []).append(new)
        key = chain_key(j, new)
        hit = entered_in_chain(j, key, exclude=new["id"])
        if hit:
            j["theses"].pop()
            return {"ok": False, "why": f"同一判断×批次已有模拟成交（{hit}）：入场后不换档", "superseded": []}
        sup = []
        for x in j["theses"][:-1]:
            if x.get("execution") == "模拟" and (x.get("paper") or {}).get("state") == "planned" and chain_key(j, x) == key:
                _supersede(x["paper"], now, f"superseded_by_revision：由 {new['id']} 替代（入场前重新选档）")
                x["paper"]["continued_by"] = new["id"]
                sup.append(x["id"])
        _write_journal(j)
        sync_ledger(j)
        fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return {"ok": True, "why": "", "superseded": sup}


def tick(now: datetime | None = None, *, depth=None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = []
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        j = json.loads(JOURNAL.read_text("utf-8"))
        changed = False
        for t in j.get("theses", []):
            p = t.get("paper")
            if t.get("execution") != "模拟" or not p:
                continue
            n_ev = len(p.get("events") or [])
            if p.get("state") == "planned":                     # 同一判断×批次已成交 → 其余候选不再入场（入场后不换档）
                hit = entered_in_chain(j, chain_key(j, t), exclude=t["id"])
                if hit:
                    _supersede(p, now, f"superseded_by_entry：同一判断×批次已有模拟成交（{hit}），入场后不换档")
                    _outcome(t)
                    changed = True
                    out.append(f"{t['id']}:skipped")
                    continue
            try:
                a = step(p, now, **({"depth": depth} if depth else {})) or audit_settlement(p, now)
            except Exception as e:                      # 取数失败：留痕并在下次唤醒重试
                p.setdefault("events", []).append({"at": now.isoformat(), "action": "error", "error": f"{type(e).__name__}: {e}"[:200]})
                a = "error"
            for e in (p.get("events") or [])[n_ev:]:          # 调度版本随事件落盘（旧/新调度分层统计）
                e.setdefault("sched", os.environ.get("PAPER_SCHED", "manual_or_unknown"))
            if a:
                changed = True
                if a in OUTCOME_ACTIONS:
                    _outcome(t)
                out.append(f"{t['id']}:{a}")
        if changed:
            _write_journal(j)
        sync_ledger(j)                                   # journal 落盘之后再投影；每次都核对补齐
        fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    return out


def size_report(theses: list) -> dict:
    """挂单量子集（Codex 027）：sufficient / insufficient / unknown 三类，各列候选数、已入场数、结果与未入场原因。
    全样本是主表，子集只是执行质量的敏感性分析 —— 不改入场规则，也不按子集结果反向调闸门。
    单位：qty = 组数，每组每腿 1 张；bid_size/ask_size 为一档挂单张数（长桥 depth 的 volume），两者可比。"""
    out = {"all": {"n": 0, "entered": 0, "pnl": [], "not_entered": {}}}
    for t in theses:
        p = t.get("paper") or {}
        if t.get("execution") != "模拟" or not p:
            continue
        lab = (p.get("entry_quote_labels") or {}).get("size_sufficient")
        k = {True: "sufficient", False: "insufficient"}.get(lab, "unknown")
        for g in ("all", k):
            b = out.setdefault(g, {"n": 0, "entered": 0, "pnl": [], "not_entered": {}})
            b["n"] += 1
            if p.get("entered_at"):
                b["entered"] += 1
                if p.get("pnl_usd") is not None:
                    b["pnl"].append(p["pnl_usd"])
            else:
                why = p.get("skip_reason") or p.get("invalid_reason") or p.get("state")
                b["not_entered"][why] = b["not_entered"].get(why, 0) + 1
    return out


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "close":                                   # python3 scripts/paper_trades.py close <id> "用户原话"
        r = manual_close(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1
    if cmd == "report":
        j = json.loads(JOURNAL.read_text("utf-8"))
        rep = size_report(j.get("theses", []))
        print("模拟仓（主表 = 全样本；挂单量子集只作执行质量敏感性，未知不并入任何一类）")
        for g in ("all", "sufficient", "insufficient", "unknown"):
            b = rep.get(g)
            if not b:
                print(f"  {g:12s} n=0"); continue
            print(f"  {g:12s} n={b['n']} 已入场 {b['entered']} 已结 {len(b['pnl'])} 盈亏合计 "
                  f"{sum(b['pnl']):+.2f} 未入场原因 {b['not_entered'] or '—'}")
        return 0
    if cmd == "tick":
        acts = tick()
        print("模拟仓：" + ("；".join(acts) if acts else "无到点动作"))
        return 1 if any(a.endswith(":error") for a in acts) else 0
    j = json.loads(JOURNAL.read_text("utf-8"))
    for t in j.get("theses", []):
        p = t.get("paper")
        if t.get("execution") == "模拟" and p:
            print(t["id"], p["state"], {k: p.get(k) for k in ("rule_version", "entry_credit", "economics", "stop_value",
                                                              "pnl_usd", "skip_reason", "settlement_status")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
