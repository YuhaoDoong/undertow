"""长桥实时报价（**只读**）—— ETF 股价 + 期权价格。

分两级能力，自动优雅降级：
  1) 有 OPRA 期权行情订阅 → 期权 last/IV 可取 → 持仓用【真实市价】估值（比 BS 理论更准）；
  2) 无订阅 / 非长桥 / 只有股票行情 → 只用【实时股价】，期权仍回退 BS 理论值。

长桥期权报价字段：last / prev_close / implied_volatility / open_interest / volume /
timestamp（quote 接口无 bid/ask/greeks —— delta 由 BS + 真实 IV 自算）。
**盘口另走 `depth` 接口**（Level-2 买卖档），见 fetch_depth()。这是唯一的实时盘口来源：
CBOE 虽有 bid/ask 但延迟 15 分钟，且实测会把点差显示得比实际宽一倍以上
（2026-08-26 TQQQ 80C：长桥实时 0.63/0.67 点差 0.04，CBOE 同时刻 0.70/0.79 点差 0.09）。
定限价必须用 depth，用 CBOE 会系统性高估摩擦成本。
股票报价含 overnight / pre_market / post_market → 取最新场次价（修掉"快照收盘价过期"）。

**边界**：只读；不下单。缺订阅时抛 LiveQuotesUnavailable，调用方降级到仅股价。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

BIN = "longbridge"


class LiveQuotesUnavailable(RuntimeError):
    """长桥不可用 / 无期权行情权限（301604）/ 超时。调用方据此降级。"""


def available() -> bool:
    return shutil.which(BIN) is not None


def _run(args: list[str], *, timeout: float = 20.0):
    if not available():
        raise LiveQuotesUnavailable("未找到 longbridge CLI")
    try:
        proc = subprocess.run([BIN, *args, "--format", "json"],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise LiveQuotesUnavailable(f"longbridge {' '.join(args)} 超时") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if "301604" in err or "no quote access" in err.lower():
            raise LiveQuotesUnavailable(f"无行情权限（未订阅）：{err[:160]}")
        raise LiveQuotesUnavailable(f"longbridge {' '.join(args)} 失败：{err[:200]}")
    try:
        # CLI 偶发在 JSON 后追加内容；容错只取第一段
        return json.JSONDecoder().raw_decode(proc.stdout.lstrip())[0]
    except (json.JSONDecodeError, ValueError) as e:
        raise LiveQuotesUnavailable(f"返回非 JSON：{proc.stdout[:160]}") from e


def _f(d: dict, k: str, default: float = 0.0) -> float:
    try:
        v = d.get(k, default)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StockQuote:
    symbol: str
    last: float               # 常规时段最新
    prev_close: float
    freshest: float           # 最新可得价（优先夜盘/盘后/盘前，否则常规）
    freshest_kind: str        # 夜盘 / 盘后 / 盘前 / 常规
    timestamp: str = ""

    @property
    def change_pct(self) -> float:
        return (self.freshest / self.prev_close - 1.0) if self.prev_close else 0.0


@dataclass(frozen=True)
class OptionQuote:
    symbol: str
    last: float
    prev_close: float
    iv: float
    open_interest: int
    volume: int
    timestamp: str = ""


def _freshest(r: dict) -> tuple[float, str]:
    """从股票报价里挑最新场次价：夜盘 > 盘后 > 盘前 > 常规。"""
    last = _f(r, "last")
    for key, label in (("overnight", "夜盘"), ("post_market", "盘后"), ("pre_market", "盘前")):
        sess = r.get(key)
        if isinstance(sess, dict):
            v = _f(sess, "last")
            if v > 0:
                return v, label
    return last, "常规"


def fetch_stock_quotes(symbols: list[str]) -> dict[str, StockQuote]:
    """ETF/股票实时报价。symbol 形如 `SLV.US`。失败抛 LiveQuotesUnavailable。"""
    if not symbols:
        return {}
    rows = _run(["quote", *symbols])
    out: dict[str, StockQuote] = {}
    for r in rows if isinstance(rows, list) else [rows]:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        fresh, kind = _freshest(r)
        out[r["symbol"]] = StockQuote(
            symbol=r["symbol"], last=_f(r, "last"), prev_close=_f(r, "prev_close"),
            freshest=fresh, freshest_kind=kind, timestamp=str(r.get("timestamp", "")))
    return out


def fetch_option_quotes(occ_symbols: list[str]) -> dict[str, OptionQuote]:
    """期权实时报价（需 OPRA 订阅）。无权限抛 LiveQuotesUnavailable，调用方降级到仅股价。"""
    if not occ_symbols:
        return {}
    rows = _run(["option", "quote", *occ_symbols])
    out: dict[str, OptionQuote] = {}
    for r in rows if isinstance(rows, list) else [rows]:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        out[r["symbol"]] = OptionQuote(
            symbol=r["symbol"], last=_f(r, "last"), prev_close=_f(r, "prev_close"),
            iv=_f(r, "implied_volatility"), open_interest=int(_f(r, "open_interest")),
            volume=int(_f(r, "volume")), timestamp=str(r.get("timestamp", "")))
    return out


@dataclass(frozen=True)
class Depth:
    """Level-2 盘口。买/卖档可能为空（该侧无挂单或无权限），调用方须处理 None。"""
    symbol: str
    bid: float | None
    bid_size: int
    ask: float | None
    ask_size: int

    @property
    def mid(self) -> float | None:
        """中价。缺任一边返回 None —— 不允许用 last 冒充中价。"""
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        return (self.ask - self.bid) / m * 100 if m else None


def fetch_depth(symbols: list[str]) -> dict:
    """逐个取实时盘口（depth 接口一次只吃一个代码）。失败的代码直接跳过，不中断整批。"""
    out: dict[str, Depth] = {}
    for sym in symbols:
        try:
            r = _run(["depth", sym])
        except LiveQuotesUnavailable:
            continue
        if not isinstance(r, dict):
            continue
        bids = r.get("bids") or []
        asks = r.get("asks") or []
        b = bids[0] if bids else {}
        a = asks[0] if asks else {}
        out[sym] = Depth(
            symbol=r.get("symbol", sym),
            bid=float(b["price"]) if b.get("price") else None,
            bid_size=int(b.get("volume") or 0),
            ask=float(a["price"]) if a.get("price") else None,
            ask_size=int(a.get("volume") or 0),
        )
    return out
