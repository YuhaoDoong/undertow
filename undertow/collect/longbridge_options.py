"""长桥期权链采集 —— 验证「能否比 CBOE 早一天拿到数据」。

用户 2026-08-30：「长桥没有实时的吗？理论上收盘的时候拿不到吗」
「能更早拿到数据会是质的飞跃」

## 当前已知（2026-08-30 周日实测，待周一验证）

| 字段 | 长桥 | 我们的 CBOE 快照 | 结论 |
|---|---|---|---|
| open_interest | 19,396 | 8/28 快照同值（=8/27 收盘结算） | **没有领先** |
| volume | 3,369 | 8/28 快照 299（=8/27 成交） | **疑似领先一天** |
| implied_volatility | 有 | 有 | 待比对 |
| delta/gamma/theta/vega | 字段存在但实测为空 | CBOE 有 delta | 长桥这块更弱 |

全链总成交：长桥 740,475（c 418,805 / p 321,670）
vs 最新快照描述 8/27 的 382,200 —— 近两倍，而 8/28 是 GLD −3.24% 的大跌日。

⚠️ **量级吻合不等于确认**。唯一严谨的验证是：把今天拿到的数存下来，
等周一 CBOE 结算到位后，比对「周一快照里描述 8/28 的成交量」是否等于今天长桥给的值。
`verify_lead()` 就是干这个的。

## 即使确认领先一天，能做什么、不能做什么

- ✅ 能做：成交量放大 + IV 方向 → "有事发生 + 谁在推价"
- ❌ 不能做：**ΔOI（持仓变化）仍必须等 OCC 结算** —— 那是市场机制，不是数据源问题。
  没有 ΔOI 就分不清"新开仓"还是"平仓了结"，而我们现有信号的核心正是 ΔOI。
  所以这不等于把现有信号整体提前一天，而是多出一层【当日活跃度】的证据。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

CLI = "longbridge"
STORE = Path("data/snapshots/longbridge_options")


def _run(args: list[str], timeout: float = 90.0):
    """调 CLI 并只解析第一个 JSON 值（CLI 会在后面追加更新提示）。"""
    r = subprocess.run([CLI] + args + ["--format", "json"],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"longbridge {' '.join(args)} 失败：{r.stderr[:200]}")
    txt = r.stdout.lstrip()
    if not txt:
        raise RuntimeError(f"longbridge {' '.join(args)} 无输出")
    return json.JSONDecoder().raw_decode(txt)[0]


def available() -> bool:
    try:
        subprocess.run([CLI, "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def expiries(symbol: str, *, future_only: bool = True) -> list[str]:
    """到期日列表。

    ⚠️ future_only：链里会带【已过期】的到期日（实测 GLD 返回的前三个是
    8/26~8/28，那时已经过期），它们的 strikes 返回格式不同、会 KeyError。
    """
    out = [x["expiry_date"] for x in _run(["option", "chain", symbol])]
    if future_only:
        today = date.today().isoformat()
        out = [e for e in out if e >= today]
    return out


def strikes(symbol: str, expiry: str) -> list[dict]:
    return _run(["option", "chain", symbol, "--date", expiry])


def quotes(occ_symbols: list[str], chunk: int = 40) -> list[dict]:
    """分批取报价 —— 一次塞太多会超时。"""
    out = []
    for i in range(0, len(occ_symbols), chunk):
        try:
            out.extend(_run(["option", "quote"] + occ_symbols[i:i + chunk]))
        except Exception as e:
            print(f"[长桥期权] 第 {i//chunk+1} 批失败：{type(e).__name__}: {e}")
    return out


def total_volume(symbol: str) -> dict:
    """全链 call/put 成交统计。"""
    v = _run(["option", "volume", symbol])
    return {"call": int(v.get("c", 0)), "put": int(v.get("p", 0))}


def snapshot_near(symbol: str, *, band: float = 0.12, max_exp: int = 6) -> dict:
    """近价带 + 近几个到期的链快照。

    ⚠️ 长桥的 `option chain --date` 对【近月】到期直接返回
    call_iv/call_vol/call_last + put_*（一次调用拿全档，快），
    而对较远的到期只返回 call_symbol/put_symbol（要再逐个 quote，慢）。
    实测分界在 9/4 与 9/18 之间。两种都要处理。
    """
    from undertow.collect.longbridge_quote import fetch_stock_quotes
    spot = fetch_stock_quotes([symbol])[symbol].last
    exps = expiries(symbol)[:max_exp]
    rich, need_quote = [], []
    for e in exps:
        for row in strikes(symbol, e):
            k = float(row.get("strike", 0) or 0)
            if not (spot * (1 - band) <= k <= spot * (1 + band)):
                continue
            if "call_vol" in row:                     # 已带数据
                rich.append({"expiry": e, "strike": k,
                             "call_iv": float(row.get("call_iv") or 0),
                             "call_vol": int(row.get("call_vol") or 0),
                             "call_last": float(row.get("call_last") or 0),
                             "put_iv": float(row.get("put_iv") or 0),
                             "put_vol": int(row.get("put_vol") or 0),
                             "put_last": float(row.get("put_last") or 0)})
            elif "call_symbol" in row:                # 只有符号，需再查
                need_quote += [row["call_symbol"], row["put_symbol"]]
    q = quotes(need_quote) if need_quote else []
    return {"symbol": symbol, "spot": spot, "expiries": exps,
            "total_volume": total_volume(symbol),
            "chain": rich, "quotes": q}


def save(payload: dict, on_date: date | None = None) -> Path:
    d = on_date or date.today()
    p = STORE / payload["symbol"] / f"{d.isoformat()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def verify_lead(symbol: str, lb_date: date, cboe_date: date) -> dict:
    """验证长桥是否真的领先一天。

    比对：lb_date 当天存下的长桥成交量 vs cboe_date 快照里【描述同一交易日】的成交量。
    若长桥领先一天，则 lb_date 存的应等于 cboe_date 快照描述的那天。
    """
    from undertow.cli import snapshot_from_payload
    from undertow.collect.store import SnapshotStore
    f = STORE / symbol / f"{lb_date.isoformat()}.json"
    if not f.exists():
        return {"ok": False, "why": f"没有 {lb_date} 的长桥快照"}
    lb = json.loads(f.read_text())
    lbv = {x["symbol"]: x.get("volume") for x in lb["quotes"] if x.get("symbol")}
    p = SnapshotStore().load("options", symbol.split(".")[0], cboe_date)
    if p is None:
        return {"ok": False, "why": f"没有 {cboe_date} 的 CBOE 快照"}
    sn = snapshot_from_payload(p, "", symbol.split(".")[0])
    match = miss = diff = 0
    for c in sn.contracts:
        occ = (f"{symbol.split('.')[0]}{c.expiry.strftime('%y%m%d')}"
               f"{c.kind}{int(c.strike*1000):08d}.US")
        if occ not in lbv:
            miss += 1
            continue
        if lbv[occ] == (c.volume or 0):
            match += 1
        else:
            diff += 1
    return {"ok": True, "matched": match, "different": diff, "not_in_lb": miss,
            "verdict": ("长桥与该快照【同源】" if match > diff * 3
                        else "长桥与该快照【不同】")}
