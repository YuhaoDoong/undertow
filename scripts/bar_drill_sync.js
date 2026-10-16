// 逐根练习页与本机文件同步时的合并规则（无 DOM；页面与 node 单测共用）。Codex 053 B：同题进度不许被旧快照覆盖。
// 每道题按种子认；动作 actions、成交 trades、标注 marks 只许追加，所以「谁是谁的前缀」就能判断谁更新：
//   本机包含服务端的全部进度 → 用本机（本机领先，或一样）；服务端包含本机 → 用服务端；
//   两边各有对方没有的动作（双标签页各自往下做）→ 分叉：保留服务端那份，本机那份原样放进 conflict_copies，不丢、不重判。
// 本机独有的题接在后面、重新编号。服务端（bar_drill_serve.check_progress）用同一套「只许追加」规则拒绝倒退的写入。
const DrillSync = (() => {
  const APPEND = ["actions", "trades", "marks"];
  const same = (x, y) => JSON.stringify(x) === JSON.stringify(y);
  function isPrefix(a, b) {
    a = a || []; b = b || [];
    if (a.length > b.length) return false;
    for (let i = 0; i < a.length; i++) if (!same(a[i], b[i])) return false;
    return true;
  }
  // a 是否包含 b 的全部进度
  function covers(a, b) {
    return APPEND.every(k => isPrefix(b[k], a[k])) && (a.cursor ?? 0) >= (b.cursor ?? 0)
      && (a.max_cursor ?? 0) >= (b.max_cursor ?? 0) && !(b.done && !a.done);
  }
  function relation(local, server) {
    const l = covers(local, server), s = covers(server, local);
    return l ? "local" : s ? "server" : "diverged";        // 两边一样时取本机（可能有还没存上的备注）
  }
  // local / server: {questions, conflict_copies}；返回合并后的 {questions, conflict_copies, diverged, changed}
  function merge(local, server, now) {
    const sq = (server && server.questions) || [], lq = (local && local.questions) || [];
    const copies = [...((server && server.conflict_copies) || [])];
    let diverged = 0;
    const questions = sq.map(s => {
      const l = lq.find(x => x.seed === s.seed);
      if (!l) return s;
      const r = relation(l, s);
      if (r === "local") return {...l, id: s.id};
      if (r === "diverged") { diverged++; copies.push({stashed_at: now, seed: s.seed, server_id: s.id, reason: "本机与本机文件各有对方没有的操作（多个标签页各自做了这题）；保留文件里那份，本机这份原样存档", question: l}); }
      return s;
    });
    let id = questions.reduce((m, q) => Math.max(m, q.id), 0);
    lq.filter(l => !sq.some(s => s.seed === l.seed)).forEach(l => questions.push({...l, id: ++id}));
    const changed = !same(questions, sq) || copies.length !== ((server && server.conflict_copies) || []).length;
    return {questions, conflict_copies: copies, diverged, changed};
  }
  return {isPrefix, covers, relation, merge};
})();
if (typeof module !== "undefined") module.exports = DrillSync;
