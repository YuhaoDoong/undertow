"""长桥期权链数据源 —— CBOE 的**独立备份源**（只读）。

## 为什么需要它

2026-09-22~24：CBOE 的 `delayed_quotes/options` 接口卡在 `2026-09-22T15:59:59`
超过 48 小时，跨了两个交易日。期间 9/23、9/24 两轮管线全部判「与上一交易日
逐行相同」→ 一份快照没落盘、研报缺两天，而 **9/22 收盘那份 OI 被永久跳过**
（源恢复后直接给出 9/23 收盘值）。期权链不可再生，缺一天就是永久少一天。

同期实测：**长桥一直正常更新**。三方比对（2026-09-24 ET 22:18）：

    长桥现在 vs 长桥基线(9/23收盘后)    相同 31.1%   Σ|ΔOI| 30,209  ← 隔天，应该变
    长桥现在 vs CBOE现在(ts 9/24收盘)   相同 96.7%   Σ|ΔOI|     23  ← 同一套数

**结论：长桥不是更快的源，它跟 CBOE 同为隔夜结算口径；它的价值是【独立】**——
两个源不会同时挂。Σ|ΔOI|=23 的残差来自两次采集相隔数分钟，不是口径差异。

## 设计：产出 CBOE 格式的 payload

本模块不引入新的数据契约，而是把长桥的数据**组装成与 CBOE 完全相同的 payload
结构**，于是 `cboe_options.snapshot_from_payload()` 能直接解析，flow / gamma /
signal_ledger 全链路零改动，两个源可以互为替补、落盘文件也同构。

## 字段对照（实测 2026-09-24）

| 字段 | CBOE | 长桥 | 本模块处理 |
|---|---|---|---|
| open_interest | ✓ | ✓ | 直接用 |
| volume | ✓ | ✓ | 直接用 |
| iv | ✓ | ✓ (`implied_volatility`) | 直接用 |
| delta / gamma | ✓ | ✗ | **BS 自算**，见下 |
| bid / ask | ✓ | ✗ (quote 无，需逐个 depth) | 留 0，调用方须知 |
| theta/vega/rho | ✓ | ✗ | 留 0（当前分析不依赖） |

**delta 自算的精度**（用 CBOE 自身数据做闭环检验，SLV 3,639 个合约）：

    全样本      中位误差 0.0064   95% 分位 0.0611
    主翼 |Δ|∈[0.18,0.45]（方向判定实际用的区间）
                中位误差 0.0072   最大 0.2178

中位可忽略，但**主翼最大偏差 0.22 足以把一条腿踢出/拉进主翼**，会给方向判定
带来噪音。误差源是本模块用统一的 r=0.04/q=0，而 CBOE 用它自己的利率与分红假设。
→ 所以本源**只作备份**，CBOE 可用时优先用 CBOE。

## 两个实测约束（写代码前必须知道）

1. **批量上限不稳定**：`option quote` 一次 100 个在 SLV 能过，在 GLD 直接
   `HTTP 500`（代码更长、且含 205 这类深度虚值档）。必须批量 50 + 失败二分降级，
   单个仍失败才记入 bad（**不静默丢弃**）。加降级后 892 个合约零失败。
2. **两步取数**：`chain --date` 给行权价列表（带 IV/last/vol，**无 OI**），
   OI 只能靠 `option quote` 逐批取。SLV 28 个到期 5,064 合约、GLD 32 个到期
   9,188 合约，单品种全链 2~4 分钟。

## 怎么被用上（2026-09-25 接进管线）

`cli.cmd_snapshot` 在 CBOE 判 unchanged 时检查源自报时点，跨 ≥`STALE_SESSIONS`
个交易日仍无新 OI 就自动切到本源。**两个前提缺一不可** —— 只看 unchanged 的话，
每天凌晨早时点（OCC 正常还没结算）都会白跑几十分钟全链。

降级成功后 `--status-file` 的 `fallback_used` 会列出品种，`daily_update.sh` 据此
推送「已切备份源」提醒（不是报错：数据补上了，但 Greeks 精度降了，读研报的人有权知道）。
两个源都拿不到新 OI 时进 `stale_unresolved` —— 那才是真事故。

副产品：有了第二个独立源，「源挂了」与「确实还没结算」第一次可以分辨了。

## 边界

只读。只用 `option chain` / `option quote` 两个子命令，绝不触碰下单接口。
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import date, datetime, timezone

from undertow.collect.base import DataSourceError
from undertow.core.clock import market_today
from undertow.collect.longbridge_quote import (BIN, LiveQuotesUnavailable,
                                               available, fetch_option_quotes,
                                               fetch_stock_quotes)

QUOTE_BATCH = 50          # 实测：100 在 GLD 会 HTTP 500，50 稳定
RISK_FREE = 0.04          # 与 blackscholes 默认一致，便于和 CBOE 对照
_ANNUALIZE = 365.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _greeks(S: float, K: float, T: float, sigma: float, kind: str) -> tuple[float, float]:
    """(delta, gamma)。无效输入返回 (0, 0)。

    ⚠️ 不复用 analyze.blackscholes：collect 层不得依赖 analyze 层（单向依赖，
    见 AGENTS.md 的代码地图）。两处公式必须一致，由 tests/test_no_drift.py 守。
    """
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0, 0.0
    vs = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (RISK_FREE + 0.5 * sigma * sigma) * T) / vs
    delta = _norm_cdf(d1) if kind == "C" else _norm_cdf(d1) - 1.0
    gamma = _norm_pdf(d1) / (S * vs)
    return delta, gamma


def _cli(args: list[str], timeout: float = 30.0):
    """跑一条只读 longbridge 子命令，返回解析后的 JSON。失败抛 DataSourceError。"""
    if not available():
        raise DataSourceError("未找到 longbridge CLI")
    try:
        p = subprocess.run([BIN, *args, "--format", "json"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DataSourceError(f"longbridge {' '.join(args)} 超时") from e
    if p.returncode != 0:
        raise DataSourceError(f"longbridge {' '.join(args)} 失败：{(p.stderr or '')[:200]}")
    try:
        # CLI 会在 JSON 后追加更新提示，用 raw_decode 只取头部对象
        obj, _ = json.JSONDecoder().raw_decode(p.stdout.lstrip())
        return obj
    except ValueError as e:
        raise DataSourceError(f"longbridge 返回非 JSON：{p.stdout[:120]}") from e


def _occ(root: str, expiry: date, kind: str, strike: float) -> str:
    """CBOE/OCC 格式：行权价 ×1000 补零到 8 位。GLD260918P00358000"""
    return f"{root}{expiry:%y%m%d}{kind}{int(round(strike * 1000)):08d}"


def _lb_symbol(root: str, expiry: date, kind: str, strike: float) -> str:
    """长桥格式：行权价 ×1000 **不补零**，带 .US 后缀。

    ⚠️ 与 OCC 的唯一区别就是补零。2026-09-24 第一次比对两边数据时全部落空，
    就是因为拿 OCC 码去查长桥（`C00058000` vs `C58000`）。
    """
    return f"{root}{expiry:%y%m%d}{kind}{int(round(strike * 1000))}.US"


def _robust_quotes(symbols: list[str]) -> tuple[dict, list[str]]:
    """批量取报价，失败二分降级。返回 (成功映射, 失败代码列表)。

    **失败的代码必须回传**，不能静默丢弃 —— 否则「某些行权价查不到」会伪装成
    「这些行权价不存在」，墙位分析会凭空少掉几档。
    """
    out: dict = {}
    bad: list[str] = []
    stack = [symbols[i:i + QUOTE_BATCH] for i in range(0, len(symbols), QUOTE_BATCH)]
    while stack:
        chunk = stack.pop()
        if not chunk:
            continue
        try:
            out.update(fetch_option_quotes(chunk))
        except LiveQuotesUnavailable:
            if len(chunk) == 1:
                bad.append(chunk[0])
                continue
            mid = len(chunk) // 2
            stack.append(chunk[:mid])
            stack.append(chunk[mid:])
    return out, bad


class LongbridgeOptionsSource:
    """长桥期权链 → CBOE 格式 payload。"""

    name = "longbridge_options"

    def fetch_raw(self, instrument, *, use_cache: bool = True,
                  max_expiries: int | None = None) -> dict:
        """与 `CboeOptionsSource.fetch_raw` **同签名**的适配器。

        管线（`cli._save_snapshot_dedup` / `snapshot_from_payload` / flow / gamma）
        只认这一个协议，所以降级切源时上层零改动 —— 换的是对象，不是代码路径。

        `use_cache` 被**故意忽略**：长桥走本地 CLI 实时取数，没有 HTTP 缓存层。
        接受这个参数只为满足协议；静默忽略在这里是正确的，因为缓存缺失不会
        让数据变错，只会变慢。
        """
        if instrument.options is None:
            raise DataSourceError(f"{instrument.key} 未配置 options 数据源")
        return self.fetch_payload(instrument.options.symbol,
                                  max_expiries=max_expiries)

    def fetch_payload(self, symbol: str, *, max_expiries: int | None = None,
                      today: date | None = None) -> dict:
        """拉全链，返回与 CBOE 同构的 payload。

        symbol      标的代码，如 "SLV"（不带 .US）
        max_expiries 只取最近 N 个到期（调试/限流时用）。None = 全部。
        """
        root = symbol.upper()
        us = f"{root}.US"
        # ⚠️ 必须用【美东】交易日，不能用 UTC date：
        # 2026-09-24 ET 22:18 时 UTC 已是 09-25，于是 9/25 到期的合约被算成 T=0，
        # delta 全部归零 —— 深度实值的 P69 本该是 −1.0，实测误差最大 1.0000。
        # 每天有 8 小时（ET 20:00–次日 04:00 / 20:00 起 UTC 跨日）会踩到这个坑。
        today = today or market_today()

        # 标的现价 —— 自算 Greeks 必需
        try:
            q = fetch_stock_quotes([us]).get(us)
        except LiveQuotesUnavailable as e:
            raise DataSourceError(f"{root} 取不到标的现价：{e}") from e
        if not q or q.freshest <= 0:
            raise DataSourceError(f"{root} 标的现价无效")
        spot = q.freshest

        exp_rows = _cli(["option", "chain", us])
        expiries: list[date] = []
        for r in exp_rows if isinstance(exp_rows, list) else []:
            try:
                expiries.append(date.fromisoformat(r["expiry_date"]))
            except (KeyError, ValueError, TypeError):
                continue
        # ⚠️ 必须剔掉【已过期】的到期日：长桥的 chain 仍会返回它们，
        # 而 T<0 会让自算 delta 全变 0，与 CBOE 给的 ±1（到期实值）差出 1.0 —— 
        # 2026-09-24 首测时最大误差正是 1.0000，根因就在这里。
        # 同时也避免把大量请求浪费在对分析毫无意义的已过期合约上。
        expiries = sorted(e for e in expiries if e >= today)
        if max_expiries:
            expiries = expiries[:max_expiries]
        if not expiries:
            raise DataSourceError(f"{root} 无未到期的到期日")

        options: list[dict] = []
        bad_all: list[str] = []
        for exp in expiries:
            try:
                rows = _cli(["option", "chain", us, "--date", exp.isoformat()])
            except DataSourceError:
                continue                       # 单个到期取不到不该拖垮全链
            strikes: list[float] = []
            for r in rows if isinstance(rows, list) else []:
                try:
                    strikes.append(float(r["strike"]))
                except (KeyError, ValueError, TypeError):
                    continue
            if not strikes:
                continue
            syms = [_lb_symbol(root, exp, k, s) for s in strikes for k in ("C", "P")]
            got, bad = _robust_quotes(syms)
            bad_all += bad
            T = max((exp - today).days, 0) / _ANNUALIZE
            for lb_sym, oq in got.items():
                # 从长桥代码还原 kind / strike（它不补零，不能用 parse_occ）
                body = lb_sym.split(".")[0][len(root) + 6:]
                kind, strike = body[0], int(body[1:]) / 1000.0
                iv = oq.iv if oq.iv and oq.iv > 0 else 0.0
                delta, gamma = _greeks(spot, strike, T, iv, kind)
                options.append({
                    "option": _occ(root, exp, kind, strike),
                    "open_interest": oq.open_interest,
                    "volume": oq.volume,
                    "iv": iv,
                    "delta": round(delta, 6),
                    "gamma": round(gamma, 8),
                    # ⚠️ 长桥 quote 不带盘口；留 0 而不是编造。
                    # 调用方若需 bid/ask 必须走 longbridge_quote.fetch_depth（逐个查）。
                    "bid": 0.0, "ask": 0.0, "bid_size": 0, "ask_size": 0,
                    "theta": 0.0, "vega": 0.0, "rho": 0.0,
                    "last_trade_price": oq.last, "prev_day_close": oq.prev_close,
                })
        if not options:
            raise DataSourceError(f"{root} 全链为空")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "_source": self.name,
            "_greeks": "bs_computed",          # 标明 delta/gamma 是自算的，非源给
            "_missing_quote": bad_all,         # 取不到报价的代码，供审计
            "data": {"current_price": spot, "options": options},
        }
