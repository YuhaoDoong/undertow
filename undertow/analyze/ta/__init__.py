"""技术面子模块包 —— 每个技术指标一个独立子模块。

用户 2026-09-03：「每个技术指标，都是技术面大模块里的一个子模块。」
「我会把 trading view 上比较热门的技术指标复制过来，你看看觉得有用，
  就建立成一个独立的子模块。我们不一定用它，可以先建好保存。」

所以这里的东西**默认不进研报、不进方向投票**。先建好、能取数、有测试锁住口径，
用得上时再单独接线。谁要进研报，必须先过统计检验（见 analyze/validation.py）。

已建子模块
----------
数据层
  frames      多周期 K 线（15m/1h/4h/1d）。⚠️ 15m 只用于择时，不判方向

指标
  macd        MACD，CM_Ult_MacD_MTF 口径。⚠️ signal 用 SMA 非标准 EMA
  supertrend  Supertrend，ATR(10)×3.0，双棘轮轨
  ut_bot      UT Bot Alerts，ATR(10)×1.0，单追踪止损线
  stoch       随机指标 + MTF（linreg 可外推出 [0,100] 之外）
  dmi         DMI/ADX + 趋势评分（⚠️ 评分 75/100 同源）

策略组件
  entries     回调入场 / 裸 DI 交叉 / regime 合成
  exits       吊灯止损 + ADX 衰减 + 冷却期
  risk        三段式 ATR 止损 + 风险仓位

回溯
  backtest    ⚠️ 成交假设焊死：次根开盘 + 必计成本 + always-in-market

⛔ 这一层的最终状态（2026-09-04 封存）
--------------------------------------
**全部检验完毕，没有一个可以接入。备用性质，不再继续挖。**

  · 方向有效性：60 个 (品种,周期,指标) 组合，57 个与「一直做多」无区别，
    3 个「劣于」而 60 次检验期望假阳性正好 3 个 —— 没有任何证据表明有效
    （validation.py: ta_indicators_direction）
  · 作为过滤器：8 个检验全部不显著，方向还不一致
    （validation.py: trend_as_filter）
  · 优化出场：能降回撤但切断厚尾；根本瓶颈在「不在场」——
    Supertrend 只在场 46~58%，而空仓期涨幅在 GLD/USO 上比在场期还多
    （scripts/st_exit_rules.py、st_position_weight.py）

方法论上留下的东西比结论更值钱，都记在 validation.py 的 caveat 里：
统计必须按品种拆、跨周期不能汇总、聚类校正是必须的、
双尾检验要挑对尾巴、基线不是 50%、多重比较要算期望假阳性数。

⚠️ 想重新启用任何一个子模块，先读 validation.py 里那两条的 caveat，
   那里写明了前三版结论是怎么被推翻的。

关键实测结论（都在 docs/ta_modules.md 有完整表）
-------------------------------------------------
· supertrend 与 ut_bot 同族，裸用判方向没有稳定优势：12 个品种×周期组合里
  只有 4 个跑赢买入持有，且多在标的下跌段。可能的正确用法是**当离场线**。
· 成交假设改一下（信号根收盘 → 次根开盘）就翻盘 3 个组合。样本撑不起结论。
· DMI 那套的回调入场与 regime 过滤器**互斥**：regime 要求趋势强，
  而回调本身削弱趋势指标 —— 实测跌破 EMA9 时 regime 仍成立的只有 0~4 次/组合。
· 三份脚本的"多重确认"评分**都是同源重复计数**（60/100、75/100），
  把一个信号数了三遍。这与我们在 SMC vs 期权墙上发现的是同一个毛病。

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

    ⚠️ 种子是 **SMA(n)** 不是首值。
    2026-09-03 codex review 曾提出这里应改为「以第一个有效 src 初始化」，
    向 TradingView 官方说明核实后**不成立**：
    「For the first EMA, we use the SMA(previous day) instead of EMA(previous day)」
    —— https://www.tradingview.com/support/solutions/43000502589-exponential-moving-average/
    保持 SMA 种子。用首值当种子会让前几十根偏离，早期柱状图对不上 TradingView。
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
    """Pine ta.tr。首根无前收，退化为 high−low。

    长度不一致直接抛错 —— zip() 会静默截断到最短，
    悄悄少算几根比报错危险得多。
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError(
            f"OHLC 长度不一致：high={len(highs)} low={len(lows)} close={len(closes)}")
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
    if n <= 0:
        raise ValueError("n 必须为正")
    out: list[float | None] = [None] * min(n - 1, len(xs))
    for i in range(n - 1, len(xs)):
        out.append(max(xs[i - n + 1:i + 1]))
    return out


def lowest(xs: list[float], n: int) -> list[float | None]:
    if n <= 0:
        raise ValueError("n 必须为正")
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
