"""指标强度回测 —— 强度到底有没有预测价值。

用户 2026-08-29：「各个指标的强度也可以简单回测一下…把之前的每天数据快照
都重新研报一遍，然后结合当日的涨跌，看指标强度是否有价值。」

⚠️ 三条限制，先说清楚再看结果：
1. **只测近端四组里的连续指标**（增仓/波动/价格）。COT 与宏观是周频/月频，
   现在拉到的是【最新值】而非当时的值，拿它回测就是 lookahead。宁可不测。
2. 收益 = 可交易日 D 当天的 prev_close→close（含隔夜跳空）。
   时点约定：快照 D 描述交易日 D−1，D 开盘才可执行。
3. 统计纪律沿用项目既有的三条：局部去趋势、按品种抽稀不重叠、n≥50 且显著才认。
   达不到就说"样本不足"，不说"无效"。
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.flow import analyze_flow          # noqa: E402
from undertow.analyze.gamma import analyze_gamma        # noqa: E402
from undertow.analyze.stretch import analyze_stretch    # noqa: E402
from undertow.analyze.strength import (                 # noqa: E402
    from_flow, from_vol, from_price, GROUP_W)
from undertow.collect.store import SnapshotStore        # noqa: E402
from undertow.collect.longbridge_kline import fetch_series  # noqa: E402
from undertow.core.config import load_config            # noqa: E402
from undertow.core.models import PriceSeries            # noqa: E402
from undertow.cli import snapshot_from_payload          # noqa: E402


def _prev_weekday(d: date) -> date:
    from datetime import timedelta
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def collect_rows():
    cfg, store = load_config(), SnapshotStore()
    rows = []
    px_cache = {}
    fails: dict = {}      # 失败原因计数 —— 绝不静默吞掉
    for key, inst in cfg.instruments.items():
        if inst.options is None:
            continue
        sym = inst.options.symbol
        dates = store.dates("options", sym)
        if len(dates) < 2:
            continue
        # 价格：长桥日线（与看盘同源）
        if sym not in px_cache:
            try:
                s = fetch_series(f"{sym}.US", period="day", count=400)
                px_cache[sym] = {str(d): c for d, c in zip(s.dates, s.closes)}
                px_cache[sym + "_ser"] = s
            except Exception as e:
                print(f"  [跳过] {sym} 价格不可用: {type(e).__name__}", file=sys.stderr)
                px_cache[sym] = {}
        px = px_cache.get(sym) or {}
        ser = px_cache.get(sym + "_ser")
        if not px:
            continue
        ds = sorted(str(d) for d in px)
        for i in range(1, len(dates)):
            d_prev, d_curr = dates[i - 1], dates[i]
            cs = str(d_curr)
            if cs not in px:
                continue                       # 可交易日无行情（休市）
            j = ds.index(cs)
            if j == 0:
                continue
            ret = (px[cs] / px[ds[j - 1]] - 1.0) * 100.0
            pp = store.load("options", sym, d_prev)
            cp = store.load("options", sym, d_curr)
            if pp is None or cp is None:
                fails["快照缺失"] = fails.get("快照缺失", 0) + 1
                continue
            try:
                # ⚠️ store.load 返回的是 payload dict，不是 OptionsSnapshot。
                # 早先这里直接喂给 analyze_flow，异常被 except 吞掉 → 0 样本，
                # 而脚本照常打印"共 0 个样本"，看起来像"没有数据"而不是"全挂了"。
                prev = snapshot_from_payload(pp, key, sym)
                curr = snapshot_from_payload(cp, key, sym)
                obs = _prev_weekday(d_curr)
                fa = analyze_flow(prev, curr, today=obs,
                                  prev_date=str(d_prev), curr_date=cs)
            except Exception as e:
                fails[type(e).__name__] = fails.get(type(e).__name__, 0) + 1
                continue
            # 价格拉伸：必须只用【严格早于可交易日】的收盘价
            st_read = None
            if ser is not None:
                n = sum(1 for d in ser.dates if str(d) < cs)
                if n >= 60:
                    sub = PriceSeries(symbol=ser.symbol, dates=ser.dates[:n],
                                      closes=ser.closes[:n],
                                      highs=ser.highs[:n] if ser.highs else [],
                                      lows=ser.lows[:n] if ser.lows else [])
                    try:
                        st_read = analyze_stretch(sub)
                    except Exception:
                        st_read = None
            r = {"inst": key, "sym": sym, "date": cs, "ret": ret}
            for f in (from_flow(fa), from_vol(fa), from_price(st_read)):
                if f:
                    r[f.key] = f.signed          # sign × strength ∈ [-1,1]
                    r[f.key + "_raw"] = f.raw
            rows.append(r)
    if fails:
        print("  失败统计：" + "、".join(f"{k}×{v}" for k, v in sorted(fails.items())),
              file=sys.stderr)
    return rows


if __name__ == "__main__":
    rows = collect_rows()
    print(f"共 {len(rows)} 个「品种×可交易日」样本")
    import json
    out = Path("data/history/strength_backtest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")
