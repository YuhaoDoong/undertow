"""手绘 SVG 图表（纯标准库，零依赖）。

每个函数返回一个自包含的 <svg> 片段（含 viewBox），可单独存盘，也可内嵌进
HTML 报告。刻意只用基本图元(line/polyline/rect/text)，不引第三方绘图库，
以保持项目"纯标准库"身份、便于将来封装 skill / 跨机部署。

四张图对应各层情报:
  price_levels_svg     —— 价格日线 + 关键位点(墙/零伽马/现价)横线
  oi_walls_svg         —— 按行权价的 call/put OI 墙(发散水平条) + 现价
  cot_net_history_svg  —— 投机资金净持仓历史曲线 + 当前分位
  vol_history_svg      —— 波动率指数(GVZ/OVX/VXSLV/VXN)近段曲线 + 均值参照 + 现值
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


def vol_history_svg(pairs: list[tuple[date, float]], *, title: str = "",
                    mean_ref: float | None = None, width: int = 680, height: int = 240,
                    max_points: int = 252) -> str:
    """波动率指数近段曲线 + 近段均值参照虚线 + 现值末点。

    pairs=[(日期, IV值 pp)]（全历史即可，内部截取最近 max_points）。
    mean_ref=展示窗口内的参照均值（画水平虚线，直观看现值相对近段高低）。
    """
    pairs = sorted(pairs)[-max_points:]
    dates = [d for d, _ in pairs]
    vals = [v for _, v in pairs]
    ml, mr, mt, mb = 52, 60, 28, 24
    px0, px1 = ml, width - mr
    py0, py1 = mt, height - mb
    s = [_svg_open(width, height)]
    if title:
        s.append(_txt(12, 16, title, size=13, weight="bold"))
    if len(vals) < 2:
        s.append(_txt(width / 2, height / 2, "波动率历史不足", anchor="middle") + "</svg>")
        return "".join(s)
    yv_all = list(vals) + ([mean_ref] if mean_ref is not None else [])
    ymin, ymax = min(yv_all), max(yv_all)
    pad = (ymax - ymin) * 0.08 or (ymax * 0.02 or 1)
    ymin, ymax = ymin - pad, ymax + pad
    # y 网格 + 标签
    for i in range(4):
        yv = ymin + (ymax - ymin) * i / 3
        yp = _lin(yv, ymin, ymax, py1, py0)
        s.append(_line(px0, yp, px1, yp, stroke=C_GRID))
        s.append(_txt(px0 - 5, yp + 3, f"{yv:.0f}", size=10, fill=C_AXIS, anchor="end"))
    # 近段均值参照（虚线）
    if mean_ref is not None and ymin <= mean_ref <= ymax:
        yp = _lin(mean_ref, ymin, ymax, py1, py0)
        s.append(_line(px0, yp, px1, yp, stroke=C_AXIS, width=1.0, dash="5,3"))
        s.append(_txt(px1 + 4, yp + 3, f"均值 {mean_ref:.1f}", size=10, fill=C_AXIS))
    # 波动率折线
    n = len(vals)
    pts = " ".join(f"{_lin(i,0,n-1,px0,px1):.1f},{_lin(v,ymin,ymax,py1,py0):.1f}"
                   for i, v in enumerate(vals))
    s.append(f'<polyline points="{pts}" fill="none" stroke="{C_PRICE}" stroke-width="1.6"/>')
    # 现值末点
    lx, ly = _lin(n-1, 0, n-1, px0, px1), _lin(vals[-1], ymin, ymax, py1, py0)
    s.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3.2" fill="{C_SPOT}"/>')
    s.append(_txt(lx, ly - 7, f"{vals[-1]:.1f}", size=10, fill=C_SPOT, anchor="end", weight="bold"))
    # x 日期首尾
    for frac, anch in ((0.0, "start"), (1.0, "end")):
        idx = int(frac * (n - 1))
        xp = _lin(idx, 0, n-1, px0, px1)
        s.append(_txt(xp, height - 8, dates[idx].strftime("%y-%m"), size=10, fill=C_AXIS, anchor=anch))
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
                          title: str = "结构时间轴：日K(高低收) vs 零伽马/墙（历史=各日收盘口径；末点(今)=盘中换算，与下方交易票一致）",
                          width: int = 680, height: int = 240) -> str:
    """策略卡时间轴：近 N 日 HLC 竖线 + 逐日零伽马/call墙/put墙折线。

    rows = [(date, close, high, low, flip, call_wall, put_wall)]，高低/结构可为 None。
    价格用 HLC 竖线（无开盘价数据），收盘为右侧短横；结构线为逐日折线（虚线）。
    """
    rows = [r for r in rows if r[1] is not None or r[4] is not None]
    if len(rows) < 2:
        return ""
    ml, mr, mt, mb = 48, 96, 30, 24
    px0, px1 = ml, width - mr
    py0, py1 = mt, height - mb

    yvals: list[float] = []
    for _, c, h, l, f, cw, pw in rows:
        yvals += [v for v in (c, h, l, f, cw, pw) if v]
    has_close = [r[1] is not None for r in rows]
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

    # HLC 竖线 + 收盘短横（价格）；close 为 None 的行 = 今日盘中占位，只画结构线
    for i, (_, c, h, l, *_r) in enumerate(rows):
        if c is None:
            continue
        x = xs[i]
        if h and l:
            s.append(_line(x, Y(h), x, Y(l), stroke=C_PRICE, width=1.4))
        s.append(_line(x, Y(c), x + 4, Y(c), stroke=C_PRICE, width=2.0))

    # 现价点（盘中参考，非收盘）——画在最后一根 x 位（今日）
    if spot:
        s.append(f'<circle cx="{xs[-1]:.1f}" cy="{Y(spot):.1f}" r="3.2" fill="{C_SPOT}"/>')
        s.append(_txt(xs[-1] - 6, Y(spot) - 6, f"现价 {spot:.1f}", size=10, fill=C_SPOT,
                      anchor="end", weight="bold"))

    # 日期标签：≤12 根全标，否则隔根标；今日盘中行标"今"
    step = 1 if n <= 12 else max(1, n // 8)
    for i in range(0, n, step):
        anch = "start" if i == 0 else ("end" if i >= n - 1 else "middle")
        lbl = rows[i][0].strftime("%m-%d") + ("(今)" if not has_close[i] else "")
        s.append(_txt(xs[i], height - 8, lbl, size=9.5, fill=C_AXIS, anchor=anch))
    if (n - 1) % step:
        lbl = rows[-1][0].strftime("%m-%d") + ("(今)" if not has_close[-1] else "")
        s.append(_txt(xs[-1], height - 8, lbl, size=9.5, fill=C_AXIS, anchor="end"))
    s.append("</svg>")
    return "".join(s)


def wall_history_svg(rows: list[dict], *, title: str = "", w: int = 1180,
                     h: int = 520) -> str:
    """墙位历史图：半透明日 K + 收盘折线 + 三道墙 + 极强信号标记。

    用户 2026-08-31 要求放进研报（期权结构下面）：「这个历史墙位图我觉得挺重要…
    每日的现价可以改为半透明的 k 线（不用标涨跌颜色），收盘价继续折线。」

    它回答的是「墙到底稳不稳、被没被破过」——静态的当日墙位表回答不了这个。
    实测差异：≤14 天口径的墙位一天一换（抖动 16~19%），≤45 天口径能连守
    30+ 个交易日；而全到期口径会被深虚建仓顶飞（GLD 8/11 因 470C/460C
    各增 4 万张，墙从 400 跳到 460，而现价才 402）。

    rows 每项：{date, o, h, l, c, topP:[[strike,oi],...], topC:[...], sig:[方向,倍数]|None}
    """
    if not rows:
        return ""
    PL, PR, PT, PB = 62, 178, 46, 62
    iw, ih = w - PL - PR, h - PT - PB
    vals = [r["h"] for r in rows] + [r["l"] for r in rows]
    for r in rows:
        vals += [k for k, _ in r.get("topP", [])] + [k for k, _ in r.get("topC", [])]
    if not vals:
        return ""
    lo, hi = min(vals) - 1, max(vals) + 2
    if hi <= lo:
        return ""
    n = len(rows)
    X = lambda i: PL + iw * i / max(n - 1, 1)          # noqa: E731
    Y = lambda v: PT + ih * (hi - v) / (hi - lo)       # noqa: E731
    bw = max(2.0, iw / n * 0.62)
    P = [_svg_open(w, h)]
    for k in range(7):
        v = lo + (hi - lo) * k / 6
        y = Y(v)
        P.append(_line(PL, y, PL + iw, y, stroke=C_GRID))
        P.append(_txt(PL - 8, y + 4, f"{v:.0f}", size=10, fill="#6e7781", anchor="end"))
    # 半透明日 K —— 不标涨跌色（用户明确要求）：这里要看的是墙与价格的关系，
    # 涨跌颜色会把注意力引到单日方向上
    for i, r in enumerate(rows):
        x = X(i)
        P.append(f'<line x1="{x:.1f}" y1="{Y(r["h"]):.1f}" x2="{x:.1f}" '
                 f'y2="{Y(r["l"]):.1f}" stroke="#57606a" stroke-width="1" opacity=".36"/>')
        y0, y1 = Y(max(r["o"], r["c"])), Y(min(r["o"], r["c"]))
        P.append(f'<rect x="{x - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" '
                 f'height="{max(y1 - y0, 1):.1f}" fill="#57606a" opacity=".2"/>')
    PC = ["#1a7f37", "#3fb950", "#7ee787"]
    CC = ["#cf222e", "#f85149", "#ffa198"]
    for i, r in enumerate(rows):
        x = X(i)
        for rk, (k, _oi) in enumerate(r.get("topP", [])[:3]):
            P.append(f'<line x1="{x - bw / 2 - 1:.1f}" y1="{Y(k):.1f}" '
                     f'x2="{x + bw / 2 + 1:.1f}" y2="{Y(k):.1f}" stroke="{PC[rk]}" '
                     f'stroke-width="{5 - rk * 1.4:.1f}" opacity="{0.95 - rk * 0.24:.2f}"/>')
        for rk, (k, _oi) in enumerate(r.get("topC", [])[:3]):
            P.append(f'<line x1="{x - bw / 2 - 1:.1f}" y1="{Y(k):.1f}" '
                     f'x2="{x + bw / 2 + 1:.1f}" y2="{Y(k):.1f}" stroke="{CC[rk]}" '
                     f'stroke-width="{5 - rk * 1.4:.1f}" opacity="{0.95 - rk * 0.24:.2f}"/>')
    pts = " ".join(f"{X(i):.1f},{Y(r['c']):.1f}" for i, r in enumerate(rows))
    P.append(f'<polyline points="{pts}" fill="none" stroke="#0969da" stroke-width="2.3"/>')
    for i, r in enumerate(rows):
        P.append(f'<circle cx="{X(i):.1f}" cy="{Y(r["c"]):.1f}" r="2.2" fill="#0969da"/>')
    for i, r in enumerate(rows):
        sig = r.get("sig")
        if not sig:
            continue
        side, ratio = sig[0], sig[1]
        x = X(i)
        up = side == "看涨"
        col = "#1a7f37" if up else "#cf222e"
        P.append(f'<line x1="{x:.1f}" y1="{PT}" x2="{x:.1f}" y2="{PT + ih}" '
                 f'stroke="{col}" stroke-width="1" stroke-dasharray="2 4" opacity=".42"/>')
        P.append(f'<text x="{x:.1f}" y="{Y(r["c"]) + (23 if up else -17):.1f}" '
                 f'font-size="10" fill="{col}" text-anchor="middle" font-weight="700">'
                 f'{"▲" if up else "▼"}{ratio:g}×</text>')
    step = max(1, n // 13)
    for i, r in enumerate(rows):
        if i % step == 0 or i == n - 1:
            x = X(i)
            P.append(f'<text x="{x:.1f}" y="{PT + ih + 17}" font-size="9.5" '
                     f'fill="#6e7781" text-anchor="middle" '
                     f'transform="rotate(-42 {x:.1f},{PT + ih + 17})">{r["date"][5:]}</text>')
    lg = [("#0969da", "收盘价", 2.4, "1"), ("#57606a", "日 K", 7, ".22"),
          ("#1a7f37", "put 墙 #1", 5, "1"), ("#3fb950", "put 墙 #2", 3.6, "1"),
          ("#7ee787", "put 墙 #3", 2.2, "1"), ("#cf222e", "call 墙 #1", 5, "1"),
          ("#f85149", "call 墙 #2", 3.6, "1"), ("#ffa198", "call 墙 #3", 2.2, "1")]
    for k, (c, t, lw, op) in enumerate(lg):
        y = PT + 14 + k * 20
        P.append(f'<line x1="{PL + iw + 12}" y1="{y}" x2="{PL + iw + 38}" y2="{y}" '
                 f'stroke="{c}" stroke-width="{lw}" opacity="{op}"/>')
        P.append(_txt(PL + iw + 44, y + 4, t, size=10.5))
    P.append(_txt(PL + iw + 12, PT + 14 + 8 * 20 + 4, "▲▼ 极强信号 ≥10×", size=10, fill="#6e7781"))
    if title:
        P.append(_txt(PL, 24, title, size=13.5, weight="700"))
    P.append("</svg>")
    return "".join(P)
