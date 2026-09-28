"""结构读数（analyze/structure_read.py 防守分级）历史回放 + 外部作者一对照（探索，不是检验）。

  python3 scripts/structure_replay.py                 # GLD，全部认证交易日
  python3 scripts/structure_replay.py --inst silver

做三件事：
1. 对每个认证交易日 S（快照按 captured_at 认证，dirledger_cli.session_index），用认证到 S 与 S 前一交易日的两份快照
   跑 analyze_flow + analyze_structure，得防守分级（进攻/中性/中性偏防守/短线偏防守/恐慌防守）与分量
   （ΔATM、Δskew25、主翼 put、证伪清单）。认证到 S 的快照 = S−1 收盘结算，S 开盘前可得 → 与作者「分析 S−1、
   S 开盘前发布」是同一信息集。
2. 按 S 开盘价计 1/5/10 日收益（skew_reading.forward_returns，与方向台账同口径）。
   结果写 data/backtest/structure_replay/{inst}.jsonl（只含我们自己的读数，入库）。
3. 若私有文件 data/soul/author_calls.jsonl 存在：按发布时刻映射到 session（dirledger_cli.session_after），
   同 session 多帖取最后一帖；输出「复刻一致率」（我们的分级 vs 作者结构状态）与双方方向命中率，
   只写 data/soul/author1_compare.json（gitignore），不入库。

⚠️ 探索性：structure_read 的阈值在 2026-08 起参照该作者读法写成、此后按他的帖子多次对照修改过，
历史样本已被看过；这里的一致率与命中率只能描述，不能作为检验。确认性检验只能靠预登记后的前瞻样本。
只读、纯标准库；日线优先长桥（联网只读），失败回退存档。
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.analyze import skew_reading as skr  # noqa: E402
from undertow.analyze import structure_read as sr  # noqa: E402
from undertow.analyze.flow import _live, analyze_flow  # noqa: E402
from undertow.core import market_calendar as mc  # noqa: E402

OUT = ROOT / "data/backtest/structure_replay"
PRIVATE_CALLS = ROOT / "data/soul/author_calls.jsonl"
PRIVATE_OUT = ROOT / "data/soul/author1_compare.json"
DEFENSIVE = ("中性偏防守", "短线偏防守", "恐慌防守")
HORIZONS = (1, 5, 10)


def coarse(level: str | None) -> str | None:
    """五级防守轴 → 三类（与作者的结构状态同粒度）。"""
    if level is None:
        return None
    return "防守" if level in DEFENSIVE else level


def lean(state: str | None) -> int:
    """结构状态 → 计分方向：进攻 +1、防守 −1、中性 0（不计分）。"""
    return {"进攻": 1, "防守": -1}.get(state or "", 0)


def call_lean(call: str) -> int:
    return {"偏多": 1, "偏空": -1, "防守": -1}.get(call, 0)


def _bars(sym: str):
    try:
        from undertow.dirledger_cli import _bars as lb
        b = lb(sym)
        if b:
            return b, "longbridge"
    except Exception as e:
        print(f"[提示] 长桥日线不可用，回退存档：{type(e).__name__}: {e}", file=sys.stderr)
    from undertow.analyze import volume_profile as vp
    rec = json.loads(gzip.decompress((ROOT / f"data/history/inputs/monthly/2026-09/cboehist_{sym}.json.gz").read_bytes()))
    return [(date.fromisoformat(x["date"]), x["o"], x["c"]) for x in vp.clean(rec["raw"]["data"]["data"])], "cboe_archive"


def replay(inst: str, sym: str) -> list[dict]:
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    from undertow.dirledger_cli import session_index
    store = SnapshotStore()
    idx = session_index(store, sym)
    sessions = sorted(idx)
    snaps, vols = {}, {}

    def snap(s):
        if s not in snaps:
            p = store.load("options", sym, idx[s])
            snaps[s] = snapshot_from_payload(p, inst, sym) if p is not None else None
        return snaps[s]

    rows = []
    for s in sessions:
        prev = mc.prev_trading_day(s)
        row = {"session": s.isoformat(), "curr_file": idx[s].isoformat(),
               "prev_file": idx[prev].isoformat() if prev in idx else None}
        cur, prv = snap(s), (snap(prev) if prev in idx else None)
        if cur is None or prv is None:
            rows.append({**row, "defense": None, "reason": "缺认证到当日或前一交易日的快照（不以更早快照顶替）"})
            continue
        quote_day = prev                       # 认证到 s 的快照报价 ≈ s 前一交易日收盘
        vols[s] = sum(c.volume for c in _live(cur, quote_day, 60))
        recent = [vols[x] for x in sessions if x < s and x in vols][-10:]
        try:
            fa = analyze_flow(prv, cur, today=quote_day, prev_date=row["prev_file"], curr_date=row["curr_file"])
            r = sr.analyze_structure(fa, _live(prv, quote_day, 60), _live(cur, quote_day, 60),
                                     recent_volumes=recent or None)
        except Exception as e:
            rows.append({**row, "defense": None, "reason": f"{type(e).__name__}: {e}"})
            continue
        if not r.ok:
            rows.append({**row, "defense": None, "reason": r.reason})
            continue
        wing = [x.d_put_pp for x in r.ladder if sr.WING_LO <= x.delta <= sr.WING_HI and x.d_put_pp is not None]
        rows.append({**row, "defense": r.defense, "state": coarse(r.defense),
                     "d_atm_pp": round(r.d_atm_pp, 3), "d_skew25_pp": round(r.d_skew25_pp, 3),
                     "d_skew10_pp": round(r.d_skew10_pp, 3) if r.d_skew10_pp is not None else None,
                     "skew25_pp": round(r.skew25_pp, 3), "wing_put_pp": round(sum(wing) / len(wing), 3) if wing else None,
                     "trend_break": r.trend_break, "eff_delta_total": r.eff_delta_total,
                     "why": r.defense_why})
    return rows


def binom_two_sided(k: int, n: int, p: float) -> float | None:
    if n == 0:
        return None
    pmf = [math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(n + 1)]
    return min(1.0, sum(x for x in pmf if x <= pmf[k] * (1 + 1e-9)))


def score(rows: list[dict], lean_of, horizons=HORIZONS) -> dict:
    """方向命中：lean=+1 看涨 ret>0 算中、−1 看跌 ret<0 算中；lean=0 不计。附基准（同期全部 session 上涨率）。"""
    out = {}
    for h in horizons:
        k = f"ret_{h}d"
        allr = [r["outcome"][k] for r in rows if (r.get("outcome") or {}).get(k) is not None]
        up = sum(x > 0 for x in allr) / len(allr) if allr else None
        sig = [(lean_of(r), r["outcome"][k]) for r in rows
               if lean_of(r) != 0 and (r.get("outcome") or {}).get(k) is not None]
        hits = sum((l > 0) == (x > 0) for l, x in sig)
        # 基准命中率：按信号方向构成、以同期上涨率为「瞎猜」概率
        p0 = (sum(up if l > 0 else 1 - up for l, _ in sig) / len(sig)) if (sig and up is not None) else None
        out[f"{h}d"] = {"n": len(sig), "hits": hits, "rate": round(hits / len(sig), 3) if sig else None,
                        "base_rate": round(p0, 3) if p0 is not None else None,
                        "p_binom_vs_base": round(binom_two_sided(hits, len(sig), p0), 4) if p0 else None,
                        "n_up": sum(1 for l, _ in sig if l > 0), "n_down": sum(1 for l, _ in sig if l < 0),
                        "mean_signed_ret_pct": round(sum(l * x for l, x in sig) / len(sig) * 100, 3) if sig else None,
                        "period_up_rate": round(up, 3) if up is not None else None, "period_n": len(allr)}
    return out


def compare_author(inst: str, rows: list[dict], bars) -> dict | None:
    if not PRIVATE_CALLS.exists():
        return None
    from undertow.dirledger_cli import session_after
    calls = [json.loads(x) for x in PRIVATE_CALLS.read_text("utf-8").splitlines() if x.strip()]
    calls = [c for c in calls if c.get("instrument") == inst]
    by_sess: dict = {}
    for c in sorted(calls, key=lambda c: c["posted_at"]):
        s = session_after(datetime.fromisoformat(c["posted_at"]))
        if s is None:
            continue
        c = {**c, "session": s.isoformat(), "outcome": skr.forward_returns(bars, s)}
        by_sess.setdefault((c.get("type", "期权"), s.isoformat()), []).append(c)
    last = {k: v[-1] for k, v in by_sess.items()}              # 同 session 多帖取最后一帖（开盘前最新看法）
    opt = [c for (t, _), c in sorted(last.items()) if t == "期权"]
    ours = {r["session"]: r for r in rows}

    # ① 复刻一致率：同一 session、双方都有读数
    pairs = []
    for c in opt:
        o = ours.get(c["session"])
        if o and o.get("state"):
            pairs.append({"session": c["session"], "author_state": c["state"], "author_call": c["call"],
                          "ours_state": o["state"], "ours_level": o["defense"],
                          "d_skew25_pp": o["d_skew25_pp"], "d_atm_pp": o["d_atm_pp"], "wing_put_pp": o["wing_put_pp"],
                          "match": o["state"] == c["state"]})
    labs = ("进攻", "中性", "防守")
    confusion = {a: {b: sum(1 for p in pairs if p["author_state"] == a and p["ours_state"] == b) for b in labs} for a in labs}
    n = len(pairs)
    agree = sum(p["match"] for p in pairs)
    # 随机基线（两边边际分布独立时的期望一致率）与 Cohen κ
    pe = sum((sum(confusion[a].values()) / n) * (sum(confusion[x][a] for x in labs) / n) for a in labs) if n else None
    kappa = ((agree / n - pe) / (1 - pe)) if (n and pe is not None and pe < 1) else None
    # 方向不冲突率：作者进攻/防守时，我们是否给出相反一侧
    decisive = [p for p in pairs if p["author_state"] != "中性"]
    opposite = sum(1 for p in decisive if lean(p["ours_state"]) == -lean(p["author_state"]))

    # ② 双方方向命中（作者：他的方向 call；我们：防守分级 → 方向），按 session 计
    author_opt = score(opt, lambda c: call_lean(c["call"]))
    author_all = score(list(last.values()), lambda c: call_lean(c["call"]))
    author_state = score(opt, lambda c: lean(c["state"]))
    ours_same = score([ours[c["session"]] for c in opt if c["session"] in ours and ours[c["session"]].get("state")],
                      lambda r: lean(r["state"]))
    return {"n_calls": len(calls), "n_sessions_option": len(opt), "n_pairs": n,
            "agreement": round(agree / n, 3) if n else None, "chance_agreement": round(pe, 3) if pe else None,
            "kappa": round(kappa, 3) if kappa is not None else None, "confusion_author_rows_ours_cols": confusion,
            "decisive_n": len(decisive), "decisive_opposite": opposite,
            "author_call_hits_option_posts": author_opt, "author_call_hits_all_posts": author_all,
            "author_state_hits": author_state, "ours_state_hits_on_author_sessions": ours_same,
            "pairs": pairs, "author_sessions": [{k: c[k] for k in ("session", "posted_at", "type", "state", "call", "outcome")} for c in last.values()]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="gold", choices=("gold", "silver"))
    a = ap.parse_args()
    sym = {"gold": "GLD", "silver": "SLV"}[a.inst]
    bars, src = _bars(sym)
    rows = replay(a.inst, sym)
    for r in rows:
        r["outcome"] = skr.forward_returns(bars, date.fromisoformat(r["session"]))
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{a.inst}.jsonl"
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    tmp.replace(p)
    ok = [r for r in rows if r.get("state")]
    dist = {k: sum(1 for r in ok if r["defense"] == k) for k in sr.DEFENSE_AXIS}
    print(f"{a.inst}/{sym}：认证交易日 {len(rows)}，可读 {len(ok)}（{rows[0]['session']} ~ {rows[-1]['session']}），日线 {src}")
    print(f"  分级分布：{dist}")
    for h, v in score(ok, lambda r: lean(r["state"])).items():
        print(f"  我们（全部 session）{h}：n={v['n']}（涨 {v['n_up']}/跌 {v['n_down']}）命中 {v['hits']}/{v['n']}={v['rate']}"
              f"，基准 {v['base_rate']}，p={v['p_binom_vs_base']}，带符号均收益 {v['mean_signed_ret_pct']}%")
    cmp = compare_author(a.inst, rows, bars)
    if cmp is not None:
        PRIVATE_OUT.write_text(json.dumps(cmp, ensure_ascii=False, indent=1), "utf-8")
        print(f"  外部作者一对照（私有 → {PRIVATE_OUT.relative_to(ROOT)}）：同 session 配对 {cmp['n_pairs']}，"
              f"结构状态一致 {cmp['agreement']}（随机期望 {cmp['chance_agreement']}，κ={cmp['kappa']}）；"
              f"他判进攻/防守的 {cmp['decisive_n']} 天里我们判反向 {cmp['decisive_opposite']} 天")
        for name in ("author_call_hits_option_posts", "author_call_hits_all_posts", "author_state_hits",
                     "ours_state_hits_on_author_sessions"):
            print(f"   {name}: " + "；".join(f"{h} {v['hits']}/{v['n']}={v['rate']}（基准 {v['base_rate']}，p={v['p_binom_vs_base']}）"
                                          for h, v in cmp[name].items()))
    print(f"写入 {p.relative_to(ROOT)}。⚠️ 探索性（规则参照作者读法写成、历史已看过），不作检验。")


if __name__ == "__main__":
    main()
