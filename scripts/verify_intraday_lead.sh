#!/bin/zsh
# 验证长桥期权数据是否【盘中实时】—— 决定能否当天收盘前布局。
#
# 用户 2026-08-30：「如果长桥能在盘中就拿到数据，比如收盘前 10 分钟拿到数据，
# 敲定策略，那岂不是当天就能布局了。这是本质区别。」
#
# 做法：美股时段内每 30 分钟采一次样，把全链 call/put 成交量记下来。
#   · 若数字随时间【单调递增】→ 盘中实时，收盘前就能拿到当天的完整成交
#   · 若整天不变 → 只是昨日收盘的静态值，没有盘中价值
#
# ⚠️ OI 不在验证范围内：OCC 每日结算，盘中必然不变，那是市场机制不是数据源问题。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$PWD"
PY="${PYTHON:-python3}"
LOG="data/logs/lb_intraday_$(TZ=America/New_York date +%Y-%m-%d).log"
mkdir -p data/logs

ET_MIN=$(( 10#$(TZ=America/New_York date +%H) * 60 + 10#$(TZ=America/New_York date +%M) ))
ET_DOW=$(TZ=America/New_York date +%u)
(( ET_DOW >= 6 )) && exit 0                      # 周末不采
(( ET_MIN < 565 || ET_MIN > 965 )) && exit 0     # 只在 ET09:25~16:05 采

for SYM in GLD.US SLV.US QQQ.US; do
  "$PY" - "$SYM" <<'PYEOF' >> "$LOG" 2>&1
import sys, json
sys.path.insert(0, ".")
from datetime import datetime, timezone, timedelta
import undertow.collect.longbridge_options as lo
ET = timezone(timedelta(hours=-4))
sym = sys.argv[1]
try:
    tv = lo.total_volume(sym)
    print(json.dumps({"t": datetime.now(ET).strftime("%H:%M"), "sym": sym,
                      "call": tv["call"], "put": tv["put"]}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"t": datetime.now(ET).strftime("%H:%M"), "sym": sym,
                      "err": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
PYEOF
done
