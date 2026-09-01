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

⛔ **`open_interest` 盘前不可用于结构分析**（2026-09-01 实测钉死）：
长桥的 OI 盘前给的是【前一天】的值，要到开盘后约 9 分钟才刷新到位。
当天逐分钟采样 + 与 CBOE 快照逐字段核对：

    06:08 ET   CBOE 已有 T−1 结算的新 OI
    09:12 ET   长桥仍是 T−2 的旧值
    09:27 ET   长桥 volume 全部归零（新交易日重置，早于开盘 3 分钟）
    09:39 ET   GLD 的 OI 刷新到位，与 CBOE 当日快照一字不差
               9/18 400P  20,848 → 21,979 ；9/18 430C 128,754 → 127,535
    09:44 ET   SLV 的 OI 才刷新（比 GLD 晚 5 分钟）
               9/18 70C  63,938 → 58,739 ；9/18 60P  17,452 → 17,572

⚠️ **刷新是逐品种的，不是全局一次**。所以没有一个"过了 X 点就都好了"的时刻，
   用之前必须【逐品种】比对，不能因为 GLD 刷新了就假设 SLV 也刷了。
   判据：长桥某品种的 OI == CBOE 当日快照 → 已刷新；
         == CBOE 前一日快照 → 还是旧值，不可用。

即盘前用长桥 OI 会差【整整一个交易日的建仓】，不是"稍旧"。
盘前的结构分析一律走 CBOE 快照；长桥 OI 仅在逐品种确认已刷新后可作交叉验证。
另：`volume` 归零可当作"新交易日数据已切换"的哨兵 —— 比盯 OI 可靠，
OI 冻结时分不清是"还没更新"还是"真的没人交易"，vol 归零是明确的边界信号。

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
    """从股票报价里挑【当前时段】的价。

    ⚠️ 不能用固定优先级。旧版写死「夜盘 > 盘后 > 盘前 > 常规」，
    收盘后是对的，**盘中就完全反了** —— 2026-08-27 ET10:44 实测：
        TQQQ 常规盘 last 72.27，而 freshest 取"夜盘" 72.19（几小时前的残留）
        SLV  常规盘 62.145，freshest 取 61.84，差 0.3
    盘中常规盘正在交易，它才是最新的；夜盘/盘后价是上一时段的历史值。

    改为按美东时间选时段（美股 RTH 09:30-16:00 ET）：
        盘中     → 常规
        非盘中   → 夜盘 > 盘后 > 盘前 > 常规（保持原逻辑）
    取不到 ET 时间时退回旧优先级（保守：宁可用非常规价，也不静默给错时段）。
    """
    last = _f(r, "last")
    rth = False
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York"))
        if et.weekday() < 5:
            hm = et.hour * 60 + et.minute
            rth = 570 <= hm < 960          # 09:30 - 16:00 ET
    except Exception:
        rth = False
    if rth and last > 0:
        return last, "常规"
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
    error: str = ""          # 非空=取数失败（区别于「该侧无挂单」）

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
    """逐个取实时盘口（depth 接口一次只吃一个代码）。

    ⚠️ 失败的代码**不静默跳过**——返回值里会带一个 Depth(bid=None, ask=None, error=...)，
    调用方据此区分「该侧无挂单」与「取数失败」。全部失败则抛 LiveQuotesUnavailable：
    一次都拿不到却返回空字典，会让自动化把「行情全挂」当成「一切正常」。
    （codex review 2026-08-26）
    """
    out: dict[str, Depth] = {}
    errs = 0
    for sym in symbols:
        try:
            r = _run(["depth", sym])
        except LiveQuotesUnavailable as e:
            errs += 1
            out[sym] = Depth(symbol=sym, bid=None, bid_size=0, ask=None, ask_size=0,
                             error=str(e)[:60])
            continue
        if not isinstance(r, dict):
            errs += 1
            out[sym] = Depth(symbol=sym, bid=None, bid_size=0, ask=None, ask_size=0,
                             error="返回格式非预期")
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
    if symbols and errs == len(symbols):
        raise LiveQuotesUnavailable(f"全部 {errs} 个代码的盘口都取不到——不是「没挂单」，是取数失败")
    return out
