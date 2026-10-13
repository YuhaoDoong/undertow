"""ps-explore-v0.3：道氏结构历史设计集探索回测（Codex 037 放行；只跑 ≤2015-12-31，2016 起不取、不算）。

  python3 scripts/ps_explore.py --run-id run3              # 取数（截止 2016-01-01 前）→ 校验 → 回测 → 写 data/backtest/ps-explore-v0.3/run3/
  python3 scripts/ps_explore.py --run-id run4 --barrier    # 另加固定近距离屏障探索（Codex 038：2ATR 主描述，1/3ATR 敏感性）
  run-id 必填；目录已存在则拒绝运行（不覆盖已看过的结果）。原始输入复用 inputs/，并与首次记录的 sha256 核对。

纯标准库；价格 = Yahoo 日线 OHLC（已按拆股调整、未按分红调整 → 价格回报研究，不称净经济优势）；做空融券成本未知 → 做空只是理论方向结果。
没有期权价格：不报任何卖方价差胜率或盈利。全部参数未校准（m=2、2R、60 日、0.1ATR 缓冲、T/V 三档、±250 日三角核）。
"""
from __future__ import annotations

import bisect
import hashlib
import json
import statistics as st
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import price_structure as ps  # noqa: E402

RUN = "ps-explore-v0.3"
BASE = ROOT / "data/backtest" / RUN
INPUTS = BASE / "inputs"
INPUT_SHA = BASE / "inputs_sha256.json"
OUT = None                      # main() 按 --run-id 设定
BARRIER_KS = (2.0, 1.0, 3.0)    # 2ATR 主描述；1、3ATR 为预先列明的敏感性（Codex 038）
BARRIER_DAYS = 10
SYMS = ("GLD", "SLV", "USO", "QQQ", "SPY")
DESIGN_END = date(2015, 12, 31)
PERIOD2 = int(datetime(2016, 1, 1, tzinfo=timezone.utc).timestamp())   # 请求就截止在设计集之后，不取 2016 起的数据
KERNEL_SPAN = 250
V_LOOKBACK = 500


# ---------------- 数据 ----------------
def fetch(sym: str) -> tuple[dict, str]:
    from undertow.collect.base import http_get_json
    f = INPUTS / f"{sym}.json"
    if f.exists():
        raw = f.read_bytes()
    else:
        d = http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                          {"period1": 0, "period2": PERIOD2, "interval": "1d", "events": "div,splits"}, timeout=60)
        raw = json.dumps(d, sort_keys=True).encode()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    rec = json.loads(INPUT_SHA.read_text("utf-8")) if INPUT_SHA.exists() else {}
    if sym in rec and rec[sym] != sha:
        raise SystemExit(f"{sym}：输入文件哈希与首次记录不符（{rec[sym][:12]} → {sha[:12]}），拒绝计算")
    if sym not in rec:
        rec[sym] = sha
        INPUT_SHA.write_text(json.dumps(rec, indent=1), "utf-8")
    return json.loads(raw), sha


def parse(d: dict) -> tuple[list, list, dict]:
    """→ (bars, 无效行, 公司行动)。任一 OHLC 缺失的行剔除并记录（不插值）。"""
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    bars, bad = [], []
    for i, ts in enumerate(r["timestamp"]):
        dt = datetime.fromtimestamp(ts, timezone.utc).date()
        row = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if any(x is None for x in row):
            bad.append(dt.isoformat()); continue
        bars.append((dt, *row))
    ev = r.get("events") or {}
    return bars, bad, {"splits": ev.get("splits") or {}, "dividends_n": len(ev.get("dividends") or {})}


def split_check(bars: list, actions: dict) -> list:
    """拆股日前后收盘比若接近拆股比 → 原始未调整价格（单位断点）→ 报错。Yahoo OHLC 实测已按拆股调整（QQQ 2000-03-20 连续）。"""
    out = []
    idx = {b[0]: i for i, b in enumerate(bars)}
    for v in actions["splits"].values():
        d = datetime.fromtimestamp(v["date"], timezone.utc).date()
        ratio = v["numerator"] / v["denominator"]
        i = idx.get(d)
        if i is None or i == 0:
            out.append({"date": d.isoformat(), "ratio": ratio, "check": "date_not_in_bars"}); continue
        jump = bars[i][4] / bars[i - 1][4]
        raw = abs(jump - 1 / ratio) < abs(jump - 1)
        out.append({"date": d.isoformat(), "ratio": ratio, "close_ratio": round(jump, 4),
                    "check": "UNADJUSTED_UNIT_BREAK" if raw else "continuous_adjusted"})
    return out


def segments(bars: list, ref_dates: list) -> list:
    """以参考日历（SPY 交易日）为准：品种首日之后参考日历有而该品种缺的会话 → 切段（段内状态机独立预热，不插值）。"""
    have = {b[0] for b in bars}
    first = bars[0][0]
    missing = [d for d in ref_dates if d >= first and d not in have]
    segs, cur = [], []
    miss = set(missing)
    ref_after = [d for d in ref_dates if d >= first]
    bi = {b[0]: b for b in bars}
    for d in ref_after:
        if d in miss:
            if cur:
                segs.append(cur); cur = []
        else:
            cur.append(bi[d])
    if cur:
        segs.append(cur)
    return segs, missing


# ---------------- 匹配变量 ----------------
def state_vars(bars: list, atr: list) -> list:
    """第 u 根入场时可知（u−1 收盘）的 T、V 档位。T = (C−SMA50)/ATR 三档；V = ATR/C 在此前 500 根中的分位三档。不足 → None。"""
    C = [b[4] for b in bars]
    out = [None] * len(bars)
    ratio = [None if atr[i] is None else atr[i] / C[i] for i in range(len(bars))]
    for u in range(1, len(bars)):
        j = u - 1
        if j < 49 or atr[j] is None or j < V_LOOKBACK:
            continue
        sma = sum(C[j - 49:j + 1]) / 50
        T = (C[j] - sma) / atr[j]
        tb = 0 if T < -1 else (2 if T > 1 else 1)
        hist = [x for x in ratio[j - V_LOOKBACK:j] if x is not None]
        if len(hist) < V_LOOKBACK * 0.9:
            continue
        pct = sum(1 for x in hist if x < ratio[j]) / len(hist)
        vb = 0 if pct < 1 / 3 else (1 if pct < 2 / 3 else 2)
        out[u] = (tb, vb)
    return out


def control_outcome(bars, atr, u, k, direction, last_i):
    """控制：u 日开盘入场，止损距离 = k × ATR_{u−1}（与事件的 k = 风险 / ATR_{入场前一根} 同一尺度，Codex 038 修），其余规则同事件。"""
    if atr[u - 1] is None:
        return None
    s = 1 if direction == "long" else -1
    stop = bars[u][1] - s * k * atr[u - 1]
    r = ps.simulate(bars, u, stop, direction, last_i=last_i)
    if r["status"] != "entered":
        return None
    return r["r_mult"], ps.barrier_survival(bars, u, stop, direction, 5), ps.barrier_survival(bars, u, stop, direction, 10)


def barrier_obs(bars, atr, i, kb, direction):
    """固定近距离屏障：入场开盘 O_i 向不利方向 kb × ATR_{i−1}。返回 (10 日内任一收盘越过, 第 10 日收盘越过)；窗口不足 → (None, None)。
    只是价格屏障描述，不是缩短止损后的交易收益。"""
    if atr[i - 1] is None or i + BARRIER_DAYS - 1 >= len(bars):
        return None, None
    s = 1 if direction == "long" else -1
    lvl = bars[i][1] - s * kb * atr[i - 1]
    surv = ps.barrier_survival(bars, i, lvl, direction, BARRIER_DAYS)
    end = s * bars[i + BARRIER_DAYS - 1][4] < s * lvl
    return (None if surv is None else not surv), end


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------- 主流程 ----------------
def run_symbol(sym: str, ref_dates: list, log: list, barrier: bool = False) -> dict:
    d, sha = fetch(sym)
    bars_all, bad, actions = parse(d)
    if bars_all and bars_all[-1][0] > DESIGN_END:
        raise SystemExit(f"{sym}：取回数据含 {bars_all[-1][0]}，超出设计集 —— 拒绝计算")
    sc = split_check(bars_all, actions)
    if any(x["check"] == "UNADJUSTED_UNIT_BREAK" for x in sc):
        return {"symbol": sym, "status": "paused_unit_break", "split_check": sc, "input_sha256": sha}
    segs, missing = segments(bars_all, ref_dates)
    log.append(f"{sym}: {len(bars_all)} 根 {bars_all[0][0]}–{bars_all[-1][0]}，缺会话 {len(missing)}，无效行 {len(bad)}，段 {len(segs)}")
    episodes, trades = [], []
    for si, bars in enumerate(segs):
        if len(bars) < ps.WARMUP + 5:
            continue
        atr = ps.wilder_atr(bars)
        piv, eps = ps.detect(bars, atr)
        last_i = len(bars) - 1
        seg_ends_at_design_end = bars[-1][0] == bars_all[-1][0]
        sv = state_vars(bars, atr)
        dates = [b[0] for b in bars]
        a_entries = {"long": set(), "short": set()}
        busy_until = {"long": -1, "short": -1}
        rows = []
        # 按信号（或起点）时间顺序处理，同向占用判断才正确
        for e in sorted(eps, key=lambda e: (e.signal_at if e.signal_at is not None else e.start, e.start)):
            s = 1 if e.direction == "long" else -1
            base = {"episode_id": f"{sym}-{si}-{e.kind}-{e.direction}-{e.start}", "symbol": sym, "segment": si,
                    "kind": e.kind, "direction": e.direction, "regime_entered_at": str(dates[e.regime_at]) if e.regime_at is not None else None,
                    "key": {"price": e.key.price, "extreme_at": str(dates[e.key.idx]), "confirmed_at": str(dates[e.key.conf])},
                    "extreme": {"price": e.extreme.price, "extreme_at": str(dates[e.extreme.idx]), "confirmed_at": str(dates[e.extreme.conf])},
                    "start": str(dates[e.start]), "break_at": str(dates[e.break_at]) if e.break_at is not None else None,
                    "hl1": None if e.hl1 is None else {"price": e.hl1.price, "extreme_at": str(dates[e.hl1.idx]),
                                                       "confirmed_at": str(dates[e.hl1.conf]), "confirm_lag": e.hl1.conf - e.hl1.idx},
                    "status": e.status, "partition": "design"}
            if e.kind == "A":
                if e.status == "signal":
                    sig = e.signal_at
                    a_ref = atr[sig - 1]
                    stop = e.extreme.price - s * ps.STOP_BUF * a_ref
                    res = ps.simulate(bars, sig + 1, stop, e.direction, last_i=last_i)
                    if res["status"] == "boundary_excluded" and not seg_ends_at_design_end:
                        res["status"] = "missing_session_in_hold"
                    base.update(signal_at=str(dates[sig]), atr_used={"value": a_ref, "as_of": str(dates[sig - 1])},
                                stop=stop, signal_to_stop_atr=round(s * (bars[sig][4] - stop) / a_ref, 4))
                    if res["status"] == "entered":
                        ei = sig + 1
                        if ei <= busy_until[e.direction]:
                            base["status"] = "occupied"; res = {"status": "occupied"}
                        else:
                            busy_until[e.direction] = res["exit_i"]
                            a_entries[e.direction].add(ei)
                            # 止损规则不变（LL − 0.1·ATR_{sig−1}）；风险距离归一化单独用入场前一根 ATR_{ei−1}，与控制同尺度（Codex 038）
                            k = res["risk"] / atr[ei - 1]
                            base.update(stop_buffer_atr_asof=str(dates[sig - 1]), entry_scale_atr_asof=str(dates[ei - 1]),
                                        entry_scale_atr=atr[ei - 1],
                                        entry_at=str(dates[ei]), entry=res["entry"], risk=res["risk"], k_atr=round(k, 4),
                                        target=res["entry"] + s * ps.TARGET_R * res["risk"],
                                        outcome={kk: res[kk] for kk in ("exit_reason", "exit_price", "r_mult", "days_held")}
                                        | {"exit_at": str(dates[res["exit_i"]])},
                                        h4={"d5": ps.barrier_survival(bars, ei, stop, e.direction, 5),
                                            "d10": ps.barrier_survival(bars, ei, stop, e.direction, 10)},
                                        _ei=ei, _k=k)
                            base["status"] = "entered"
                    else:
                        base["status"] = res["status"]
                rows.append(base)
            else:
                # H2：立即政策 I 与等待政策 W（共同起点 = HL₁ᴮ 确认日）
                sig = e.signal_at
                stopI = e.extreme.price - s * ps.STOP_BUF * atr[sig - 1]
                rI = ps.simulate(bars, sig + 1, stopI, e.direction, last_i=last_i)
                w = {"status": e.policy_w_status}
                if e.policy_w_status == "signal":
                    ws = e.policy_w_signal
                    stopW = e.hl1.price - s * ps.STOP_BUF * atr[ws - 1]
                    rW = ps.simulate(bars, ws + 1, stopW, e.direction, last_i=last_i)
                    w.update(signal_at=str(dates[ws]), result=rW.get("status"), r_mult=rW.get("r_mult"),
                             days_held=rW.get("days_held"), wait_days=ws - sig)
                base.update(policy_i={"result": rI.get("status"), "r_mult": rI.get("r_mult"), "days_held": rI.get("days_held")},
                            policy_w=w, end_beyond_data=(sig + 1 + ps.WAIT_MAX + ps.HOLD_MAX - 1 > last_i))
                rows.append(base)
        # —— 对照：同年同距离（描述）与状态匹配三角核（描述）——
        for row in rows:
            if row.get("status") != "entered":
                continue
            ei, k, dirn = row.pop("_ei"), row.pop("_k"), row["direction"]
            yr = dates[ei].year
            lo = bisect.bisect_left(dates, date(yr, 1, 1)); hi = bisect.bisect_right(dates, date(yr, 12, 31))
            yus = [u for u in range(max(lo, ps.WARMUP + 1), hi)]
            yo = [(u, control_outcome(bars, atr, u, k, dirn, last_i)) for u in yus]
            yo = [(u, x) for u, x in yo if x]
            yrs = [x for _, x in yo]
            row["baseline_year"] = {"n": len(yrs), "r": _mean([x[0] for x in yrs]), "h4_d5": _mean([x[1] for x in yrs]),
                                    "h4_d10": _mean([x[2] for x in yrs])}
            tv = sv[ei]
            mus = []
            if tv is None:
                row["baseline_matched"] = {"status": "unmatched_no_state_vars"}
            else:
                num = [0.0, 0.0, 0.0]; den = [0.0, 0.0, 0.0]; n = 0
                for u in range(max(ps.WARMUP + 1, ei - KERNEL_SPAN), min(last_i, ei + KERNEL_SPAN) + 1):
                    if u in a_entries[dirn] or sv[u] != tv:
                        continue
                    o = control_outcome(bars, atr, u, k, dirn, last_i)
                    if not o:
                        continue
                    wgt = 1 - abs(u - ei) / (KERNEL_SPAN + 1)
                    n += 1
                    mus.append((u, wgt))
                    for j in range(3):
                        if o[j] is not None:
                            num[j] += wgt * o[j]; den[j] += wgt
                row["baseline_matched"] = ({"status": "unmatched_no_candidates"} if n == 0 else
                                           {"status": "matched", "n": n, "tv_bins": list(tv),
                                            "r": num[0] / den[0], "h4_d5": num[1] / den[1] if den[1] else None,
                                            "h4_d10": num[2] / den[2] if den[2] else None})
            if barrier:
                bo = {}
                for kb in BARRIER_KS:
                    ev_any, ev_end = barrier_obs(bars, atr, ei, kb, dirn)
                    ys = [barrier_obs(bars, atr, u, kb, dirn) for u, _ in yo]
                    ys = [x for x in ys if x[0] is not None]
                    ms = [(barrier_obs(bars, atr, u, kb, dirn), w) for u, w in mus]
                    ms = [(x, w) for x, w in ms if x[0] is not None]
                    wsum = sum(w for _, w in ms)
                    bo[f"{kb:g}"] = {
                        "event_cross_any": ev_any, "event_cross_end": ev_end,
                        "year": {"n": len(ys), "cross_any": sum(1 for x in ys if x[0]), "cross_end": sum(1 for x in ys if x[1])},
                        "matched": None if not ms else {"n": len(ms), "cross_any_unweighted": sum(1 for x, _ in ms if x[0]),
                                                        "cross_any_rate_w": sum(w for x, w in ms if x[0]) / wsum,
                                                        "cross_end_rate_w": sum(w for x, w in ms if x[1]) / wsum}}
                row["barrier"] = bo
        episodes += rows
    return {"symbol": sym, "status": "ok", "input_sha256": sha, "n_bars": len(bars_all),
            "range": [str(bars_all[0][0]), str(bars_all[-1][0])], "missing_sessions": [str(x) for x in missing],
            "invalid_rows": bad, "split_check": sc, "dividends_in_range": actions["dividends_n"], "episodes": episodes}


def summarize(eps: list) -> dict:
    out = {}
    for dirn in ("long", "short"):
        A = [e for e in eps if e["kind"] == "A" and e["direction"] == dirn]
        T = sorted([e for e in A if e["status"] == "entered"], key=lambda e: e["entry_at"])
        R = [e["outcome"]["r_mult"] for e in T]
        wins, losses = [r for r in R if r > 0], [r for r in R if r < 0]
        cum, peak, mdd, run, worst_run = 0.0, 0.0, 0.0, 0, 0
        for r in R:
            cum += r; peak = max(peak, cum); mdd = min(mdd, cum - peak)
            run = run + 1 if r < 0 else 0; worst_run = max(worst_run, run)
        cnt = {}
        for e in A:
            cnt[e["status"]] = cnt.get(e["status"], 0) + 1
        by = [e for e in T if (e.get("baseline_matched") or {}).get("status") == "matched"]
        H2 = [e for e in eps if e["kind"] == "H2" and e["direction"] == dirn]
        # H2 共同分母（Codex 038 §5）：规则拒绝入场 → 0R；缺数据 / 窗口不完整 → unknown（不折零）；两政策都已知的 episode 才进共同集合
        pI = [_policy_value(e["policy_i"].get("result"), e["policy_i"].get("r_mult")) for e in H2]
        pW = [_policy_value(e["policy_w"].get("result") if e["policy_w"]["status"] == "signal" else e["policy_w"]["status"],
                            e["policy_w"].get("r_mult")) for e in H2]
        common = [(a, b) for a, b in zip(pI, pW) if a is not None and b is not None]
        rI_entered = [e["policy_i"]["r_mult"] for e in H2 if e["policy_i"].get("result") == "entered"]
        rW_entered = [e["policy_w"]["r_mult"] for e in H2 if e["policy_w"].get("result") == "entered"]
        out[dirn] = {
            "A_episodes": len(A), "A_status_counts": cnt, "entered": len(T),
            "confirm_lag_days": _desc([e["hl1"]["confirm_lag"] for e in A if e.get("hl1") and e["status"] in ("entered", "occupied", "open_beyond_stop")]),
            "signal_to_stop_atr": _desc([e["signal_to_stop_atr"] for e in A if "signal_to_stop_atr" in e]),
            "r_mean": _r(_mean(R)), "r_median": _r(st.median(R)) if R else None,
            "win_rate_r_gt_0": _r(len(wins) / len(R)) if R else None, "n_zero": sum(1 for r in R if r == 0),
            "avg_win_r": _r(_mean(wins)), "avg_loss_r": _r(_mean(losses)), "worst_r": _r(min(R)) if R else None,
            "max_consecutive_losses": worst_run, "cum_r": _r(cum), "max_drawdown_r": _r(mdd),
            "days_held": _desc([e["outcome"]["days_held"] for e in T]),
            "exit_reasons": _count([e["outcome"]["exit_reason"] for e in T]),
            "baseline_year_r_mean": _r(_mean([e["baseline_year"]["r"] for e in T])),
            "excess_vs_year_r": _r(_mean([e["outcome"]["r_mult"] - e["baseline_year"]["r"] for e in T if e["baseline_year"]["r"] is not None])),
            "matched_n": len(by), "unmatched": len(T) - len(by),
            "excess_vs_matched_r": _r(_mean([e["outcome"]["r_mult"] - e["baseline_matched"]["r"] for e in by])),
            "h4_d5": _r(_mean([e["h4"]["d5"] for e in T])), "h4_d10": _r(_mean([e["h4"]["d10"] for e in T])),
            "h4_d10_year": _r(_mean([e["baseline_year"]["h4_d10"] for e in T])),
            "h4_d10_matched": _r(_mean([e["baseline_matched"]["h4_d10"] for e in by])),
            "h4_d10_event_on_matched": _r(_mean([e["h4"]["d10"] for e in by])),
            "H2": {"episodes": len(H2), "common_known": len(common), "unknown": len(H2) - len(common),
                   "common_mean_r_I": _r(_mean([a for a, _ in common])), "common_mean_r_W": _r(_mean([b for _, b in common])),
                   "entry_rate_I": _r(len(rI_entered) / len(H2)) if H2 else None,
                   "entry_rate_W": _r(len(rW_entered) / len(H2)) if H2 else None,
                   "conditional_mean_r_I": _r(_mean(rI_entered)), "conditional_mean_r_W": _r(_mean(rW_entered)),
                   "policy_w_status": _count([e["policy_w"]["status"] for e in H2]),
                   "policy_w_wait_days": _desc([e["policy_w"].get("wait_days") for e in H2 if e["policy_w"].get("wait_days") is not None])},
            "barrier": _barrier_summary(T),
        }
    return out


RULE_REJECT = ("open_beyond_stop", "timeout_no_entry", "invalidated_close_beyond_extreme", "invalidated_close_beyond_hl1",
               "invalidated_pullback_beyond_hl1", "invalidated_new_extreme")
UNKNOWN = ("boundary_excluded", "missing_session_in_hold", "no_entry_bar", "no_open", "open_at_end", None)


def _policy_value(result, r):
    """政策在某 episode 上的等风险结果：入场 → R；规则拒绝 → 0；缺数据 / 窗口不完整 → None（unknown，不折零）。"""
    if result == "entered":
        return r
    if result in RULE_REJECT:
        return 0.0
    return None


def _barrier_summary(T: list) -> dict | None:
    """每个 k：主比较 = 有匹配控制的同一集合（事件越界数 / n vs 匹配控制加权越界率）；同年对照另报；无匹配单列。"""
    if not T or "barrier" not in T[0]:
        return None
    out = {}
    for kb in (f"{x:g}" for x in BARRIER_KS):
        rows = [e["barrier"][kb] for e in T if e["barrier"][kb]["event_cross_any"] is not None]
        mrows = [b for b in rows if b["matched"]]
        ev_m = sum(1 for b in mrows if b["event_cross_any"])
        ctl_m = _mean([b["matched"]["cross_any_rate_w"] for b in mrows])
        ev_all = sum(1 for b in rows if b["event_cross_any"])
        yr = _mean([b["year"]["cross_any"] / b["year"]["n"] for b in rows if b["year"]["n"]])
        out[kb] = {"role": "主描述" if kb == "2" else "敏感性",
                   "matched_set_n": len(mrows), "event_cross_any_matched_set": ev_m,
                   "event_survival_matched_set": _r(1 - ev_m / len(mrows)) if mrows else None,
                   "matched_control_survival_w": _r(1 - ctl_m) if ctl_m is not None else None,
                   "survival_diff_vs_matched": _r((1 - ev_m / len(mrows)) - (1 - ctl_m)) if mrows and ctl_m is not None else None,
                   "unmatched": len(rows) - len(mrows),
                   "all_events_n": len(rows), "event_cross_any_all": ev_all,
                   "year_control_survival": _r(1 - yr) if yr is not None else None,
                   "survival_diff_vs_year": _r((1 - ev_all / len(rows)) - (1 - yr)) if rows and yr is not None else None,
                   "event_cross_end_all": sum(1 for b in rows if b["event_cross_end"])}
    return out


def _r(x):
    return None if x is None else round(x, 4)


def _desc(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]
    return {"n": len(xs), "p10": _r(q(0.1)), "median": _r(q(0.5)), "p90": _r(q(0.9)), "mean": _r(sum(xs) / len(xs))}


def _count(xs):
    c = {}
    for x in xs:
        c[x] = c.get(x, 0) + 1
    return c


def main() -> int:
    import argparse
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--barrier", action="store_true")
    a = ap.parse_args()
    OUT = BASE / a.run_id
    if OUT.exists():
        raise SystemExit(f"{OUT} 已存在：不覆盖已看过的结果，请用新的 --run-id")
    t0 = time.time()
    OUT.mkdir(parents=True)
    log = []
    ref, _ = fetch("SPY")
    ref_bars, _, _ = parse(ref)
    ref_dates = [b[0] for b in ref_bars]
    results = {}
    for sym in SYMS:
        results[sym] = run_symbol(sym, ref_dates, log, barrier=a.barrier)
        print(log[-1] if log else sym, flush=True)
    files = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in (
        "undertow/analyze/price_structure.py", "scripts/ps_explore.py",
        "docs/prereg/2026-10-13_price_structure_v1_DRAFT_v0.2.md", "docs/prereg/2026-10-13_price_structure_v1_DRAFT_v0.3.md")}
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "undertow", "scripts"], capture_output=True, text=True, cwd=ROOT).stdout
    config = {"run": RUN, "run_id": a.run_id, "barrier": a.barrier, "command": " ".join(sys.argv),
              "generated_at": datetime.now(timezone.utc).isoformat(), "git_head": head, "worktree_dirty": dirty.splitlines(),
              "file_sha256": files, "params": {"m_atr": ps.M_ATR, "atr_n": ps.ATR_N, "stop_buffer_atr": ps.STOP_BUF,
              "target_r": ps.TARGET_R, "hold_max": ps.HOLD_MAX, "wait_max": ps.WAIT_MAX, "kernel_span": KERNEL_SPAN,
              "v_lookback": V_LOOKBACK, "T_bins": "<-1 | [-1,1] | >1", "V_bins": "terciles",
              "risk_scale_atr": "ATR[入场前一根]（run3 起；run2 误用 ATR[信号前一根]）",
              "barrier_ks": list(BARRIER_KS) if a.barrier else None, "barrier_days": BARRIER_DAYS if a.barrier else None},
              "partition": {"design_end": str(DESIGN_END), "request_period2": PERIOD2, "test_and_2026": "未取数、未计算"},
              "source": "Yahoo chart v8 interval=1d events=div,splits；OHLC 已按拆股调整、未按分红调整（价格回报研究）",
              "inputs": {s_: {"sha256": r["input_sha256"], "range": r.get("range"), "n_bars": r.get("n_bars"),
                              "missing_sessions": len(r.get("missing_sessions", [])), "invalid_rows": r.get("invalid_rows"),
                              "split_check": r.get("split_check"), "dividends_in_range": r.get("dividends_in_range")} for s_, r in results.items()}}
    (OUT / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), "utf-8")
    all_eps = [e for r in results.values() for e in r.get("episodes", [])]
    with open(OUT / "episodes.jsonl", "w", encoding="utf-8") as f:
        for e in all_eps:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    cols = ["episode_id", "symbol", "direction", "signal_at", "entry_at", "entry", "stop", "risk", "k_atr", "target",
            "exit_at", "exit_reason", "exit_price", "r_mult", "days_held", "h4_d5", "h4_d10", "year_r", "matched_r"]
    with open(OUT / "trades.csv", "w", encoding="utf-8") as f:
        f.write(",".join(cols) + "\n")
        for e in all_eps:
            if e["kind"] != "A" or e.get("status") != "entered":
                continue
            o = e["outcome"]
            vals = [e["episode_id"], e["symbol"], e["direction"], e["signal_at"], e["entry_at"], e["entry"], e["stop"], e["risk"],
                    e["k_atr"], e["target"], o["exit_at"], o["exit_reason"], o["exit_price"], o["r_mult"], o["days_held"],
                    e["h4"]["d5"], e["h4"]["d10"], e["baseline_year"]["r"], (e.get("baseline_matched") or {}).get("r")]
            f.write(",".join("" if v is None else (f"{v:.6g}" if isinstance(v, float) else str(v)) for v in vals) + "\n")
    by = {s_: summarize(r.get("episodes", [])) if r["status"] == "ok" else {"status": r["status"]} for s_, r in results.items()}
    summary = {"run": RUN, "run_id": a.run_id, "n_episode_rows": len(all_eps),
               "note": "探索结果（设计集 ≤2015），无 p 值、未做区间推断、非已验证；R 序列是风险单位，不是账户收益；无期权价格，不报卖方价差胜率",
               "by_symbol": by, "pooled_descriptive": pooled(all_eps), "elapsed_s": round(time.time() - t0, 1)}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    (OUT / "report.md").write_text(report_md(summary, config), "utf-8")
    print(f"完成，用时 {summary['elapsed_s']} 秒 → {OUT.relative_to(ROOT)}")
    return 0


def pooled(eps: list) -> dict:
    out = {}
    for d in ("long", "short"):
        T = [e for e in eps if e["kind"] == "A" and e["direction"] == d and e["status"] == "entered"]
        R = [e["outcome"]["r_mult"] for e in T]
        m = [e["outcome"]["r_mult"] - e["baseline_matched"]["r"] for e in T if (e.get("baseline_matched") or {}).get("status") == "matched"]
        out[d] = {"n": len(T), "r_mean": _r(_mean(R)), "r_median": _r(st.median(R)) if R else None,
                  "win_rate": _r(sum(r > 0 for r in R) / len(R)) if R else None,
                  "excess_vs_year": _r(_mean([e["outcome"]["r_mult"] - e["baseline_year"]["r"] for e in T if e["baseline_year"]["r"] is not None])),
                  "excess_vs_matched": _r(_mean(m)), "matched_n": len(m),
                  "k_atr_median": _r(st.median([e["k_atr"] for e in T])) if T else None,
                  "time_exits": sum(1 for e in T if e["outcome"]["exit_reason"] == "time_exit"),
                  "h4_d10_survival": _r(_mean([e["h4"]["d10"] for e in T])),
                  "barrier": _barrier_summary(T)}
    return out


def report_md(summary: dict, config: dict) -> str:
    """人读报告全部从机器汇总生成（Codex 038：不手写数字）。"""
    L = [f"# ps-explore-v0.3 {summary['run_id']}：道氏结构设计集（≤2015）探索结果\n",
         f"**性质：** {summary['note']}。价格回报（OHLC 已按拆股调整、未计分红）；做空未计融券。全部参数未校准。2016 起未取数。\n",
         f"代码 HEAD `{config['git_head'][:8]}`，工作区改动 {len(config['worktree_dirty'])} 项；源码 sha256 见 config.json；"
         f"episodes.jsonl {summary['n_episode_rows']} 行（含失败与未入场）。命令：`{config['command']}`\n",
         "## 各品种 A 型主事件（风险距离与对照尺度：入场前一根 ATR）\n",
         "| 品种 | 方向 | 事件 | 入场 | 平均R | 中位R | R>0 | 最差R | 累计R | 最大回撤R | 时间出场 | 相对同年 | 相对匹配(n) | 10日收盘生存 事件/同年/匹配 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    surv = []
    for sym, v in summary["by_symbol"].items():
        for d, x in v.items():
            L.append(f"| {sym} | {'多' if d == 'long' else '空'} | {x['A_episodes']} | {x['entered']} | {x['r_mean']} | {x['r_median']} | "
                     f"{x['win_rate_r_gt_0']} | {x['worst_r']} | {x['cum_r']} | {x['max_drawdown_r']} | "
                     f"{x['exit_reasons'].get('time_exit', 0)}/{x['entered']} | {x['excess_vs_year_r']} | "
                     f"{x['excess_vs_matched_r']}({x['matched_n']}) | {x['h4_d10']}/{x['h4_d10_year']}/{x['h4_d10_matched']} |")
            if x["h4_d10"] is not None:
                surv.append((x["h4_d10"], sym, d))
    lo = min(surv) if surv else None
    L.append("\n## 五品种合并（只作描述；同日相关，不是独立样本；未做区间推断）\n")
    for d, x in summary["pooled_descriptive"].items():
        L.append(f"- {'做多' if d == 'long' else '做空'} {x['n']} 笔：平均 {x['r_mean']:+.2f}R、中位 {x['r_median']:+.2f}R、R>0 占 {x['win_rate']:.0%}；"
                 f"相对同年 {x['excess_vs_year']:+.2f}R，相对匹配 {x['excess_vs_matched']:+.2f}R（{x['matched_n']} 笔有匹配）；"
                 f"入场到止损中位 {x['k_atr_median']} ATR；60 日到期出场 {x['time_exits']}/{x['n']}；10 日收盘生存 {x['h4_d10_survival']:.0%}。")
    if lo:
        L.append(f"- 10 日收盘生存率许多格接近 1，但有低于 90% 的格（最低 {lo[1]} {'多' if lo[2] == 'long' else '空'} {lo[0]:.0%}）；样本太少，不能稳定估计差异。")
    L.append("\n## H2（共同起点 HL₁ᴮ；规则拒绝入场计 0R，缺数据计 unknown 不折零）\n")
    L.append("| 品种 | 方向 | episode | 共同已知 | 立即 均值 | 等待 均值 | 立即入场率 | 等待入场率 | 立即 条件均值 | 等待 条件均值 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for sym, v in summary["by_symbol"].items():
        for d, x in v.items():
            h = x["H2"]
            L.append(f"| {sym} | {'多' if d == 'long' else '空'} | {h['episodes']} | {h['common_known']} | {h['common_mean_r_I']} | {h['common_mean_r_W']} | "
                     f"{h['entry_rate_I']} | {h['entry_rate_W']} | {h['conditional_mean_r_I']} | {h['conditional_mean_r_W']} |")
    if any(v.get(d, {}).get("barrier") for v in summary["by_symbol"].values() for d in ("long", "short")):
        L.append("\n## 固定近距离屏障（入场开盘向不利方向 k×ATR[入场前一根]；10 日内任一收盘越过 = 越界）\n")
        L.append("只是价格屏障描述，不是缩短止损后的交易收益，也不是卖方价差胜率。主比较在「有匹配控制的同一集合」上；同年对照另报。\n")
        for kb in (f"{x:g}" for x in BARRIER_KS):
            L.append(f"\n### k = {kb} ATR（{'主描述' if kb == '2' else '敏感性'}）\n")
            L.append("| 品种 | 方向 | 匹配集 n | 事件越界 | 事件生存 | 匹配控制生存(加权) | 差 | 无匹配 | 全部事件 n | 全部事件越界 | 同年控制生存 | 差(同年) | 第10日收盘越界 |")
            L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            rows = [(sym, d, v[d]["barrier"][kb]) for sym, v in summary["by_symbol"].items() for d in ("long", "short") if v.get(d, {}).get("barrier")]
            rows += [("合并", d, summary["pooled_descriptive"][d]["barrier"][kb]) for d in ("long", "short") if summary["pooled_descriptive"][d]["barrier"]]
            for sym, d, b in rows:
                L.append(f"| {sym} | {'多' if d == 'long' else '空'} | {b['matched_set_n']} | {b['event_cross_any_matched_set']} | {b['event_survival_matched_set']} | "
                         f"{b['matched_control_survival_w']} | {b['survival_diff_vs_matched']} | {b['unmatched']} | {b['all_events_n']} | "
                         f"{b['event_cross_any_all']} | {b['year_control_survival']} | {b['survival_diff_vs_year']} | {b['event_cross_end_all']} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
