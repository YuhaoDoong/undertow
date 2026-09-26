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
import math
import shutil
import subprocess
from dataclasses import dataclass, field

BIN = "longbridge"


class LongbridgeUnavailable(RuntimeError):
    """CLI 没装、没登录、或超时。调用方应优雅降级（打印安装/登录提示，不崩）。"""


class AccountDataError(LongbridgeUnavailable):
    """券商返回了数据，但不符合认证格式：未知 schema、关键字段缺失、非数值或非有限数（Codex 008 G01）。

    继承 LongbridgeUnavailable：现有调用方的降级分支会把它当作「没读成」报出来，
    而不是像旧实现那样把坏数据解析成空列表 → 「账户当前无持仓」。
    """


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


def _num(d: dict, k: str, *, where: str) -> float:
    """必填数值字段：缺失、空串、非数值、NaN/Inf 一律 AccountDataError，不默认 0。"""
    v = d.get(k)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise AccountDataError(f"{where}：字段 {k} 缺失")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise AccountDataError(f"{where}：字段 {k} 不是数值（{str(v)[:40]!r}）") from None
    if not math.isfinite(x):
        raise AccountDataError(f"{where}：字段 {k} 非有限数（{x}）")
    return x


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

    旧实现对未知形态返回 []（→「无持仓」），坏行静默丢弃。现在只接受这两种认证形态：
    列表里每行必须是对象；对象形态必须至少含一个已知键，且出现的 stock_list/option_list 必须是列表。
    合法的空列表仍是「空仓」，与坏数据分开。
    """
    if isinstance(data, list):
        bad = [type(r).__name__ for r in data if not isinstance(r, dict)]
        if bad:
            raise AccountDataError(f"positions：列表含 {len(bad)} 个非对象行（{bad[0]}）")
        return list(data)
    if isinstance(data, dict):
        known = ("account_type", "stock_list", "option_list", "crypto_list", "cash_list")
        if not any(k in data for k in known):
            raise AccountDataError(f"positions：未知响应形态，键为 {sorted(map(str, data))[:8]}")
        out: list[dict] = []
        for key in ("stock_list", "option_list"):
            v = data.get(key)
            if v is None:
                continue
            if not isinstance(v, list):
                raise AccountDataError(f"positions：{key} 不是列表（{type(v).__name__}）")
            bad = [r for r in v if not isinstance(r, dict)]
            if bad:
                raise AccountDataError(f"positions：{key} 含 {len(bad)} 个非对象行")
            out += v
        return out
    raise AccountDataError(f"positions：响应类型为 {type(data).__name__}，不是列表或对象")


def fetch_positions() -> list[RawPosition]:
    """当前全部股票+期权持仓（跨子账户）。只读。"""
    data = _run(["positions"])
    out: list[RawPosition] = []
    for i, r in enumerate(_rows(data)):
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            raise AccountDataError(f"positions 第 {i} 行：symbol 缺失")
        qty = _num(r, "quantity", where=f"positions {sym}")
        if qty == 0:
            continue                        # 已解析出的 0 = 券商列出的已平仓行，合法跳过
        out.append(RawPosition(
            symbol=sym,
            name=str(r.get("name") or sym).strip(),
            quantity=qty,
            cost_price=_num(r, "cost_price", where=f"positions {sym}"),
            currency=str(r.get("currency") or "").strip(),
            market=str(r.get("market") or "").strip(),
        ))
    return out


def fetch_assets() -> AccountAssets:
    """账户资产快照（净资产/购买力/分币种现金）。只读。"""
    data = _run(["assets"])
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]
    elif isinstance(data, dict):
        row = data
    else:
        raise AccountDataError(f"assets：响应为空或形态未知（{type(data).__name__}）")
    infos = row.get("cash_infos", [])
    if not isinstance(infos, list):
        raise AccountDataError("assets：cash_infos 不是列表")
    cash = {}
    for c in infos:
        if not isinstance(c, dict) or not c.get("currency"):
            raise AccountDataError("assets：cash_infos 含无币种的行")
        cash[str(c["currency"])] = _num(c, "available_cash", where=f"assets 现金 {c['currency']}")
    # ⚠️ 旧实现 `net_assets or total_assets`：净资产恰为 0（本账户就曾接近 0）时被替换成总资产。
    # 净资产 0 是合法值，而且正是最需要告警的时刻；缺失则报错，不拿别的字段顶替。
    return AccountAssets(
        buy_power=_num(row, "buy_power", where="assets"),
        net_assets=_num(row, "net_assets", where="assets"),
        cash_by_ccy=cash,
    )


# —— 交易流水（原样落盘，供将来历史复盘；不做加工，字段随 CLI）——


def _raw_list(data: object, what: str) -> list[dict]:
    """原样列表：非列表不再静默变成 []（那会被读成「这段时间没有流水」）。"""
    if not isinstance(data, list):
        raise AccountDataError(f"{what}：响应类型为 {type(data).__name__}，不是列表")
    bad = [r for r in data if not isinstance(r, dict)]
    if bad:
        raise AccountDataError(f"{what}：含 {len(bad)} 个非对象行")
    return list(data)


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
    return _raw_list(data, "原样流水")


def fetch_today_executions() -> list[dict]:
    """当日成交（非 history 接口——历史接口不含当天）。原样返回。"""
    data = _run(["order", "executions"])
    return _raw_list(data, "原样流水")


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
    return _raw_list(data, "原样流水")
