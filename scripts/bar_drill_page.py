"""逐根人工回测练习页（bar-drill-v1；用户 2026-10-16：「建立一个模拟逐根人工回测的模块。就像上次你做的网页那样。
但是每次可以重新随机生成新的题目，同时加入颠倒镜像K线。k线的图可以左右拖动，方便我看之前的历史k线。」）

  python3 scripts/bar_drill_page.py                 # → data/drill/index.html（本地打开即可，记录存浏览器）

与 replay_v1/v2 标注页的区别：不是固定的几段，而是把整段日线装进页面，由页面每次随机出题（种子记在题目里，可复现）；
按设定概率把整段 K 线上下颠倒（P' = 100²/P，与 ps_trigger_v2.mirror 同口径），做完才揭晓品种、日期与是否颠倒；
可下单（下一根开盘成交）、设止损止盈（盘中判定、跳空按开盘），统计胜率与 R，并分原始/镜像、多/空对比。
数据：ps-explore-v0.3 已存的 Yahoo 日线（≤2015-12-31，逐个与首次记录的 sha256 核对），2016 年起不进练习，留给研究确认集。
纯逻辑在 scripts/bar_drill_engine.js（撮合，页面与 node 单测共用，文件 sha256 即引擎版本）与 scripts/bar_drill_sync.js
（与本机文件同步时的合并规则）；页面模板 scripts/bar_drill_page.html；本机自动保存服务 scripts/bar_drill_serve.py。

二次标注题（relabel-v1；用户 2026-10-17：「用你标注的图（遮住未来走势），让我二次标注来看看我们的标注差异。
然后再对比下游未来的走势，判断我和你谁对。」）：出题时用固定种子从同一数据池抽题，「Claude 标注」= 确定性算法
（price_structure 收盘之字形 HH/HL/LH/LL、directional_zones 截止时仍有效的 R/S 区、最近两高两低定方向），
只用截止根及之前（页面可见的 300 根）计算后嵌入页面；判定在 scripts/bar_drill_relabel.js（与撮合引擎分开，
改它不会让未完成的逐根题变成「旧引擎只读」）。口径见 GPTcom/20261017T115205+0800_…_1017-support-scoring 第三节。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:                    # 直接当脚本跑时也要能 import undertow（算法标注只调用、不重写）
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "data/backtest/ps-explore-v0.3"
ETF = ("GLD", "SLV", "USO", "QQQ", "SPY", "IWM", "TLT", "GC=F", "SI=F")
STOCK = ("AAPL", "MSFT", "GOOGL", "NVDA", "AMD", "INTC", "TSLA")
THROUGH = 2015


def _g(x: float) -> float:
    return float(f"{x:.6g}")


def load_series(sym: str) -> tuple[dict, str]:
    raw = (SRC / "inputs" / f"{sym.replace('=', '_')}.json").read_bytes()
    want = json.loads((SRC / "inputs_sha256.json").read_text("utf-8"))[sym]
    got = hashlib.sha256(raw).hexdigest()
    if got != want:
        raise SystemExit(f"{sym}：输入哈希与 ps-explore 首次记录不符（{want[:12]} → {got[:12]}），拒绝生成")
    r = json.loads(raw)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    s = {"d": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for i, ts in enumerate(r["timestamp"]):
        row = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
        if any(x is None for x in row) or min(row) <= 0:          # 缺行剔除，不插值
            continue
        d = datetime.fromtimestamp(ts, timezone.utc).date()
        if d.year > THROUGH:
            continue
        s["d"].append(int(d.strftime("%Y%m%d")))
        for k, x in zip("ohlc", row):
            s[k].append(_g(x))
        v = q["volume"][i]
        s["v"].append(None if v is None else int(v))
    return s, got


RELABEL = {"version": "relabel-v1", "seed": 20261017, "n": 120, "left": 300, "horizon": 20,
           "tie_atr": 0.5, "point_tol_bars": 3, "line_tol_atr": 0.25}
ALGO_FILES = ("undertow/analyze/price_structure.py", "undertow/analyze/directional_zones.py")


def transform(raw: dict, i0: int, left: int, mirror: bool, upto: int | None = None) -> list:
    """与 bar_drill_engine.js Drill.window 同一口径：截止根收盘 = 100；镜像 P' = 10000/P（高低互换）。
    返回 [(YYYYMMDD 整数, o, h, l, c)]，覆盖 i0−left+1 … upto（默认 i0，即只到截止根）。之字形与区间只用 OHLC、不用日期。"""
    a, b, f = i0 - left + 1, i0 if upto is None else upto, 100 / raw["c"][i0]
    out = []
    for i in range(a, b + 1):
        o, h, l, c = raw["o"][i] * f, raw["h"][i] * f, raw["l"][i] * f, raw["c"][i] * f
        if mirror:
            K = 10000
            o, h, l, c = K / o, K / l, K / h, K / c
        out.append((raw["d"][i], o, h, l, c))
    return out


def annotate(raw: dict, i0: int, left: int, mirror: bool) -> dict:
    """「Claude 标注」：只用截止根及之前 left 根（页面可见部分）。下标 i 为窗口内下标（0 … left−1，截止根 = left−1）。

    - 结构点：price_structure.zigzag（收盘之字形，m = 2·ATR，Wilder ATR14）已确认的摆动点；每个高点与前一个高点比 → HH/LH，
      低点与前一个低点比 → HL/LL（第一个无可比对象，标 H/L）。价位 = 收盘价。
    - 区间：directional_zones.find_zones 的 R/S 区中，启用根时价格在正确一侧（R 在下方、S 在上方），且启用后到截止根没有收盘越过远沿。
    - 方向：最近两个高点与最近两个低点同为 HH+HL → 看涨；LH+LL → 看跌；否则不确定。失效位：看涨 = 最近低点，看跌 = 最近高点。
    """
    from undertow.analyze import directional_zones as dz
    from undertow.analyze import price_structure as ps
    bars = transform(raw, i0, left, mirror)
    atr = ps.wilder_atr(bars)
    piv = ps.zigzag(bars, atr)
    C = [b[4] for b in bars]
    pts, last = [], {"H": None, "L": None}
    for p in piv:
        prev = last[p.kind]
        if prev is None:
            lab = p.kind
        elif p.kind == "H":
            lab = "HH" if p.price > prev.price else "LH"
        else:
            lab = "HL" if p.price > prev.price else "LL"
        pts.append({"i": p.idx, "conf": p.conf, "kind": p.kind, "label": lab, "price": round(p.price, 6)})
        last[p.kind] = p
    zones = []
    for z in dz.find_zones(bars, atr, piv):
        a = z.active
        side_ok = C[a] < z.lo if z.kind == "R" else C[a] > z.hi
        held = all((c <= z.hi) if z.kind == "R" else (c >= z.lo) for c in C[a + 1:])
        if side_ok and held:
            zones.append({"kind": z.kind, "lo": round(z.lo, 6), "hi": round(z.hi, 6), "formed": z.formed, "active": a})
    hs = [p for p in pts if p["kind"] == "H"][-2:]
    ls = [p for p in pts if p["kind"] == "L"][-2:]
    d = "不确定"
    if len(hs) == 2 and len(ls) == 2:
        if hs[1]["price"] > hs[0]["price"] and ls[1]["price"] > ls[0]["price"]:
            d = "看涨"
        elif hs[1]["price"] < hs[0]["price"] and ls[1]["price"] < ls[0]["price"]:
            d = "看跌"
    inv = ls[-1]["price"] if d == "看涨" else hs[-1]["price"] if d == "看跌" else None
    a_cut = atr[-1]
    return {"atr": round(a_cut, 6) if a_cut else None, "points": pts, "zones": zones, "direction": d, "invalid": inv}


def relabel_bank(series: dict, cfg: dict = RELABEL) -> dict:
    """固定种子抽二次标注题：品种均匀抽，截止根 i0 ∈ [left−1, n−horizon−1]（未来 horizon 根必须存在），原始/镜像交替各半。"""
    import random
    rng = random.Random(cfg["seed"])
    syms = [s for s in (*ETF, *STOCK) if s in series and len(series[s]["c"]) >= cfg["left"] + cfg["horizon"] + 30]
    code = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in ALGO_FILES}
    items = []
    for k in range(cfg["n"]):
        sym = syms[rng.randrange(len(syms))]
        n = len(series[sym]["c"])
        i0 = rng.randint(cfg["left"] - 1, n - cfg["horizon"] - 1)
        mirror = k % 2 == 1
        it = {"rid": f"{cfg['version']}-{cfg['seed']}-{k:03d}", "sym": sym, "i0": i0, "left": cfg["left"], "right": cfg["horizon"],
              "mirror": mirror, "claude": annotate(series[sym], i0, cfg["left"], mirror)}
        it["item_sha"] = hashlib.sha256(json.dumps(it, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        items.append(it)
    return {"meta": {**cfg, "algo_sha256": code, "status": "练习题库：算法标注只是确定性规则的输出，不是已验证的方向判断"},
            "items": items}


def build() -> dict:
    series, sha = {}, {}
    for sym in (*ETF, *STOCK):
        series[sym], sha[sym] = load_series(sym)
    return {"meta": {"version": "bar-drill-v1", "etf": list(ETF), "stock": list(STOCK), "through": f"{THROUGH}-12-31", "sha256": sha},
            "series": series, "relabel": relabel_bank(series)}


def render(pool: dict) -> str:
    """嵌入撮合引擎、同步合并逻辑与数据。撮合引擎文件的 sha256 写进 pool["meta"]["engine_sha256"]（就地，唯一来源），
    页面据此给每道新题记引擎版本，本机服务据此拒绝旧页面的写入。"""
    page = (ROOT / "scripts/bar_drill_page.html").read_text("utf-8")
    engine_raw = (ROOT / "scripts/bar_drill_engine.js").read_bytes()
    sync = (ROOT / "scripts/bar_drill_sync.js").read_text("utf-8")
    relabel_raw = (ROOT / "scripts/bar_drill_relabel.js").read_bytes()
    pool["meta"]["engine_sha256"] = hashlib.sha256(engine_raw).hexdigest()
    pool["meta"]["relabel_sha256"] = hashlib.sha256(relabel_raw).hexdigest()   # 判定规则身份：揭示时写进记录
    pool.setdefault("relabel", {"meta": {}, "items": []})
    assert all(page.count(k) == 1 for k in ("__ENGINE__", "__SYNC__", "__RELABEL__", "__DATA__"))
    return (page.replace("__ENGINE__", engine_raw.decode("utf-8")).replace("__SYNC__", sync)
            .replace("__RELABEL__", relabel_raw.decode("utf-8"))
            .replace("__DATA__", json.dumps(pool, ensure_ascii=False, separators=(",", ":"))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/drill/index.html")
    a = ap.parse_args()
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    html = render(build())
    out.write_text(html, "utf-8")
    print(f"{out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}（{len(html) / 1e6:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
