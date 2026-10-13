"""structure-v0 盲图标注页 v2（用户 2026-10-13 反馈后重做）。

用户原话：「道氏结构形态应该是看趋势反转……要先找到持续破新低，并且反弹不破前高这种趋势」「让我标注更多信息，比如HL，LL，HH等」
「K 线的范围基本不够，你这几个图是日线吗？」

  python3 scripts/structure_label_page_v2.py --out data/backtest/structure-v0/labeling_v2

抽样（只用截图当时可知的量，不看之后走势）：四个情景各抽 —— 下跌延续、下跌后反弹（潜在反转）、上涨延续、上涨后回落（潜在反转）。
日线 8 个标的 × 4 情景（≤2015），每张 250 根（约 1 年）；黄金 4 小时（币安 PAXGUSDT，UTC 分桶，2020-09–2023-12）每情景 2 张，每张 360 根（约 60 天）。
标注：HH / HL / LH / LL、关键前高 / 关键前低、突破点、入场点、失效位，每个点可标「不确定」；另答前序趋势、当前阶段、反转顺序、形态、此刻会怎么做。
标注存浏览器本地，导出 JSON 交回。只作认点标注，不是收益真值；PAXG 只是 4 小时代理沙箱。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import structure_patterns as sp  # noqa: E402

SEED = 20261015
DAILY = ("GLD", "SLV", "GC=F", "SI=F", "SPY", "QQQ", "AAPL", "GOOGL")
WIN = {"1d": 250, "4h": 360}
PAXG_END = datetime(2024, 1, 1, tzinfo=timezone.utc)
# 用户 10/13：「4h 线有的话，也要加进来」→ Yahoo 1 小时线（约 730 天：期货 2024-05 起、ETF 2023-11 起）合成 4 小时：
# 期货按 UTC 每 4 小时分桶（与 PAXG 同相位）；ETF 每个交易日两根（9:30–13:30、13:30–16:00 ET）。只取完整的 1 小时线，截至 2026-10-10 00:00 UTC。
YAHOO_4H = ("GC=F", "SI=F", "GLD", "SLV", "SPY", "QQQ")
YAHOO_END = datetime(2026, 10, 10, tzinfo=timezone.utc)
INPUTS = ROOT / "data/backtest/structure-v0/inputs"
SCEN = {"down_cont": "下跌延续", "down_bounce": "下跌后反弹（潜在反转）", "up_cont": "上涨延续", "up_pullback": "上涨后回落（潜在反转）"}


def paxg_4h() -> tuple[list, str]:
    f = INPUTS / "PAXGUSDT_4h_2020-08-28_2023-12-31.json"
    if not f.exists():
        from undertow.collect.base import http_get_json
        rows, start = [], 1598572800000
        end = int(PAXG_END.timestamp() * 1000) - 1
        while start < end:
            d = http_get_json("https://api.binance.com/api/v3/klines",
                              {"symbol": "PAXGUSDT", "interval": "4h", "startTime": start, "endTime": end, "limit": 1000}, timeout=30)
            if not d:
                break
            rows += d
            start = d[-1][0] + 1
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rows), "utf-8")
    raw = f.read_bytes()
    rows = json.loads(raw)
    bars = [(datetime.fromtimestamp(r[0] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M"),
             float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows if r[6] < PAXG_END.timestamp() * 1000]
    return bars, hashlib.sha256(raw).hexdigest()


def yahoo_4h(sym: str) -> tuple[list, str]:
    from zoneinfo import ZoneInfo
    f = INPUTS / f"{sym.replace('=', '_')}_1h_730d.json"
    if not f.exists():
        from undertow.collect.base import http_get_json
        d = http_get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"range": "730d", "interval": "1h"}, timeout=60)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d, sort_keys=True), "utf-8")
    raw = f.read_bytes()
    r = json.loads(raw)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    et = ZoneInfo("America/New_York")
    hours = []
    for k, ts in enumerate(r["timestamp"]):
        t = datetime.fromtimestamp(ts, timezone.utc)
        row = (q["open"][k], q["high"][k], q["low"][k], q["close"][k])
        if any(x is None for x in row) or t + timedelta(hours=1) > YAHOO_END:          # 只要完整的 1 小时线
            continue
        hours.append((t, *row))
    groups: dict = {}
    for t, o, h, l, c in hours:
        if sym in pe.FUTURES:
            key = t.replace(hour=t.hour - t.hour % 4, minute=0)
            label = key.strftime("%Y-%m-%d %H:%M")
        else:
            e = t.astimezone(et)
            key = (e.date(), 0 if (e.hour, e.minute) < (13, 30) else 1)
            label = f"{e.date()} {'09:30' if key[1] == 0 else '13:30'}ET"
        groups.setdefault(key, [label, []])[1].append((o, h, l, c))
    bars = []
    for key in sorted(groups):
        label, xs = groups[key]
        bars.append((label, xs[0][0], max(x[1] for x in xs), min(x[2] for x in xs), xs[-1][3]))
    return bars, hashlib.sha256(raw).hexdigest()


def scenario(bars, atr, zz, i) -> str | None:
    """截图当时可知的情景。lo/hi = 近 60 根最低 / 最高；背景 = 2ATR 收盘之字形最近两高两低。"""
    A = atr[i]
    if A is None or i < 80:
        return None
    lo60 = min(b[3] for b in bars[i - 59:i + 1]); hi60 = max(b[2] for b in bars[i - 59:i + 1])
    c = bars[i][4]
    now = sp._bg_state([p for p in zz if p.conf <= i])
    before = sp._bg_state([p for p in zz if p.conf <= i - 20])
    if now == "down" and c <= lo60 + A:
        return "down_cont"
    if before == "down" and c >= lo60 + 2 * A:
        return "down_bounce"
    if now == "up" and c >= hi60 - A:
        return "up_cont"
    if before == "up" and c <= hi60 - 2 * A:
        return "up_pullback"
    return None


def pools(bars, win, step):
    atr = ps.wilder_atr(bars)
    zz = ps.zigzag(bars, atr)
    out = {k: [] for k in SCEN}
    for i in range(max(win, 700 if step == 5 else win), len(bars), step):
        s = scenario(bars, atr, zz, i)
        if s:
            out[s].append(i)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    rng = random.Random(SEED)
    samples, series = [], {}
    for sym in DAILY:
        d, sha = pe.fetch(sym)
        bars = [(str(b[0]), *b[1:]) for b in pe.parse(d)[0]]
        series[sym] = bars
        pl = pools(bars, WIN["1d"], 5)
        for sc in SCEN:
            i = rng.choice(pl[sc])
            samples.append({"symbol": sym, "tf": "1d", "prefix_index": i, "prefix_end": bars[i][0], "scenario": sc, "input_sha256": sha})
    bars, sha = paxg_4h()
    series["PAXG"] = bars
    pl = pools(bars, WIN["4h"], 6)
    for sc in SCEN:
        for i in rng.sample(pl[sc], 2):
            samples.append({"symbol": "PAXG", "tf": "4h", "prefix_index": i, "prefix_end": bars[i][0], "scenario": sc, "input_sha256": sha})
    skipped = []
    for sym in YAHOO_4H:
        bars, sha = yahoo_4h(sym)
        key = f"{sym}·4h"
        series[key] = bars
        pl = pools(bars, WIN["4h"], 3)
        for sc in SCEN:
            if not pl[sc]:
                skipped.append(f"{sym} {sc}"); continue
            i = rng.choice(pl[sc])
            samples.append({"symbol": sym, "series": key, "tf": "4h", "prefix_index": i, "prefix_end": bars[i][0], "scenario": sc,
                            "input_sha256": sha, "source": "Yahoo 1h 合成 4h"})
    rng.shuffle(samples)
    n_disc = round(len(samples) * 0.7)
    for k, m in enumerate(samples, 1):
        m["id"], m["role"] = k, ("讨论" if k <= n_disc else "暂留核对")
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps({"seed": SEED, "window": WIN, "scenarios": SCEN, "skipped_no_candidate": skipped, "samples": samples},
                                                   ensure_ascii=False, indent=1), "utf-8")
    data = []
    for m in samples:
        b = series[m.get("series", m["symbol"])]
        w = WIN[m["tf"]]
        i = m["prefix_index"]
        data.append({"id": m["id"], "symbol": m["symbol"], "tf": m["tf"], "prefix_end": m["prefix_end"], "role": m["role"],
                     "bars": [[x[0], round(x[1], 4), round(x[2], 4), round(x[3], 4), round(x[4], 4)] for x in b[i - w + 1:i + 1]]})
    page = (ROOT / "scripts/structure_label_page_v2.html").read_text("utf-8").replace("__DATA__", json.dumps(data, ensure_ascii=False))
    (out / "index.html").write_text(page, "utf-8")
    from collections import Counter
    print(f"{len(samples)} 张（4 小时 {sum(1 for x in samples if x['tf'] == '4h')}）→ {(out / 'index.html').relative_to(ROOT)}；"
          f"情景 {dict(Counter(s['scenario'] for s in samples))}；无候选 {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
