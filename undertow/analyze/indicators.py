"""指标家族 —— 把散落各处的读数按【数据来源】归组，每组一个小标签。

**为什么要分家族**（用户 2026-08-29 的要求）：
「每个非同源指标，应该都以小 label 的形式出现在 index 研报里，
然后在各自品种研报里不同栏目详细解释。」

判据是**数据是否同源**，不是名字听着像不像。举两个反例：
  · ⚡强信号 与 持仓增量 用的是同一套 upside/downside_pressure，实测方向 100%
    共线 —— 它们是**同一个指标的两种说法**，不能算两票；
  · 净有效 Delta 与 加权增仓 都来自两日 ΔOI，同源，只是加权方式不同
    （一个按 Delta、一个按推断的买卖方向），所以并在同一个家族里对照，
    而不是分成两个家族假装是两份独立证据。

六个家族：
  🧱 结构   期权链【当前】OI 分布      —— 静态，昨天的钱堆在哪
  💰 增仓   期权链【两日之差】         —— 动态，昨天钱往哪流
  🌊 波动   IV 与 skew                —— 保险的价格，与 OI 无关
  📈 价格   价格历史                   —— 完全不碰期权
  🏦 大资金 CFTC 周频持仓              —— 期货市场，周频
  🌍 宏观   利率/美元等                 —— 月频背景

⚠️ **它们不是六份互相独立的证据，别这么读**（这一条是我自己在 2026-08-29
   向用户承认的，先前的注释把话说满了）：
   · 结构与增仓**来自同一份快照** —— 一个看存量（几个月累积的堆积），
     一个看增量（昨天一天的变化）。存量的变化就是增量，两者信息重叠，
     只是重叠得不完全。**不能当两票独立证据。**
   · 波动率面的 IV 与增仓的买卖方推断也不完全独立 —— 后者正是**用 IV 方向**
     推断谁是主动方的。
   真正明确不同源的只有：📈 价格（整条链上唯一不碰期权数据的一层）
   与 🏦 大资金 / 🌍 宏观（不同市场、不同频率）。

⚠️ 本模块【不投票、不改结论】，只把已有读数分组呈现。
"""
from __future__ import annotations

from dataclasses import dataclass

# 分层回测结论（2026-08-29，138 样本 / 日聚类 bootstrap / 前瞻 1 日 / 局部去趋势）。
# 门槛按【各品种自身】|日涨跌| 的 70 分位定，不用统一的 1.5% —— 原油日常波动
# 本就比黄金大，一刀切会把原油的横盘算成大波动。
#
# 起因（用户 2026-08-29）：「横盘的日期和大涨大跌的日期要区分。横盘的时候，
# 指标乱是正常的。」—— 这个改进直接救活了两个此前被误判为无效的指标：
# 波动率面混在一起测只有 52.1%，分层后大波动日 72.7%、横盘 41.3%，
# 等于把两个相反的东西平均掉了。
REGIME: dict[str, tuple[str, str]] = {
    # key → (大波动日表现, 横盘日表现)
    "flow":  ("63.9% [50.0,77.4]", "64.5% [53.9,74.6]"),
    "vol":   ("72.7% [55.2,88.6]", "41.3% [28.8,54.5]"),
    "price": ("73.5% [57.1,87.9]", "53.7% [40.6,66.2]"),
}
# 每组在什么行情下可信 —— 直接写给用户看，别让他自己去猜
# ⚠️ 全部为【事后描述】，不是可执行规则（codex 2026-08-29 P1）：
# 分组用的是"当日实际涨跌"，而盘前并不知道今天属于哪一组 ——
# 实测前日波动对今日大波动是【负相关】，没有可靠的盘前代理。
# 所以这里只陈述"事后分组时出现了什么差异"，不写"该看/别看"。
WHEN_TRUST: dict[str, str] = {
    "flow":  "事后按当日波动分组：大波动日 63.9%、横盘 64.5%，两组接近。"
             "（35 个日期聚类，未达 n≥50 门槛，属探索性观察）",
    "vol":   "事后按当日波动分组差异最大：大波动日 72.7%、横盘日 41.3%。"
             "但盘前无法判断今天属于哪一组，**不能据此择时**。",
    "price": "事后按当日波动分组：大波动日 73.5%、横盘日 53.7%。同上，"
             "盘前不可知归属。",
    "struct": "未单独回测（离散投票，无连续强度可测）。",
    "cot":   "未回测：周频数据，现在拉到的是最新值而非当时的值，"
             "拿它回测就是 lookahead。",
    "macro": "未回测：同上，月频。",
}

# 家族定义：key → (图标, 短名, 数据源一句话, 大白话"它在说什么")
FAMILIES: dict[str, tuple[str, str, str, str]] = {
    "struct": ("🧱", "结构", "期权链当前 OI 分布（静态）",
               "钱已经堆在哪些价位上 —— 哪里是天花板、哪里是地板。"
               "它不说方向，只说【走到哪会卡住】。"),
    "flow":   ("💰", "增仓", "期权链两日 OI 之差（动态）",
               "昨天新的钱往哪边下注 —— 买 put／卖 call 算看跌侧，"
               "买 call／卖 put 算看涨侧。这是最贴近『刚刚发生了什么』的一层。"),
    "vol":    ("🌊", "波动", "ATM IV 与 25Δ skew",
               "保险贵不贵、谁在抢 —— put 比 call 贵得多，说明有人急着买下跌保护。"
               "它与持仓量无关，是独立的一份证据。"),
    "price":  ("📈", "价格", "价格历史（不碰期权）",
               "现在离常态多远 —— 涨过头了还是跌过头了。"
               "整条链上唯一完全不依赖期权数据的一层。"),
    "cot":    ("🏦", "大资金", "CFTC 周频持仓报告",
               "期货市场上大机构的净持仓变化。周频，慢，但看的是真金白银的方向。"),
    "macro":  ("🌍", "宏观", "实际利率／美元等（月频）",
               "大背景。慢变量，决定几周到几个月的底色，不决定明天。"),
}

# outlook 投票层 → 家族
_LAYER2FAM = {"Gamma": "struct", "Flow": "flow", "COT": "cot", "Macro": "macro"}


@dataclass(frozen=True)
class Label:
    key: str
    icon: str
    name: str
    sign: int          # +1 看涨 / -1 看跌 / 0 中性或不表态
    reading: str       # 带强度的读数，如 "看跌 53.5×"
    detail: str        # 一句依据
    horizon: str       # 近端 / 中期 / 远期 —— 这层管多久

    @property
    def color(self) -> str:
        return "#1a7f37" if self.sign > 0 else ("#cf222e" if self.sign < 0 else "#6e7781")

    @property
    def word(self) -> str:
        return "看涨" if self.sign > 0 else ("看跌" if self.sign < 0 else "中性")


_HORIZON = {"struct": "近端", "flow": "近端", "vol": "近端",
            "price": "近端", "cot": "中期", "macro": "远期"}


def build(outlook, fa=None, stretch=None) -> list[Label]:
    """把一个品种的各层读数归成六个 label。缺哪层就少哪个，不编。"""
    out: list[Label] = []
    votes = list(getattr(outlook, "votes", None) or [])

    # ① 结构 / ⑤ 大资金 / ⑥ 宏观：直接聚合对应层的票
    for fam in ("struct", "cot", "macro"):
        layers = [k for k, v in _LAYER2FAM.items() if v == fam]
        sub = [v for v in votes if getattr(v, "layer", "") in layers]
        if not sub:
            continue
        score = sum(v.weight * v.sign for v in sub)
        sign = 1 if score > 0.3 else (-1 if score < -0.3 else 0)
        top = max(sub, key=lambda v: v.weight * abs(v.sign), default=None)
        icon, name, _src, _plain = FAMILIES[fam]
        out.append(Label(fam, icon, name, sign,
                         f"{('看涨' if sign>0 else '看跌' if sign<0 else '中性')}"
                         f"（合计 {score:+.1f} 票）",
                         (top.factor if top else ""), _HORIZON[fam]))

    # ② 增仓：**必须带强度**。它是本项目最尖锐的一层，压力比 1.4× 和 53.5×
    #    在投票里同样只值 0.8 票（2026-08-28 黄金即此），至少在展示上要分得开。
    if fa is not None:
        up = getattr(fa, "upside_pressure", 0) or 0
        dn = getattr(fa, "downside_pressure", 0) or 0
        if up or dn:
            sign = 1 if up > dn else (-1 if dn > up else 0)
            hi, lo = max(up, dn), max(min(up, dn), 1.0)
            ratio = hi / lo
            nd = getattr(fa, "net_delta_total", None)
            nd_txt = f"；净有效 Delta {nd:+,.0f}" if nd is not None else ""
            icon, name, _s, _p = FAMILIES["flow"]
            out.append(Label("flow", icon, name, sign,
                             f"{'看涨' if sign>0 else '看跌'}侧 {ratio:.1f}×",
                             f"加权增仓 看涨 {up:,.0f} / 看跌 {dn:,.0f}{nd_txt}",
                             _HORIZON["flow"]))

        # ③ 波动率面：ATM IV + 25Δ skew，与 OI 无关的独立证据
        vs = getattr(fa, "vol", None)
        if vs is not None and getattr(vs, "prev", None):
            dsk = getattr(vs, "d_skew25_pp", 0.0) or 0.0
            datm = getattr(vs, "d_atm_pp", 0.0) or 0.0
            sign = -1 if dsk >= 0.3 else (1 if dsk <= -0.3 else 0)
            icon, name, _s, _p = FAMILIES["vol"]
            out.append(Label("vol", icon, name, sign,
                             f"skew {dsk:+.2f}pp",
                             f"ATM IV {datm:+.2f}pp；25Δ skew(put−call) {dsk:+.2f}pp"
                             f"（正=put 更贵=有人抢下跌保护）", _HORIZON["vol"]))

    # ④ 价格拉伸：唯一不碰期权的一层
    if stretch is not None:
        pct = getattr(stretch, "pctile", None)   # 两维分位均值，档位由它决定
        if pct is not None:
            # ⚠️ StretchRead.pctile 是 0~1 的小数，报告里渲染时才 ×100。
            # 直接当百分数用会把 78% 显示成 1%（2026-08-29 实测）。
            pct = pct * 100.0 if pct <= 1.0 else pct
            # 超买→回落压力（看跌），超卖→反弹动能（看涨）。它是【位置】不是【方向预言】。
            sign = -1 if pct >= 70 else (1 if pct <= 30 else 0)
            icon, name, _s, _p = FAMILIES["price"]
            word = getattr(stretch, "band", "") or f"{pct:.0f}%"
            out.append(Label("price", icon, name, sign, f"{word} {pct:.0f}%",
                             "偏离度(离均线多少个ATR) + 60日回撤，合并成一个分位",
                             _HORIZON["price"]))

    order = {"flow": 0, "struct": 1, "vol": 2, "price": 3, "cot": 4, "macro": 5}
    out.sort(key=lambda l: order.get(l.key, 9))
    return out


def render_pills(labels: list[Label], esc, *, scores: dict | None = None) -> str:
    """index 卡片上的一行小标签 + 两套评分并列。"""
    if not labels:
        return ""
    ps = []
    for l in labels:
        ps.append(
            f'<span title="{esc(FAMILIES[l.key][2])}" style="display:inline-block;'
            f'margin:2px 4px 2px 0;padding:1px 7px;border-radius:9px;font-size:11.5px;'
            f'border:1px solid {l.color}44;background:{l.color}12;color:{l.color}">'
            f'{l.icon} {esc(l.name)} <b>{esc(l.reading)}</b></span>')
    sq_pill = ""
    if scores and scores.get("squeeze") is not None:
        from undertow.analyze.squeeze import render_pill as _sq_pill
        sq_pill = _sq_pill(scores["squeeze"], esc)
    sc = ""
    if scores:
        old, new = scores.get("legacy"), scores.get("weighted")
        near = scores.get("near")
        def _c(v):
            return "#1a7f37" if v > 0.15 else ("#cf222e" if v < -0.15 else "#6e7781")
        parts = []
        if old is not None:
            parts.append(f'现行综合 <b>{old:+.1f}</b>')
        if new is not None:
            parts.append(f'<span style="color:{_c(new)}">实验分 <b>{new:+.2f}</b></span>')
        if near is not None:
            parts.append(f'<span style="color:{_c(near)}">仅近端 <b>{near:+.2f}</b></span>')
        if parts:
            sc = ('<div style="margin-top:4px;font-size:12px">📐 ' + '　｜　'.join(parts) +
                  '<span style="color:#6e7781;font-size:11px">'
                  '　（两把不同的尺子，别直接比大小；实验分未通过验证门槛）</span></div>')
    return ('<div style="margin-top:6px">' + "".join(ps) + sq_pill + sc +
            '<div style="font-size:11px;color:#6e7781;margin-top:2px">'
            '六组按数据来源分的读数。⚠️ 不是六份独立证据：结构与增仓同出一份快照'
            '（存量 vs 增量），波动率面又参与了增仓的买卖方推断。'
            '⚡强信号与「增仓」100% 共线，已合并不重复计票。'
            '</div></div>')


def render_section(labels: list[Label], esc) -> str:
    """品种研报里的「指标说明」栏目：每组是什么、这次读数多少、管多久。

    用户 2026-08-29：「指标一一说明后（用大白话）」。
    所以这里不写公式，只写"它在说什么"和"这次它说了什么"。
    """
    if not labels:
        return ""
    rows = []
    for l in labels:
        icon, name, src, plain = FAMILIES[l.key]
        rows.append(
            f'<tr>'
            f'<td style="white-space:nowrap;padding:7px 8px;vertical-align:top">'
            f'<b style="color:{l.color}">{icon} {esc(name)}</b><br>'
            f'<span style="font-size:11px;color:#6e7781">{esc(l.horizon)}</span></td>'
            f'<td style="padding:7px 8px;vertical-align:top;white-space:nowrap">'
            f'<b style="color:{l.color}">{esc(l.reading)}</b></td>'
            f'<td style="padding:7px 8px;vertical-align:top;font-size:12.5px;'
            f'line-height:1.6">{esc(plain)}<br>'
            f'<b style="font-size:11.5px">何时可信：</b>'
            f'<span style="font-size:11.5px">{esc(WHEN_TRUST.get(l.key, ""))}</span><br>'
            f'<span style="color:#6e7781;font-size:11.5px">数据源：{esc(src)}'
            f'{("　｜　本次依据：" + esc(l.detail)) if l.detail else ""}</span></td>'
            f'</tr>')
    return (
        '<h2>指标说明 · 这六组各自在说什么</h2>'
        '<div class="sub" style="margin-bottom:6px">'
        '按【数据来源】分组，不按名字像不像。⚡强信号与「增仓」用的是同一套压力数'
        '（实测方向 100% 共线），已合并、算一票不算两票。<br>'
        '⚠️ <b>但这六行并非六份互相独立的证据</b>：🧱结构与💰增仓来自同一份快照'
        '（一个看存量、一个看增量），🌊波动率面又参与了增仓的买卖方推断。'
        '真正明确不同源的是 📈价格（唯一不碰期权数据）与 🏦大资金/🌍宏观'
        '（不同市场、不同频率）。</div>'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<tr style="background:#f6f8fa"><th style="text-align:left;padding:6px 8px">组／时域</th>'
        '<th style="text-align:left;padding:6px 8px">本次读数</th>'
        '<th style="text-align:left;padding:6px 8px">它在说什么</th></tr>'
        + "".join(rows) + '</table>'
        '<div class="sub" style="margin-top:6px">'
        '⚠️ 各组权重目前是<b>固定</b>的，不随读数强弱变化 —— '
        '「增仓 53.5×」和「增仓 1.4×」在综合投票里同样只值 0.8 票。'
        '这是已知缺陷，强度只在本表和上面的小标签里看得到。<br>'
        '📊 「何时可信」来自 2026-08-29 的分层回测：138 个样本、日聚类 bootstrap、'
        '前瞻 1 日、局部去趋势；大波动门槛取各品种自身 |日涨跌| 的 70 分位。'
        '样本只覆盖 2026-06-25~08-28 两个月、主力是金银油，结论谈不上牢固。</div>')
