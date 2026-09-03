"""技术面子模块包 —— 每个技术指标一个独立子模块。

用户 2026-09-03：「每个技术指标，都是技术面大模块里的一个子模块。」
「我会把 trading view 上比较热门的技术指标复制过来，你看看觉得有用，
  就建立成一个独立的子模块。我们不一定用它，可以先建好保存。」

所以这里的东西**默认不进研报、不进方向投票**。先建好、能取数、有测试锁住口径，
用得上时再单独接线。谁要进研报，必须先过统计检验（见 analyze/validation.py）。

已建子模块
----------
frames      多周期 K 线数据层（15m / 1h / 4h / 1d）
macd        MACD，CM_Ult_MacD_MTF 口径（⚠️ signal 用 SMA，非标准 EMA）
supertrend  Supertrend，ATR(10)×3.0，双棘轮轨
ut_bot      UT Bot Alerts，ATR(10)×1.0，单追踪止损线

supertrend 与 ut_bot 是同一族（ATR 追踪止损+状态机），差异在轨道结构与敏感度，
对比表与实测见 docs/ta_modules.md。实测结论：裸用判方向没有稳定优势
（12 个品种×周期组合里只有 3 个跑赢买入持有，且全在标的下跌段），
可能的正确用法是**当离场线**而非开仓方向 —— 未检验，未接线。

规划中
------
· smc.py 目前仍在 analyze/ 下（先前已接研报），后续可平移进来
· 反向导出：把我们自研的指标（墙位、极强信号）写成 Pine 脚本贴到 TradingView 看
  —— 用户 2026-09-03 提出，先记，未做

公共基础函数（Pine 口径）放在本文件，各子模块共用。
"""
from __future__ import annotations


def sma(xs: list[float], n: int) -> list[float | None]:
    """Pine ta.sma。前 n−1 个位置为 None，保持与输入等长便于对齐。"""
    if n <= 0:
        raise ValueError("n 必须为正")
    out: list[float | None] = [None] * min(n - 1, len(xs))
    if len(xs) < n:
        return out
    run = sum(xs[:n])
    out.append(run / n)
    for i in range(n, len(xs)):
        run += xs[i] - xs[i - n]
        out.append(run / n)
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    """Pine ta.ema。

    ⚠️ 种子是 **SMA(n)** 不是首值 —— Pine 的实现是
        ema := na(ema[1]) ? sma(src, n) : alpha*src + (1-alpha)*ema[1]
    用首值当种子会让前几十根偏离，MACD 快慢线之差被放大，早期柱状图对不上 TradingView。
    """
    if n <= 0:
        raise ValueError("n 必须为正")
    out: list[float | None] = [None] * min(n - 1, len(xs))
    if len(xs) < n:
        return out
    a = 2.0 / (n + 1)
    prev = sum(xs[:n]) / n
    out.append(prev)
    for x in xs[n:]:
        prev = a * x + (1 - a) * prev
        out.append(prev)
    return out


def rma(xs: list[float], n: int) -> list[float | None]:
    """Pine ta.rma（Wilder 平滑）。ATR / RSI 内部用的就是它。"""
    if n <= 0:
        raise ValueError("n 必须为正")
    out: list[float | None] = [None] * min(n - 1, len(xs))
    if len(xs) < n:
        return out
    prev = sum(xs[:n]) / n
    out.append(prev)
    for x in xs[n:]:
        prev = (prev * (n - 1) + x) / n
        out.append(prev)
    return out


def true_range(highs: list[float], lows: list[float],
               closes: list[float]) -> list[float]:
    """Pine ta.tr。首根无前收，退化为 high−low。"""
    out = []
    for i in range(len(highs)):
        if i == 0:
            out.append(highs[i] - lows[i])
        else:
            pc = closes[i - 1]
            out.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return out


def atr(highs: list[float], lows: list[float], closes: list[float],
        n: int, *, method: str = "rma") -> list[float | None]:
    """ATR。

    method="rma"  → Pine 的 ta.atr（Wilder 平滑），绝大多数指标用这个
    method="sma"  → sma(tr, n)，Supertrend 脚本里 changeATR=false 时的分支

    两者在波动突变后收敛速度不同：RMA 记忆更长、反应更钝，
    SMA 在窗口滚出极端值时会阶跃。Supertrend 默认走 RMA。
    """
    tr = true_range(highs, lows, closes)
    if method == "rma":
        return rma(tr, n)
    if method == "sma":
        return sma(tr, n)
    raise ValueError(f'method 须为 "rma" 或 "sma"，收到 {method!r}')


def linreg(xs: list[float], n: int, offset: int = 0) -> list[float | None]:
    """Pine ta.linreg —— 最近 n 点最小二乘拟合，取 (n−1−offset) 处的值。

    ⚠️ 它**不是**滞后平均：拟合直线会比均线更早转向。
    这既是它的卖点，也是它容易制造"看起来提前"的假象的原因 ——
    转向早不等于预测对，早转向也早出错。
    """
    if n <= 1:
        raise ValueError("n 必须大于 1")
    out: list[float | None] = [None] * min(n - 1, len(xs))
    if len(xs) < n:
        return out
    sx = n * (n - 1) / 2
    sxx = sum(j * j for j in range(n))
    den = n * sxx - sx * sx
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        sy = sum(w)
        sxy = sum(j * w[j] for j in range(n))
        b = (n * sxy - sx * sy) / den
        a = (sy - b * sx) / n
        out.append(a + b * (n - 1 - offset))
    return out


def highest(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * min(n - 1, len(xs))
    for i in range(n - 1, len(xs)):
        out.append(max(xs[i - n + 1:i + 1]))
    return out


def lowest(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * min(n - 1, len(xs))
    for i in range(n - 1, len(xs)):
        out.append(min(xs[i - n + 1:i + 1]))
    return out


def last_valid(xs: list[float | None]) -> float | None:
    """最后一个非 None 值。"""
    for x in reversed(xs):
        if x is not None:
            return x
    return None
