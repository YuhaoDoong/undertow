"""到期日磁吸与墙定义的历史探索（Codex 025 §三：允许现在跑；只作探索性描述，不是检验、不产生信号）。

  python3 scripts/expiry_pin_explore.py [GLD SLV ...]     # 默认全部有期权链的品种

数据：已存的认证快照（session_index：快照 OI = D 前一交易日收盘结算，D 开盘前可得）+ CBOE 公开日线（开高低收）。
计算全在 analyze/expiry_pin.py（定义见该文件）。三部分：
  P1（主描述）：单日 pull。到期日 D 用该到期自己的 K*；非到期日用下一个到期的 K*（这只是描述对照，不是充分对照 ——
     充分对照是同日安慰剂，见 diff = 墙 − 安慰剂）。按到期类型 Q/M/W/D 分层。
  P2（次要）：E−2 多日版：用 E−2 盘前可得的快照与 ATR，看 open(E−2) → close(E) 的 pull。
  W：W0 / W1 / W3(C、P、合并) 三种墙定义在每个交易日的单日 pull 与 diff，只比较、不择优。
区间 = 按日期簇 bootstrap（同一天多品种不独立）；Q 只作案例系列。输出写 data/backtest/。只读、纯标准库。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from undertow.analyze import expiry_pin as ep  # noqa: E402
from undertow.analyze.expiry_type import classify  # noqa: E402
from undertow.analyze.technicals import _atr  # noqa: E402
from undertow.collect.base import http_get_json  # noqa: E402
from undertow.collect.cboe_history import CBOE_HIST_URL  # noqa: E402
from undertow.collect.cboe_options import snapshot_from_payload  # noqa: E402
from undertow.collect.store import SnapshotStore  # noqa: E402
from undertow.core import market_calendar as mc  # noqa: E402
from undertow.core.config import load_config  # noqa: E402
from undertow.dirledger_cli import session_index  # noqa: E402

OUT = ROOT / "data/backtest"
MIN_DAYS = 10          # 默认只跑认证交易日 ≥ 10 的品种（个股链只有零星几天）


def daily_ohlc(sym: str) -> dict:
    d = http_get_json(CBOE_HIST_URL.format(symbol=sym))
    out = {}
    for r in d.get("data") or []:
        try:
            out[date.fromisoformat(r["date"])] = tuple(float(r[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def pre(bars: dict, d: date):
    """d 之前的日线 → (ATR14, 前收)。"""
    ds = sorted(x for x in bars if x < d)[-40:]
    if len(ds) < 15:
        return None, None
    h = [bars[x][1] for x in ds]; lo = [bars[x][2] for x in ds]; c = [bars[x][3] for x in ds]
    return _atr(h, lo, c, 14), c[-1]


def main():
    cfg, store = load_config(), SnapshotStore()
    key_of = {i.options.symbol: i.key for i in cfg.instruments.values() if i.options}
    syms = sys.argv[1:] or sorted(k for k in key_of if len(session_index(store, k)) >= MIN_DAYS)
    rows = []
    for sym in syms:
        bars = daily_ohlc(sym)
        idx = session_index(store, sym)
        snaps = {}
        for sess, f in sorted(idx.items()):
            if sess not in bars:
                continue
            snaps[sess] = snapshot_from_payload(store.load("options", sym, f), key_of.get(sym, sym.lower()), sym).contracts
        for d, cs in sorted(snaps.items()):
            atr, ref = pre(bars, d)
            if not atr:
                continue
            o, _, _, c = bars[d]
            exps = sorted({x.expiry for x in cs if x.expiry >= d})
            if not exps:
                continue
            e = exps[0]
            k = ep.max_oi_strike(cs, {e}, ref)
            if k is not None:
                rows.append({"part": "P1", "sym": sym, "d": d.isoformat(), "expiry": e.isoformat(),
                             "is_expiry_day": e == d, "type": classify(e)["type"], "dte": (e - d).days,
                             **ep.wall_row(o, c, atr, k, ep.listed_strikes(cs, {e}, ref))})
            near = ep.near_expiries(cs, d)
            for name, kk, ex in (("W0", ep.wall_w0(cs, d, ref), near),
                                 ("W1", *ep.wall_w1(cs, d, ref)),
                                 ("W3", ep.wall_w3(cs, d, ref), near),
                                 ("W3C", ep.wall_w3(cs, d, ref, "C"), near),
                                 ("W3P", ep.wall_w3(cs, d, ref, "P"), near)):
                if kk is None:
                    continue
                exset = ex if isinstance(ex, set) else {ex}
                rows.append({"part": "W", "def": name, "sym": sym, "d": d.isoformat(),
                             "is_expiry_day": d in near and any(x.expiry == d for x in cs),
                             **ep.wall_row(o, c, atr, kk, ep.listed_strikes(cs, exset, ref))})
        # P2：E−2 多日（只用 E−2 盘前可得的快照与 ATR）
        for e in sorted({x.expiry for cs in snaps.values() for x in cs}):
            if e not in bars or mc.is_trading_day(e) is not True:
                continue
            d2 = mc.prev_trading_day(mc.prev_trading_day(e))
            if d2 not in snaps:
                continue
            atr, ref = pre(bars, d2)
            k = ep.max_oi_strike(snaps[d2], {e}, ref) if atr else None
            if k is None:
                continue
            rows.append({"part": "P2", "sym": sym, "d": d2.isoformat(), "expiry": e.isoformat(), "type": classify(e)["type"],
                         **ep.wall_row(bars[d2][0], bars[e][3], atr, k, ep.listed_strikes(snaps[d2], {e}, ref))})
        print(f"{sym}: {len(snaps)} 个有日线的认证交易日", file=sys.stderr)

    lines = [f"# 到期日磁吸与墙定义：历史探索（{date.today()}；探索性描述，非检验）",
             "pull = (|开−K| − |收−K|)/ATR14（正 = 收盘更靠近 K）；diff = pull(K) − 同日同侧同距离分箱安慰剂行权价 pull 均值。",
             "区间 = 日期簇 bootstrap 95%；n = 品种日，簇 = 日期。Q 仅案例系列。",
             "读法：原始 pull 受墙距机械约束（pull ≤ 开盘墙距；墙贴着开盘价时只能 ≤ 0），须按墙距分箱看；",
             "diff 只有收盘越过墙或安慰剂行权价时才非 0 —— 中位为 0 是机制，看「diff≠0」的条数与其中墙更近的条数。", ""]

    def line(label, sel):
        a = ep.cluster_bootstrap(sel, key=lambda r: r["pull_wall"], cluster=lambda r: r["d"])
        b = ep.cluster_bootstrap(sel, key=lambda r: r["diff"], cluster=lambda r: r["d"])
        f = lambda s: (f"均值 {s['mean']:+.3f} 中位 {s['median']:+.3f} [{s['lo']:+.3f}, {s['hi']:+.3f}]"
                       if s["n"] else "—")
        unm = sum(r["diff"] is None for r in sel)
        nz = [r for r in sel if r["diff"] is not None and abs(r["diff"]) > 1e-9]
        pos = sum(r["diff"] > 0 for r in nz)
        lines.append(f"  {label:28s} n={a['n']:4d} 簇={a['clusters']:3d} | pull {f(a)} | diff n={b['n']} {f(b)} | 不匹配 {unm}"
                     f" | diff≠0 {len(nz)}（墙更近 {pos}）")

    P1 = [r for r in rows if r["part"] == "P1"]
    lines.append("## P1 单日（主描述）：到期日 vs 非到期日（非到期日 = 下一到期的 K*）")
    line("到期日（全部）", [r for r in P1 if r["is_expiry_day"]])
    line("非到期日（全部）", [r for r in P1 if not r["is_expiry_day"]])
    for t in "QMWD":
        line(f"到期日·类型 {t}", [r for r in P1 if r["is_expiry_day"] and r["type"] == t])
    for b, lab in enumerate(("<0.5", "0.5–1", "1–2", "≥2")):
        line(f"到期日·墙距 {lab} ATR", [r for r in P1 if r["is_expiry_day"] and r["bin"] == b])
        line(f"非到期日·墙距 {lab} ATR", [r for r in P1 if not r["is_expiry_day"] and r["bin"] == b])
    for s in syms:
        line(f"{s} 到期日", [r for r in P1 if r["is_expiry_day"] and r["sym"] == s])
        line(f"{s} 非到期日", [r for r in P1 if not r["is_expiry_day"] and r["sym"] == s])
    lines.append("")
    lines.append("## P2 E−2 多日（次要）：open(E−2) → close(E)，K* 与 ATR 取 E−2 盘前")
    P2 = [r for r in rows if r["part"] == "P2"]
    line("全部", P2)
    for t in "QMWD":
        line(f"类型 {t}", [r for r in P2 if r["type"] == t])
    lines.append("")
    lines.append("## W 墙定义比较（每个交易日单日；只比较、不择优）")
    for name in ("W0", "W1", "W3", "W3C", "W3P"):
        sel = [r for r in rows if r["part"] == "W" and r["def"] == name]
        line(f"{name} 全部", sel)
        line(f"{name} 到期日", [r for r in sel if r["is_expiry_day"]])
    lines.append("")
    lines.append("## Q 案例系列（逐条）")
    for r in P1:
        if r["is_expiry_day"] and r["type"] == "Q":
            lines.append(f"  {r['sym']} {r['d']} K*={r['k']:g} 距 {r['dist_atr']:.2f}ATR pull {r['pull_wall']:+.3f} "
                         f"diff {('%+.3f' % r['diff']) if r['diff'] is not None else '不匹配'}")
    lines += ["", "局限：K* 与安慰剂都来自同一组行权价，分箱内行权价彼此很近时 diff 天然接近 0；同一到期在多个品种上共享冲击；",
              "非到期日的下一到期 K* 同时改变了 DTE 与墙距；W1 可能超出当日持有期；W3 是 gamma-OI 强度代理，不代表做市商净头寸；",
              "尚未做「方向 + 价差」之外的净收益与尾损对照（Codex 建议的下一步策略检验）。这批快照在墙位研究里看过。"]
    OUT.mkdir(parents=True, exist_ok=True)
    tag = date.today().strftime("%Y%m%d")
    (OUT / f"expiry_pin_explore_{tag}.txt").write_text("\n".join(lines) + "\n", "utf-8")
    (OUT / f"expiry_pin_explore_{tag}.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
