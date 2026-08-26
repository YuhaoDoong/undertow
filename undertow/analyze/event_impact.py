"""事件影响捕捉（纯确定性，无 I/O）——数据/事件落地前后的横截面快照与对比。

**为什么要它**：事件时点的市场状态是**不可再生**的——错过那一刻就永远没了（与期权链快照
同理）。攒够若干次之后，能用**自己的数据**回答："PCE/非农/FOMC 对我的品种到底有多大影响？
IV 塌多少？方向怎么走？" 而不是靠感觉。

用法：事件**落地前**捕一次、**落地后约 10 分钟**再捕一次，然后 compare 看差异。

**已知限制（诚实标注）**：盘前时段 ETF 有报价但**期权 IV 通常不更新**——盘前快照的 IV
会是上一收盘值。对比时会显式标出 iv_stale，避免把陈旧 IV 当成"事件未影响波动率"。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class InstrumentSnap:
    key: str
    display_name: str
    # —— 主信号：期货实时价（≈23 小时交易，数据公布瞬间最真实、最连续）——
    fut_symbol: str = ""
    fut_price: float | None = None
    fut_asof: str = ""
    # —— ETF 各场次（分开记，事后才看得出是哪个场次在动；单一 freshest 会丢信息）——
    spot: float | None = None          # freshest（按 夜盘>盘后>盘前>常规 选）
    spot_kind: str = ""
    etf_regular: float | None = None
    etf_overnight: float | None = None
    etf_pre: float | None = None
    etf_post: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    atm_iv: float | None = None        # ATM 隐含波动率
    iv_stale: bool = False             # 盘前等时段 IV 未更新
    call_wall: float | None = None
    put_wall: float | None = None
    heat_score: int | None = None      # 技术面过热分
    trend: str = ""
    bias: str = ""


@dataclass(frozen=True)
class EventSnapshot:
    label: str                          # 如 "PCE-before" / "PCE-after"
    at: str                             # ISO 时间戳
    phase: str = ""                     # before / after / open / close
    event_name: str = ""
    instruments: list = field(default_factory=list)   # list[InstrumentSnap]
    headlines: list = field(default_factory=list)     # 当时的新闻标题（背景）
    note: str = ""


def _d(a, b):
    return (a - b) if (a is not None and b is not None) else None


def compare(before: EventSnapshot, after: EventSnapshot) -> dict:
    """两次快照的横截面差异。纯函数。"""
    bm = {i.key: i for i in before.instruments}
    rows = []
    for a in after.instruments:
        b = bm.get(a.key)
        if b is None:
            continue
        # 主口径用期货（连续、实时）；无期货再退回 ETF freshest
        use_fut = a.fut_price is not None and b.fut_price is not None
        pb, pa = (b.fut_price, a.fut_price) if use_fut else (b.spot, a.spot)
        dpx = _d(pa, pb)
        rows.append({
            "key": a.key, "display_name": a.display_name,
            "price_source": ("期货 " + (a.fut_symbol or "")) if use_fut else f"ETF({a.spot_kind or '-'})",
            "spot_before": pb, "spot_after": pa,
            "etf_before": b.spot, "etf_after": a.spot,
            "move_pct": (dpx / pb * 100) if (dpx is not None and pb) else None,
            "iv_before": b.atm_iv, "iv_after": a.atm_iv,
            "d_iv_pp": (_d(a.atm_iv, b.atm_iv) * 100) if _d(a.atm_iv, b.atm_iv) is not None else None,
            "iv_stale": b.iv_stale or a.iv_stale,
            "heat_before": b.heat_score, "heat_after": a.heat_score,
        })
    rows.sort(key=lambda r: abs(r["move_pct"] or 0), reverse=True)
    return {"event": after.event_name or before.event_name,
            "before_at": before.at, "after_at": after.at, "rows": rows}


def render_compare_md(cmp: dict) -> str:
    L = [f"# 事件影响：{cmp['event'] or '(未命名)'}", "",
         f"*{cmp['before_at']} → {cmp['after_at']}*", ""]
    if not cmp["rows"]:
        L.append("- 无可对比的品种（两次快照标的不重合）")
        return "\n".join(L)
    L.append("| 品种 | 口径 | 价格前 | 价格后 | 变动 | ATM IV 前 | 后 | ΔIV | 过热分 |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in cmp["rows"]:
        mv = f"{r['move_pct']:+.2f}%" if r["move_pct"] is not None else "—"
        ivb = f"{r['iv_before']*100:.1f}" if r["iv_before"] else "—"
        iva = f"{r['iv_after']*100:.1f}" if r["iv_after"] else "—"
        div = f"{r['d_iv_pp']:+.2f}pp" if r["d_iv_pp"] is not None else "—"
        if r["iv_stale"]:
            div += " ⚠陈旧"
        hb, ha = r["heat_before"], r["heat_after"]
        heat = f"{hb:+d}→{ha:+d}" if (hb is not None and ha is not None) else "—"
        L.append(f"| {r['display_name']} | {r.get('price_source','—')} | {r['spot_before'] or '—'} | "
                 f"{r['spot_after'] or '—'} | **{mv}** | {ivb} | {iva} | {div} | {heat} |")
    L.append("")
    L.append("> ⚠️ **价格以期货为主口径**：期货≈23 小时交易，数据公布瞬间最真实；ETF 盘前/夜盘流动性差、")
    L.append("> 且非盘前时段 `pre_market` 字段会返回上一次的陈旧值。期权 IV 在盘前不更新，标『陈旧』的 ΔIV 不可用。")
    L.append("> 事件影响数据不可再生：攒够若干次后，可用自己的数据统计各类事件对各品种的真实冲击。")
    return "\n".join(L)


def render_snap_md(s: EventSnapshot) -> str:
    L = [f"## {s.label}　{s.at}" + (f"　（{s.event_name}）" if s.event_name else ""), ""]
    L.append("| 品种 | 期货(实时) | ETF常规 | ETF夜盘 | ETF盘前 | 场次 | ATM IV | 过热分 |")
    L.append("|---|---:|---:|---:|---:|---|---:|---:|")
    for i in s.instruments:
        iv = (f"{i.atm_iv*100:.1f}" + (" ⚠陈旧" if i.iv_stale else "")) if i.atm_iv else "—"
        hs = f"{i.heat_score:+d}" if i.heat_score is not None else "—"
        fut = f"{i.fut_price:,.2f}" if i.fut_price is not None else "—"
        L.append(f"| {i.display_name} | **{fut}** | {i.etf_regular or '—'} | {i.etf_overnight or '—'} | "
                 f"{i.etf_pre or '—'} | {i.spot_kind or '—'} | {iv} | {hs} |")
    if s.headlines:
        L.append("")
        L.append("**当时新闻**：")
        for h in s.headlines[:5]:
            L.append(f"- {h}")
    return "\n".join(L)
