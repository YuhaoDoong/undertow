"""共振层 —— 期权结构（主）× 超买超卖（辅）是否同向。纯确定性，无 I/O。

**定位**：主体判断是期权结构（Gamma 墙位 + 资金流 = 近端 bias）。超买超卖是辅助
参考，只在与主体同向时才值得抬高注意力。本模块只做一件事：把两层的方向摆在一起，
判成【共振 / 背离 / 单边 / 无信号】，并把它落盘，等样本攒够再校准。

**⚠️ 本层尚未校准，且有一处已知的反证**

期权结构快照目前只有 22~46 天（GLD 46 / SLV 43 / QQQ 22），一年不到 50 个观测，
做不了任何有统计意义的共振回测。唯一有长历史、能代表"中期结构"的是 COT
（1054 周 ≈ 20 年）。在那一层上测下来，**共振并没有带来增益**：

    组合                     样本   +5日边缘   +10日     5日涨率    t
    超卖（单独）             1213   +0.885pp  +1.337pp   53.0%   +2.42  ✅
    COT低分位（单独）        3958   +0.106pp  +0.041pp   52.9%   +0.69  ❌
    共振：超卖 + COT低分位    491   +0.988pp  +1.087pp   57.0%   +0.97  ❌
    超买（单独）             1273   -0.384pp  -1.234pp   57.0%   -1.20  ❌
    共振：超买 + COT高分位    498   -0.353pp  -1.136pp   56.0%   -0.39  ❌

加上 COT 条件后样本从 1213 掉到 491，边缘只从 +0.885 微升到 +0.988pp，**t 反而
从 2.42 掉到 0.97**。边缘几乎全部来自超卖那一侧，COT 层没有贡献可测的增量。

这不能直接否定"期权结构 × 超买超卖"的共振——COT 是周频中期数据，Gamma 墙位与
资金流是日频微观结构，两者性质不同。但在拿到证据之前，**共振标记只用于分配注意力，
不得当作独立的入场理由，更不得据此放大仓位**。

**因此本模块的产出分两类，界限必须清楚**：
  * `ResonanceRead` —— 当日状态，可以展示，标注"未校准"
  * `snapshot_row()` —— 落盘用的一行记录，攒够 200+ 个观测后才做校准

**超买超卖那一侧本身的可用边界**（见 `stretch.py`）：只有【极超卖】有真方向准确率
（5 日涨率 vs 基准，具体数字读 `stretch.CALIB_META["dir_acc"]`，**不在本文件重复写**——
codex review 2026-08-26 抓到源码里同一个样本量存在三种写法，硬编码必然漂移）；
强超卖/偏超卖没有方向价值；**超买档作为看跌信号无效**（超买后 5 日下跌率低于基准）。
故下方判定里，超买侧一律降权表述。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .stretch import CALIB_META

# 只有这些档位才算"超买超卖侧有信号"。偏超卖/偏超买触发率各 15%，
# 且实测无方向价值（偏超卖 5 日涨率 54.3% < 基准 55.5%），不参与共振判定。
OS_BANDS = ("极超卖", "强超卖")
OB_BANDS = ("极超买", "强超买")
# 极超卖是唯一通过方向准确率检验的档位，单独标出
OS_STRONG = "极超卖"

BULLISH = ("偏多", "偏多(弱)")
BEARISH = ("偏空", "偏空(弱)")


@dataclass(frozen=True)
class ResonanceRead:
    ok: bool
    state: str = ""            # 共振看多 / 共振看空 / 背离 / 仅结构 / 仅超卖 / 仅超买 / 无信号
    struct_bias: str = ""      # 期权结构（近端）方向
    band: str = ""             # 超买超卖档位
    regime: str = ""
    strong: bool = False       # 是否落在唯一通过方向检验的【极超卖】档
    headline: str = ""
    caveat: str = ""


CAVEAT = ("共振层未校准：期权结构快照仅 22~46 天，样本不足以回测；"
          "唯一可测的 COT 层上共振未见增益（t 从 2.42 降到 0.97）。"
          "本标记只用于分配注意力，不作为入场理由，不据此放大仓位。")


def _side(bias: str) -> int:
    if bias in BULLISH:
        return 1
    if bias in BEARISH:
        return -1
    return 0


def assess_resonance(struct_bias: str, sr) -> ResonanceRead:
    """struct_bias = Outlook.near_bias（Gamma 墙位 + 资金流）；sr = StretchRead。"""
    if sr is None or not getattr(sr, "ok", False):
        return ResonanceRead(ok=False, caveat="超买超卖读数不可用，共振层跳过")
    band, regime = sr.band, sr.regime
    s = _side(struct_bias)
    strong = band == OS_STRONG

    if band in OS_BANDS and s > 0:
        state = "共振看多"
        da = CALIB_META.get("dir_acc", {})
        head = (f"期权结构偏多，超买超卖也在【{band}】"
                + (f"（唯一通过方向准确率检验的档位：5 日涨率 {da.get('极超卖_5d','?')}% "
                   f"vs 基准 {da.get('基准_5d','?')}%）"
                   if strong else "（该档无独立方向价值，仅作同向佐证）"))
    elif band in OB_BANDS and s < 0:
        state = "共振看空"
        da = CALIB_META.get("dir_acc", {})
        head = (f"期权结构偏空，超买超卖在【{band}】——"
                f"但超买档作为看跌信号实测无效（超买后 5 日下跌率 "
                f"{da.get('极超买_5d_跌','?')}% < 基准 {da.get('基准_5d_跌','?')}%），"
                f"这里只算「追高性价比差」的旁证，不构成看空依据")
    elif (band in OS_BANDS and s < 0) or (band in OB_BANDS and s > 0):
        state = "背离"
        head = f"期权结构{struct_bias}，超买超卖却在【{band}】——两层不同向，降低仓位与确定性"
    elif band in OS_BANDS or band in OB_BANDS:
        state = "仅超卖" if band in OS_BANDS else "仅超买"
        head = f"只有超买超卖侧有读数【{band}】，期权结构方向为「{struct_bias or '未知'}」，未构成共振"
    elif s != 0:
        state = "仅结构"
        head = f"只有期权结构有方向（{struct_bias}），超买超卖在【{band}】无边缘"
    else:
        state = "无信号"
        head = f"两层都无方向：结构「{struct_bias or '未知'}」、超买超卖【{band}】"

    return ResonanceRead(ok=True, state=state, struct_bias=struct_bias, band=band,
                         regime=regime, strong=strong, headline=head, caveat=CAVEAT)


def snapshot_row(instrument: str, date_s: str, rr: ResonanceRead, sr,
                 spot: float | None = None) -> dict:
    """落盘一行：当日联合状态 + 事后可回填的收益字段。

    共振能不能用，只能靠自己攒数据回答。这行记录把当时的两层状态与现价钉死，
    forward_* 留空，日后由校准脚本按真实价格回填——与事件快照同一思路：
    **不可再生的横截面，先落盘再说**。
    """
    return {
        "instrument": instrument, "date": date_s, "spot": spot,
        "state": rr.state, "struct_bias": rr.struct_bias,
        "band": rr.band, "regime": rr.regime,
        "stretch": getattr(sr, "stretch", None),
        "stretch_pctile": getattr(sr, "stretch_pctile", None),
        "drawdown": getattr(sr, "drawdown", None),
        "dd_pctile": getattr(sr, "dd_pctile", None),
        "combo_pctile": getattr(sr, "pctile", None),
        "forward_5d": None, "forward_10d": None, "forward_20d": None,
    }


def render_md(rr: ResonanceRead) -> str:
    if not rr.ok:
        return f"- 共振层：{rr.caveat}"
    return "\n".join([
        f"**共振层**：{rr.state} —— {rr.headline}",
        f"> {rr.caveat}",
    ])
