"""最小 Black-Scholes 工具：gamma 重定价 + 期权理论定价。

为什么要自算：
  * gamma 翻转位：CBOE 给的是"当前现价"下的 gamma；要找 gamma 翻转(零伽马)位，
    必须在不同假设现价 S 下重算各期权 gamma。gamma 对利率/红利不敏感，故用简化 BS。
  * 期权理论价：CBOE 快照只给 OI/delta/iv，没有 bid/ask。策略模块（如铁鹰）要估
    净权利金/盈亏结构，用各腿的 iv 反算 BS 理论中值（实盘按 bid/ask 会略低于此）。
纯标准库（math），不引 numpy。
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)
_SQRT2 = math.sqrt(2.0)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _norm_cdf(x: float) -> float:
    """标准正态累积分布，用 math.erf（纯标准库）。"""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def price(S: float, K: float, T: float, sigma: float, kind: str = "C",
          r: float = 0.04, q: float = 0.0) -> float:
    """欧式期权 BS 理论价（每股）。

    S 现价, K 行权价, T 年化到期时间, sigma 隐含波动率, kind "C"/"P",
    r 无风险利率, q 红利率。到期(T≤0)返回内在价值；无效输入返回 0。
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    disc_s = S * math.exp(-q * T)
    disc_k = K * math.exp(-r * T)
    if kind == "C":
        return disc_s * _norm_cdf(d1) - disc_k * _norm_cdf(d2)
    return disc_k * _norm_cdf(-d2) - disc_s * _norm_cdf(-d1)


def delta(S: float, K: float, T: float, sigma: float, kind: str = "C",
          r: float = 0.04, q: float = 0.0) -> float:
    """期权每股 delta（call 0~1、put −1~0）。

    仅在链上查不到某腿的 delta 时（无 OI 的行权价/到期）用来兜底估算持仓方向敞口。
    到期(T≤0)退化为示性：ITM 返回 ±1、OTM 返回 0；无效输入返回 0。
    """
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        if kind == "C":
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    disc = math.exp(-q * T)
    if kind == "C":
        return disc * _norm_cdf(d1)
    return disc * (_norm_cdf(d1) - 1.0)


def theta(S: float, K: float, T: float, sigma: float, kind: str = "C",
          r: float = 0.04, q: float = 0.0) -> float:
    """每股 theta（**每日**，通常为负=每天损耗）。

    买方结构的核心成本：标的不动时每天亏多少。与 delta 配合可得
    「每天需要标的动多少才打平」= |theta| / delta。
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    disc_s, disc_k = S * math.exp(-q * T), K * math.exp(-r * T)
    term1 = -disc_s * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
    if kind == "C":
        annual = term1 - r * disc_k * _norm_cdf(d2) + q * disc_s * _norm_cdf(d1)
    else:
        annual = term1 + r * disc_k * _norm_cdf(-d2) - q * disc_s * _norm_cdf(-d1)
    return annual / 365.0


def gamma(S: float, K: float, T: float, sigma: float, r: float = 0.04, q: float = 0.0) -> float:
    """期权每股 gamma（call 与 put 相同）。

    S 现价, K 行权价, T 年化到期时间, sigma 隐含波动率, r 无风险利率, q 红利率。
    无效输入（到期/零波动）返回 0。
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    return math.exp(-q * T) * _norm_pdf(d1) / (S * vol_sqrt_t)
