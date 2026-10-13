"""structure-v0 图例：固定前缀图（≤2015），叠加局部分形点（左右各 2 根、影线价）与既有 2ATR 收盘之字形点，标出极值与确认时点。

  python3 scripts/structure_figs.py --out data/backtest/structure-v0/figs

抽样：固定种子、按截断时可知的量（不看之后走势、不看收益）抽前缀日；清单先落盘再画图。每张只含截至前缀日的 120 根。
纯标准库手写 SVG。只作识别诊断，不是信号。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402

SEED = 20261013
WINDOW = 120
SAMPLE = [("GLD", 2), ("SLV", 1), ("SPY", 2), ("QQQ", 1), ("GC=F", 1), ("AAPL", 1)]


def sample_prefixes(seed: int = SEED) -> list:
    rng = random.Random(seed)
    out = []
    for sym, n in SAMPLE:
        d, sha = pe.fetch(sym)
        bars, _, _ = pe.parse(d)
        idx = sorted(rng.sample(range(700, len(bars)), n))      # 至少 700 根历史（ATR / 背景足够），只按位置抽
        out += [{"symbol": sym, "prefix_end": str(bars[i][0]), "prefix_index": i, "input_sha256": sha} for i in idx]
    return out


def svg(sym, bars, frac, seq, zz, end_i, W=1100, H=460):
    lo_i = end_i - WINDOW + 1
    win = bars[lo_i:end_i + 1]
    pmin, pmax = min(b[3] for b in win), max(b[2] for b in win)
    pad = (pmax - pmin) * 0.06
    pmin, pmax = pmin - pad, pmax + pad
    x = lambda i: 40 + (i - lo_i + 0.5) * (W - 60) / WINDOW
    y = lambda v: 20 + (pmax - v) * (H - 60) / (pmax - pmin)
    cw = (W - 60) / WINDOW * 0.6
    g = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" font-family="sans-serif" font-size="11">',
         f'<rect width="{W}" height="{H}" fill="#fff"/>',
         f'<text x="40" y="14">{sym} 截至 {bars[end_i][0]}（只含此前 {WINDOW} 根；右侧未来未提供）</text>']
    for i in range(lo_i, end_i + 1):
        _, o, h, l, c = bars[i]
        col = "#2a9d4b" if c >= o else "#d1495b"
        g.append(f'<line x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{y(h):.1f}" y2="{y(l):.1f}" stroke="{col}"/>')
        g.append(f'<rect x="{x(i) - cw / 2:.1f}" y="{y(max(o, c)):.1f}" width="{cw:.1f}" height="{max(1, abs(y(o) - y(c))):.1f}" fill="{col}"/>')
    pts = [p for p in seq if lo_i <= p.idx and p.conf <= end_i]
    if len(pts) > 1:
        g.append('<polyline fill="none" stroke="#1f6feb" stroke-width="1.5" points="' +
                 " ".join(f"{x(p.idx):.1f},{y(p.price):.1f}" for p in pts) + '"/>')
    for p in frac:
        if p.idx < lo_i or p.conf > end_i:
            continue
        up = p.kind == "H"
        yy = y(p.price) + (-8 if up else 8)
        col = "#999" if p.ambiguous_order else "#1f6feb"
        g.append(f'<path d="M{x(p.idx) - 4:.1f},{yy - (4 if up else -4):.1f} L{x(p.idx) + 4:.1f},{yy - (4 if up else -4):.1f} L{x(p.idx):.1f},{yy + (2 if up else -2):.1f} Z" fill="{col}"/>')
        g.append(f'<line x1="{x(p.conf):.1f}" x2="{x(p.conf):.1f}" y1="{H - 38}" y2="{H - 30}" stroke="#1f6feb"/>')
    for p in zz:
        if p.idx < lo_i or p.conf > end_i:
            continue
        g.append(f'<circle cx="{x(p.idx):.1f}" cy="{y(p.price):.1f}" r="5" fill="none" stroke="#e36209" stroke-width="2"/>')
        g.append(f'<line x1="{x(p.idx):.1f}" x2="{x(p.conf):.1f}" y1="{H - 24}" y2="{H - 24}" stroke="#e36209" stroke-width="2"/>')
    g.append(f'<text x="40" y="{H - 6}" fill="#555">蓝三角 = 局部分形点（左右各2根，影线价；灰 = 同根高低歧义）；蓝线 = 交替投影；蓝短竖 = 确认根。'
             f'橙圈 = 2ATR 收盘之字形点；橙横线 = 极值到确认的滞后。</text>')
    g.append("</svg>")
    return "\n".join(g)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    out.mkdir(parents=True)
    man = sample_prefixes()
    (out / "manifest.json").write_text(json.dumps({"seed": SEED, "window": WINDOW, "fractal_k": ps.FRACTAL_K,
                                                    "zigzag_m": ps.M_ATR, "samples": man}, ensure_ascii=False, indent=1), "utf-8")
    html = ["<!doctype html><meta charset=utf-8><title>structure-v0 图例</title><body style='font-family:sans-serif'>"]
    for k, m in enumerate(man):
        d, _ = pe.fetch(m["symbol"])
        bars, _, _ = pe.parse(d)
        pre = bars[:m["prefix_index"] + 1]                       # 只给截至前缀日的数据
        frac = ps.fractal_pivots(pre)
        seq = ps.alternating(frac)
        zz = ps.zigzag(pre, ps.wilder_atr(pre))
        f = out / f"{k:02d}_{m['symbol'].replace('=', '_')}_{m['prefix_end']}.svg"
        f.write_text(svg(m["symbol"], pre, frac, seq, zz, len(pre) - 1), "utf-8")
        html.append(f"<h3>{k:02d} {m['symbol']} 截至 {m['prefix_end']}</h3><img src='{f.name}'>")
    (out / "index.html").write_text("\n".join(html), "utf-8")
    print(f"{len(man)} 张 → {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
