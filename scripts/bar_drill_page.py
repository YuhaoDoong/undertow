"""逐根人工回测练习页（bar-drill-v1；用户 2026-10-16：「建立一个模拟逐根人工回测的模块。就像上次你做的网页那样。
但是每次可以重新随机生成新的题目，同时加入颠倒镜像K线。k线的图可以左右拖动，方便我看之前的历史k线。」）

  python3 scripts/bar_drill_page.py                 # → data/drill/index.html（本地打开即可，记录存浏览器）

与 replay_v1/v2 标注页的区别：不是固定的几段，而是把整段日线装进页面，由页面每次随机出题（种子记在题目里，可复现）；
按设定概率把整段 K 线上下颠倒（P' = 100²/P，与 ps_trigger_v2.mirror 同口径），做完才揭晓品种、日期与是否颠倒；
可下单（下一根开盘成交）、设止损止盈（盘中判定、跳空按开盘），统计胜率与 R，并分原始/镜像、多/空对比。
数据：ps-explore-v0.3 已存的 Yahoo 日线（≤2015-12-31，逐个与首次记录的 sha256 核对），2016 年起不进练习，留给研究确认集。
纯逻辑在 scripts/bar_drill_engine.js（页面与 node 单测共用）；页面模板 scripts/bar_drill_page.html。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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


def build() -> dict:
    series, sha = {}, {}
    for sym in (*ETF, *STOCK):
        series[sym], sha[sym] = load_series(sym)
    return {"meta": {"version": "bar-drill-v1", "etf": list(ETF), "stock": list(STOCK), "through": f"{THROUGH}-12-31", "sha256": sha},
            "series": series}


def render(pool: dict) -> str:
    page = (ROOT / "scripts/bar_drill_page.html").read_text("utf-8")
    engine = (ROOT / "scripts/bar_drill_engine.js").read_text("utf-8")
    assert page.count("__ENGINE__") == 1 and page.count("__DATA__") == 1
    return page.replace("__ENGINE__", engine).replace("__DATA__", json.dumps(pool, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/drill/index.html")
    a = ap.parse_args()
    out = ROOT / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    html = render(build())
    out.write_text(html, "utf-8")
    print(f"{out.relative_to(ROOT)}（{len(html) / 1e6:.1f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
