"""能否【事前】从数据看出「大行情酝酿中」。

用户 2026-08-29：「我们不该预测哪天是事件日，而是通过数据推测酝酿大行情的前夕。
应该可以通过数据看出来吧，不能仅仅通过新闻。」

这一步成不成，决定了「波动率面只在大波动日可信」这个发现能不能变成交易规则。

⚠️ 所有候选特征必须是【可交易日 D 开盘前就已知】的：
   快照 D 在 D 盘前抓到，其 OI 是 D−1 收盘结算，IV 是抓取时刻的延迟报价。
   目标变量是 D 当天的 |涨跌|。特征与目标之间没有时间重叠。
"""
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undertow.analyze.flow import analyze_flow          # noqa: E402
from undertow.cli import snapshot_from_payload          # noqa: E402
from undertow.collect.longbridge_kline import fetch_series  # noqa: E402
from undertow.collect.store import SnapshotStore        # noqa: E402
from undertow.core.config import load_config            # noqa: E402


def _prev_weekday(d):
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def collect():
    cfg, store = load_config(), SnapshotStore()
    rows, fails = [], {}
    for key, inst in cfg.instruments.items():
        if inst.options is None:
            continue
        sym = inst.options.symbol
        dates = store.dates("options", sym)
        if len(dates) < 2:
            continue
        try:
            ser = fetch_series(f"{sym}.US", period="day", count=400)
        except Exception:
            continue
        px = {str(d): c for d, c in zip(ser.dates, ser.closes)}
        ds = sorted(px)
        for i in range(1, len(dates)):
            d_prev, d_curr = dates[i - 1], dates[i]
            cs = str(d_curr)
            if cs not in px:
                continue
            j = ds.index(cs)
            if j == 0:
                continue
            ret = (px[cs] / px[ds[j - 1]] - 1.0) * 100.0
            pp, cp = store.load("options", sym, d_prev), store.load("options", sym, d_curr)
            if pp is None or cp is None:
                continue
            try:
                prev = snapshot_from_payload(pp, key, sym)
                curr = snapshot_from_payload(cp, key, sym)
                fa = analyze_flow(prev, curr, today=_prev_weekday(d_curr),
                                  prev_date=str(d_prev), curr_date=cs)
            except Exception as e:
                fails[type(e).__name__] = fails.get(type(e).__name__, 0) + 1
                continue
            vs = getattr(fa, "vol", None)
            cr = getattr(vs, "curr", None) if vs else None
            if cr is None:
                continue
            adds = [c for c in fa.changes if c.d_oi > 0]
            total_add = sum(c.d_oi for c in adds)
            total_oi = sum(c.open_interest or 0 for c in curr.contracts)
            vol_sum = sum(c.curr_volume or 0 for c in fa.changes)
            rows.append({
                "inst": key, "date": cs, "ret": ret, "absret": abs(ret),
                # —— 事前可知的候选特征 ——
                "atm_iv": getattr(cr, "atm_iv_pp", None),           # IV 绝对水平
                "d_atm": getattr(vs, "d_atm_pp", None),             # IV 昨日变化
                "skew25": getattr(cr, "skew25_pp", None),           # skew 陡峭度
                "d_skew": getattr(vs, "d_skew25_pp", None),
                "add_ratio": (total_add / total_oi) if total_oi else None,  # 增仓占存量比
                "vol_oi": (vol_sum / total_oi) if total_oi else None,       # 成交/OI
                "d_spot": abs(getattr(vs, "d_spot_pct", 0) or 0),   # 昨日已实现波动
            })
    if fails:
        print("  失败：" + "、".join(f"{k}×{v}" for k, v in fails.items()), file=sys.stderr)
    return rows


if __name__ == "__main__":
    rows = collect()
    print(f"共 {len(rows)} 个样本")
    Path("data/history").mkdir(parents=True, exist_ok=True)
    Path("data/history/regime_predict.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→ data/history/regime_predict.json")
