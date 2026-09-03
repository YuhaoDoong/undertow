"""Smart Money Concepts —— 按 LuxAlgo Pine 源码逐条移植（2026-09-03）。

用户 2026-09-03 提供了 LuxAlgo SMC 的完整 Pine v5 源码
（CC BY-NC-SA 4.0，© LuxAlgo）。本模块是其核心逻辑的 Python 移植，
只取「结构区」部分，不含画图、告警、MTF、溢价折价区。

═══════════════════════════════════════════════════════════════════
存在的理由（用户 2026-09-03）
═══════════════════════════════════════════════════════════════════
「期权结构落后一天，同时容易噪声，而且大多代表大资金和机构的观点，
  他们也有可能错的。希望能引入一些技术分析层面的。」
期权 OI 是前一交易日结算的滞后量，K 线结构是当下的 —— 这是第二视角。

═══════════════════════════════════════════════════════════════════
移植前我的实现错在哪（对照源码逐条纠正）
═══════════════════════════════════════════════════════════════════
1. **用错了对象**：我全程在算 FVG，而源码里 `showFairValueGapsInput` 默认
   **false** —— 图上那些色块是 Internal Order Blocks（默认 true，数量 5）。
2. **订单块定义错**：源码 `storeOrdeBlock` 取的是【摆动点到突破点之间，
   parsedLow 最低（看涨）/ parsedHigh 最高（看跌）那一根】的**完整 high~low**；
   我取的是"最后一根反向 K 线的实体(open~close)"。
3. **缺高波动过滤**：源码有个关键技巧 ——
       highVolatilityBar = (high - low) >= 2 * atr(200)
       parsedHigh = highVolatilityBar ? low : high      # 对调！
       parsedLow  = highVolatilityBar ? high : low
   波幅超过 2 倍 ATR 的 K 线，high/low 互换，使它在 min/max 里永远选不中。
   这样异常放量的长影线不会变成订单块。我完全没有这一层。
4. **摆动检测方式不同**：源码用 `leg()` 状态机 —— 单侧确认（只看右边 size 根）
   加状态切换；我用的是左右各 n 根的双侧 pivot，产出的点位置不同。
5. **失效判定**：源码默认 `HIGHLOW`，用 high/low 越过订单块边界；我用收盘价。

═══════════════════════════════════════════════════════════════════
⛔ 已证伪：不要用它给期权墙加权重（2026-09-03 实测）
═══════════════════════════════════════════════════════════════════
· 墙落在同向结构区的比例 put 68% / call 72%，而同距离的随机非墙行权价是
  62% / 81% —— 墙并不比随机位置更容易重合
· 按缓冲与 DTE 分层后，区内区外破墙率差异小于层内噪声且方向不一致
· 找不到"同日同 DTE、缓冲差 <1pp"的配对（0 对）：
  "落在结构区"与"缓冲小"共线到无法分离
根因：两者不是独立信息源，持仓本来就跟着价格走。同源重合只是重复计数。
**正确用法：独立视角展示，不进任何加权投票。**
"""
from __future__ import annotations

from dataclasses import dataclass

# ── LuxAlgo 默认参数（源码 input 默认值）────────────────────────────
SWING_LENGTH = 50          # swingsLengthInput
INTERNAL_LENGTH = 5        # getCurrentStructure(5, false, true)
OB_COUNT = 5               # internalOrderBlocksSizeInput / swingOrderBlocksSizeInput
ATR_LENGTH = 200           # ta.atr(200)
HIGH_VOL_MULT = 2.0        # (high-low) >= 2 * volatilityMeasure
EQ_LENGTH = 3              # equalHighsLowsLengthInput
EQ_THRESHOLD = 0.1         # equalHighsLowsThresholdInput

BULLISH, BEARISH = 1, -1
BEARISH_LEG, BULLISH_LEG = 0, 1


@dataclass(frozen=True)
class Zone:
    """一个结构区。bias=BULLISH 为需求/支撑，BEARISH 为供给/阻力。"""
    bias: int
    source: str            # "InternalOB" | "SwingOB" | "FVG"
    lo: float
    hi: float
    idx: int

    @property
    def kind(self) -> str:
        return "需求" if self.bias == BULLISH else "供给"

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2

    def contains(self, px: float, tol: float = 0.0) -> bool:
        pad = self.mid * tol
        return self.lo - pad <= px <= self.hi + pad


def _rma(xs: list[float], n: int) -> list[float]:
    """Pine 的 ta.rma（Wilder 平滑），ta.atr 内部用的就是它。"""
    out, acc = [], None
    for i, x in enumerate(xs):
        if acc is None:
            acc = x
        else:
            acc = (acc * (n - 1) + x) / n
        out.append(acc)
    return out


def atr(highs, lows, closes, n: int = ATR_LENGTH) -> list[float]:
    """ta.atr(n)。首根用 high-low。"""
    tr = []
    for i in range(len(highs)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            tr.append(max(highs[i] - lows[i],
                          abs(highs[i] - closes[i - 1]),
                          abs(lows[i] - closes[i - 1])))
    return _rma(tr, n)


def parsed_hl(highs, lows, closes) -> tuple[list[float], list[float]]:
    """源码的 parsedHigh / parsedLow：高波动 K 线把 high 与 low 对调。

        highVolatilityBar = (high - low) >= 2 * atr(200)
        parsedHigh = highVolatilityBar ? low : high
        parsedLow  = highVolatilityBar ? high : low

    对调后该根的 parsedHigh 变得很小、parsedLow 变得很大，
    于是在"取最高/最低"时永远选不中它 —— 这就是过滤异常波动 K 线的手法。
    """
    a = atr(highs, lows, closes)
    ph, pl = [], []
    for i in range(len(highs)):
        hv = (highs[i] - lows[i]) >= HIGH_VOL_MULT * a[i]
        ph.append(lows[i] if hv else highs[i])
        pl.append(highs[i] if hv else lows[i])
    return ph, pl


def legs(highs, lows, size: int) -> list[int]:
    """源码的 leg()。

        newLegHigh = high[size] > ta.highest(size)
        newLegLow  = low[size]  < ta.lowest(size)

    注意这是**单侧**确认：只比较 size 根之前那一根，与其后 size 根的极值。
    左侧不看。leg 是状态量，只在切换时才产生新的摆动点。
    """
    out, leg = [], BEARISH_LEG
    for i in range(len(highs)):
        if i >= size:
            after_h = highs[i - size + 1:i + 1]
            after_l = lows[i - size + 1:i + 1]
            if after_h and highs[i - size] > max(after_h):
                leg = BEARISH_LEG
            elif after_l and lows[i - size] < min(after_l):
                leg = BULLISH_LEG
        out.append(leg)
    return out


def pivots(highs, lows, size: int) -> list[tuple[int, str, float]]:
    """leg 状态切换处产生的摆动点：(索引, 'high'|'low', 价格)。

    源码：pivotLow = startOfBullishLeg（change==+1）→ 记 low[size]
          pivotHigh = startOfBearishLeg（change==-1）→ 记 high[size]
    """
    lg = legs(highs, lows, size)
    out = []
    for i in range(1, len(lg)):
        if lg[i] == lg[i - 1] or i < size:
            continue
        j = i - size
        if lg[i] == BULLISH_LEG:
            out.append((j, "low", lows[j]))
        else:
            out.append((j, "high", highs[j]))
    return out


def order_blocks(opens, highs, lows, closes, *, size: int = INTERNAL_LENGTH,
                 count: int = OB_COUNT, source: str = "InternalOB") -> list[Zone]:
    """按源码 displayStructure + storeOrdeBlock + deleteOrderBlocks 移植。

    流程：
      ① 摆动点由 leg 状态机产生
      ② 收盘价上穿摆动高（crossover）→ 结构突破 → 存**看涨** OB
         收盘价下穿摆动低（crossunder）→ 存**看跌** OB
      ③ OB 区间 = 从摆动点到突破点之间，parsedLow 最低（看涨）/
         parsedHigh 最高（看跌）那一根的完整 (parsedHigh, parsedLow)
      ④ 失效：high 越过看跌 OB 上沿、low 跌破看涨 OB 下沿（默认 HIGHLOW）
      ⑤ 新 OB 插到最前，最后取前 count 个 —— 即**最近的 count 个未失效 OB**
    """
    ph, pl = parsed_hl(highs, lows, closes)
    pv = pivots(highs, lows, size)
    stack: list[Zone] = []          # 最新的在前（对应源码 unshift）
    pv_i = 0
    # 每个摆动点只能被突破一次（源码的 p_ivot.crossed）
    pending: list[tuple[int, str, float, bool]] = []
    for i in range(1, len(closes)):
        while pv_i < len(pv) and pv[pv_i][0] <= i:
            k, w, lvl = pv[pv_i]
            pending.append((k, w, lvl, False))
            pv_i += 1
        for n, (k, w, lvl, crossed) in enumerate(pending):
            if crossed or k >= i:
                continue
            up = closes[i] > lvl and closes[i - 1] <= lvl
            dn = closes[i] < lvl and closes[i - 1] >= lvl
            if w == "high" and up:
                seg = pl[k:i]
                if seg:
                    j = k + seg.index(min(seg))
                    stack.insert(0, Zone(BULLISH, source, pl[j], ph[j], j))
                pending[n] = (k, w, lvl, True)
            elif w == "low" and dn:
                seg = ph[k:i]
                if seg:
                    j = k + seg.index(max(seg))
                    stack.insert(0, Zone(BEARISH, source, pl[j], ph[j], j))
                pending[n] = (k, w, lvl, True)
        # 失效检查（源码 deleteOrderBlocks，默认用 high/low）
        stack = [z for z in stack
                 if not ((z.bias == BEARISH and highs[i] > max(z.lo, z.hi))
                         or (z.bias == BULLISH and lows[i] < min(z.lo, z.hi)))]
        if len(stack) > 100:
            stack = stack[:100]
    # 去重：同一根 K 线可能被多个摆动点选中，源码靠画图覆盖掩盖了这点，
    # 我们要出数值，必须按 (bias, idx) 去重。
    seen, out = set(), []
    for z in stack:
        key = (z.bias, z.idx)
        if key in seen:
            continue
        seen.add(key)
        lo, hi = min(z.lo, z.hi), max(z.lo, z.hi)
        out.append(Zone(z.bias, z.source, lo, hi, z.idx))
        if len(out) >= count:
            break
    return out


def fair_value_gaps(opens, highs, lows, closes, *,
                    auto_threshold: bool = True) -> list[Zone]:
    """源码 drawFairValueGaps。**默认在 LuxAlgo 里是关闭的**，此处仅备用。

        barDeltaPercent = (close[1] - open[1]) / (open[1] * 100)
        threshold = cum(|barDeltaPercent|) / bar_index * 2
        bullish = low[0] > high[2] and close[1] > high[2] and delta > threshold

    注意除了缺口本身，还要求**中间那根的收盘突破**、且其涨跌幅超过
    历史平均的两倍 —— 我先前只做了第一个条件，所以产出了过多 FVG。
    """
    out, acc = [], 0.0
    for i in range(2, len(closes)):
        d = (closes[i - 1] - opens[i - 1]) / (opens[i - 1] * 100) if opens[i - 1] else 0.0
        acc += abs(d)
        thr = (acc / max(i, 1)) * 2 if auto_threshold else 0.0
        if lows[i] > highs[i - 2] and closes[i - 1] > highs[i - 2] and d > thr:
            out.append(Zone(BULLISH, "FVG", highs[i - 2], lows[i], i))
        if highs[i] < lows[i - 2] and closes[i - 1] < lows[i - 2] and -d > thr:
            out.append(Zone(BEARISH, "FVG", highs[i], lows[i - 2], i))
    return out


def equal_highs_lows(highs, lows, closes, *, size: int = EQ_LENGTH,
                     threshold: float = EQ_THRESHOLD) -> list[Zone]:
    """EQH/EQL：相邻同类摆动点差值小于 threshold×ATR 即为等高/等低（流动性池）。"""
    a = atr(highs, lows, closes)
    pv = pivots(highs, lows, size)
    out, last = [], {}
    for j, w, lvl in pv:
        prev = last.get(w)
        if prev is not None and abs(prev[1] - lvl) < threshold * a[j]:
            lo, hi = sorted((prev[1], lvl))
            out.append(Zone(BEARISH if w == "high" else BULLISH,
                            "EQH" if w == "high" else "EQL", lo, hi, j))
        last[w] = (j, lvl)
    return out


def confluence(price: float, zones: list[Zone], *, tol: float = 0.005) -> list[Zone]:
    """某个价格点（如期权墙的行权价）落在哪些结构区里。"""
    return [z for z in zones if z.contains(price, tol)]


def read_4h_zones(symbol: str, *, bars: int = 200, per_side: int = 2,
                  sizes: tuple[int, ...] = (INTERNAL_LENGTH, 20)) -> list[Zone] | None:
    """取该标的 4H 的订单块，**按我们的用法裁剪**（不完全照搬 LuxAlgo 的显示逻辑）。

    与源码的两处有意偏离（用户 2026-09-03：「不用完全照搬，要用最适合我们的」）：

    ① **按方向各取 per_side 个**，而非混在一起取最近 N 个。
       源码 `orderBlocks.slice(0, maxOrderBlocks)` 取的是最近 N 个，
       实测 GLD 上最近 5 个里 4 个是需求区，上方参考位就没了。
       我们要的是"上下各几个参考位"，与找墙取最近三道同构。

    ② **size 用 (5, 20) 而非 (5, 50)**。源码的 Swing length 50 是给
       TradingView 的完整历史用的；我们只有 200 根 4H（约 4 个月），
       size=50 全窗口只产出 1 个摆动点、0 个订单块。实测 size=20 能产出
       5 个摆动点，是这个数据量下的结构级替代。

    ⚠️ 只做展示，不得进入方向投票或给期权墙加权 —— 见文件头"已证伪"。
    """
    try:
        from undertow.collect.longbridge_kline import aggregate, fetch_bars
        b4 = aggregate(fetch_bars(symbol, period="1h", count=1000), 4)
    except Exception:
        return None
    if len(b4) < 60:
        return None
    w = b4[-bars:]
    o = [x["open"] for x in w]; h = [x["high"] for x in w]
    l = [x["low"] for x in w]; c = [x["close"] for x in w]
    pool: list[Zone] = []
    for sz in sizes:
        pool += order_blocks(o, h, l, c, size=sz, count=20,
                             source=f"OB{sz}")
    spot = c[-1]
    out, seen = [], set()
    for bias, key in ((BULLISH, lambda z: -z.hi), (BEARISH, lambda z: z.lo)):
        # 需求区按上沿由近及远、供给区按下沿由近及远
        g = [z for z in pool if z.bias == bias
             and (z.hi <= spot if bias == BULLISH else z.lo >= spot)]
        for z in sorted(g, key=key):
            k = (round(z.lo, 4), round(z.hi, 4))
            if k in seen:
                continue
            seen.add(k)
            out.append(z)
            if sum(1 for x in out if x.bias == bias) >= per_side:
                break
    # 价格正处其中的区单独保留一个 —— 当下的争夺区
    cross = [z for z in pool if z.lo < spot < z.hi]
    if cross:
        z = min(cross, key=lambda x: abs(x.mid - spot))
        if (round(z.lo, 4), round(z.hi, 4)) not in seen:
            out.append(z)
    return sorted(out, key=lambda z: z.mid)
