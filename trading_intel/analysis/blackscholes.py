"""最小 Black-Scholes 工具：仅用于 gamma 重定价（计算 gamma 翻转位）。

为什么要自算：CBOE 给的是"当前现价"下的 gamma；要找 gamma 翻转(零伽马)位，
必须在不同假设现价 S 下重算各期权 gamma。gamma 对利率/红利不敏感，故用简化 BS。
纯标准库（math），不引 numpy。
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


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
