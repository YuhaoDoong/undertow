"""模拟仓自动执行（用户 2026-09-29：「今天开盘后，一个是模拟仓开仓，一个是继续记录数据验证。自动定时进行」）。

  python3 scripts/paper_trades.py tick          # session 钩子每 5 分钟调用：到点才做事，没到点什么都不写
  python3 scripts/paper_trades.py status

只读行情、只写私有文件（data/soul/journal.json 里 execution=模拟 且带 paper 规格的事前判断 + data/paper/ledger.jsonl 研究台账）；
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
LEDGER = ROOT / "data/paper/ledger.jsonl"          # 模拟仓数据单独一个文件夹（用户 2026-09-30），私有、不入库
ET = ZoneInfo("America/New_York")
RULE_VERSION = "paper-sim-v2-20260929"
RTH = (time(9, 30), time(16, 0))
TERMINAL = ("settled", "closed_stop", "closed_manual", "closed_tp", "closed_time", "skipped", "missed", "invalid_spec")


def _clock() -> datetime:
    """取数返回后的真实时刻（Codex 031：选档可能耗时数分钟，入场时刻按报价取回时刻记，不按开始时刻）。测试可替换。"""
    return datetime.now(timezone.utc)
STRIKE_RULES = ("wall-dynamic-v1", "wall-dynamic-v1.1", "user-sell-fixed-v1", "user-debit-atm-v1")
# 结构：credit = 收权利金的价差（卖方，默认）；debit = 付权利金的价差（买方，用户 2026-09-30 原油：「干脆就买方」）。
# 借记价差沿用同一套符号：entry_credit = 卖腿 bid − 买腿 ask < 0（= −付出的权利金），估值/结算/盈亏公式不变。
STRUCTURES = ("credit", "debit")
# 出场规则（只用于 Claude 批次；用户 2026-09-30：「你的那一批你可以自己设置提前平仓规则。不适用我的一批，我的由我主观操作。你可以测试一下」）
EXIT_RULES = {"claude-exit-v1": {"take_profit_frac": 0.5, "time_exit_dte": 3}}
EXIT_RULE_BATCHES = ("rule_dynamic",)        # 自动出场只授权给 Claude 的规则批；用户批次由用户主观操作（Codex 032 R3）


def exit_rule_error(p: dict) -> str | None:
    """出场规则配置是否合法：未知规则名，或挂在未授权批次（如 user_subjective）上 → 返回原因；没有 exit_rule → None。"""
    r = p.get("exit_rule")
    if not r:
        return None
    if r not in EXIT_RULES:
        return f"未知出场规则 {r}"
    if p.get("batch") not in EXIT_RULE_BATCHES:
        return f"出场规则 {r} 未授权给批次 {p.get('batch')}（只授权 {', '.join(EXIT_RULE_BATCHES)}）"
    return None


def in_rth(t: datetime) -> bool:
    """常规交易时段：工作日 09:30–16:00 ET（节假日由交易日历另管；这里只防周末与盘外）。"""
    e = t.astimezone(ET)
    return e.weekday() < 5 and RTH[0] <= e.time() < RTH[1]


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def _finite(*xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def is_debit(side: str, k_sell: float, k_buy: float) -> bool:
    """借记价差 = 买腿更靠近价内（call 买低卖高、put 买高卖低）。由行权价顺序决定，与 validate_spec 的结构声明互相核对。"""
    return (k_sell > k_buy) if side == "C" else (k_sell < k_buy)


def economics(side: str, k_sell: float, k_buy: float, credit: float, qty: int, fee_round_trip: float) -> dict:
    """价差经济量的唯一来源（Codex 025-2）。fee_round_trip = 每组往返费用；整单 = 每组 × qty。
    借记价差：credit 为负（= −付出的权利金 debit）；最大亏损 = debit + 费用，最大收益 = 宽度 − debit − 费用。"""
    width = abs(k_sell - k_buy)
    fee_ps = fee_round_trip / 100.0                      # 每股摊到的费用（1 组 = 100 股）
    if is_debit(side, k_sell, k_buy):
        debit = -credit
        be = (k_buy + debit + fee_ps) if side == "C" else (k_buy - debit - fee_ps)
        return {"structure": "debit", "width": width, "credit": credit, "debit": debit,
                "max_gain_usd": round(((width - debit) * 100 - fee_round_trip) * qty, 2),
                "max_loss_usd": round((debit * 100 + fee_round_trip) * qty, 2),
                "breakeven": round(be, 4), "fee_total_usd": round(fee_round_trip * qty, 2)}
    be = (k_sell - credit + fee_ps) if side == "P" else (k_sell + credit - fee_ps)
    return {"width": width, "credit": credit,
            "max_gain_usd": round((credit * 100 - fee_round_trip) * qty, 2),
            "max_loss_usd": round(((width - credit) * 100 + fee_round_trip) * qty, 2),
            "breakeven": round(be, 4), "fee_total_usd": round(fee_round_trip * qty, 2)}


def _debit(p: dict) -> bool:
    return p.get("structure", "credit") == "debit"


def validate_spec(p: dict) -> str | None:
    if p.get("state") == "planned" and exit_rule_error(p):
        return exit_rule_error(p)
    if p.get("structure", "credit") not in STRUCTURES:
        return f"未知结构 {p.get('structure')}"
    if _debit(p):
        # 借记价差：最大亏损 = 已付权利金，没有 2 倍权利金止损这一说（AGENTS：止损是减损选择）；
        # stop_mult = None 表示不设自动止损（用户批次由用户主动平仓），min_credit_ratio 不适用
        if p.get("stop_mult") is not None and not (_finite(p["stop_mult"]) and 0 < p["stop_mult"] < 1):
            return "借记价差 stop_mult 须为 None 或 (0,1)（价值跌到入场权利金的该比例即止损）"
        p = {**p, "stop_mult": 2.0, "min_credit_ratio": 0.0}           # 以下通用校验对借记价差只查其余字段
    if p.get("strike_rule") and p.get("state") == "planned":          # 自动选档：入场前没有固定档位
        if p["strike_rule"] not in STRIKE_RULES:
            return f"未知选档规则 {p['strike_rule']}"
        if (p["strike_rule"] == "user-debit-atm-v1") != _debit(p):
            return "user-debit-atm-v1 只用于借记价差（structure=debit），借记价差也只能用它"
        if p["strike_rule"] == "user-debit-atm-v1" and not (_finite(p.get("target_width")) and p["target_width"] > 0):
            return "user-debit-atm-v1 需要正的 target_width"
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
    if is_debit(p["side"], p["k_sell"], p["k_buy"]) != _debit(p):
        return ("腿方向错误（借记价差：call 买低卖高、put 买高卖低）" if _debit(p) else
                "腿方向错误（put 价差卖腿行权价须高于买腿，call 相反）")
    if p["fee_round_trip"] < 0 or p["stop_mult"] <= 1 or not 0 <= p["min_credit_ratio"] < 1:
        return "费用/止损倍数/门槛不在合理范围"
    return None


def stop_value_of(p: dict, credit: float):
    """止损线（平仓价值口径，与 close_value 同号）。贷记：价值 ≥ stop_mult × 权利金；
    借记：stop_mult 为 None → 不设自动止损；否则价值 ≥ −stop_mult × debit（即持有价值跌到 debit 的 stop_mult 倍以下）。"""
    if p.get("stop_mult") is None:
        return None
    return round(p["stop_mult"] * credit, 4)


def quote_ok(q: dict) -> bool:
    """报价可用：无显式错误、bid/ask 有限、0 < bid ≤ ask（Codex 026：带 error 的报价一律拒绝）。"""
    return q is not None and not q.get("error") and _finite(q.get("bid"), q.get("ask")) and 0 < q["bid"] <= q["ask"]


def leg_ok(q: dict | None, need: str) -> bool:
    """入场按腿校验（Codex 031：与自动选档一致）——卖腿要有效 bid、买腿要有效 ask；另一侧缺失可以，两侧都有而交叉不行；有 error 不行。"""
    if q is None or q.get("error") or not (_finite(q.get(need)) and q[need] > 0):
        return False
    return not (_finite(q.get("bid")) and _finite(q.get("ask")) and q["bid"] > q["ask"])


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
    flip = (v2 != 0) != (p["settle_value"] != 0) or (pnl2 >= 0) != (p["pnl_usd"] >= 0)
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
    from scripts.paper_strike_rule import PARAMS, PARAMS_V11, params_hash, select_with_shadow
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
    v11 = p["strike_rule"] == "wall-dynamic-v1.1"
    params = PARAMS_V11 if v11 else PARAMS             # v1.1：墙 OI 窗口 = max(14, 目标 DTE)，规则身份与参数哈希随之改变（Codex 031）
    horizon = max(14, (exp - today).days) if v11 else 14
    oi, toi = defaultdict(int), defaultdict(int)
    for c in snap.contracts:
        if c.kind == p["side"] and 0 <= (c.expiry - today).days <= horizon:
            oi[c.strike] += c.open_interest
        if c.kind == p["side"] and c.expiry == exp:
            toi[c.strike] += c.open_interest
    sq = fetch_stock_quotes([p["underlying"]])[p["underlying"]]
    spot, spot_at = sq.last, datetime.now(timezone.utc).isoformat()
    span = params["wall_range"] + params["max_width_frac"] + 0.01
    lo, hi = (spot, spot * (1 + span)) if p["side"] == "C" else (spot * (1 - span), spot)
    listed = sorted({c.strike for c in snap.contracts if c.expiry == exp and c.kind == p["side"] and lo <= c.strike <= hi})
    syms = {k: option_symbol(root, p["expiry"], p["side"], k) for k in listed}
    d = _depth(list(syms.values())) if syms else {}
    quotes = {k: d[v] for k, v in syms.items()}
    sel = select_with_shadow(spot, dict(oi), listed, quotes, params, side=p["side"], target_oi=dict(toi),
                             fee_round_trip=p["fee_round_trip"], qty=p["qty"])
    main = sel["main"]
    return {**main, "shadow_no_ratio": {k: sel["shadow_no_ratio"].get(k) for k in ("ok", "sell", "buy", "credit", "reason")},
            "inputs": {"oi_snapshot": str(path), "oi_snapshot_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                       "oi_window_days": horizon, "spot": spot, "spot_fetched_at": spot_at,
                       "quotes": {str(k): v for k, v in quotes.items()}, "symbols": {str(k): v for k, v in syms.items()},
                       "params_hash": params_hash(params)}}


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


def _debit_atm_select(p: dict, now: datetime) -> dict:
    """user-debit-atm-v1（用户 2026-09-30 原油：「原油也考虑晚上模拟仓开仓 牛市看涨价差（原油期权墙好像一直不太准？所以干脆就买方）」）：
    买腿 = 入场时离现价最近的挂牌行权价（平值），卖腿 = 买腿 ± target_width（call 向上、put 向下）。
    不看墙；按入场时实时保守报价（买腿 ask、卖腿 bid）直接成交。"""
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
    idx = session_index(st, root)
    f = idx.get(today) or idx.get(max(d for d in idx if d <= today))
    snap = snapshot_from_payload(st.load("options", root, f), key, root)
    listed = sorted({c.strike for c in snap.contracts if c.expiry.isoformat() == p["expiry"] and c.kind == p["side"]})
    sq = fetch_stock_quotes([p["underlying"]])[p["underlying"]]
    spot, spot_at = sq.last, datetime.now(timezone.utc).isoformat()
    tw, up = float(p["target_width"]), p["side"] == "C"
    kb = atm_strike(spot, listed, side=p["side"])
    cands = [] if kb is None else [k for k in listed if (k > kb if up else k < kb) and tw - 1 <= abs(k - kb) <= tw + 1]
    syms = {k: option_symbol(root, p["expiry"], p["side"], k) for k in ([kb] if kb is not None else []) + cands}
    d = _depth(list(syms.values())) if syms else {}
    quotes = {k: d[v] for k, v in syms.items()}
    base = {"rule": "user-debit-atm-v1", "buy": kb, "nearest_wall": None,
            "inputs": {"spot": spot, "spot_fetched_at": spot_at, "quotes": {str(k): v for k, v in quotes.items()},
                       "symbols": {str(k): v for k, v in syms.items()}, "oi_snapshot": str(st.path_of("options", root, f)),
                       "params_hash": f"atm+target_width={tw}"}}
    if kb is None:
        return {**base, "ok": False, "reason": "no_listed_strike（目标到期无挂牌行权价）"}
    return {**base, **pick_debit(p["side"], kb, tw, cands, quotes, p["qty"], p["fee_round_trip"])}


def atm_strike(spot, listed: list, *, side: str):
    """离现价最近的挂牌行权价；并列时取更价内的一档（call 取低、put 取高）。"""
    if not _finite(spot) or not listed:
        return None
    return min(listed, key=lambda k: (abs(k - spot), k if side == "C" else -k))


def pick_debit(side: str, kb: float, tw: float, cands: list, quotes: dict, qty: int, fee: float) -> dict:
    """纯函数：买腿 kb 固定（须有有效 ask）；卖腿在宽度 [tw−1, tw+1] 的候选里按「离 tw 最近、并列取更宽」依次试，
    第一个有有效 bid、无交叉、0 < 付出权利金 < 宽度、含费最大收益 > 0 的即选定。返回的 credit 为负（= −debit）。"""
    from scripts.paper_strike_rule import _crossed, _ok
    qb = quotes.get(kb) or {}
    if qb.get("error") or not _ok(qb.get("ask")) or _crossed(qb):
        return {"ok": False, "reason": f"buy_no_ask_or_crossed（{kb:g}）"}
    tried = []
    for ks in sorted((k for k in cands if tw - 1 <= abs(k - kb) <= tw + 1), key=lambda k: (abs(abs(k - kb) - tw), -abs(k - kb))):
        qs = quotes.get(ks) or {}
        if qs.get("error") or not _ok(qs.get("bid")) or _crossed(qs):
            tried.append({"sell": ks, "reject": "sell_no_bid_or_crossed"}); continue
        w = abs(ks - kb)
        debit = round(qb["ask"] - qs["bid"], 4)
        if not 0 < debit < w:
            tried.append({"sell": ks, "debit": debit, "reject": "debit_outside_0_width"}); continue
        e = economics(side, ks, kb, -debit, qty, fee)
        if e["max_gain_usd"] <= 0:
            tried.append({"sell": ks, "debit": debit, "reject": "fee_exceeds_max_gain"}); continue
        tried.append({"sell": ks, "debit": debit, "reject": None})
        return {"ok": True, "sell": ks, "buy": kb, "width": w, "credit": -debit, "debit": debit,
                "debit_ratio": round(debit / w, 4), "economics": e, "tried": tried}
    return {"ok": False, "reason": "no_short_leg（宽度 target±1 内无可成交卖腿）", "tried": tried}


_SELECTORS = {"wall-dynamic-v1": "_dynamic_select", "wall-dynamic-v1.1": "_dynamic_select",
              "user-sell-fixed-v1": "_user_sell_select", "user-debit-atm-v1": "_debit_atm_select"}


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
        back = max(now, _clock())                      # 报价取回时刻（Codex 032 R1：固定档位路径同样不得倒填）
        b_et = back.astimezone(ET)
        if b_et.date() != today or b_et.time() >= hi:
            p.update(state="skipped", skip_reason=f"quote_returned_after_window（{et:%H:%M:%S} 请求、{b_et:%H:%M:%S} 才取回，"
                                                   f"已过窗口 {p['entry_window_et'][1]}）")
            ev.append({"at": back.isoformat(), "action": "skipped", "why": p["skip_reason"], "quotes": q,
                       "requested_at": now.isoformat(), "returned_at": back.isoformat()})
            return "skipped"
        req, now = now, back                           # 以下入场时刻 = 取回时刻
        s, b = q.get(p["sell"]), q.get(p["buy"])
        if not (leg_ok(s, "bid") and leg_ok(b, "ask")):
            p["last_reject"] = "no_valid_quote（卖腿无有效 bid 或买腿无有效 ask，或报价交叉/出错）"
            ev.append({"at": now.isoformat(), "action": "entry_quote_invalid", "quotes": q, "why": p["last_reject"]})
            return "retry"
        credit = round(s["bid"] - b["ask"], 4)
        amt = -credit if _debit(p) else credit             # 借记价差：付出的权利金
        if not 0 < amt < width:
            p["last_reject"] = f"quote_anomaly（{'付出' if _debit(p) else ''}权利金 {amt} 不在 (0, 宽度 {width:g})）"
            ev.append({"at": now.isoformat(), "action": "entry_quote_invalid", "quotes": q, "why": p["last_reject"]})
            return "retry"
        if not _debit(p) and credit / width < p["min_credit_ratio"]:
            p["last_reject"] = f"credit_low（{credit:.2f}/{width:g} < {p['min_credit_ratio']:.0%}）"
            ev.append({"at": now.isoformat(), "action": "entry_quote", "quotes": q, "why": p["last_reject"]})
            return "retry"                                 # 窗口内继续取；第一份合格即入场
        econ = economics(p["side"], p["k_sell"], p["k_buy"], credit, p["qty"], p["fee_round_trip"])
        lab = quote_labels(q, p["sell"], p["buy"], p["qty"])
        p.update(state="entered", entered_at=now.isoformat(), entry_credit=credit, economics=econ,
                 stop_value=stop_value_of(p, credit), entry_quote_labels=lab)
        ev.append({"at": now.isoformat(), "action": "enter", "quotes": q, "credit": credit, "economics": econ,
                   "quote_labels": lab, "requested_at": req.isoformat(), "returned_at": now.isoformat()})
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
    if p.get("close_request") and in_rth(now):
        # 用户主动平仓请求在非交易时段提出 → 到常规时段第一次唤醒时按当时保守报价执行（Codex 031）
        q = depth([p["sell"], p["buy"]])
        back = max(now, _clock())
        if not in_rth(back) or back.astimezone(ET).date() != today:   # 取回时已收盘：请求保留，下个常规时段再执行（032 R1）
            ev.append({"at": back.isoformat(), "action": "manual_close_retry", "quotes": q, "why": "报价取回时已出常规时段，请求保留",
                       "requested_at": now.isoformat(), "returned_at": back.isoformat()})
            return "retry"
        req, now = now, back
        v = close_value(q, p)
        prob = "报价不可用" if v is None else exit_problem(q, p, v[0], v[1], automatic=False)
        if prob:
            ev.append({"at": now.isoformat(), "action": "manual_close_retry", "quotes": q, "why": f"{prob}；请求保留"})
            return "retry"
        val, assume = v
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        rq = p.pop("close_request")
        p.update(state="closed_manual", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl, exit_assumption=assume)
        ev.append({"at": now.isoformat(), "action": "manual_close", "quotes": q, "value": val, "pnl": pnl,
                   "valuation_assumption": assume, "in_rth": True, "user_note": rq.get("note"), "requested_at": rq.get("at"),
                   "quote_requested_at": req.isoformat(), "returned_at": now.isoformat()})
        return "manual_close"
    done = {e.get("slot") for e in ev if e.get("action") in ("mark", "mark_offhours")}
    for slot in p["mark_slots_et"]:
        key = f"{today.isoformat()} {slot}"
        t0 = _hm(slot)
        if key in done or today > exp or et.time() < t0 or (et.hour * 60 + et.minute) > t0.hour * 60 + t0.minute + 15:
            continue
        q = depth([p["sell"], p["buy"]])
        back = max(now, _clock())                  # 报价取回时刻：执行资格按取回时判断（032 R1：15:59 请求、16:01 取回不得当盘内执行）
        v = close_value(q, p)
        if v is None:
            ev.append({"at": back.isoformat(), "action": "mark_retry", "slot": key, "quotes": q})
            return "retry"
        val, assume = v
        req, now = now, back
        live = in_rth(now) and now.astimezone(ET).date() == today       # 周末/盘外/跨日：只估值、不执行
        prob = exit_problem(q, p, val, assume, automatic=True)
        if prob:
            # 估值不可信：不执行任何退出、不占用本时点，窗口内下次唤醒重取（坏报价不得影响模拟仓）
            ev.append({"at": now.isoformat(), "action": "mark_incomplete", "slot": key, "quotes": q, "value": val,
                       "valuation_assumption": assume, "requested_at": req.isoformat(), "returned_at": now.isoformat(),
                       "why": prob, "note": "估值不可信：不执行止损/止盈/时间出场，窗口内重取；盘外也不当作有效估值"})
            return "retry"
        ev.append({"at": now.isoformat(), "action": "mark" if live else "mark_offhours", "slot": key,
                   "quotes": q, "value": val, "valuation_assumption": assume,
                   "requested_at": req.isoformat(), "returned_at": now.isoformat(),
                   "note": "稀疏检查（非连续止损）" + ("" if live else "；取回时不在常规时段，只估值、不执行")})
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        if live and p.get("stop_value") is not None and val >= p["stop_value"]:
            p.update(state="closed_stop", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl, exit_assumption=assume)
            ev.append({"at": now.isoformat(), "action": "stop", "value": val, "pnl": pnl})
            return "stop"
        bad = exit_rule_error(p)
        if bad:                                     # 误带出场规则（如用户批次）：绝不自动退出，配置错误显式上报一次（032 R3）
            if not any(e.get("action") == "exit_rule_config_error" for e in ev):
                ev.append({"at": now.isoformat(), "action": "exit_rule_config_error", "why": bad})
                return "exit_rule_config_error"
            return "mark"
        xr = EXIT_RULES.get(p.get("exit_rule") or "")
        if live and xr and val <= xr["take_profit_frac"] * p["entry_credit"]:
            p.update(state="closed_tp", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl, exit_assumption=assume)
            ev.append({"at": now.isoformat(), "action": "take_profit", "value": val, "pnl": pnl, "exit_rule": p["exit_rule"]})
            return "take_profit"
        if live and xr and (exp - today).days <= xr["time_exit_dte"]:
            p.update(state="closed_time", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl, exit_assumption=assume)
            ev.append({"at": now.isoformat(), "action": "time_exit", "value": val, "pnl": pnl, "exit_rule": p["exit_rule"]})
            return "time_exit"
        return "mark"
    return None


WORTHLESS_ASK = 0.05      # 保护腿无买价、但卖价 ≤ 此值 → 视作确实一文不值，按 0 计可以执行


VALUE_TOL = 0.05          # 估值越界容差：贷记价差价值 ∈ [−tol, 宽度+tol]，借记 ∈ [−宽度−tol, +tol]
LEG_SPREAD_MAX = (0.50, 0.50)   # 自动退出时单腿买卖差上限：max(0.50 美元, 中间价的 50%)；更宽视为报价异常


def exit_problem(q: dict, p: dict, val, assume, *, automatic: bool = True) -> str | None:
    """执行任何退出之前的估值体检（用户 2026-10-06：「要严查错误。模拟仓主要是为了记录交易和数据。这种错误不要影响模拟仓」）。
    返回问题描述（不得执行，只记估值、窗口内重取），None = 可以执行。
    1) 保护腿缺买价、但卖价 > 0.05（不是一文不值）→ 按 0 计的估值不可信。
       起因：2026-10-05 15:45 GLD 10/16 380/385C，385C 瞬时无买价（卖价 3.2）→ 估值 5.3 越过止损 4.9 被平（−$288.2，
       超过该价差理论最大亏损 $258.2）；同时刻按两腿报价约 2.1–2.3，当天收盘 379.55 仍在卖腿下方。
    2) 估值越出价差的理论范围（贷记 0…宽度，借记 −宽度…0）→ 报价异常。
    3) 仅自动退出：任一腿买卖差过宽（> max(0.50, 中间价 50%)）→ 报价异常。用户主动平仓不受此限（宽报价本身是可成交的现实）。"""
    w = abs(p["k_sell"] - p["k_buy"])
    if assume is not None:
        b = q.get(p["buy"]) or {}
        if not (_finite(b.get("ask")) and b["ask"] <= WORTHLESS_ASK):
            return f"保护腿缺买价且卖价 {b.get('ask')} > {WORTHLESS_ASK}，按 0 计的估值不可信"
    lo, hi = ((-w - VALUE_TOL, VALUE_TOL) if p.get("structure") == "debit" else (-VALUE_TOL, w + VALUE_TOL))
    if not (_finite(val) and lo <= val <= hi):
        return f"估值 {val} 越出价差理论范围 [{lo:.2f}, {hi:.2f}]（宽 {w:g}）"
    if automatic:
        for sym in (p["sell"], p["buy"]):
            x = q.get(sym) or {}
            if _finite(x.get("bid")) and _finite(x.get("ask")):
                mid, spr = (x["bid"] + x["ask"]) / 2, x["ask"] - x["bid"]
                if spr > max(LEG_SPREAD_MAX[0], LEG_SPREAD_MAX[1] * mid):
                    return f"{sym} 买卖差 {spr:.2f} 过宽（中间价 {mid:.2f}）"
    return None


def exit_executable(q: dict, p: dict, assume, val=None, *, automatic: bool = True) -> bool:
    if val is None:                               # 兼容旧调用：只查保护腿假设
        b = q.get(p["buy"]) or {}
        return assume is None or (_finite(b.get("ask")) and b["ask"] <= WORTHLESS_ASK)
    return exit_problem(q, p, val, assume, automatic=automatic) is None


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
    # 真实买价 0（报价就是 0）与缺失分开（Codex 032 R2）：前者是可执行报价，后者是估算假设
    assume = None if _finite(b_bid) else "buy_bid_missing→按 0 计（保守估算，非两腿完成退出）"
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

        def _pend(at: datetime, why: str, q=None) -> dict:
            # 不能当场执行 → 记成待执行请求，tick 在下一个常规时段按当时保守报价执行（032 R1：失败不能只留孤立 retry）
            p["close_request"] = {"at": now.isoformat(), "note": note}
            e = {"at": at.isoformat(), "action": "close_requested", "user_note": note, "why": why}
            if q is not None:
                e["quotes"] = q
            p["events"].append(e)
            _write_journal(j)
            sync_ledger(j)
            return {"ok": True, "pending": True, "why": why}
        if not in_rth(now):
            return _pend(now, "非常规交易时段：记下请求，常规时段第一次唤醒按当时保守报价执行")
        q = depth([p["sell"], p["buy"]])
        back = max(now, _clock())
        if not in_rth(back):
            return _pend(back, "报价取回时已出常规时段：请求保留，下个常规时段执行", q)
        v = close_value(q, p)
        prob = "报价不可用" if v is None else exit_problem(q, p, v[0], v[1], automatic=False)
        if prob:
            return _pend(back, f"{prob}：请求保留，下次常规时段唤醒重试", q)
        req, now = now, back
        val, assume = v
        pnl = round(((p["entry_credit"] - val) * 100 - p["fee_round_trip"]) * p["qty"], 2)
        p.update(state="closed_manual", closed_at=now.isoformat(), close_value=val, pnl_usd=pnl, exit_assumption=assume)
        p["events"].append({"at": now.isoformat(), "action": "manual_close", "quotes": q, "value": val, "pnl": pnl,
                            "valuation_assumption": assume, "in_rth": True, "user_note": note,
                            "requested_at": req.isoformat(), "returned_at": now.isoformat()})
        _outcome(t)
        _write_journal(j)
        sync_ledger(j)
        return {"ok": True, "value": val, "pnl_usd": pnl, "valuation_assumption": assume, "in_rth": True}


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
    back = max(now, _clock())                          # 报价取回时刻（选档可能耗时数分钟；取回不可能早于请求）
    prev = next((e["selection"] for e in reversed(ev) if e.get("action") in ("selection", "prep_selection")), None)
    reason = wall_change_reason(prev, sel)
    ev.append({"at": now.isoformat(), "action": "selection", "selection": sel, "wall_change_reason": reason,
               "requested_at": now.isoformat(), "returned_at": back.isoformat()})
    b_et = back.astimezone(ET)
    if b_et.date() != today or b_et.time() >= hi:
        p.update(state="skipped", skip_reason=f"selection_returned_after_window（{now.astimezone(ET):%H:%M:%S} 开始、"
                                               f"{b_et:%H:%M:%S} 才取回，已过窗口 {p['entry_window_et'][1]}）")
        ev.append({"at": back.isoformat(), "action": "skipped", "why": p["skip_reason"]})
        return "skipped"
    now = back                                         # 入场时刻 = 报价取回时刻
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
             stop_value=stop_value_of(p, sel["credit"]), entry_quote_labels=lab)
    ev.append({"at": now.isoformat(), "action": "enter", "quotes": q, "credit": sel["credit"], "economics": econ,
               "quote_labels": lab, "selection_rule": sel.get("rule"), "wall_change_reason": reason})
    return "enter"


def _outcome(t: dict) -> None:
    p = t["paper"]
    if p["state"] in ("settled", "closed_stop", "closed_manual", "closed_tp", "closed_time"):
        t["trade_pnl"] = f"{p['pnl_usd']:.2f}"
        full = p["state"] == "settled" and (abs(p.get("settle_value", 0)) == (p.get("economics") or {}).get("width")
                                            if _debit(p) else p.get("settle_value", 1) == 0)
        t["outcome"] = "对" if full else ("错" if p["pnl_usd"] < 0 else "部分")
        t["scored_at"] = datetime.now(ET).date().isoformat()
    elif p["state"] in ("skipped", "missed", "invalid_spec"):
        t["outcome"] = "未执行"
        t["review"] = (t.get("review") or "") + f"【模拟仓未入场】{p.get('skip_reason') or p.get('invalid_reason')}"


def void_exit(tid: str, note: str, why: str, now: datetime | None = None) -> dict:
    """作废一次由坏数据触发的退出，恢复在场（用户 2026-10-06：「恢复，但是要严查错误……这种错误不要影响模拟仓」）。
    不删任何事件：原退出事件与平仓字段整体移入 voided_exits（含作废理由与用户原话），追加 exit_voided 事件并投影到研究台账；
    只允许作废带估值假设或估值越界的退出（真实止损不能被「恢复」）。"""
    now = now or datetime.now(timezone.utc)
    with open(LOCK, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        j = json.loads(JOURNAL.read_text("utf-8"))
        t = next((x for x in j.get("theses", []) if x["id"] == tid), None)
        p = (t or {}).get("paper") or {}
        if p.get("state") not in ("closed_stop", "closed_tp", "closed_time", "closed_manual"):
            return {"ok": False, "why": f"{tid} 不是已退出状态"}
        ex = next(e for e in reversed(p["events"]) if e["action"] in ("stop", "take_profit", "time_exit", "manual_close"))
        mk = next((e for e in reversed(p["events"]) if e.get("at") == ex["at"] and e["action"] in ("mark", "mark_offhours")), {})
        q = ex.get("quotes") or mk.get("quotes") or {}
        prob = exit_problem(q, p, p.get("close_value"), p.get("exit_assumption"), automatic=ex["action"] != "manual_close")
        if not prob:
            return {"ok": False, "why": "该退出的估值通过体检，不能作废（真实退出不得恢复）"}
        keep = {k: p.pop(k) for k in ("closed_at", "close_value", "pnl_usd", "exit_assumption") if k in p}
        p.setdefault("voided_exits", []).append({"state": p["state"], **keep, "exit_event_at": ex["at"], "problem": prob,
                                                 "why": why, "user_note": note, "voided_at": now.isoformat()})
        p["state"] = "entered"
        p["events"].append({"at": now.isoformat(), "action": "exit_voided", "exit_event_at": ex["at"], "problem": prob,
                            "why": why, "user_note": note})
        t.update(outcome="未验证", trade_pnl=None, scored_at=None)
        _write_journal(j)
        sync_ledger(j)
        return {"ok": True, "problem": prob, "voided": keep}


OUTCOME_ACTIONS = ("enter", "skipped", "missed", "invalid_spec", "stop", "settle", "take_profit", "time_exit")
MANUAL_ACTIONS = ("manual_close", "close_requested", "exit_voided")
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
                        "spec": {k: p.get(k) for k in ("underlying", "expiry", "side", "structure", "strike_rule", "batch",
                                                       "judgment_id", "exit_rule",
                                                       "target_width", "sell", "buy", "k_sell", "k_buy",
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


def judgment_id(j: dict, t: dict) -> str:
    """同一事前判断的标识（统计时判断只算一次）。三种身份分离（Codex 032 R4）：
    judgment_id = 观点（可跨到期/结构/批次）；slot_key = 仓位槽位（成交锁）；continuation_of 链 = 同槽位的修订版本。
    规格里有显式 paper.judgment_id 就用它；没有（旧记录）才退回修订链的根 —— 未知的历史判断关系不自动推断。"""
    jid = (t.get("paper") or {}).get("judgment_id")
    if jid:
        return jid
    by = {x["id"]: x for x in j.get("theses", [])}
    root, seen = t, set()
    while (root.get("paper") or {}).get("continuation_of") in by and root["id"] not in seen:
        seen.add(root["id"])
        root = by[root["paper"]["continuation_of"]]
    return root["id"]


def slot_key(t: dict) -> tuple:
    """仓位槽位（Codex 031）= 批次 × 标的 × 到期 × 方向。入场锁定与修订都按槽位，不按判断：
    同一判断下不同到期是不同仓位，互不阻挡；修订只能改同一槽位里尚未入场的候选。"""
    p = t.get("paper") or {}
    k = (p.get("batch", "legacy"), p.get("underlying"), p.get("expiry"), p.get("side"))
    return k + ("debit",) if _debit(p) else k               # 借记与贷记是不同仓位；贷记的槽位键保持原样


def entered_in_slot(j: dict, slot: tuple, exclude: str | None = None) -> str | None:
    """同一槽位里已有模拟成交的记录 id（有 entered_at 即算，含已结算/已止损/已平仓）。"""
    for x in j.get("theses", []):
        if x["id"] != exclude and x.get("execution") == "模拟" and (x.get("paper") or {}).get("entered_at") \
                and slot_key(x) == slot:
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
    # 回读比对【序列化文本】而不是对象：选档结果里有元组，JSON 回读成列表，对象比较恒不等 →
    # 2026-09-30 ET 09:40 起每次 tick 都「回读校验失败」、预选一条没存（入场前 8 分钟发现）。文本一致 = 落盘无损。
    back = Path(name).read_text("utf-8")
    if back != body or json.loads(back) is None:
        raise RuntimeError("journal 回读校验失败，未替换")
    os.replace(name, JOURNAL)


def register_revision(new: dict, now: datetime | None = None) -> dict:
    """登记新版本（用户 2026-09-29：墙/价格更新后重新判断算延续；「盘中已成交，那么就不应该乱动了，换挡应该是开没开仓的时候」）。
    与 tick 共用同一把锁：同一槽位已有模拟成交 → 拒绝；修订不得改变槽位（入场后行权价/到期/数量固定，不自动平旧开新）；
    否则把同槽位上仍 planned 的旧版本记 superseded（原因留痕），再追加新版本。返回 {"ok", "why", "superseded"}。"""
    now = now or datetime.now(timezone.utc)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        j = json.loads(JOURNAL.read_text("utf-8"))
        if any(x["id"] == new["id"] for x in j.get("theses", [])):
            return {"ok": False, "why": f"id 已存在：{new['id']}", "superseded": []}
        pred_id = (new.get("paper") or {}).get("continuation_of")
        pred = next((x for x in j.get("theses", []) if x["id"] == pred_id), None) if pred_id else None
        if pred is not None and pred.get("paper") and slot_key(pred) != slot_key(new):
            return {"ok": False, "why": f"修订不得改变仓位槽位（{slot_key(pred)} → {slot_key(new)}）；另一个到期/方向请登记为新仓位",
                    "superseded": []}
        if pred is not None and pred.get("paper"):          # 修订沿用原判断身份；显式改判断 = 新观点，不能挂在修订链上
            pj, nj = pred["paper"].get("judgment_id"), new["paper"].get("judgment_id")
            if pj and nj and pj != nj:
                return {"ok": False, "why": f"修订不得改变判断身份（{pj} → {nj}）；新观点请登记为新仓位", "superseded": []}
            if pj and not nj:
                new["paper"]["judgment_id"] = pj
        j.setdefault("theses", []).append(new)
        key = slot_key(new)
        hit = entered_in_slot(j, key, exclude=new["id"])
        if hit:
            j["theses"].pop()
            return {"ok": False, "why": f"同一槽位已有模拟成交（{hit}）：入场后不换档", "superseded": []}
        sup = []
        for x in j["theses"][:-1]:
            if x.get("execution") == "模拟" and (x.get("paper") or {}).get("state") == "planned" and slot_key(x) == key:
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
                hit = entered_in_slot(j, slot_key(t), exclude=t["id"])
                if hit:
                    _supersede(p, now, f"superseded_by_entry：同一槽位已有模拟成交（{hit}），入场后不换档")
                    _outcome(t)
                    changed = True
                    out.append(f"{t['id']}:skipped")
                    continue
            pnow = max(now, _clock())                  # 每个仓位按自己的请求时刻（032 R1：整批复用开始时刻会让排在后面的仓位倒填）
            try:
                a = step(p, pnow, **({"depth": depth} if depth else {})) or audit_settlement(p, pnow)
            except Exception as e:                      # 取数失败：留痕并在下次唤醒重试
                p.setdefault("events", []).append({"at": pnow.isoformat(), "action": "error", "error": f"{type(e).__name__}: {e}"[:200]})
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
    if cmd == "void-exit":                               # python3 scripts/paper_trades.py void-exit <id> "用户原话" "作废理由"
        r = void_exit(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "", sys.argv[4] if len(sys.argv) > 4 else "")
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
