#!/bin/zsh
# 开盘后点差日志 —— 用来把"避开开盘前 N 分钟"里的 N 从拍脑袋换成实测。
#
# 背景：原规则是"开盘后 30 分钟"，其中两条支柱（CBOE 延迟15分钟、期权IV开盘后才更新）
# 在换长桥实时报价后已失效，只剩"开盘头几分钟点差最宽"。这条要多久才消退，没人测过。
# 本脚本每 5 分钟记一次近月 ATM 附近的 bid/ask 与点差%，攒够 10 个交易日即可回答。
#
# ⚠️ 点差只能从 CBOE 取（长桥实时报价不提供 bid/ask），故记录带 15 分钟延迟——
# 这不影响"点差随时间如何收窄"这个形状，只是横轴要整体左移 15 分钟来读。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="$PWD"
ET_H=$((10#$(TZ=America/New_York date +%H))); ET_M=$((10#$(TZ=America/New_York date +%M)))
MIN=$((ET_H*60+ET_M))
# 只在 ET 09:30–11:00 记录（开盘后 90 分钟足够看清收窄过程）
(( MIN < 570 || MIN > 660 )) && exit 0
"${PYTHON:-python3}" - <<'PY'
import sys, json, pathlib, datetime as dt
sys.path.insert(0, ".")
from undertow.collect.cboe_options import CboeOptionsSource
from undertow.core.config import load_config
ET = dt.timezone(dt.timedelta(hours=-4))
now = dt.datetime.now(ET)
out = pathlib.Path("data/history/spreads"); out.mkdir(parents=True, exist_ok=True)
cfg = load_config()
rows = []
for key in ("qqq", "tqqq", "silver", "gold"):
    inst = cfg.instruments.get(key)
    if inst is None or inst.options is None:
        continue
    try:
        snap = CboeOptionsSource().fetch_snapshot(inst, use_cache=False)
    except Exception as e:
        rows.append({"key": key, "error": str(e)[:80]}); continue
    S = snap.spot
    # 近月：最近的到期；取最接近平值的 call 与 put 各一
    exps = sorted({c.expiry for c in snap.contracts if c.expiry > now.date()})
    if not exps:
        continue
    exp = exps[0]
    for kind in ("C", "P"):
        pool = [c for c in snap.contracts if c.expiry == exp and c.kind == kind and c.mid]
        if not pool:
            continue
        c = min(pool, key=lambda x: abs(x.strike - S))
        rows.append({"key": key, "spot": S, "expiry": exp.isoformat(), "kind": kind,
                     "strike": c.strike, "bid": c.bid, "ask": c.ask,
                     "mid": round(c.mid, 4), "spread_pct": round(c.spread_pct, 2),
                     "bid_size": c.bid_size, "ask_size": c.ask_size,
                     "volume": c.volume, "asof": getattr(snap, "asof", "")})
f = out / f"{now.date().isoformat()}.jsonl"
with f.open("a") as fh:
    fh.write(json.dumps({"et": now.strftime("%H:%M"),
                         "mins_after_open": (now.hour*60+now.minute) - 570,
                         "rows": rows}, ensure_ascii=False) + "\n")
print(f"[{now:%H:%M} ET] 点差记录 {len(rows)} 条 → {f}")
PY
