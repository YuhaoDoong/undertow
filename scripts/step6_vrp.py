"""第六步：按状态和期限描述 IV − 未来实现波动的点差（探索性研究）。

近墙检验尚未检出额外支撑；本步检验另一个候选解释。IV − RV 并不等于
可成交信用价差损益，也不能单凭它指定方向、期限或过滤规则。

  A. **按状态拆**：ATR 扩张 / ATR 高低分位 / IV 分位 —— 第四步说 ATR 扩张时破墙率约 2×，
     那时 VRP 还在不在？卖方的边是不是恰好在最该躲的日子消失？
  B. **短到期**：策略卖的是 2~7 DTE，指数是 30 天。用快照 ATM IV（3~10 DTE）vs 该到期内实现波动。
     样本只有 ~57 个可交易日，只作描述。

口径：VRP = IV − 其后实现波动（年化 pp，复用 vrp_history.forward_realized_vol）。
长历史 IV/状态取同一收盘，结果从次日起；不重叠子样本每 21 根取 1。
短期 ATM 取真实前收，收益窗包含决策日并精确止于已成熟到期日；按实际收益区间去重。
全样本只作描述，均值与 t 在同一不重叠子样本单独展示；不重叠不等于独立。
用法：python3 scripts/step6_vrp.py [--emit]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

import step5_wall_hold as s5                                            # noqa: E402
from undertow.analyze.stretch import _atr_series                        # noqa: E402
from undertow.analyze.stretch_backtest import _pct_rank_series          # noqa: E402
from undertow.analyze.vrp_history import forward_realized_vol           # noqa: E402
from undertow.analyze.vrp_research import (                             # noqa: E402
    compare_samples, describe, one_sample_t, realized_expiry_window, short_summary, welch,
)
from undertow.collect.cboe_history import CboeHistorySource             # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload         # noqa: E402
from undertow.collect.cboe_vol import CboeVolSource                     # noqa: E402
from undertow.collect.store import SnapshotStore                        # noqa: E402
from undertow.core.config import load_config                            # noqa: E402

WINDOW = 21


def long_history(inst, iv_src, px_src):
    iv = dict(iv_src.fetch_series(inst.vol_index))
    ser = px_src.fetch_series(inst)
    dates, c, h, l = list(ser.dates), list(ser.closes), list(ser.highs), list(ser.lows)
    fwd = forward_realized_vol(dates, c, WINDOW)
    atr = _atr_series(h, l, c, 14)
    atr_pct = _pct_rank_series(atr)
    iv_list = [iv.get(d) for d in dates]
    iv_pct = _pct_rank_series(iv_list)
    rows = []
    for i, d in enumerate(dates):
        if d not in iv or d not in fwd or i < 20 or not atr[i] or not atr[i - 5]:
            continue
        rows.append({"i": i, "d": d, "vrp": iv[d] - fwd[d], "iv": iv[d], "rv": fwd[d],
                     "atr_x": atr[i] / atr[i - 5], "atr_pct": atr_pct[i], "iv_pct": iv_pct[i]})
    states = {
        "ATR扩张≥1.3": lambda r: r["atr_x"] >= 1.3,
        "ATR扩张<1.3": lambda r: r["atr_x"] < 1.3,
        "ATR分位≥90%": lambda r: r["atr_pct"] is not None and r["atr_pct"] >= 0.9,
        "ATR分位≤10%": lambda r: r["atr_pct"] is not None and r["atr_pct"] <= 0.1,
        "IV分位≥70%": lambda r: r["iv_pct"] is not None and r["iv_pct"] >= 0.7,
        "IV分位≤30%": lambda r: r["iv_pct"] is not None and r["iv_pct"] <= 0.3,
    }
    if not rows:
        return None
    i0 = rows[0]["i"]
    sub = [r for r in rows if (r["i"] - i0) % WINDOW == 0]
    out = {"index": inst.vol_index, "span": f"{rows[0]['d']}→{rows[-1]['d']}", "n": len(rows),
           "all": describe([r["vrp"] for r in rows]),
           "nonoverlap": describe([r["vrp"] for r in sub]),
           "nonoverlap_dates": [r["d"].isoformat() for r in sub],
           "t_nonoverlap": one_sample_t([r["vrp"] for r in sub]), "n_nonoverlap": len(sub), "states": {}}
    for name, f in states.items():
        sel = [r for r in rows if f(r)]
        ssel = [r["vrp"] for r in sub if f(r)]
        srest = [r["vrp"] for r in sub if not f(r)]
        out["states"][name] = {**(describe([r["vrp"] for r in sel]) or {}),
                               "share": len(sel) / len(rows), "n_nov": len(ssel),
                               **compare_samples(ssel, srest)}
    return out


def short_dte(key, inst, store, px_src, *, audit=None):
    """快照近 ATM IV（3~10 DTE，最近到期）vs 完整到期内实现波动。

    保留 list 返回值兼容研究调用；可通过 audit 取得剔除原因和覆盖情况。
    """
    ser = px_src.fetch_series(inst)
    tdays, c = list(ser.dates), list(ser.closes)
    idx = {d: i for i, d in enumerate(tdays)}
    by_T, dropped = s5.load_days(store, key, inst.options.symbol, tdays)
    diagnostics = {"decision_days": len(by_T), "unmapped_snapshots": dropped,
                   "history_end": tdays[-1].isoformat() if tdays else None,
                   "excluded": {}}
    def exclude(reason):
        diagnostics["excluded"][reason] = diagnostics["excluded"].get(reason, 0) + 1
    rows = []
    for T in sorted(by_T):
        if T not in idx or idx[T] == 0:
            exclude("missing_base_close")
            continue
        decision_price = c[idx[T] - 1]
        if not math.isfinite(decision_price) or decision_price <= 0:
            exclude("invalid_base_close")
            continue
        snap = snapshot_from_payload(by_T[T][1], key, inst.options.symbol)
        cands = [ct for ct in snap.contracts if 3 <= (ct.expiry - T).days <= 10
                 and abs(ct.strike / decision_price - 1) < 0.02
                 and ct.iv is not None and math.isfinite(ct.iv) and ct.iv > 0]
        if not cands:
            exclude("no_atm_contract")
            continue
        exp = min(ct.expiry for ct in cands)
        ivs = [ct.iv for ct in cands if ct.expiry == exp]
        dte = (exp - T).days
        window, reason = realized_expiry_window(tdays, c, T, exp)
        if window is None:
            exclude(reason)
            continue
        ivp = st.mean(ivs) * 100
        rows.append({"T": T.isoformat(), "dte": dte, "expiry": exp.isoformat(),
                     "captured_at": by_T[T][0], "quote_asof": snap.asof,
                     "snapshot_spot": snap.spot,
                     "atm_strikes": sorted({ct.strike for ct in cands if ct.expiry == exp}),
                     "iv_contract_count": len(ivs), "iv": ivp, **window,
                     "vrp": ivp - window["rv"]})
    diagnostics["included"] = len(rows)
    if audit is not None:
        audit.update(diagnostics)
    return rows


def _fmt(value, spec="+.2f"):
    return format(value, spec) if value is not None else "不可估计"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", action="store_true")
    ap.add_argument("--output", type=Path, default=ROOT / "data" / "history" / "wall_spread" / "vrp_states_v2.json",
                    help="--emit 的新产物路径；默认保留原 schema 1 历史结果")
    args = ap.parse_args()
    cfg = load_config(); iv_src = CboeVolSource(); px_src = CboeHistorySource(); store = SnapshotStore()
    caveats = ["IV−RV 是波动率点差，不是信用价差净损益。",
               "不重叠只消除共享日收益，不保证跨期独立；t 不等于已通过显著性检验。",
               "短期 RV 延用日对数收益总体标准差；短窗去均值、有限样本偏差会影响期限比较。",
               "快照抓取时刻不证明期权 IV 报价新鲜；quote_asof 保留供核查，未验证的报价不能视作可成交价。",
               "已精确核对结果窗起止日；日线缺少交易所完整日历，未认证中间缺行。"]
    emit = {"schema": 2, "asof": date.today().isoformat(), "window": WINDOW,
            "long": {}, "short": {}, "short_summary": {}, "short_audit": {}, "caveats": caveats}
    print(f"A. 长历史 IV−其后 {WINDOW} 日 RV（年化 pp）；IV 和状态均取起点收盘。"
          "全样本描述与不重叠统计分列；p5 为点差分位数，不是损失概率。")
    for key, inst in cfg.instruments.items():
        if not inst.vol_index or not inst.price:
            continue
        r = long_history(inst, px_src=px_src, iv_src=iv_src)
        emit["long"][key] = r
        if r is None:
            print(f"  {key}: 无完整的历史配对样本")
            continue
        a = r["all"]
        sub = r["nonoverlap"]
        print(f"\n{key} ({r['index']}) {r['span']}"
              f"\n  全样本描述 n={a['n']}: 均值 {a['mean']:+.2f}pp 正比例 {a['pos']:.0%} p5 {a['p5']:+.1f}"
              f"\n  不重叠 n={r['n_nonoverlap']}: 均值 {_fmt(sub['mean'] if sub else None)}pp"
              f" t={_fmt(r['t_nonoverlap'])}")
        for name, s in r["states"].items():
            if not s.get("n"):
                continue
            sel, rest = s["nonoverlap"], s["rest_nonoverlap"]
            print(f"  {name}: 全样本 n={s['n']} 均值={s['mean']:+.2f} p5={s['p5']:+.1f}；"
                  f"不重叠 状态/其余 n={sel['n'] if sel else 0}/{rest['n'] if rest else 0}"
                  f" 均值={_fmt(sel['mean'] if sel else None)}/{_fmt(rest['mean'] if rest else None)}"
                  f" 差={_fmt(s['mean_diff_nonoverlap'])} Welch t={_fmt(s['welch_vs_rest'])}")

    print("\nB. 短到期近 ATM IV 3~10 DTE vs 完整到期内 RV；探索性，按实际区间去重")
    for key in ("silver", "gold", "wti", "qqq"):
        inst = cfg.get(key)
        audit = {}
        rows = short_dte(key, inst, store, px_src, audit=audit)
        emit["short"][key] = rows
        result = short_summary(rows)
        emit["short_summary"][key] = result
        emit["short_audit"][key] = audit
        print(f"  {key}: 覆盖 {audit['decision_days']} 日；纳入 {audit['included']}；"
              f"未映射快照 {audit['unmapped_snapshots']}；剔除 {audit['excluded']}")
        if not rows:
            print(f"  {key}: 无样本"); continue
        d, sub = result["all"], result["nonoverlap"]
        print(f"    全样本描述 n={d['n']} DTE 中位 {st.median(r['dte'] for r in rows):.0f}"
              f" 均值={d['mean']:+.2f}pp 正比例={d['pos']:.0%} p5={d['p5']:+.1f}"
              f"\n    不重叠 n={result['n_nonoverlap']} 均值={_fmt(sub['mean'] if sub else None)}pp"
              f" t={_fmt(result['t_nonoverlap'])}")
    for caveat in caveats:
        print(f"  限制：{caveat}")
    if args.emit:
        out = args.output
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(emit, ensure_ascii=False, indent=1, default=str, allow_nan=False), encoding="utf-8")
            saved = json.loads(tmp.read_text("utf-8"))
            if saved["schema"] != 2 or saved["short_summary"] != emit["short_summary"]:
                raise ValueError("VRP 产物回读不一致")
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        print(f"\n已落盘 {out}")


if __name__ == "__main__":
    main()
