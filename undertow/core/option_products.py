"""期权产品时段与交割类型映射（按根代码；Codex 007：先主池，扩展池保持未认证）。

只写查到官方来源的内容；查不到的字段写 None 并在 UNVERIFIED 里说明，不按常识补。
用途：证明影子账的观察窗（开盘窗 10:00–10:20、收盘窗 = 标的核心收市前 30~15 分钟）落在每个主池品种的
期权交易时段之内。它不改变任何数值政策，所以不进 shadow.CONFIG、不改配置 hash；换内容升 VERSION。
临时停牌由报价状态识别，本表不替代盘口。
"""
from __future__ import annotations

from datetime import date

VERSION = "option-products-20260926-v1"
SOURCES = {
    "nasdaq_hours": {"url": "https://www.nasdaqtrader.com/Trader.aspx?id=optionshours", "read_at": "2026-09-26",
                     "says": "Equity 与 ETF/ETN 期权常规 9:30–16:00 ET；列表内根代码交易至 16:15 ET"},
    "nyse_calendar": {"url": "https://www.nyse.com/trade/hours-calendars", "read_at": "2026-09-26",
                      "says": "2026-12-24 提前收市 13:00，并注明期权 13:15 收市"},
    "cboe_spec": {"url": "https://www.cboe.com/exchange-traded-stock/equity-options-spec/", "read_at": "2026-09-26",
                  "says": "标的一般为 100 股 ETF/ETN（即交割标的份额）"},
}
UNVERIFIED = [
    "行权方式（美式）：Cboe 规格页的该行在读取时未渲染，未取得原文；",
    "到期当日到期系列的收市时刻（16:00 或 16:15）：未取得交易所原文；",
    "2026-11-27 提前收市日 16:15 类品种是否 13:15 收市：NYSE 页面只对 12-24 注明。",
]

# 类别 → 常规收市时刻（ET）。13:00 收市日：16:15 类按 NYSE 12-24 注明为 13:15，16:00 类为 13:00。
CLASSES = {
    "etf_415": {"regular_close": "16:15", "early_close": "13:15", "settlement": "physical_100_shares",
                "source": ["nasdaq_hours", "nyse_calendar", "cboe_spec"]},
    "etf_400": {"regular_close": "16:00", "early_close": "13:00", "settlement": "physical_100_shares",
                "source": ["nasdaq_hours", "cboe_spec"]},
}
# 主池七个 ETF：已按 Nasdaq 列表核对。扩展池（tqqq、个股）未认证：None。
ROOTS = {
    "GLD": "etf_415", "SLV": "etf_415", "QQQ": "etf_415", "TLT": "etf_415", "SPY": "etf_415", "IWM": "etf_415",
    "USO": "etf_400",
    "TQQQ": None,       # 不在 Nasdaq 16:15 列表 → 推断为 16:00，但属扩展池，未做认证
}


def product_class(root: str) -> dict | None:
    c = ROOTS.get(root.upper())
    return None if c is None else CLASSES[c]


def option_close_minutes(root: str, early: bool) -> int | None:
    """该根代码期权的收市分钟（ET）；未认证 → None。"""
    pc = product_class(root)
    if pc is None:
        return None
    t = pc["early_close"] if early else pc["regular_close"]
    return int(t[:2]) * 60 + int(t[3:])
