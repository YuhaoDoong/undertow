"""长桥证券实盘账户接口（**只读**）—— 通过 `longbridge` CLI 包装。

为什么走 CLI 而不是 Python SDK：
  - CLI 已处理 device-flow 鉴权与 token 刷新（token 存 ~/.longbridge/openapi/tokens/），
    脚本侧不碰任何密钥；
  - undertow 铁律是**纯标准库零依赖**，装 `longbridge` PyPI 包会破坏这个性质；
    CLI 只是一个外部二进制，subprocess 调用不引入 pip 依赖。
  - CLI 的 `--format json` 输出字段稳定，专为 agent 设计。

**边界（务必）**：本模块只读取 `positions` / `assets`——**绝不下单、撤单、改单**。
undertow 的定位是研判与复盘，实盘执行永远由用户自己在券商端完成。

**隐私（务必）**：账户持仓/资金/盈亏是敏感数据。调用方落盘一律写 gitignore 的
`data/account/`，**绝不提交进公开仓库**（见仓库 .gitignore）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

BIN = "longbridge"


class LongbridgeUnavailable(RuntimeError):
    """CLI 没装、没登录、或超时。调用方应优雅降级（打印安装/登录提示，不崩）。"""


def available() -> bool:
    return shutil.which(BIN) is not None


def _run(args: list[str], *, timeout: float = 30.0) -> object:
    if not available():
        raise LongbridgeUnavailable(
            "未找到 longbridge CLI。安装：brew install --cask longbridge/tap/longbridge-terminal，"
            "然后 `longbridge auth login` 登录一次。")
    try:
        proc = subprocess.run([BIN, *args, "--format", "json"],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise LongbridgeUnavailable(f"longbridge {' '.join(args)} 超时（{timeout}s）") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if "auth" in err.lower() or "token" in err.lower() or "login" in err.lower():
            raise LongbridgeUnavailable(f"未登录/凭证失效：请先 `longbridge auth login`\n{err[:200]}")
        raise LongbridgeUnavailable(f"longbridge {' '.join(args)} 失败：\n{err[:400]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LongbridgeUnavailable(f"longbridge 返回非 JSON：{proc.stdout[:200]}") from e


def _f(d: dict, k: str, default: float = 0.0) -> float:
    try:
        return float(d.get(k, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RawPosition:
    """券商原样持仓行（未解析期权代码，解析在 analyze/portfolio.py）。"""
    symbol: str            # 长桥格式，如 SLV260826P61000.US（期权）/ AAPL.US（股票）
    name: str              # 人读名，如 "SLV 260826 61 Put"
    quantity: float        # 正=多头/正股；负=空头（如卖出的 put）
    cost_price: float      # 每股/每份成本
    currency: str
    market: str            # US / HK / ...


@dataclass(frozen=True)
class AccountAssets:
    buy_power: float
    net_assets: float
    cash_by_ccy: dict[str, float] = field(default_factory=dict)


def _rows(data: object) -> list[dict]:
    """把 positions 响应规整成 list[dict]。

    长桥两种形态：
      - HK/CN 账户：直接是 [{symbol,name,quantity,...}, ...]
      - US 账户：{account_type, stock_list, option_list, crypto_list, cash_list}
    US 形态把 stock_list + option_list 合并（crypto/cash 不作持仓分析）。
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        out: list[dict] = []
        for key in ("stock_list", "option_list"):
            v = data.get(key)
            if isinstance(v, list):
                out += [r for r in v if isinstance(r, dict)]
        return out
    return []


def fetch_positions() -> list[RawPosition]:
    """当前全部股票+期权持仓（跨子账户）。只读。"""
    data = _run(["positions"])
    out: list[RawPosition] = []
    for r in _rows(data):
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        qty = _f(r, "quantity")
        if qty == 0:
            continue
        out.append(RawPosition(
            symbol=sym,
            name=str(r.get("name") or sym).strip(),
            quantity=qty,
            cost_price=_f(r, "cost_price"),
            currency=str(r.get("currency") or "").strip(),
            market=str(r.get("market") or "").strip(),
        ))
    return out


def fetch_assets() -> AccountAssets:
    """账户资产快照（净资产/购买力/分币种现金）。只读。"""
    data = _run(["assets"])
    row = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    cash = {c["currency"]: _f(c, "available_cash")
            for c in row.get("cash_infos", []) if isinstance(c, dict) and c.get("currency")}
    return AccountAssets(
        buy_power=_f(row, "buy_power"),
        net_assets=_f(row, "net_assets") or _f(row, "total_assets"),
        cash_by_ccy=cash,
    )


# —— 交易流水（原样落盘，供将来历史复盘；不做加工，字段随 CLI）——


def fetch_cash_flow(start: str | None = None, end: str | None = None) -> list[dict]:
    """资金流水（入金/出金/分红/结算/期权买卖/换汇/手续费）。原样返回。

    start/end 缺省=CLI 默认近 30 天。历史复盘要更长窗口就传 start。
    """
    args = ["cash-flow"]
    if start:
        args += ["--start", start]
    if end:
        args += ["--end", end]
    data = _run(args)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def fetch_executions(start: str | None = None, end: str | None = None) -> list[dict]:
    """历史成交（逐笔 fills：order_id/price/quantity/side/symbol/time）。原样返回。

    单笔真实费用明细在 `order detail <id>` 的 charges 里（本函数不逐单展开，
    落盘 order_id 供将来按需拉取校准费率）。
    """
    args = ["order", "executions", "--history"]
    if start:
        args += ["--start", start]
    if end:
        args += ["--end", end]
    data = _run(args)
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
