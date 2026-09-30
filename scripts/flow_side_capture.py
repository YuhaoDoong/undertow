"""资金流买卖方推断的验证数据：收盘后抓【当天】金银近价合约的逐分钟成交（longbridge intraday，不占历史 K 线配额）。

用户 2026-09-29：「资金流分歧怎么验证？……买卖方向看不到吗？」→「1 做一下。」
协议：docs/prereg/2026-09-29_flow_side_check_v0.md。

  python3 scripts/flow_side_capture.py [gold silver]        # ET 16:05 之后、当日 ET 结束之前运行

范围（事前固定）：前一交易日认证快照里的合约，行权价在现价 ±5% 内，到期在 45 天内且为季度/月度/周五（Q/M/W）类型，call 与 put 都取。
写入 data/history/option_intraday/（与 ⑩ 同一存储、同一质量标签与终态规则；共用 ⑩ 的文件锁，不与它并发）。
只读行情、从不下单。
"""
from __future__ import annotations

import fcntl
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
BAND, MAX_DTE, TYPES = 0.05, 45, ("Q", "M", "W")
PACE_S = 0.15


def plan(inst_key: str, day) -> tuple[str, list]:
    from undertow.analyze.expiry_type import classify
    from undertow.collect.cboe_options import snapshot_from_payload
    from undertow.collect.longbridge_bars import option_symbol
    from undertow.collect.store import SnapshotStore
    from undertow.core.config import load_config
    from undertow.dirledger_cli import session_index
    cfg, s = load_config(), SnapshotStore()
    inst = cfg.instruments[inst_key]
    root = inst.options.symbol
    snap = snapshot_from_payload(s.load("options", root, session_index(s, root)[day]), inst_key, root)
    spot = snap.spot
    syms = sorted({option_symbol(root, c.expiry.isoformat(), c.kind, c.strike) for c in snap.contracts
                   if abs(c.strike / spot - 1) <= BAND and 0 <= (c.expiry - day).days <= MAX_DTE
                   and classify(c.expiry)["type"] in TYPES})
    return root, syms


def main():
    from undertow.collect import longbridge_bars as lbb
    from undertow.core import market_calendar as mc
    from undertow.core.clock import market_today
    from undertow.shadow_cli import INTRADAY_LOCK
    insts = sys.argv[1:] or ["gold", "silver"]
    day = market_today()
    now = datetime.now(ET)
    if mc.is_trading_day(day) is not True or (now.hour, now.minute) < (16, 5):
        print(f"{day} ET {now:%H:%M}：非交易日或未到 16:05，不抓。"); return 0
    INTRADAY_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(INTRADAY_LOCK, "a+") as lk:
        try:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("⑩ 当天逐分钟仍在运行（锁被占），本次跳过。"); return 4
        tot = full = req = pend = 0
        for k in insts:
            root, syms = plan(k, day)
            path = lbb.path_of(root, day, lbb.INTRADAY_DIR)
            cur = lbb.load_day(path) or lbb.new_intraday_day(root, day)
            todo = [s for s in syms if lbb.symbol_state(cur["contracts"].get(s)) == "pending"]
            for s in todo:
                try:
                    res = lbb.fetch_intraday_today(s, day)
                except lbb.BarsUnavailable as e:
                    res = {"status": "error", "error": str(e)[:160], "fetched_at": datetime.now(ET).isoformat()}
                cur["contracts"][s] = lbb.merge_attempt(cur["contracts"].get(s), res)
                req += 1
                time.sleep(PACE_S)
            if todo:
                lbb.save_day(path, cur)
            st = [lbb.symbol_state(cur["contracts"].get(s)) for s in syms]
            tot += len(syms); full += st.count("complete"); pend += st.count("pending")
            print(f"  {root}: 计划 {len(syms)}，本次请求 {len(todo)}，完成 {st.count('complete')}，"
                  f"确认空 {st.count('empty_confirmed')}，待续 {st.count('pending')}")
        fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    print(f"资金流验证采集 {day}：计划 {tot}、完成 {full}、本次请求 {req}、待续 {pend}")
    return 1 if pend else 0                           # 有待续 → rc=1，调度层下次唤醒重试


if __name__ == "__main__":
    sys.exit(main())
