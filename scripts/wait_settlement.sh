#!/bin/zsh
# 盯 OCC 隔夜结算落地 —— 一到就抓快照 + 出研报 + 通知。
#
# 由来（2026-08-29 周六 ET 02:12）：8/28 是周五，黄金大跌那天。用户要的正是
# 描述 8/28 交易日的持仓数据，但此刻 OI 尚未结算（oi_change_total==0：
# 所有存活合约一张没动，总量差全来自到期滚出）。历史上两个周六分别在
# ET 07:00 / 08:00 才拿到 —— 但那只是【成功那次】的时刻，不是"必须等到"，
# 所以这里用轮询而不是猜一个时点。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONPATH="$PWD"
PY="${PYTHON:-python3}"
LOG="data/logs/settlement_$(TZ=America/New_York date +%Y-%m-%d).log"
mkdir -p data/logs
DEADLINE=${1:-14}          # 最多盯多少小时
INTERVAL=${2:-1200}        # 轮询间隔秒（默认 20 分钟）
END=$(( $(date +%s) + DEADLINE * 3600 ))

say() { printf '%s ET %s | %s\n' "$(TZ=America/New_York date +%F)" \
        "$(TZ=America/New_York date +%H:%M)" "$1" | tee -a "$LOG"; }

say "开始盯结算（最多 ${DEADLINE}h，每 $((INTERVAL/60)) 分钟一次）"
while (( $(date +%s) < END )); do
  READY=$("$PY" - <<'PYEOF'
import sys; sys.path.insert(0, ".")
# 有几个品种的 OI 已经结算？判据与落盘 dedup 完全一致（oi_change_total>0），
# 不另立标准 —— 两套判据早晚会漂开。
try:
    from undertow.collect.cboe_options import CboeOptionsSource, oi_change_total
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    from undertow.cli import snapshot_from_payload
    cfg, src, store = load_config(), CboeOptionsSource(), SnapshotStore()
    n = 0
    for key, inst in cfg.instruments.items():
        if inst.options is None:
            continue
        sym = inst.options.symbol
        try:
            curr = snapshot_from_payload(src.fetch_raw(inst, use_cache=False), key, sym)
            latest = store.latest("options", sym)
            if not latest or latest[1] is None:
                continue
            prev = snapshot_from_payload(latest[1], key, sym)
            if oi_change_total(prev, curr) > 0:
                n += 1
        except Exception:
            pass          # 单品种失败不影响整体判断，下一轮再试
    print(n)
except Exception as e:
    print(f"ERR:{type(e).__name__}")
PYEOF
)
  if [[ "$READY" == ERR:* ]]; then
    say "检查出错（$READY）—— 继续重试"
  elif [[ "${READY:-0}" -ge 1 ]]; then
    say "✅ $READY 个品种 OI 已结算 —— 开抓"
    if "$PY" -m undertow.cli --no-cache report >> "$LOG" 2>&1; then
      DAY=$(ls -t data/reports/index_*.html 2>/dev/null | head -1 | sed 's/.*index_//;s/\.html//')
      say "✅ 研报已出：可交易日 $DAY"
      /usr/bin/osascript -e "display notification \"周五美盘数据已结算，研报已更新（可交易日 $DAY）\" with title \"📊 结算落地\" sound name \"Glass\"" 2>/dev/null
    else
      say "⚠️ 研报生成失败 —— 见上方日志"
      /usr/bin/osascript -e 'display notification "结算已到但研报生成失败" with title "⚠️ undertow" sound name "Basso"' 2>/dev/null
    fi
    exit 0
  else
    say "尚未结算（0 个品种有 OI 变动）"
  fi
  sleep "$INTERVAL"
done
say "⏰ 到点仍未结算，停止盯守"
/usr/bin/osascript -e 'display notification "盯守到点，OI 仍未结算" with title "⏰ undertow" sound name "Basso"' 2>/dev/null
exit 1
