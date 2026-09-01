"""墙位卖方价差回测 —— 可复现版本（codex 2026-08-31 P1-6/8/10/14）

此前的数字只存在于一次临时会话里，仓库中只有硬编码汇总值。codex 指出：
「在用户会据此实盘的项目里，这些结果不能仅存在于一次临时分析会话中。」

本脚本修掉四处方法论问题：

P1-14 可复现 + 前瞻
  · 逐笔账本落盘 data/backtest/credit_wall_trades.jsonl（信号日、报价时间戳、
    腿位、结算价来源、是否被排除）
  · px_on() 原实现取「到期日或之后最近一天」的收盘 —— 到期后的价格变动会被
    带进结算，是明确的 look-ahead。改为只接受到期日当天收盘，缺失即丢弃并计数。

P1-10 策略定义漂移
  报告按 annual_roi 排序取 spreads[0]，但档位统计是全候选均值。
  本脚本用【每信号只选一笔】的事前规则（可配置 first/max_credit/max_roi），
  报告用哪条规则，回测就必须用同一条。

P1-6 独立性
  同一信号下多个到期共享方向/路径/事件，60 笔不是 60 个独立样本。
  现在一信号一笔，再按【日期簇】聚合（同日跨品种合成一个等权组合收益）。

P1-8 检验对象
  胜率的二项检验答错了问题 —— balanced 档 76% 胜率却是 -0.43%/笔。
  改为对日期簇收益做 permutation / bootstrap，检验净收益是否显著大于零。

用法：
  python3 scripts/backtest_credit_wall.py                    # 默认三档
  python3 scripts/backtest_credit_wall.py --tier aggressive  # 单档
  python3 scripts/backtest_credit_wall.py --rule max_credit  # 换选择规则
"""
# ⚠️ 2026-09-01：本脚本用 snapshot.spot 当开仓现价，而 46 个快照里 34 个的
#    spot 不是文件名当天的价（见 memory/snapshot-date-alignment-p0）。
#    结论仍保留（它记录的是"策略不通过验证"这个负面结果，方向不受影响），
#    但数值不可引用。新回测走 scripts/backtest_wall_spread.py（决策价=C[D−1]）。

import argparse
import json
import math
import os
import pathlib
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from undertow.analyze.credit_wall import RISK_TIERS, propose          # noqa: E402
from undertow.analyze.flow import analyze_flow, tradeable_info        # noqa: E402
from undertow.cli import snapshot_from_payload, _prev_weekday         # noqa: E402
from undertow.collect.longbridge_kline import fetch_bars              # noqa: E402
from undertow.collect.store import SnapshotStore                      # noqa: E402

INST = {"GLD": "gold", "SLV": "silver", "QQQ": "qqq", "TQQQ": "tqqq",
        "SPY": "spy", "IWM": "iwm", "TLT": "tlt"}
FEE_PER_LEG = 0.80
OUT_DIR = pathlib.Path("data/backtest")


def load_closes(sym):
    try:
        return {str(b["ts"])[:10]: b["close"]
                for b in fetch_bars(f"{sym}.US", period="day", count=300)}
    except Exception:
        return {}


def settle_price(closes, d):
    """只接受到期日【当天】收盘。缺失返回 None —— 绝不向后找价格。

    原实现向后找最近一天，把到期后的价格变动带进内在价值计算，是 look-ahead。
    """
    return closes.get(d.isoformat())


def pick(spreads, rule):
    """每信号只选一笔的【事前】规则。报告用哪条，回测就得用哪条。"""
    if not spreads:
        return None
    if rule == "first":            # 最近到期
        return min(spreads, key=lambda s: s.dte)
    if rule == "max_credit":       # 权利金最多
        return max(spreads, key=lambda s: s.credit)
    return max(spreads, key=lambda s: s.annual_roi)   # max_roi（报告当前用的）


def run(tier, rule, min_ratio=None):
    st = SnapshotStore()
    px = {s: load_closes(s) for s in INST}
    trades, skipped = [], defaultdict(int)
    for sym, inst in INST.items():
        dirp = f"data/snapshots/options/{sym}"
        if not os.path.isdir(dirp):
            continue
        ds = []
        for f in sorted(os.listdir(dirp)):
            if f.endswith(".json.gz"):
                try:
                    ds.append(datetime.strptime(f[:10], "%Y-%m-%d").date())
                except ValueError:
                    pass
        for i in range(1, len(ds)):
            d, p = ds[i], ds[i - 1]
            try:
                cur = snapshot_from_payload(st.load("options", sym, d), inst, sym)
                prv = snapshot_from_payload(st.load("options", sym, p), inst, sym)
                fa = analyze_flow(prv, cur, today=_prev_weekday(d),
                                  prev_date=p.isoformat(), curr_date=d.isoformat(),
                                  horizon_days=45)
            except Exception:
                skipped["快照或flow失败"] += 1
                continue
            ti = tradeable_info(fa)
            side = ti.get("side")
            if side not in ("看涨", "看跌"):
                skipped["方向不明"] += 1
                continue
            ratio = ti["ratio"]
            if min_ratio is not None and ratio < min_ratio:
                skipped["未过压力比闸门"] += 1
                continue
            v = propose(cur, _prev_weekday(d), cur.spot, side, ratio,
                        tier=tier, execution_date=d)
            if not v.ok:
                skipped["无候选"] += 1
                continue
            s0 = pick(v.spreads, rule)
            se = settle_price(px.get(sym, {}), s0.expiry)
            if se is None:
                skipped["到期日无收盘价(丢弃,不向后取)"] += 1
                continue
            w = s0.width / 100.0
            intr = (max(0.0, min(se - s0.sell_strike, w)) if s0.kind == "C"
                    else max(0.0, min(s0.sell_strike - se, w))) * 100
            pnl = s0.credit - intr - FEE_PER_LEG * 4
            trades.append({
                "sym": sym, "signal_date": d.isoformat(),
                "oi_session": _prev_weekday(d).isoformat(),
                "quote_asof": getattr(cur, "asof", None),
                "side": side, "ratio": round(ratio, 2),
                "kind": s0.kind, "sell": s0.sell_strike, "buy": s0.buy_strike,
                "expiry": s0.expiry.isoformat(), "dte": s0.dte,
                "credit": round(s0.credit, 2), "occupancy": round(s0.occupancy, 2),
                "spot_entry": round(cur.spot, 4), "spot_settle": round(se, 4),
                "settle_source": "到期日当天收盘",
                "pnl": round(pnl, 2), "roi": round(pnl / s0.occupancy, 4),
                "broke": bool(se > s0.sell_strike if s0.kind == "C"
                              else se < s0.sell_strike),
            })
    return trades, skipped


def cluster_returns(trades):
    """同日跨品种合成一个等权组合收益 —— 金银 0.89、QQQ/TQQQ 0.99，
    品种-日不是独立样本，日期簇才是。"""
    by = defaultdict(list)
    for t in trades:
        by[t["signal_date"]].append(t["roi"])
    return {d: sum(v) / len(v) for d, v in sorted(by.items())}


def bootstrap_p(vals, n_iter=20000, seed=42):
    """置换检验：净收益是否显著 > 0（P1-8 —— 不是检验胜率>50%）。

    对每个簇收益随机翻转符号，看 |均值| ≥ 实测的比例（单侧）。
    """
    if len(vals) < 3:
        return 1.0, (0.0, 0.0)
    rng = random.Random(seed)
    obs = statistics.mean(vals)
    cnt = sum(1 for _ in range(n_iter)
              if statistics.mean(v * rng.choice((1, -1)) for v in vals) >= obs)
    # bootstrap 置信区间
    means = sorted(statistics.mean(rng.choices(vals, k=len(vals)))
                   for _ in range(n_iter // 4))
    lo = means[int(len(means) * 0.025)]
    hi = means[int(len(means) * 0.975)]
    return (cnt + 1) / (n_iter + 1), (lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default=None, choices=list(RISK_TIERS))
    ap.add_argument("--rule", default="max_roi",
                    choices=["max_roi", "max_credit", "first"])
    ap.add_argument("--min-ratio", type=float, default=None)
    a = ap.parse_args()
    tiers = [a.tier] if a.tier else list(RISK_TIERS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"选择规则：每信号只取一笔（{a.rule}）　"
          f"结算：只用到期日当天收盘，缺失即丢弃\n")
    hdr = (f"{'档位':<10}{'笔数':>5}{'日期簇':>7}{'胜率':>7}{'簇均ROI':>9}"
           f"{'置换p':>8}{'95%CI':>18}{'最差':>8}{'总PnL':>9}")
    print(hdr)
    print("-" * len(hdr.encode("utf-8")) // 2 * "-" if False else "-" * 82)
    for tier in tiers:
        tr, sk = run(tier, a.rule, a.min_ratio)
        if not tr:
            print(f"{tier:<10}  无样本　跳过原因：{dict(sk)}")
            continue
        cl = cluster_returns(tr)
        vals = list(cl.values())
        p, (lo, hi) = bootstrap_p(vals)
        wr = sum(1 for t in tr if t["pnl"] > 0) / len(tr)
        print(f"{tier:<10}{len(tr):>5}{len(cl):>7}{wr:>6.0%}"
              f"{statistics.mean(vals) * 100:>+8.2f}%{p:>8.3f}"
              f"  [{lo * 100:+.2f}%, {hi * 100:+.2f}%]"
              f"{min(t['roi'] for t in tr) * 100:>+7.0f}%"
              f"{sum(t['pnl'] for t in tr):>+9.0f}")
        out = OUT_DIR / f"credit_wall_{tier}_{a.rule}.jsonl"
        with open(out, "w") as f:
            for t in tr:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        meta = OUT_DIR / f"credit_wall_{tier}_{a.rule}_meta.json"
        meta.write_text(json.dumps({
            "tier": tier, "rule": a.rule, "min_ratio": a.min_ratio,
            "n_trades": len(tr), "n_clusters": len(cl),
            "win_rate": wr, "cluster_mean_roi": statistics.mean(vals),
            "permutation_p": p, "ci95": [lo, hi],
            "worst_roi": min(t["roi"] for t in tr),
            "total_pnl": sum(t["pnl"] for t in tr),
            "skipped": dict(sk),
            "generated_by": "scripts/backtest_credit_wall.py",
        }, ensure_ascii=False, indent=2))
        print(f"{'':>10}逐笔账本 → {out}　跳过：{dict(sk)}")
    print("\n⚠️ 置换 p 检验的是【日期簇净收益 > 0】，不是「胜率 > 50%」。")
    print("   胜率高不等于赚钱：balanced 档 76% 胜率、单笔 -0.43%。")


if __name__ == "__main__":
    main()
