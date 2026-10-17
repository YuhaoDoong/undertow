// 二次标注题（relabel-v1）的纯逻辑（无 DOM；页面与 node 单测共用）。与撮合引擎 bar_drill_engine.js 分开：
// 改这里不会改变撮合引擎的 sha256，未完成的逐根题不会因此变成「旧引擎只读」。本文件 sha256 = 判定规则身份，揭示时写进记录。
// 用户 2026-10-17：「用你标注的图（遮住未来走势），让我二次标注来看看我们的标注差异。然后再对比下游未来的走势，判断我和你谁对。」
// 「Claude 的标注」是出题时 Python 用确定性算法（之字形结构点 + 来向区间 + 最近两高两低定方向）只读截止根之前算好嵌入的，这里不重算。
//
// 判定（练习口径，不是方向能力的证据）：A = 截止根 ATR14（Wilder，出题时嵌入）；位移 = (收盘[截止+horizon] − 收盘[截止]) / A。
//   |位移| < tie（0.5）→「不确定」算对、看涨/看跌算错；≥ tie → 方向一致算对，「不确定」算错。
//   失效位（可选）：只记后 horizon 根内是否被盘中高低触及（看涨看最低价 ≤ 失效位，看跌看最高价 ≥ 失效位），不判先后。
const Relabel = (() => {
  const VERSION = "relabel-v1";
  const DIRS = ["看涨", "看跌", "不确定"];
  const kindOf = lab => (lab === "HH" || lab === "LH" || lab === "H") ? "H" : (lab === "HL" || lab === "LL" || lab === "L") ? "L" : null;

  // 锁定前的作答检查：方向必选；失效位只配看涨/看跌，且要在截止收盘的反方向一侧。返回错误文案或 null。
  function checkAnswer(dir, invalid, closeCut) {
    if (!DIRS.includes(dir)) return "先选方向（看涨 / 看跌 / 不确定）";
    if (invalid == null) return null;
    if (!Number.isFinite(invalid) || invalid <= 0) return "失效位不是有效价格";
    if (dir === "不确定") return "选「不确定」时不设失效位";
    if (dir === "看涨" && !(invalid < closeCut)) return "看涨的失效位要在截止收盘下方";
    if (dir === "看跌" && !(invalid > closeCut)) return "看跌的失效位要在截止收盘上方";
    return null;
  }

  // bars: Drill.window 的输出 [[rel,o,h,l,c,v]]；cut = 截止根下标（left−1）
  function judge(bars, cut, atr, dir, invalid, horizon, tie) {
    const end = cut + horizon;
    if (!(atr > 0) || end >= bars.length) return null;
    const move = (bars[end][4] - bars[cut][4]) / atr;
    const big = Math.abs(move) >= tie;
    const ok = dir === "不确定" ? !big : big && (dir === "看涨" ? move > 0 : move < 0);
    let inv_hit = null;
    if (invalid != null && dir !== "不确定") {
      inv_hit = false;
      for (let i = cut + 1; i <= end; i++) if (dir === "看涨" ? bars[i][3] <= invalid : bars[i][2] >= invalid) { inv_hit = true; break; }
    }
    return {move, ok, inv_hit};
  }

  // 差异：结构点同类（HH/LH/H 为高点、HL/LL/L 为低点）且标签相同（算法首个无可比对象的 H/L 与同类任一标签相容）、
  // 相距 ≤ tolBars 根算一致，一对一、按距离贪心；支撑线 ↔ S 区、压力线 ↔ R 区：线价落在区间内或距区间 ≤ lineTol·ATR 算一致。
  function diff(userMarks, claude, atr, tolBars, lineTol) {
    const up = userMarks.filter(m => kindOf(m.type)), cp = (claude.points || []).slice();
    const cand = [];
    up.forEach((u, a) => cp.forEach((c, b) => {
      const d = Math.abs(u.i - c.i), lab = c.label === "H" || c.label === "L" ? kindOf(u.type) === c.kind : u.type === c.label;
      if (lab && d <= tolBars) cand.push([d, a, b]);
    }));
    cand.sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2]);
    const ua = new Set(), cb = new Set(), pm = [];
    cand.forEach(([d, a, b]) => { if (!ua.has(a) && !cb.has(b)) { ua.add(a); cb.add(b); pm.push({user: up[a], claude: cp[b], d}); } });
    const ul = userMarks.filter(m => m.type === "支撑线" || m.type === "压力线"), cz = (claude.zones || []).slice();
    const near = (p, z) => p >= z.lo - lineTol * atr && p <= z.hi + lineTol * atr;
    const la = new Set(), lb = new Set(), lm = [];
    ul.forEach((u, a) => { const k = u.type === "支撑线" ? "S" : "R";
      const b = cz.findIndex((z, j) => !lb.has(j) && z.kind === k && near(u.price, z));
      if (b >= 0) { la.add(a); lb.add(b); lm.push({user: u, claude: cz[b]}); } });
    return {points: {match: pm, user_only: up.filter((_, a) => !ua.has(a)), claude_only: cp.filter((_, b) => !cb.has(b))},
            levels: {match: lm, user_only: ul.filter((_, a) => !la.has(a)), claude_only: cz.filter((_, b) => !lb.has(b))}};
  }

  // 累计：只算已揭示且有判定的记录。分歧 = 双方方向不同。
  function stats(results) {
    const r = results.filter(x => x && x.user && x.claude);
    const dis = r.filter(x => x.user.dir !== x.claude.dir);
    const n = k => r.filter(x => x[k].ok).length;
    return {n: r.length, user_ok: n("user"), claude_ok: n("claude"), disagree: dis.length,
            disagree_user_ok: dis.filter(x => x.user.ok).length, disagree_claude_ok: dis.filter(x => x.claude.ok).length,
            user_inv_hit: r.filter(x => x.user.inv_hit === true).length, user_inv_set: r.filter(x => x.user.inv_hit != null).length};
  }

  return {VERSION, DIRS, kindOf, checkAnswer, judge, diff, stats};
})();
if (typeof module !== "undefined") module.exports = Relabel;
