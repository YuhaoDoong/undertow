"""品种对比值观察 —— 每日记录，不判定。

用户 2026-08-30：「可以，记录一下。这种数据不麻烦，顺手就记了」

## 为什么值得记

金银比 = 两个期权结构的比值。单看一个品种时，墙位告诉你"走到哪会卡住"；
两个品种放一起，各自的墙就隐含了一个**比值区间**。
2026-08-30 实测：GLD 400 / SLV 60（各自最厚那道）隐含金银比 64.0~67.4，
而当日实际值 66.83 —— 正落在区间内。

这个区间到底有没有约束力，**现在完全不知道**。所以先攒数据：
每天记下实际比值、两边墙位、隐含区间、实际值离边缘多远。
攒几个月后才谈得上检验。

## 铁律

- **只记录，不参与任何判定**。不进方向票、不改置信度、不出现在结论里。
- 换算比（期货/ETF）逐日变化且有 2~3% 的波动，必须**同时记下当日比值**，
  否则日后复算会对不上（2026-08-30 用单点换算判错过点位）。
- 失败不阻断主流程，但**必须出声** —— 静默失败是本项目反复栽的坑。
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

STORE = Path("data/history/ratio_watch.json")

# 要跟踪的品种对：(分子, 分母, 名称)
PAIRS = [("gold", "silver", "金银比")]


@dataclass
class RatioRow:
    date: str
    pair: str
    num_key: str
    den_key: str
    num_fut: float | None = None      # 分子的期货价
    den_fut: float | None = None
    ratio: float | None = None        # 实际比值
    num_etf: float | None = None      # 分子的 ETF 收盘
    den_etf: float | None = None
    num_mult: float | None = None     # 当日 期货/ETF 换算比（会变，必须记）
    den_mult: float | None = None
    num_wall: float | None = None     # 分子最厚的持续 put 墙（ETF 口径）
    num_wall_oi: int = 0
    den_wall: float | None = None
    den_wall_oi: int = 0
    implied_lo: float | None = None   # 两墙隐含的比值下界
    implied_hi: float | None = None
    inside: bool | None = None        # 实际比值是否落在隐含区间内
    dist_pct: float | None = None     # 落在区间外时，离最近边缘多远（%）
    note: str = ""


def _persistent_put_wall(snap, today: date, min_dte: int = 7,
                         band: float = 0.15) -> tuple[float | None, int]:
    """现价 ±band 内、剩余到期 ≥min_dte 的最厚 put 档。"""
    lo, hi = snap.spot * (1 - band), snap.spot * (1 + band)
    agg: dict = defaultdict(int)
    for c in snap.contracts:
        if (c.kind == "P" and c.open_interest
                and lo <= c.strike <= hi and (c.expiry - today).days >= min_dte):
            agg[c.strike] += c.open_interest
    if not agg:
        return None, 0
    k, v = max(agg.items(), key=lambda x: x[1])
    return k, v


def build(on_date: date, snaps: dict, futs: dict, etfs: dict,
          mult_range: dict) -> list[RatioRow]:
    """组装当日各对的记录。

    snaps: {品种key: OptionsSnapshot}
    futs / etfs: {品种key: 收盘价}
    mult_range: {品种key: (比值下限, 比值上限)} —— 用近期区间而非单点
    """
    out = []
    obs = on_date
    for a, b, name in PAIRS:
        r = RatioRow(date=on_date.isoformat(), pair=name, num_key=a, den_key=b)
        r.num_fut, r.den_fut = futs.get(a), futs.get(b)
        r.num_etf, r.den_etf = etfs.get(a), etfs.get(b)
        if r.num_fut and r.den_fut:
            r.ratio = round(r.num_fut / r.den_fut, 3)
        if r.num_fut and r.num_etf:
            r.num_mult = round(r.num_fut / r.num_etf, 4)
        if r.den_fut and r.den_etf:
            r.den_mult = round(r.den_fut / r.den_etf, 4)
        for key, pre in ((a, "num"), (b, "den")):
            sn = snaps.get(key)
            if sn is None:
                continue
            k, v = _persistent_put_wall(sn, obs)
            setattr(r, f"{pre}_wall", k)
            setattr(r, f"{pre}_wall_oi", v)
        # 隐含区间：用【换算比区间】而非单点，否则跨一道墙都可能
        ga, gb = mult_range.get(a), mult_range.get(b)
        if r.num_wall and r.den_wall and ga and gb:
            r.implied_lo = round(r.num_wall * ga[0] / (r.den_wall * gb[1]), 2)
            r.implied_hi = round(r.num_wall * ga[1] / (r.den_wall * gb[0]), 2)
            if r.ratio is not None:
                r.inside = r.implied_lo <= r.ratio <= r.implied_hi
                if not r.inside:
                    edge = r.implied_lo if r.ratio < r.implied_lo else r.implied_hi
                    r.dist_pct = round((r.ratio / edge - 1) * 100, 2)
        out.append(r)
    return out


def save(rows: list[RatioRow]) -> int:
    """按日期覆盖写入。返回本次写入行数。"""
    STORE.parent.mkdir(parents=True, exist_ok=True)
    old = []
    if STORE.exists():
        try:
            old = json.loads(STORE.read_text())
        except Exception as e:
            q = STORE.with_suffix(".corrupt")
            STORE.rename(q)
            print(f"[比值观察] 台账损坏（{type(e).__name__}），已隔离为 {q.name}，从空表重建")
            old = []
    keys = {(r["date"], r["pair"]) for r in map(asdict, rows)}
    kept = [r for r in old if (r.get("date"), r.get("pair")) not in keys]
    allrows = kept + [asdict(r) for r in rows]
    allrows.sort(key=lambda r: (r.get("date", ""), r.get("pair", "")))
    STORE.write_text(json.dumps(allrows, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(rows)


def render(rows: list[RatioRow], esc) -> str:
    """index 底部的一行观察记录（不含判断）。"""
    live = [r for r in rows if r.ratio is not None]
    if not live:
        return ""
    parts = []
    for r in live:
        seg = f'<b>{esc(r.pair)} {r.ratio:.2f}</b>'
        if r.implied_lo is not None:
            mark = "区间内" if r.inside else f"区间外 {r.dist_pct:+.1f}%"
            seg += (f'　<span style="color:#6e7781">两边最厚的墙'
                    f'（{r.num_wall:g} / {r.den_wall:g}）隐含 '
                    f'{r.implied_lo:.1f}~{r.implied_hi:.1f}，当前{mark}</span>')
        parts.append(seg)
    return ('<div class="card" style="background:#f6f8fa">'
            '<h2 style="margin:0 0 4px;font-size:14px">📐 品种对比值（只记录，不判定）</h2>'
            + "<br>".join(parts) +
            '<div class="sub" style="margin-top:4px">'
            '两边期权最厚的持续 put 墙，隐含一个比值区间。这个区间有没有约束力'
            '<b>目前完全未知</b> —— 先攒数据，够了再检验。</div></div>')
