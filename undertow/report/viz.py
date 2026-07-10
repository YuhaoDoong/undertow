"""手绘 SVG 图表（纯标准库，零依赖）。

每个函数返回一个自包含的 <svg> 片段（含 viewBox），可单独存盘，也可内嵌进
HTML 报告。刻意只用基本图元(line/polyline/rect/text)，不引第三方绘图库，
以保持项目"纯标准库"身份、便于将来封装 skill / 跨机部署。

三张图对应三层情报:
  price_levels_svg     —— 价格日线 + 关键位点(墙/零伽马/现价)横线
  oi_walls_svg         —— 按行权价的 call/put OI 墙(发散水平条) + 现价
  cot_net_history_svg  —— 投机资金净持仓历史曲线 + 当前分位
"""
from __future__ import annotations

from datetime import date

# 配色
C_PRICE = "#1f77b4"
C_RES = "#2ca02c"     # 阻力/call/支撑墙用绿
C_SUP = "#d62728"     # 支撑/put 用红
C_FLIP = "#9467bd"    # 零伽马紫
C_SPOT = "#ff7f0e"    # 现价橙
C_GRID = "#e6e6e6"
C_AXIS = "#888"
C_TEXT = "#333"
C_ZERO = "#bbb"


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _lin(v: float, vmin: float, vmax: float, pmin: float, pmax: float) -> float:
    if vmax == vmin:
        return (pmin + pmax) / 2
    return pmin + (v - vmin) * (pmax - pmin) / (vmax - vmin)


def _svg_open(w: int, h: int) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
            f'<rect width="{w}" height="{h}" fill="#fff"/>')


def _txt(x, y, s, *, size=11, fill=C_TEXT, anchor="start", weight="normal") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{_esc(s)}</text>')


def _line(x1, y1, x2, y2, *, stroke, width=1.0, dash=None) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"{d}/>')


def price_levels_svg(dates: list[date], closes: list[float],
                     levels: list[tuple[str, float, str]], spot: float,
                     *, title: str = "", width: int = 680, height: int = 290,
                     max_points: int = 120) -> str:
    """价格日线 + 关键位点横线。levels=[(标签, 价位, 颜色)]。"""
    if closes:
        dates = dates[-max_points:]
        closes = closes[-max_points:]
    ml, mr, mt, mb = 48, 96, 26, 24
    px0, px1 = ml, width - mr
    py0, py1 = mt, height - mb

    yvals = list(closes) + [v for _, v, _ in levels] + ([spot] if spot else [])
    yvals = [v for v in yvals if v and v == v]
    if not yvals:
        return _svg_open(width, height) + _txt(width/2, height/2, "无价格数据", anchor="middle") + "</svg>"
    ymin, ymax = min(yvals), max(yvals)
    pad = (ymax - ymin) * 0.06 or (ymax * 0.02 or 1)
    ymin, ymax = ymin - pad, ymax + pad

    s = [_svg_open(width, height)]
    if title:
        s.append(_txt(ml, 16, title, size=13, weight="bold"))
    # y 网格 + 标签
    for i in range(5):
        yv = ymin + (ymax - ymin) * i / 4
        yp = _lin(yv, ymin, ymax, py1, py0)
        s.append(_line(px0, yp, px1, yp, stroke=C_GRID))
        s.append(_txt(px0 - 5, yp + 3, f"{yv:.1f}", size=10, fill=C_AXIS, anchor="end"))
    # 价格折线
    if len(closes) >= 2:
        n = len(closes)
        pts = " ".join(f"{_lin(i,0,n-1,px0,px1):.1f},{_lin(c,ymin,ymax,py1,py0):.1f}"
                       for i, c in enumerate(closes))
        s.append(f'<polyline points="{pts}" fill="none" stroke="{C_PRICE}" stroke-width="1.6"/>')
    # x 日期标签（首/中/尾）
    if dates:
        for frac, anch in ((0.0, "start"), (0.5, "middle"), (1.0, "end")):
            idx = int(frac * (len(dates) - 1))
            xp = _lin(idx, 0, len(dates) - 1, px0, px1)
            s.append(_txt(xp, height - 8, dates[idx].strftime("%m-%d"), size=10, fill=C_AXIS, anchor=anch))
    # 关键位横线 + 右侧标签
    seen_y: list[float] = []
    for label, v, color in levels:
        if not v or v != v:
            continue
        yp = _lin(v, ymin, ymax, py1, py0)
        s.append(_line(px0, yp, px1, yp, stroke=color, width=1.2, dash="5,3"))
        # 避免标签重叠：与已放置标签太近则微调
        ly = yp
        while any(abs(ly - y) < 11 for y in seen_y):
            ly += 11
        seen_y.append(ly)
        s.append(_txt(px1 + 4, ly + 3, f"{label} {v:.1f}", size=10, fill=color))
    # 现价（橙色实线，更粗）
    if spot and spot == spot:
        yp = _lin(spot, ymin, ymax, py1, py0)
        s.append(_line(px0, yp, px1, yp, stroke=C_SPOT, width=1.6))
        s.append(_txt(px0 + 3, yp - 4, f"现价 {spot:.1f}", size=10, fill=C_SPOT, weight="bold"))
    s.append("</svg>")
    return "".join(s)


def oi_walls_svg(rows: list[tuple[float, int, int]], spot: float,
                 call_wall: float | None, put_wall: float | None,
                 *, title: str = "", width: int = 680, height: int = 320,
                 max_rows: int = 20) -> str:
    """按行权价的 call(右,绿)/put(左,红) OI 发散条形图。rows=[(strike, call_oi, put_oi)]。

    行权价过多会挤成一团，故只保留组合 OI 最大的 max_rows 个（墙位强制保留），
    再按行权价排序展示——既清晰又不漏掉真正的墙。"""
    rows = [r for r in rows if (r[1] or r[2])]
    walls = {w for w in (call_wall, put_wall) if w is not None}
    if len(rows) > max_rows:
        keep = [r for r in rows if r[0] in walls]
        rest = sorted([r for r in rows if r[0] not in walls], key=lambda r: -(r[1] + r[2]))
        rows = keep + rest[: max(0, max_rows - len(keep))]
    rows = sorted(rows, key=lambda r: r[0])
    s = [_svg_open(width, height)]
    if title:
        s.append(_txt(12, 16, title, size=13, weight="bold"))
    if not rows:
        s.append(_txt(width/2, height/2, "无 OI 数据", anchor="middle") + "</svg>")
        return "".join(s)
    ml, mr, mt, mb = 56, 16, 28, 30
    cx = (ml + (width - mr)) / 2          # 中轴
    half = (width - mr - ml) / 2 - 30     # 单侧最大条长
    py0, py1 = mt, height - mb
    maxoi = max(max(c, p) for _, c, p in rows) or 1
    n = len(rows)
    band = (py1 - py0) / n
    bh = min(band * 0.66, 13)

    # 中轴 + 图例
    s.append(_line(cx, py0 - 4, cx, py1 + 2, stroke=C_AXIS))
    s.append(_txt(cx - half/2, mt - 8, "← put OI", size=10, fill=C_SUP, anchor="middle"))
    s.append(_txt(cx + half/2, mt - 8, "call OI →", size=10, fill=C_RES, anchor="middle"))
    for i, (strike, coi, poi) in enumerate(rows):
        yc = py1 - band * (i + 0.5)
        # put 左
        pw = _lin(poi, 0, maxoi, 0, half)
        s.append(f'<rect x="{cx-pw:.1f}" y="{yc-bh/2:.1f}" width="{pw:.1f}" height="{bh:.1f}" fill="{C_SUP}" opacity="0.85"/>')
        # call 右
        cw = _lin(coi, 0, maxoi, 0, half)
        s.append(f'<rect x="{cx:.1f}" y="{yc-bh/2:.1f}" width="{cw:.1f}" height="{bh:.1f}" fill="{C_RES}" opacity="0.85"/>')
        # 行权价标签（墙位加粗+🧱）
        is_wall = (call_wall and abs(strike-call_wall) < 1e-6) or (put_wall and abs(strike-put_wall) < 1e-6)
        lbl = f"{strike:.0f}"
        s.append(_txt(ml - 6, yc + 3, lbl, size=10, anchor="end",
                      weight="bold" if is_wall else "normal",
                      fill=C_TEXT if not is_wall else C_FLIP))
    # 现价水平参考线
    if spot and spot == spot:
        # 找最接近现价的行做位置
        near_i = min(range(n), key=lambda i: abs(rows[i][0] - spot))
        # 线性插值现价 y
        if n > 1:
            # 用 strike 线性映射
            smin, smax = rows[0][0], rows[-1][0]
            yc = _lin(spot, smin, smax, py1 - band*0.5, py0 + band*0.5)
        else:
            yc = py1 - band * (near_i + 0.5)
        s.append(_line(ml, yc, width - mr, yc, stroke=C_SPOT, width=1.4, dash="4,3"))
        s.append(_txt(width - mr, yc - 3, f"现价 {spot:.1f}", size=10, fill=C_SPOT, anchor="end", weight="bold"))
    s.append("</svg>")
    return "".join(s)


def cot_net_history_svg(dates: list[date], nets: list[int], *, percentile: float | None = None,
                        title: str = "", width: int = 680, height: int = 240,
                        max_points: int = 156) -> str:
    """投机资金净持仓历史曲线 + 零线 + 末点。"""
    dates, nets = dates[-max_points:], nets[-max_points:]
    ml, mr, mt, mb = 60, 16, 28, 24
    px0, px1 = ml, width - mr
    py0, py1 = mt, height - mb
    s = [_svg_open(width, height)]
    ttl = title + (f"（当前净持仓分位 {percentile:.0f}%）" if percentile is not None and percentile == percentile else "")
    if ttl:
        s.append(_txt(12, 16, ttl, size=13, weight="bold"))
    if len(nets) < 2:
        s.append(_txt(width/2, height/2, "历史不足", anchor="middle") + "</svg>")
        return "".join(s)
    ymin, ymax = min(nets), max(nets)
    if ymin > 0:
        ymin = 0
    if ymax < 0:
        ymax = 0
    pad = (ymax - ymin) * 0.08 or 1
    ymin, ymax = ymin - pad, ymax + pad
    # y 标签
    for i in range(4):
        yv = ymin + (ymax - ymin) * i / 3
        yp = _lin(yv, ymin, ymax, py1, py0)
        s.append(_line(px0, yp, px1, yp, stroke=C_GRID))
        s.append(_txt(px0 - 5, yp + 3, f"{yv/1000:.0f}k", size=10, fill=C_AXIS, anchor="end"))
    # 零线
    if ymin < 0 < ymax:
        yz = _lin(0, ymin, ymax, py1, py0)
        s.append(_line(px0, yz, px1, yz, stroke=C_ZERO, width=1.2))
    n = len(nets)
    pts = " ".join(f"{_lin(i,0,n-1,px0,px1):.1f},{_lin(v,ymin,ymax,py1,py0):.1f}"
                   for i, v in enumerate(nets))
    s.append(f'<polyline points="{pts}" fill="none" stroke="{C_PRICE}" stroke-width="1.6"/>')
    # 末点
    lx, ly = _lin(n-1,0,n-1,px0,px1), _lin(nets[-1],ymin,ymax,py1,py0)
    s.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.2" fill="{C_SPOT}"/>')
    s.append(_txt(lx, ly - 7, f"{nets[-1]:+,}", size=10, fill=C_SPOT, anchor="end", weight="bold"))
    # x 日期
    for frac, anch in ((0.0, "start"), (1.0, "end")):
        idx = int(frac * (n - 1))
        xp = _lin(idx, 0, n-1, px0, px1)
        s.append(_txt(xp, height - 8, dates[idx].strftime("%y-%m"), size=10, fill=C_AXIS, anchor=anch))
    s.append("</svg>")
    return "".join(s)


def strategy_timeline_svg(rows: list[tuple], spot: float | None = None, *,
                          title: str = "结构时间轴：日K(高低收) vs 零伽马/墙（日收盘口径）",
                          width: int = 680, height: int = 240) -> str:
    """策略卡时间轴：近 N 日 HLC 竖线 + 逐日零伽马/call墙/put墙折线。

    rows = [(date, close, high, low, flip, call_wall, put_wall)]，高低/结构可为 None。
    价格用 HLC 竖线（无开盘价数据），收盘为右侧短横；结构线为逐日折线（虚线）。
    """
    rows = [r for r in rows if r[1] is not None]
    if len(rows) < 2:
        return ""
    ml, mr, mt, mb = 48, 96, 30, 24
    px0, px1 = ml, width - mr
    py0, py1 = mt, height - mb

    yvals: list[float] = []
    for _, c, h, l, f, cw, pw in rows:
        yvals += [v for v in (c, h, l, f, cw, pw) if v]
    if spot:
        yvals.append(spot)
    ymin, ymax = min(yvals), max(yvals)
    pad = (ymax - ymin) * 0.07 or (ymax * 0.02 or 1)
    ymin, ymax = ymin - pad, ymax + pad
    n = len(rows)
    xs = [_lin(i, 0, max(n - 1, 1), px0 + 8, px1 - 8) for i in range(n)]
    Y = lambda v: _lin(v, ymin, ymax, py1, py0)

    s = [_svg_open(width, height), _txt(ml, 16, title, size=12.5, weight="bold")]
    for i in range(5):
        yv = ymin + (ymax - ymin) * i / 4
        s.append(_line(px0, Y(yv), px1, Y(yv), stroke=C_GRID))
        s.append(_txt(px0 - 5, Y(yv) + 3, f"{yv:.1f}", size=10, fill=C_AXIS, anchor="end"))

    # 结构折线（逐日）：零伽马紫、call墙绿、put墙红——右端标最新值
    for idx, color, label in ((4, C_FLIP, "零伽马"), (5, C_RES, "call墙"), (6, C_SUP, "put墙")):
        pts = [(xs[i], Y(rows[i][idx])) for i in range(n) if rows[i][idx]]
        if len(pts) >= 2:
            s.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) +
                     f'" fill="none" stroke="{color}" stroke-width="1.4" stroke-dasharray="5,3"/>')
        if pts:
            last_v = next(rows[i][idx] for i in range(n - 1, -1, -1) if rows[i][idx])
            s.append(_txt(px1 + 4, pts[-1][1] + 3, f"{label} {last_v:.1f}", size=10, fill=color))

    # HLC 竖线 + 收盘短横（价格）
    for i, (_, c, h, l, *_r) in enumerate(rows):
        x = xs[i]
        if h and l:
            s.append(_line(x, Y(h), x, Y(l), stroke=C_PRICE, width=1.4))
        s.append(_line(x, Y(c), x + 4, Y(c), stroke=C_PRICE, width=2.0))

    # 现价点（盘中参考，非收盘）
    if spot:
        s.append(f'<circle cx="{px1 - 4:.1f}" cy="{Y(spot):.1f}" r="3.2" fill="{C_SPOT}"/>')
        s.append(_txt(px1 - 10, Y(spot) - 6, f"现价 {spot:.1f}", size=10, fill=C_SPOT,
                      anchor="end", weight="bold"))

    for frac, anch in ((0.0, "start"), (0.5, "middle"), (1.0, "end")):
        i = int(frac * (n - 1))
        s.append(_txt(xs[i], height - 8, rows[i][0].strftime("%m-%d"), size=10,
                      fill=C_AXIS, anchor=anch))
    s.append("</svg>")
    return "".join(s)
