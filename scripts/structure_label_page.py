"""structure-v0 盲图标注页：固定前缀图（≤2015），用户手工标 HH/HL/LH/LL、结构状态、形态候选与不确定。

  python3 scripts/structure_label_page.py --out data/backtest/structure-v0/labeling

抽样：种子 20261014，每个标的 × 截图当时可知的 2ATR 背景状态（up / down / none）各 1 张，共 30 张；清单先落盘再生成页面；
1–20 号用于讨论定义，21–30 号暂留核对（Codex 039）。页面只含截至前缀日的 120 根 K 线，不显示算法认点、不显示之后走势。
标注存浏览器本地（localStorage），点「导出」下载 JSON 交回。纯标准库生成，页面无外部依赖。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ps_explore as pe  # noqa: E402
from undertow.analyze import price_structure as ps  # noqa: E402
from undertow.analyze import structure_patterns as sp  # noqa: E402

SEED = 20261014
WINDOW = 120
SYMS = ("GLD", "SLV", "SPY", "QQQ", "GC=F", "SI=F", "AAPL", "MSFT", "GOOGL", "INTC")


def sample() -> list:
    # 每个标的 × 每种背景状态各抽 1 张（10 × 3 = 30）：首版按全体候选抽样会偏向历史长的个股（INTC 8 张、GLD/SPY 0 张），
    # 在任何人看图之前改为按标的分层（见 research_decisions.md）。
    rng = random.Random(SEED)
    out = []
    for sym in SYMS:
        d, sha = pe.fetch(sym)
        bars, _, _ = pe.parse(d)
        atr = ps.wilder_atr(bars)
        zz = ps.zigzag(bars, atr)
        pool = {"up": [], "down": [], "none": []}
        for i in range(700, len(bars), 5):                    # 每 5 根取一个候选前缀日（只按位置与当时背景，不看之后走势）
            pool[sp._bg_state([p for p in zz if p.conf <= i])].append(i)
        for st in ("up", "down", "none"):
            i = rng.choice(pool[st])
            out.append({"symbol": sym, "prefix_index": i, "prefix_end": str(bars[i][0]), "background_at_cut": st, "input_sha256": sha})
    rng.shuffle(out)                                          # 打乱顺序，标注时看不出分层
    for k, m in enumerate(out, 1):
        m["id"] = k
        m["role"] = "讨论" if k <= 20 else "暂留核对"
    return out


PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>结构盲图标注</title>
<style>
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--up:#2a9d4b;--dn:#d1495b;--h:#1f6feb;--l:#e36209;--line:#d0d0d5}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f2;--mut:#a0a0a6;--line:#3a3a3e}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"PingFang SC",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.card{border:1px solid var(--line);border-radius:10px;padding:12px;margin:14px 0}
svg{width:100%;height:auto;display:block;touch-action:manipulation}
.row{display:flex;flex-wrap:wrap;gap:6px 14px;margin:6px 0;align-items:center}
.row b{min-width:96px}
label{white-space:nowrap}
button{padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer}
.mut{color:var(--mut)} textarea{width:100%;min-height:36px;background:transparent;color:var(--fg);border:1px solid var(--line);border-radius:6px}
.bar{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:2}
</style></head><body><div class="wrap">
<div class="bar"><b>结构盲图标注</b> <span class="mut">（只到截图日为止，右侧未来不提供；点 K 线上半部 = 标高点、下半部 = 标低点；再点一次 = 改为「不确定」；第三次 = 取消）</span>
<div class="row"><button id="exp">导出标注 JSON</button><span id="cnt" class="mut"></span></div></div>
<div id="list"></div></div>
<script>
const DATA = __DATA__;
const KEY = "structure_v0_labels";
let L = {};
try { L = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { L = {}; }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(L)); } catch (e) {} count(); };
const opts = {
  state: ["上升", "下降", "区间", "看不出"],
  pattern: ["无", "双顶", "双底", "头肩顶", "头肩底", "不确定"],
  confirmed: ["已确认", "未确认", "不确定"]
};
function count(){ const n = DATA.filter(d => L[d.id] && L[d.id].state).length; document.getElementById("cnt").textContent = `已标 ${n}/${DATA.length}`; }
function draw(d){
  const W = 1000, H = 380, n = d.bars.length;
  const lo = Math.min(...d.bars.map(b => b[3])), hi = Math.max(...d.bars.map(b => b[2]));
  const pad = (hi - lo) * 0.06, pmin = lo - pad, pmax = hi + pad;
  const x = i => 10 + (i + 0.5) * (W - 20) / n, y = v => 10 + (pmax - v) * (H - 20) / (pmax - pmin), cw = (W - 20) / n * 0.62;
  const lab = L[d.id] || (L[d.id] = {pivots: {}});
  let s = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  d.bars.forEach((b, i) => {
    const [o, h, l, c] = [b[1], b[2], b[3], b[4]], col = c >= o ? "var(--up)" : "var(--dn)";
    s += `<rect data-i="${i}" x="${x(i) - (W - 20) / n / 2}" y="0" width="${(W - 20) / n}" height="${H}" fill="transparent"/>`;
    s += `<line x1="${x(i)}" x2="${x(i)}" y1="${y(h)}" y2="${y(l)}" stroke="${col}" pointer-events="none"/>`;
    s += `<rect x="${x(i) - cw / 2}" y="${y(Math.max(o, c))}" width="${cw}" height="${Math.max(1, Math.abs(y(o) - y(c)))}" fill="${col}" pointer-events="none"/>`;
  });
  Object.entries(lab.pivots).forEach(([i, p]) => {
    const b = d.bars[+i], hiP = p.kind === "H", yy = hiP ? y(b[2]) - 9 : y(b[3]) + 9, col = hiP ? "var(--h)" : "var(--l)";
    s += `<text x="${x(+i)}" y="${yy + (hiP ? 0 : 6)}" text-anchor="middle" font-size="12" fill="${col}" pointer-events="none">${hiP ? "▼" : "▲"}${p.uncertain ? "?" : ""}</text>`;
  });
  return s + "</svg>";
}
function radios(d, field){
  const lab = L[d.id];
  return `<div class="row"><b>${{state: "结构状态", pattern: "形态候选", confirmed: "此刻是否已确认"}[field]}</b>` +
    opts[field].map(v => `<label><input type="radio" name="${field}${d.id}" value="${v}" ${lab[field] === v ? "checked" : ""}> ${v}</label>`).join("") + "</div>";
}
function render(){
  const list = document.getElementById("list"); list.innerHTML = "";
  DATA.forEach(d => {
    const lab = L[d.id] || (L[d.id] = {pivots: {}});
    const card = document.createElement("div"); card.className = "card";
    card.innerHTML = `<div class="row"><b>#${d.id}</b><span>${d.symbol} 截至 ${d.prefix_end}</span><span class="mut">（${d.role}）</span></div>
      <div class="chart">${draw(d)}</div>${radios(d, "state")}${radios(d, "pattern")}${radios(d, "confirmed")}
      <div class="row"><b>备注</b></div><textarea>${lab.note || ""}</textarea>`;
    card.querySelector(".chart").addEventListener("click", ev => {
      const r = ev.target.closest("rect[data-i]"); if (!r) return;
      const i = r.dataset.i, svg = card.querySelector("svg"), pt = svg.getBoundingClientRect();
      const kind = (ev.clientY - pt.top) < pt.height / 2 ? "H" : "L", cur = lab.pivots[i];
      if (!cur) lab.pivots[i] = {kind, uncertain: false};
      else if (!cur.uncertain) cur.uncertain = true;
      else delete lab.pivots[i];
      card.querySelector(".chart").innerHTML = draw(d); save();
    });
    card.querySelectorAll("input[type=radio]").forEach(el => el.addEventListener("change", () => { lab[el.name.replace(/\d+$/, "")] = el.value; save(); }));
    card.querySelector("textarea").addEventListener("input", e => { lab.note = e.target.value; save(); });
    list.appendChild(card);
  });
  count();
}
document.getElementById("exp").onclick = () => {
  const out = {version: "structure-v0-labels", exported_at: new Date().toISOString(), samples: DATA.map(d => ({id: d.id, symbol: d.symbol, prefix_end: d.prefix_end})), labels: L};
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], {type: "application/json"}));
  a.download = "structure_labels.json"; a.click();
};
render();
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if out.exists():
        raise SystemExit(f"{out} 已存在：不覆盖")
    out.mkdir(parents=True)
    man = sample()
    (out / "manifest.json").write_text(json.dumps({"seed": SEED, "window": WINDOW, "symbols": SYMS,
                                                    "samples": man}, ensure_ascii=False, indent=1), "utf-8")
    data = []
    cache = {}
    for m in man:
        if m["symbol"] not in cache:
            cache[m["symbol"]] = pe.parse(pe.fetch(m["symbol"])[0])[0]
        bars = cache[m["symbol"]]
        i = m["prefix_index"]
        win = bars[i - WINDOW + 1:i + 1]                       # 只给截至前缀日的数据
        data.append({"id": m["id"], "symbol": m["symbol"], "prefix_end": m["prefix_end"], "role": m["role"],
                     "bars": [[str(b[0]), round(b[1], 4), round(b[2], 4), round(b[3], 4), round(b[4], 4)] for b in win]})
    (out / "index.html").write_text(PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False)), "utf-8")
    print(f"{len(man)} 张 → {(out / 'index.html').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
