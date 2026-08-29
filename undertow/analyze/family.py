"""同族一致性检查 —— 高相关品种给出不同方向时，必须自己说出来。

**为什么需要它**（用户 2026-08-29 的要求，原话）：
「记住金银同向，QQQ 和 TQQQ 同向，这几个是大概率互相绑定的，往往同向，
最多是幅度不同。」

由来是 2026-08-28：黄金亮 ⚡极强看跌（53.5×），白银判为中性、综合偏多，
两者日收益相关约 0.89。没有任何地方指出这个矛盾。次日 GLD -3.24%、SLV -4.38%
—— **同一波贵金属抛售，我们对其中一个说对了、对另一个说反了，
却从没把这两个结论放在一起看过。**

这个模块**不改任何品种的结论**（不投票、不覆盖），只做一件事：把该并排看的
结论并排放出来。相关性高不代表必然同向，但方向相反时读者有权先知道。
"""
from __future__ import annotations

from dataclasses import dataclass

# 同族对：(A, B, 日收益相关量级)。只用于【是否并排提示】，不参与任何计算。
# 相关值是量级估计，不是精确统计量 —— 用它排序和过滤，不用它做推断。
FAMILIES: list[tuple[str, str, float]] = [
    ("qqq", "tqqq", 0.99),   # 同一标的的 3 倍杠杆 ETF，方向相反基本等于有 bug
    ("gold", "silver", 0.89),
    ("qqq", "spy", 0.95),
    ("spy", "iwm", 0.85),
]
MIN_CORR = 0.85


def _sgn(bias: str) -> int:
    if not bias:
        return 0
    return 1 if "偏多" in bias else (-1 if "偏空" in bias else 0)


@dataclass(frozen=True)
class FamilyNote:
    a: str
    b: str
    corr: float
    a_txt: str
    b_txt: str
    kind: str        # "方向相反" / "强信号落单"
    severity: int    # 越大越该看

    def headline(self) -> str:
        return (f"{self.a} 与 {self.b}（相关 {self.corr:.2f}）{self.kind}："
                f"{self.a} {self.a_txt} ／ {self.b} {self.b_txt}")


def check(views: dict[str, dict]) -> list[FamilyNote]:
    """views = {品种: {near, mid, bias, signal_dir, signal_level}}。

    两条规则：
      1. 近端方向相反 —— 同族最不该出现的情况；
      2. 一边有强信号、另一边近端不同向（含中性）—— 强信号是我们最高等级的证据，
         它落单时另一边的沉默本身就是需要解释的事。
    """
    out: list[FamilyNote] = []
    for a, b, corr in FAMILIES:
        if corr < MIN_CORR:
            continue
        va, vb = views.get(a), views.get(b)
        if not va or not vb:
            continue
        na, nb = va.get("near", ""), vb.get("near", "")
        sa, sb = _sgn(na), _sgn(nb)

        if sa and sb and sa * sb < 0:
            out.append(FamilyNote(a, b, corr, f"近端{na}", f"近端{nb}",
                                  "近端方向相反", 3 if corr >= 0.95 else 2))
            continue

        # 强信号落单：一边开火、另一边近端不同向
        for x, y, vx, vy, nx, ny, sx, sy in ((a, b, va, vb, na, nb, sa, sb),
                                             (b, a, vb, va, nb, na, sb, sa)):
            d = vx.get("signal_dir") or ""
            if not d:
                continue
            want = 1 if d == "看涨" else -1
            if sy == want:
                continue    # 另一边同向，没矛盾
            lvl = vx.get("signal_level") or ""
            out.append(FamilyNote(
                x, y, corr, f"⚡{lvl}{d}", f"近端{ny or '—'}",
                "强信号落单", 4 if lvl == "极强" else 3))
            break

    out.sort(key=lambda n: -n.severity)
    return out


def render_html(notes: list[FamilyNote], esc) -> str:
    """索引页上的并排提示块。不输出方向、不改结论，只把矛盾摆出来。"""
    if not notes:
        return ""
    items = []
    for n in notes:
        items.append(
            f'<div style="margin:5px 0;font-size:12.5px;line-height:1.6">'
            f'<b>{esc(n.a)} ↔ {esc(n.b)}</b> '
            f'<span style="color:#57606a">（相关 {n.corr:.2f}）</span> '
            f'<b style="color:#bf8700">{esc(n.kind)}</b><br>'
            f'　{esc(n.a)}：{esc(n.a_txt)}　／　{esc(n.b)}：{esc(n.b_txt)}</div>')
    return (
        '<div class="card" style="border:2px solid #bf8700;background:#fff8c5">'
        '<div style="font-size:15px;font-weight:800;color:#9a6700">'
        '🔗 同族品种结论不一致</div>'
        '<div class="sub" style="margin:3px 0 6px">'
        '这几对历史上大概率同向、最多幅度不同。方向相反时不代表谁对谁错，'
        '但你有权先知道它们没对上。</div>'
        + "".join(items) +
        '<div class="sub" style="margin-top:6px">'
        '2026-08-28 的教训：黄金 ⚡极强看跌、白银判中性，次日 GLD −3.24%、SLV −4.38%'
        '——同一波抛售，两个结论从没被放在一起看过。</div></div>')
