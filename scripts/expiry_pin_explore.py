"""到期日磁吸与墙定义的历史探索（Codex 025 §三：允许现在跑；只作探索性描述，不是检验、不产生信号）。

  python3 scripts/expiry_pin_explore.py --start 2026-06-25 --end 2026-09-25 --asof 2026-09-29 [GLD SLV ...]

v2（Codex 026）：日期范围与 asof 必须显式给出（默认遍历全部数据会让次日重跑悄悄扩大样本）；输出写进
data/backtest/expiry_pin/<运行标签>/（已存在则拒绝覆盖）：逐行结果（含开/收/ATR/安慰剂行权价）、所用日线原文与哈希、
快照路径与哈希、运行清单（代码哈希、git HEAD）。v1 产物 data/backtest/expiry_pin_explore_20260929.* 保留不动。

数据：已存的认证快照（session_index：快照 OI = D 前一交易日收盘结算，D 开盘前可得）+ CBOE 公开日线（开高低收）。
计算全在 analyze/expiry_pin.py（定义见该文件）。三部分：
  P1（主描述）：单日 pull。到期日 D 用该到期自己的 K*；非到期日用下一个到期的 K*（只是描述对照，不是充分对照）。
  P2（次要）：E−2 多日版：用 E−2 盘前可得的快照与 ATR，看 open(E−2) → close(E)。相邻到期的持有期重叠 →
     区间用跨品种同步的移动块 bootstrap（块长 1/3/5 敏感性）。
  W：W0 / W1 / W3(C、P、合并) 三种墙定义，只比较、不择优。W1 另分「所选墙自己的到期就是今天」。
已知几何局限（Codex 026）：diff = d0 − d1，只在收盘越过墙或安慰剂行权价时非 0；「朝墙移动」本身在此对照下可能无差别。
本探索未显示 diff 定义下稳定正增量，但这不是磁吸无效的证据。只读、纯标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
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

OUT = ROOT / "data/backtest/expiry_pin"
VERSION = "expiry-pin-explore-v2-20260929"
MIN_DAYS = 10          # 默认只跑认证交易日 ≥ 10 的品种（个股链只有零星几天）
BLOCKS = (1, 3, 5)     # P2 移动块长度敏感性（交易日）


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def daily_ohlc(sym: str, end: date) -> tuple[dict, list]:
    """CBOE 日线 → ({日: (开,高,低,收)}, 原始行)。只保留 ≤ end 的行，结果不随之后的新数据变化。"""
    d = http_get_json(CBOE_HIST_URL.format(symbol=sym))
    out, raw = {}, []
    for r in d.get("data") or []:
        try:
            k = date.fromisoformat(r["date"])
            v = tuple(float(r[x]) for x in ("open", "high", "low", "close"))
        except (KeyError, ValueError, TypeError):
            continue
        if k <= end:
            out[k] = v
            raw.append(r)
    return out, raw


def pre(bars: dict, d: date):
    """d 之前的日线 → (ATR14, 前收)。"""
    ds = sorted(x for x in bars if x < d)[-40:]
    if len(ds) < 15:
        return None, None
    h = [bars[x][1] for x in ds]; lo = [bars[x][2] for x in ds]; c = [bars[x][3] for x in ds]
    return _atr(h, lo, c, 14), c[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--start", required=True); ap.add_argument("--end", required=True)
    ap.add_argument("--asof", required=True, help="运行记账日（写进清单；数据只用 ≤ end 的部分）")
    a = ap.parse_args()
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    run_dir = OUT / f"{VERSION}_{a.start}_{a.end}_asof{a.asof}"
    if run_dir.exists():
        sys.exit(f"{run_dir} 已存在：不覆盖历史产物")
    cfg, store = load_config(), SnapshotStore()
    key_of = {i.options.symbol: i.key for i in cfg.instruments.values() if i.options}
    syms = a.symbols or sorted(k for k in key_of if len(session_index(store, k)) >= MIN_DAYS)
    rows, inputs = [], {"ohlc": {}, "snapshots": {}}
    run_dir.mkdir(parents=True)
    for sym in syms:
        bars, raw = daily_ohlc(sym, end)
        blob = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
        (run_dir / f"ohlc_{sym}.json").write_bytes(blob)
        inputs["ohlc"][sym] = {"sha256": sha(blob), "rows": len(raw), "source": CBOE_HIST_URL.format(symbol=sym)}
        idx = {s: f for s, f in session_index(store, sym).items() if start <= s <= end}
        snaps = {}
        for sess, f in sorted(idx.items()):
            if sess not in bars:
                continue
            path = store.path_of("options", sym, f)
            inputs["snapshots"][f"{sym}|{sess}"] = {"path": str(path.relative_to(ROOT)), "sha256": sha(path.read_bytes())}
            snaps[sess] = snapshot_from_payload(store.load("options", sym, f), key_of.get(sym, sym.lower()), sym).contracts
        for d, cs in sorted(snaps.items()):
            atr, ref = pre(bars, d)
            if not atr:
                continue
            o, _, _, c = bars[d]
            exps = sorted({x.expiry for x in cs if x.expiry >= d})
            if not exps:
                continue
            has_exp = exps[0] == d
            e = exps[0]
            k = ep.max_oi_strike(cs, {e}, ref)
            if k is not None:
                rows.append({"part": "P1", "sym": sym, "d": d.isoformat(), "expiry": e.isoformat(), "ref": ref,
                             "is_expiry_day": has_exp, "type": classify(e)["type"], "dte": (e - d).days,
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
                rows.append({"part": "W", "def": name, "sym": sym, "d": d.isoformat(), "ref": ref,
                             "underlying_has_expiry": has_exp,
                             "selected_wall_expiry_today": (ex == d) if name == "W1" else None,
                             "wall_expiries": sorted(x.isoformat() for x in exset),
                             **ep.wall_row(o, c, atr, kk, ep.listed_strikes(cs, exset, ref))})
        for e in sorted({x.expiry for cs in snaps.values() for x in cs}):
            if e not in bars or e > end or mc.is_trading_day(e) is not True:
                continue
            d2 = mc.prev_trading_day(mc.prev_trading_day(e))
            if d2 not in snaps:
                continue
            atr, ref = pre(bars, d2)
            k = ep.max_oi_strike(snaps[d2], {e}, ref) if atr else None
            if k is None:
                continue
            rows.append({"part": "P2", "sym": sym, "d": d2.isoformat(), "expiry": e.isoformat(), "ref": ref,
                         "type": classify(e)["type"],
                         **ep.wall_row(bars[d2][0], bars[e][3], atr, k, ep.listed_strikes(snaps[d2], {e}, ref))})
        print(f"{sym}: {len(snaps)} 个有日线的认证交易日", file=sys.stderr)

    lines = [f"# 到期日磁吸与墙定义：历史探索 {VERSION}（{a.start}–{a.end}，asof {a.asof}；探索性描述，非检验）",
             "pull = (|开−K| − |收−K|)/ATR14（正 = 收盘更靠近 K）；diff = pull(K) − 同日同侧同距离分箱安慰剂行权价 pull 均值。",
             "n = 全部品种日；配对 n = 有安慰剂可比的（diff 的分母）；簇 = 日期。簇 < 5 不给区间（只作案例）。",
             "⚠️ 几何局限：diff = d0 − d1，收盘未越过墙或安慰剂行权价时恒为 0；「朝墙移动」在此对照下可能无差别。",
             "   本表未显示稳定正增量 ≠ 磁吸无效。原始 pull 受墙距机械约束（墙贴开盘价时只能 ≤ 0）。", ""]

    def fmt(s):
        if s["n"] == 0:
            return "—"
        if s["lo"] is None:
            return f"均值 {s['mean']:+.3f}（{s.get('interval', '')}）"
        return f"均值 {s['mean']:+.3f} [{s['lo']:+.3f}, {s['hi']:+.3f}]"

    def line(label, sel):
        a_ = ep.cluster_bootstrap(sel, key=lambda r: r["pull_wall"], cluster=lambda r: r["d"])
        b_ = ep.cluster_bootstrap(sel, key=lambda r: r["diff"], cluster=lambda r: r["d"])
        nz = [r for r in sel if r["diff"] is not None and abs(r["diff"]) > 1e-9]
        lines.append(f"  {label:26s} n={a_['n']:4d} 簇={a_['clusters']:3d} | pull {fmt(a_)} | "
                     f"diff 配对 n={b_['n']} 簇={b_['clusters']} {fmt(b_)} | diff≠0 {len(nz)}（墙更近 {sum(r['diff'] > 0 for r in nz)}）")

    P1 = [r for r in rows if r["part"] == "P1"]
    lines.append("## P1 单日（主描述）：到期日 vs 非到期日（两组品种构成不同：GLD/QQQ/SPY/IWM 几乎每天有到期，")
    lines.append("   非到期日主要来自 SLV/USO/TLT/TQQQ —— 两组均值相减不能解释为到期效应）")
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
    lines += ["", "## P2 E−2 多日（次要）：open(E−2) → close(E)，K* 与 ATR 取 E−2 盘前；持有期跨日重叠 → 移动块 bootstrap"]
    P2 = [r for r in rows if r["part"] == "P2"]
    for label, sel in [("全部", P2)] + [(f"类型 {t}", [r for r in P2 if r["type"] == t]) for t in "QMWD"]:
        m = [r for r in sel if r["diff"] is not None]
        parts = []
        for blk in BLOCKS:
            s_ = ep.block_bootstrap(m, key=lambda r: r["diff"], date_of=lambda r: r["d"], block=blk)
            parts.append(f"块{blk}: {fmt(s_)}")
        lines.append(f"  {label:10s} n={len(sel)} diff 配对 n={len(m)} 簇={len({r['d'] for r in m})} | " + " ; ".join(parts))
    lines += ["", "## W 墙定义比较（每个交易日单日；只比较、不择优；子组多、未做多重比较校正）"]
    for name in ("W0", "W1", "W3", "W3C", "W3P"):
        sel = [r for r in rows if r["part"] == "W" and r["def"] == name]
        line(f"{name} 全部", sel)
        line(f"{name} 标的当天有到期", [r for r in sel if r["underlying_has_expiry"]])
        if name == "W1":
            line("W1 所选墙自身到期=今天", [r for r in sel if r["selected_wall_expiry_today"]])
    lines += ["", "## Q 案例系列（逐条，不给区间）"]
    for r in P1:
        if r["is_expiry_day"] and r["type"] == "Q":
            lines.append(f"  {r['sym']} {r['d']} 开 {r['open']:g} 收 {r['close']:g} K*={r['k']:g} 距 {r['dist_atr']:.2f}ATR "
                         f"pull {r['pull_wall']:+.3f} 安慰剂 {r['placebos']} diff "
                         f"{('%+.3f' % r['diff']) if r['diff'] is not None else '不匹配'}")
    lines += ["", "局限：同一到期在多个品种上共享冲击；非到期日换下一到期的墙同时改变 DTE 与墙距；W3 是 gamma-OI 强度代理，",
              "不代表做市商净头寸；距离分箱界为未校准设计值；这批快照在墙位研究里看过；尚未做「方向 + 价差」之外的净收益与尾损对照。"]
    (run_dir / "rows.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    (run_dir / "summary.txt").write_text("\n".join(lines) + "\n", "utf-8")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    code = {p: sha((ROOT / p).read_bytes()) for p in ("scripts/expiry_pin_explore.py", "undertow/analyze/expiry_pin.py",
                                                     "undertow/analyze/expiry_type.py")}
    (run_dir / "manifest.json").write_text(json.dumps({
        "version": VERSION, "start": a.start, "end": a.end, "asof": a.asof, "symbols": syms, "git_head": head,
        "code_sha256": code, "inputs": inputs, "n_rows": len(rows),
        "rows_sha256": sha((run_dir / "rows.jsonl").read_bytes())}, ensure_ascii=False, indent=1), "utf-8")
    print("\n".join(lines))
    print(f"\n产物：{run_dir.relative_to(ROOT)}", file=sys.stderr)


if __name__ == "__main__":
    main()
