// 逐根人工回测练习的纯逻辑（无 DOM）：出题、换算、镜像、按根撮合。页面与 node 单测共用这一份。
// 价格口径：题目起点收盘 = 100；镜像 P' = 100²/P（与 scripts/ps_trigger_v2.mirror 同：高低互换后取倒数，对数涨跌正负对调）。
// 成交约定：决定在当前这根收盘后做出，下一根开盘执行；止损/止盈按下一根起的盘中价判定，跳空越过按开盘价成交；
// 同一根既碰止损又碰止盈，无法知道先后，按止损算（保守）。不计手续费与滑点。
// 结束本题（engine-2，Codex 053 E）：主动提前结束也是「看完当前收盘再决定」，持仓在下一根开盘平（正常成交）；
// 已无下一根、或本题自然走到头时仍持仓，只能按最后收盘平——这是行政盯市（exit_kind="admin"），不是可执行成交，统计里单列。
// engine-1 曾按已知的当前收盘平（等于用已经看到的价成交）。
// R：开仓单成交时就带着的止损才是事前风险（stop0_late=false）；成交后才设第一个止损的标 stop0_late=true，统计里单列。
// 镜像 P' = 10000/P 让对数涨跌对调，但简单收益与 R 不对称：原始 100→110 多单（止损 95）+10%、2R，
// 镜像对应空单（100→90.91，止损 105.26）+9.09%、1.727R。原始与镜像的成绩差不能直接读成方向偏好。
const Drill = (() => {
  const VERSION = "bar-drill-engine-2";   // 撮合口径变了就改这里；页面另记本文件 sha256，旧题按当时的引擎保留、不重判
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

  // kind: "fill" = 按约定可执行的成交；"admin" = 行政盯市（无下一根可成交，按最后收盘记）。
  function closeTrade(st, px, i, reason, kind = "fill") {
    const p = st.pos;
    const t = {side: p.side, entry: p.entry, entry_i: p.entry_i, exit: px, exit_i: i, reason, exit_kind: kind,
               stop0: p.stop0, stop0_late: p.stop0 == null ? null : p.stop0_late === true ? true : p.stop0_late === false ? false : null,
               ret_pct: p.side * (px / p.entry - 1) * 100,
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
          st.pos = {side, entry: o, entry_i: j, stop: pend.stop ?? null, stop0: pend.stop ?? null, target: pend.target ?? null,
                    stop0_late: pend.stop != null ? false : null};
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
    if (kind === "stop") { tgt.stop = px; if (st.pos && st.pos.stop0 == null) { st.pos.stop0 = px; st.pos.stop0_late = true; st.pos.stop0_set_i = st.cursor; } }
    else tgt.target = px;
    return null;
  }

  // early=true：用户主动结束；false：走到最后一根。挂着的开仓单一律作废。返回本次平仓（没有则 null）。
  function finish(st, bars, early) {
    let t = null;
    const j = st.cursor + 1;
    if (st.pos) {
      if (early && j < bars.length) {
        t = closeTrade(st, bars[j][1], j, "提前结束：下一根开盘平仓", "fill");
        st.cursor = j;
        st.max_cursor = Math.max(st.max_cursor, j);
      } else {
        t = closeTrade(st, bars[st.cursor][4], st.cursor,
                       early ? "提前结束但已无下一根：按最后收盘行政盯市" : "本题走到头：按最后收盘行政平仓", "admin");
      }
    }
    st.pending = null;
    st.done = true;
    return t;
  }

  // 正常成交才进胜率、简单收益之和与 R；行政平仓单列。R 分「事前止损」与「迟设止损（或未知）」两组，不混算。
  // sum_pct 是各笔简单百分比直接相加（算术和），不是复利、不是资金曲线。
  function stats(trades) {
    const sum = a => a.reduce((x, y) => x + y, 0);
    const fills = trades.filter(t => t.exit_kind !== "admin"), admin = trades.filter(t => t.exit_kind === "admin");
    const n = fills.length, win = fills.filter(t => t.ret_pct > 0).length;
    const rs = fills.filter(t => t.r != null && t.stop0_late === false).map(t => t.r);
    const rl = fills.filter(t => t.r != null && t.stop0_late !== false).map(t => t.r);
    return {n, win, win_rate: n ? win / n : null, sum_pct: sum(fills.map(t => t.ret_pct)),
            n_r: rs.length, avg_r: rs.length ? sum(rs) / rs.length : null, sum_r: sum(rs),
            n_r_late: rl.length, avg_r_late: rl.length ? sum(rl) / rl.length : null, sum_r_late: sum(rl),
            n_no_stop: fills.filter(t => t.r == null).length,
            n_admin: admin.length, sum_pct_admin: sum(admin.map(t => t.ret_pct))};
  }

  return {VERSION, mulberry32, pick, window: window_, step, setLevel, finish, stats, closeTrade};
})();
if (typeof module !== "undefined") module.exports = Drill;
