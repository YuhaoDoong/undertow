"""同日期权链去重判据（2026-11-06 从 cboe_options.materially_same 拆出）。

为什么另起文件：cboe_options.py 在方向算法预登记指纹 signal_ledger.CALL_CODE_FILES 里，改它任何一个字节都会让
shadow_direction 冻结的 call_code_sha 失配（研报 v2「验证中」随之下架）。去重与方向计算无关，放这里不动指纹。

旧判据的问题：键 =（到期, 类型, 行权价），键冲突即判「不同」。SPY 自 2026-11-02、IWM 自 2026-11-06 起链里出现
B1SPY / B1IWM 代码的合约（产品性质未核实，OI 占比 <0.01%），与标准合约同键 → 同一份快照与自己比都判不同 →
daily 每轮重写快照（11/06 SPY 重写 15 次，只变 captured_at）→ 冻结台账（墙位价差、到期画像）判「新输入不同」反复告警。

「物质内容」定义不变（见 cboe_options.materially_same 文档）：合约集合 + 每个合约的 OI + volume；spot 不计入。
按多重集合比较：任一合约 OI/volume 变化、合约增减都能识别；唯一识别不了的是同键两合约数值恰好互换。
⚠️ materially_same 只用于同一天的重复抓取；跨日判结算到没到用下面的 settled_oi_change()。

settled_oi_change（2026-11-16 补）：跨日「OCC 结算到没到」闸门。cboe_options.oi_change_total 用 dict 按键取前日 OI，
同键合约（B1SPY/B1IWM/B1VDA/B1SLA）互相覆盖 → 链里 OI 一张没动也算出 18 万～250 万的 Σ|ΔOI| → 未结算的旧链被当成
今日快照落盘。实测 11/02–11/16：SPY/IWM/NVDA/TSLA 每天 ET ~01:00 先落一份「全部存活合约 OI = 前一日」的旧链，
04:30–06:30 才被真链覆盖；SPY 11/10 末轮抓取时 CBOE 仍未更新 → 旧链永久留存。凌晨冻结的台账（到期画像、shadow v5、
conviction）按旧链冻结，真链进来后判「新输入不同」。修法：同键在任一侧出现多次的键整键剔除，其余语义与 oi_change_total
完全一致（新键按全额计、消失的合约不计）。剔除的调整合约 OI 占比 <0.01%，不影响「到没到」的判断。
"""
from __future__ import annotations

from collections import Counter

from undertow.core.models import OptionsSnapshot


def materially_same(a: OptionsSnapshot, b: OptionsSnapshot) -> bool:
    if len(a.contracts) != len(b.contracts):
        return False
    sig = lambda s: Counter((c.expiry, c.kind, round(c.strike, 4), c.open_interest or 0, c.volume or 0)
                            for c in s.contracts)
    return sig(a) == sig(b)


def settled_oi_change(prev: OptionsSnapshot, curr: OptionsSnapshot) -> int:
    """前一交易日快照 → 本次抓取：唯一键合约的 Σ|ΔOI|。0 = OCC 隔夜结算尚未落地，不得作为今日快照落盘。"""
    key = lambda c: (c.expiry, c.kind, round(c.strike, 4))
    dup = {k for s in (prev, curr) for k, n in Counter(key(c) for c in s.contracts).items() if n > 1}
    pm = {key(c): c.open_interest or 0 for c in prev.contracts if key(c) not in dup}
    return sum(abs((c.open_interest or 0) - pm.get(key(c), 0)) for c in curr.contracts if key(c) not in dup)
