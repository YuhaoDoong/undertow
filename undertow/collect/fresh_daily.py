"""盘前决策用的「新鲜 T−1 日线」（Codex 056 F0）：价格、ATR、波动率语境必须全部截止同一个 expected=T−1 交易日。

起因：影子账 v5 capture 取 CBOE 历史日线里「T 之前最后一根」当基准，实测 219 行中 197 行拿到的是 T−2
（10/14 盘前 fresh_fetch 仍只到 10/12）—— 不是前视，但选墙带、离墙距离、ATR 都按晚一天的价算。
v5 冻结不改（只追加新鲜度侧车审计，见 shadow_cli freshness-audit）；新研究账（mainwall）从这里取输入。

规则（固定优先顺序，不拼接两个源）：
1. expected_base_date 只来自预存交易日历 `mc.prev_trading_day(T)`，不从日线倒推。
2. 依次试 CBOE 历史日线（原主源）→ Yahoo chart 同代码 ETF 日线；取第一个「最后一根 = expected 且 H/L/C 齐全」的源，
   整条序列都用该源（价格、ATR、语境同源同 asof），不把 A 源的历史接上 B 源的最后一根。
3. 交叉核对：另一个源（若也有 expected 那根）与期权快照里带日期的前收盘（若有）都和所选收盘比；
   相对差 > CROSS_CHECK_MAX_REL 判 conflict → 状态 incomplete，不用。
4. 缺 H/L 的 bar 不用收盘顶替（会让 ATR 系统性偏小），那一根视为不可用。
5. available_at = 本次所用原文的抓取时刻（缓存文件里的 fetched_at）；调用方负责判断它早于决策冻结时刻。
6. ATR / 语境由分析层（analyze.mainwall.derive_inputs）从返回的同一条序列计算 —— 采集层不 import 分析层。

返回值只描述输入，不做任何交易判断。失败一律 typed（stale / conflict / error / calendar_unknown），不抛给调用方吞掉。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime

from undertow.collect.base import DataSourceError, http_get_json
from undertow.collect.cache import FileCache
from undertow.core import market_calendar as mc
from undertow.core.clock import ET
from undertow.core.models import PriceSeries

VERSION = "fresh-daily-v1-20261017"
SOURCE_ORDER = ("cboe_history", "yahoo_chart")
# 两源 / 快照前收盘的相对差上限。未校准的健全性检查（不是统计阈值）：GLD/SLV 两源同日收盘历史上应一致到分，
# 0.5% 只用来拦「抓到的是另一天 / 另一种复权」这类整根错位。重设需改 VERSION。
CROSS_CHECK_MAX_REL = 0.005
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_TTL_S = 900                  # 有界重抓：盘前每 15 分钟最多一次（daily 每 15 分钟唤醒）
PRICE_BASIS = {"cboe_history": "CBOE delayed_quotes historical：未复权日线 OHLC",
               "yahoo_chart": "Yahoo chart indicators.quote：拆股调整、未做分红调整（adjclose 未用）"}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _cache_meta(cache: FileCache, key: str) -> tuple[float | None, str | None]:
    """缓存文件里的抓取时刻与原文哈希（与 fetch 消费的是同一份字节；文件不存在或坏 → None）。"""
    p = cache._path(key)
    try:
        raw = p.read_bytes()
        return float(json.loads(raw.decode("utf-8"))["fetched_at"]), _sha(raw)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return None, None


def _truncate(ser: PriceSeries, T: date) -> PriceSeries:
    """只留 date < T 的 bar（含 T 当天的盘中半根就是前视）。"""
    n = sum(1 for d in ser.dates if d < T)
    return PriceSeries(symbol=ser.symbol, dates=ser.dates[:n], closes=ser.closes[:n],
                       highs=ser.highs[:n], lows=ser.lows[:n])


def fetch_cboe(inst, cache: FileCache) -> tuple[PriceSeries, dict]:
    from undertow.collect.cboe_history import CboeHistorySource
    ser = CboeHistorySource(cache=cache).fetch_series(inst)
    at, sha = _cache_meta(cache, f"cboehist_{inst.price.symbol}")
    return ser, {"fetched_at": at, "raw_sha": sha}


def parse_yahoo(payload: dict, symbol: str) -> PriceSeries:
    """Yahoo 日线 → PriceSeries。日期按美东归位；任一 H/L/C 缺失或矛盾的 bar 整根丢弃（不拿收盘顶替高低）。"""
    try:
        res = payload["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
    except (KeyError, IndexError, TypeError):
        raise DataSourceError(f"Yahoo {symbol} 返回无 chart 数据")
    rows = {}
    for i, t in enumerate(ts):
        try:
            c, h, lo = (float(q[k][i]) for k in ("close", "high", "low"))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not (lo <= c <= h):
            continue
        d = datetime.fromtimestamp(t, ET).date()
        if d in rows and rows[d] != (c, h, lo):
            raise DataSourceError(f"Yahoo {symbol} 同日 OHLC 冲突（{d}）")
        rows[d] = (c, h, lo)
    ds = sorted(rows)
    return PriceSeries(symbol=symbol, dates=ds, closes=[rows[d][0] for d in ds],
                       highs=[rows[d][1] for d in ds], lows=[rows[d][2] for d in ds])


def fetch_yahoo(inst, cache: FileCache) -> tuple[PriceSeries, dict]:
    sym = inst.price.symbol
    key = f"yahoo_etf_{sym}_2y"
    payload = cache.get(key, YAHOO_TTL_S)
    if payload is None:
        payload = http_get_json(YAHOO_URL.format(symbol=sym), params={"range": "2y", "interval": "1d"},
                                headers={"User-Agent": "Mozilla/5.0"})
        cache.set(key, payload)
    at, sha = _cache_meta(cache, key)
    return parse_yahoo(payload, sym), {"fetched_at": at, "raw_sha": sha}


FETCHERS = {"cboe_history": fetch_cboe, "yahoo_chart": fetch_yahoo}


def _rel(a: float, b: float) -> float:
    return abs(a / b - 1)


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, ET).isoformat() if ts else None


def resolve(inst, T: date, *, cache: FileCache | None = None, fetchers: dict | None = None,
            snapshot_prev_close: dict | None = None) -> tuple[dict, PriceSeries | None]:
    """T 盘前决策可用的日线输入。返回 (记录, 截到 T−1 的序列或 None)。记录可直接落盘（全是 JSON 值）。

    snapshot_prev_close: {"close": x, "date": "YYYY-MM-DD"}（期权快照自带的前收盘，带日期才核对；None 跳过）。
    status: fresh（可用）/ stale（所有源都没到 expected）/ conflict（源间不一致）/ calendar_unknown。
    fresh 只说明 T−1 的 H/L/C 到了且一致；ATR/语境是否截止同日由 analyze.mainwall.derive_inputs 再核。
    """
    cache = cache or FileCache()
    fetchers = fetchers or FETCHERS
    expected = mc.prev_trading_day(T)
    rec = {"version": VERSION, "session": T.isoformat(),
           "expected_base_date": expected.isoformat() if expected else None,
           "status": None, "source": None, "base_date": None, "attempts": [], "cross_check": []}
    if expected is None or mc.is_trading_day(T) is not True:
        rec["status"] = "calendar_unknown"
        return rec, None
    got = {}
    for src in SOURCE_ORDER:
        att = {"source": src, "tried_at": datetime.now(ET).isoformat()}
        try:
            ser, meta = fetchers[src](inst, cache)
        except (DataSourceError, OSError, ValueError) as e:
            att.update(status="error", error=f"{type(e).__name__}: {e}"[:200])
            rec["attempts"].append(att)
            continue
        tr = _truncate(ser, T)
        last = tr.dates[-1] if tr.dates else None
        att.update(meta, available_at=_iso(meta.get("fetched_at")), last_date=last.isoformat() if last else None)
        if last != expected:
            att["status"] = "stale"      # 含「T−1 那根缺 H/L 被解析器整根丢掉」：两个源都不拿收盘顶替高低
        else:
            att["status"] = "fresh"
            got[src] = (tr, meta)
        rec["attempts"].append(att)
    if not got:
        rec["status"] = "stale"
        return rec, None
    src = next(s for s in SOURCE_ORDER if s in got)
    ser, meta = got[src]
    close = ser.closes[-1]
    for other in SOURCE_ORDER:
        if other != src and other in got:
            oc = got[other][0].closes[-1]
            rec["cross_check"].append({"against": other, "close": round(oc, 4), "rel_diff": round(_rel(oc, close), 6),
                                       "ok": _rel(oc, close) <= CROSS_CHECK_MAX_REL})
    if snapshot_prev_close and snapshot_prev_close.get("close") and snapshot_prev_close.get("date") == expected.isoformat():
        sc = float(snapshot_prev_close["close"])
        rec["cross_check"].append({"against": "snapshot_prev_close", "close": round(sc, 4),
                                   "rel_diff": round(_rel(sc, close), 6), "ok": _rel(sc, close) <= CROSS_CHECK_MAX_REL})
    elif snapshot_prev_close:
        rec["cross_check"].append({"against": "snapshot_prev_close", "skipped": "日期缺失或与 expected 不符",
                                   "date": snapshot_prev_close.get("date")})
    rec.update(source=src, base_date=ser.dates[-1].isoformat(), price_basis=PRICE_BASIS[src],
               close=round(close, 4), high=round(ser.highs[-1], 4), low=round(ser.lows[-1], 4),
               available_at=_iso(meta.get("fetched_at")), raw_sha=meta.get("raw_sha"))
    if any(c.get("ok") is False for c in rec["cross_check"]):
        rec["status"] = "conflict"
        return rec, None
    rec["status"] = "fresh"
    return rec, ser
