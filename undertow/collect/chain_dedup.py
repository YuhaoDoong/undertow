"""同日期权链去重判据（2026-11-06 从 cboe_options.materially_same 拆出）。

为什么另起文件：cboe_options.py 在方向算法预登记指纹 signal_ledger.CALL_CODE_FILES 里，改它任何一个字节都会让
shadow_direction 冻结的 call_code_sha 失配（研报 v2「验证中」随之下架）。去重与方向计算无关，放这里不动指纹。

旧判据的问题：键 =（到期, 类型, 行权价），键冲突即判「不同」。SPY 自 2026-11-02、IWM 自 2026-11-06 起链里出现
B1SPY / B1IWM 代码的合约（产品性质未核实，OI 占比 <0.01%），与标准合约同键 → 同一份快照与自己比都判不同 →
daily 每轮重写快照（11/06 SPY 重写 15 次，只变 captured_at）→ 冻结台账（墙位价差、到期画像）判「新输入不同」反复告警。

「物质内容」定义不变（见 cboe_options.materially_same 文档）：合约集合 + 每个合约的 OI + volume；spot 不计入。
按多重集合比较：任一合约 OI/volume 变化、合约增减都能识别；唯一识别不了的是同键两合约数值恰好互换。
⚠️ 只用于同一天的重复抓取；跨日判结算到没到仍用 oi_change_total()。
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
