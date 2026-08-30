#!/bin/zsh
# 验证长桥期权数据的【时效】—— 两个问题，一次测完。
#
# 用户 2026-08-30：
#   「如果长桥能在盘中就拿到数据，比如收盘前 10 分钟敲定策略，那岂不是当天就能布局」
#   「持仓变化只能收盘吗？长桥收盘能否看到持仓变化，而不用等 2 点」
#
# Q1 成交量是否盘中实时？ → 盘中每次采样，看全链 call/put 成交是否单调递增
# Q2 OI 什么时候更新？    → 固定几个探针合约，记录 OI；它跳变的那一刻就是更新时点
#
# 采样窗口 ET 09:00 ~ 次日 04:00（覆盖盘中、盘后、以及我们现在抓 CBOE 的凌晨 02:00），
# 这样能直接比出「长桥比 CBOE 早多少」。
#
# ⚠️ 周末不采（两边都不更新，采了也只是噪声）。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$PWD"
PY="${PYTHON:-python3}"
mkdir -p data/logs

ET_DOW=$(TZ=America/New_York date +%u)
ET_MIN=$(( 10#$(TZ=America/New_York date +%H) * 60 + 10#$(TZ=America/New_York date +%M) ))
# 周六全天、周日 04:00 之后都不采
(( ET_DOW == 6 )) && exit 0
(( ET_DOW == 7 && ET_MIN > 240 )) && exit 0
# 只在 ET 09:00~24:00 与 00:00~04:00 之间采
(( ET_MIN > 240 && ET_MIN < 540 )) && exit 0

"$PY" - <<'PYEOF' >> "data/logs/lb_timing_$(TZ=America/New_York date +%Y-%m-%d).log" 2>&1
import sys, json
sys.path.insert(0, ".")
from datetime import datetime, timezone, timedelta
import undertow.collect.longbridge_options as lo
ET = timezone(timedelta(hours=-4))
now = datetime.now(ET).strftime("%m-%d %H:%M")
# 探针合约：近价、有持仓量的几个。OI 变化的时刻 = 数据更新时刻。
PROBES = ["GLD260918P400000.US", "GLD260918C430000.US", "GLD260918P420000.US",
          "SLV260918C70000.US", "SLV260918P60000.US"]
rec = {"t": now}
try:
    q = {x["symbol"]: x for x in lo.quotes(PROBES)}
    rec["oi"] = {s.split("2609")[0] + s[-11:-3]: q[s].get("open_interest")
                 for s in PROBES if s in q}
    rec["vol"] = {s.split("2609")[0] + s[-11:-3]: q[s].get("volume")
                  for s in PROBES if s in q}
except Exception as e:
    rec["probe_err"] = f"{type(e).__name__}: {e}"
for sym in ("GLD.US", "SLV.US"):
    try:
        rec[sym] = lo.total_volume(sym)
    except Exception as e:
        rec[sym] = f"ERR {type(e).__name__}"
print(json.dumps(rec, ensure_ascii=False))
PYEOF
