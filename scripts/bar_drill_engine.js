// 逐根人工回测练习的纯逻辑（无 DOM）：出题、换算、镜像、按根撮合。页面与 node 单测共用这一份。
// 价格口径：题目起点收盘 = 100；镜像 P' = 100²/P（与 scripts/ps_trigger_v2.mirror 同：高低互换后取倒数，对数涨跌正负对调）。
// 成交约定：决定在当前这根收盘后做出，下一根开盘执行；止损/止盈按下一根起的盘中价判定，跳空越过按开盘价成交；
// 同一根既碰止损又碰止盈，无法知道先后，按止损算（保守）。不计手续费与滑点。
const Drill = (() => {
  function mulberry32(a) {
    return () => {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // pool: {sym: {n}}；opts: {left, right, mirrorProb, syms}
  function pick(pool, seed, opts) {
    const rng = mulberry32(seed);
    const syms = opts.syms.filter(s => pool[s] && pool[s].n >= opts.left + opts.right + 1);
    if (!syms.length) return null;
    const sym = syms[Math.floor(rng() * syms.length)];
    const lo = opts.left - 1, hi = pool[sym].n - opts.right - 1;      // i0 ∈ [lo, hi]
    const i0 = lo + Math.floor(rng() * (hi - lo + 1));
    return {sym, i0, mirror: rng() < opts.mirrorProb, seed, left: opts.left, right: opts.right};
  }

  // raw: {d:[], o:[], h:[], l:[], c:[], v:[]}；返回 [[rel, o, h, l, c, v]]，rel = 相对起点的根数
  function window_(raw, q) {
    const a = q.i0 - q.left + 1, b = q.i0 + q.right, f = 100 / raw.c[q.i0];
    const vols = [];
    for (let i = a; i <= q.i0; i++) if (raw.v[i] != null && raw.v[i] > 0) vols.push(raw.v[i]);
    vols.sort((x, y) => x - y);
    const vm = vols.length ? vols[Math.floor(vols.length / 2)] : null;
    const out = [];
    for (let i = a; i <= b; i++) {
      let o = raw.o[i] * f, h = raw.h[i] * f, l = raw.l[i] * f, c = raw.c[i] * f;
      if (q.mirror) { const K = 10000; [o, h, l, c] = [K / o, K / l, K / h, K / c]; }
      const v = raw.v[i] == null || vm == null ? null : raw.v[i] / vm;
      out.push([i - q.i0, o, h, l, c, v]);
    }
    return out;
  }

  function closeTrade(st, px, i, reason) {
    const p = st.pos;
    const t = {side: p.side, entry: p.entry, entry_i: p.entry_i, exit: px, exit_i: i, reason,
               stop0: p.stop0, ret_pct: p.side * (px / p.entry - 1) * 100,
               r: p.stop0 == null || p.stop0 === p.entry ? null : p.side * (px - p.entry) / Math.abs(p.entry - p.stop0)};
    st.trades.push(t);
    st.pos = null;
    return t;
  }

  // 推进一根：先执行挂着的开盘单，再按这一根的高低判定止损/止盈。返回本根的成交列表。
  function step(st, bars) {
    const j = st.cursor + 1;
    if (j >= bars.length) return [];
    const [, o, h, l] = bars[j];
    const fills = [];
    const pend = st.pending;
    st.pending = null;
    if (pend) {
      if (pend.type === "exit" && st.pos) fills.push(closeTrade(st, o, j, "手动离场"));
      if (pend.type === "long" || pend.type === "short") {
        const side = pend.type === "long" ? 1 : -1;
        if (st.pos && st.pos.side !== side) fills.push(closeTrade(st, o, j, "反手"));
        if (!st.pos) {
          st.pos = {side, entry: o, entry_i: j, stop: pend.stop ?? null, stop0: pend.stop ?? null, target: pend.target ?? null};
          fills.push({open: side, px: o, i: j});
        }
      }
    }
    const p = st.pos;
    if (p) {
      const s = p.side, stop = p.stop, tg = p.target;
      const gapStop = stop != null && (s > 0 ? o <= stop : o >= stop);
      const gapTg = tg != null && (s > 0 ? o >= tg : o <= tg);
      const hitStop = stop != null && (s > 0 ? l <= stop : h >= stop);
      const hitTg = tg != null && (s > 0 ? h >= tg : l <= tg);
      if (gapStop) fills.push(closeTrade(st, o, j, "跳空越过止损，按开盘"));
      else if (gapTg) fills.push(closeTrade(st, o, j, "跳空越过止盈，按开盘"));
      else if (hitStop && hitTg) fills.push(closeTrade(st, stop, j, "同根碰止损和止盈，按止损"));
      else if (hitStop) fills.push(closeTrade(st, stop, j, "止损"));
      else if (hitTg) fills.push(closeTrade(st, tg, j, "止盈"));
    }
    st.cursor = j;
    st.max_cursor = Math.max(st.max_cursor, j);
    return fills;
  }

  // 止损/止盈设在持仓上；还没成交就设在挂着的开盘单上。价位与方向不符返回错误文案，不修改。
  function setLevel(st, bars, kind, px) {
    const ref = bars[st.cursor][4];
    const tgt = st.pos || (st.pending && st.pending.type !== "exit" ? st.pending : null);
    if (!tgt) return "先开仓（或挂好开仓单）再设" + (kind === "stop" ? "止损" : "止盈");
    const side = st.pos ? st.pos.side : (st.pending.type === "long" ? 1 : -1);
    const ok = kind === "stop" ? (side > 0 ? px < ref : px > ref) : (side > 0 ? px > ref : px < ref);
    if (!ok) return `${side > 0 ? "多" : "空"}单的${kind === "stop" ? "止损要在现价" + (side > 0 ? "下方" : "上方") : "止盈要在现价" + (side > 0 ? "上方" : "下方")}`;
    if (kind === "stop") { tgt.stop = px; if (st.pos && st.pos.stop0 == null) st.pos.stop0 = px; }
    else tgt.target = px;
    return null;
  }

  function finish(st, bars, reason) {
    if (st.pos) closeTrade(st, bars[st.cursor][4], st.cursor, reason);
    st.pending = null;
    st.done = true;
  }

  function stats(trades) {
    const n = trades.length, rs = trades.filter(t => t.r != null).map(t => t.r);
    const win = trades.filter(t => t.ret_pct > 0).length;
    const sum = a => a.reduce((x, y) => x + y, 0);
    return {n, win, win_rate: n ? win / n : null, sum_pct: sum(trades.map(t => t.ret_pct)),
            n_r: rs.length, avg_r: rs.length ? sum(rs) / rs.length : null, sum_r: sum(rs)};
  }

  return {mulberry32, pick, window: window_, step, setLevel, finish, stats, closeTrade};
})();
if (typeof module !== "undefined") module.exports = Drill;
