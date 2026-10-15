"""实盘试行状态命令（Codex 043 WP3；044 包 B/C）：只读账户 + 私有状态文件，从不下单、不入金。

  undertow trial status                          # 试行状态、额度、暂停（读长桥权益与成交，只读）
  undertow trial init --planned 500              # 建立试行（计划金额，不是到账事实）
  undertow trial fund --amount 500 --note "…"    # 记录用户在券商端已完成的到账；之后的正数=追加入金、负数=转出
  undertow trial trade --id … --pnl -12.4 --closed-at … --complete --note "…"   # 登记一笔逻辑持仓的含费净结果
  undertow trial verify-trades --note "…"        # 登记「截至现在所有已平逻辑交易都已登记」（对照券商成交核对后）
  undertow trial reconcile [--apply --note "…"]  # 券商流水+成交自动对账（v2）：默认只读候选；阻断项为零时 --apply 才登记并推进覆盖时点
  undertow trial resume --note "…"               # 解除暂停：只按用户明确决定，记原话

状态在 data/account/live_trial/state.json（gitignore）。所有写入走 trial_store.update（锁内读—算—写—回读）。
损坏 → 隔离并当场报告，冻结新增。连续亏损：券商在核对时点之后还有成交 → 不可判定，冻结新增（未知不当 0 笔）。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from undertow.analyze import risk_policy as rp
from undertow.analyze import trial_capital as tc
from undertow.collect import trial_store as ts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fills_after(through: str | None):
    """券商成交记录里 through 之后的最新成交时间：None = 已核查且没有；... = 没核查成（读不到/格式不对）。
    历史接口不含当天，所以当天接口一起读。只读。"""
    if tc.ts(through) is None:
        return ...
    from undertow.collect import longbridge_account as lb
    cut = tc.ts(through)
    try:
        rows = lb.fetch_executions(start=cut.date().isoformat()) + lb.fetch_today_executions()
    except Exception:                                          # noqa: BLE001 —— 读不到 = 没核查，不是没有成交
        return ...
    times = [tc.ts(r.get("time")) for r in rows]
    if any(t is None for t in times):
        return ...                                             # 有成交行没有可比时间：不猜
    later = [t for t in times if t > cut]
    return max(later).isoformat() if later else None


def _evaluate_in_store(equity, equity_complete, asof, latest_fill_at, persist: bool):
    """锁内对最新状态评估（只会置上 latch，不会解除）。→ (state, view, problem)。"""
    out = {}

    def fn(cur):
        s2, v = tc.evaluate(cur, equity=equity, equity_complete=equity_complete, asof=asof, policy=rp.POLICY_B,
                            latest_fill_at=latest_fill_at)
        out["view"] = v
        return s2 if persist else None
    try:
        st = ts.update(fn)
    except ts.TrialStateError as e:
        return None, None, str(e)
    return st, out["view"], None


def _check_identity(st) -> str | None:
    if st["policy_version"] != rp.VERSION_B:
        return f"试行状态的政策版本 {st['policy_version']} ≠ 现行 {rp.VERSION_B}：不自动沿用，需重新确认"
    return None


_FETCH = object()


def pause_view(equity, *, equity_complete: bool, asof: str, persist: bool = True, latest_fill_at=_FETCH) -> tuple[dict | None, str]:
    """给预算入口用的暂停视图；None = 未知/未建立/损坏（预算 → incomplete）。越线时把 latch 写盘（锁内事务）。
    latest_fill_at 缺省时自己去券商读成交核对；测试可直接传（None = 已核查无成交，... = 未核查）。"""
    st, prob = ts.load()
    if prob:
        return None, f"⚠️ {prob} —— 冻结新增"
    if st is None:
        return None, "试行未建立（undertow trial init）：暂停状态未知，预算不放行"
    bad = _check_identity(st)
    if bad:
        return None, f"⚠️ {bad} —— 冻结新增"
    if latest_fill_at is _FETCH:
        latest_fill_at = fills_after(st["trade_coverage"]["through"])
    st2, view, prob = _evaluate_in_store(equity, equity_complete, asof, latest_fill_at, persist)
    if prob:
        return None, f"⚠️ {prob} —— 冻结新增"
    return view["pause"], view.get("note", "")


def cmd_status(args) -> int:
    from undertow.collect import longbridge_account as lb
    st, prob = ts.load()
    if prob:
        print(f"⚠️ {prob}\n新增一律冻结；请检查隔离文件后再决定。"); return 2
    if st is None:
        print("试行未建立。用 `undertow trial init --planned 500` 建立（计划金额，不是到账事实）。"); return 0
    bad = _check_identity(st)
    if bad:
        print(f"⚠️ {bad}；新增冻结。"); return 2
    try:
        equity = lb.fetch_assets().net_assets; complete = equity is not None
    except Exception as e:                                     # noqa: BLE001 —— 账户读不到 = 不可判定
        equity, complete = None, False
        print(f"账户不可用：{str(e)[:120]}")
    fill = fills_after(st["trade_coverage"]["through"])
    st2, v, prob = _evaluate_in_store(equity, complete, _now(), fill, persist=True)
    if prob:
        print(f"⚠️ {prob}\n新增一律冻结。"); return 2
    P = rp.POLICY_B
    R = equity
    f = lambda x: "未知" if x is None else f"${x:,.2f}"
    pct = lambda x: "—" if x is None else format(x, ".1%")
    print(f"试行 {st2['trial_id']} · 政策 {P['version']} · 状态版本 rev {st2['rev']}")
    print(f"计划资本 {f(st2['planned_capital'])} · 起始到账 {f(st2['funded_capital'])} · 外部净现金流 {f(v['net_external_flows'])}")
    print(f"当前权益 R {f(R)}（{tc.EQUITY_BASIS}）")
    print(f"起始资本亏损 {pct(v['initial_capital_loss_frac'])}（剔除外部现金流，暂停判据）"
          f" · 累计投入亏损 {pct(v['cumulative_invested_loss_frac'])}（只报告）"
          f" · 峰值回撤 {pct(v['peak_drawdown_frac'])}（只报告）")
    cov = st2["trade_coverage"]
    print(f"连续亏损 {v['consecutive_losses'] if v['consecutive_losses'] is not None else '不可判定'} 笔"
          f" · 交易结果已核对至 {cov['through'] or '—'}（{cov['source'] or '—'}）"
          f" · 已登记完整交易 {sum(t['complete'] for t in st2['closed_trades'].values())} 笔")
    if v.get("consecutive_losses_note"):
        print(f"  {v['consecutive_losses_note']}")
    pv = v["pause"]
    print("暂停：" + ("未知（不可判定 → 冻结新增）" if pv is None else ("🔴 已暂停：" + (pv["reason"] or "") if pv["latched"] else "未暂停")))
    if v.get("stale_observation"):
        print("（本次观察早于已记录的最新观察：只计算，不写入权益历史）")
    if v.get("note"):
        print(v["note"])
    for name, rule in (("单笔计划止损", P["stop_loss"]), ("单笔理论最大亏损", P["max_loss"]),
                       ("金银合计", P["categories"]["metals"]), ("其它合计", P["categories"]["other"]), ("全场合计", P["account"])):
        print(f"  {name} ≤ {f(rp.limit_usd(rule, R))}（max({rule['frac']:.0%}·R, ${rule['floor_usd']:.0f})）")
    print(f"  大仓（理论最大亏损 > ${P['big_position']['threshold_usd']:.0f}）同时最多 {P['big_position']['max_count']} 笔；止盈 {P['take_profit_gain_frac']:.0%}")
    print("政策额度 ≠ 能开：价差权限、组合占用、到期/指派处置需在券商 App 核实。本工具只读，从不下单、不入金。")
    return 0


def _write(fn, *, create=False) -> tuple[dict | None, str | None]:
    try:
        return ts.update(fn, create=create), None
    except (ts.TrialStateError, ValueError) as e:
        return None, str(e)


def cmd_init(args) -> int:
    tid = f"trial-{datetime.now(timezone.utc):%Y%m%d}"
    st, err = _write(lambda cur: tc.new_state(trial_id=tid, planned_capital=float(args.planned),
                                              policy_version=rp.VERSION_B, created_at=_now()), create=True)
    if err:
        print(err); return 1
    print(f"已建立 {tid}：计划资本 ${float(args.planned):,.2f}（计划，不是到账）"); return 0


def cmd_fund(args) -> int:
    if not (args.note or "").strip():
        print("需要用户原话（--note）"); return 1
    st, err = _write(lambda cur: tc.record_funding(cur, amount=float(args.amount), at=_now(), note=args.note))
    if err:
        print(err); return 1
    kind = "起始到账" if not st["external_flows"] else ("追加入金" if args.amount > 0 else "转出")
    print(f"已记录{kind} ${abs(float(args.amount)):,.2f}（外部现金流从权益里剔除，不洗掉亏损、不解除暂停）"); return 0


def cmd_trade(args) -> int:
    if not (args.note or "").strip():
        print("需要来源说明（--note：用户原话或对照的流水）"); return 1
    st, err = _write(lambda cur: tc.record_trade(cur, trade_id=args.id, net_pnl_usd=args.pnl, closed_at=args.closed_at,
                                                 complete=args.complete, source="人工登记", note=args.note,
                                                 revise=args.revise))
    if err:
        print(err); return 1
    print(f"已登记 {args.id}：含费净结果 ${args.pnl:,.2f}，{'完整平仓' if args.complete else '部分平仓（不计入连续亏损）'}")
    print("登记完所有已平交易后，用 `undertow trial verify-trades --note …` 确认覆盖时点；否则连续亏损仍不可判定。")
    return 0


def cmd_verify_trades(args) -> int:
    st, prob = ts.load()
    if prob or st is None:
        print(prob or "试行未建立"); return 1
    now = _now()
    fill = fills_after(st["trade_coverage"]["through"])
    if fill is ...:
        print("读不到券商成交记录：无法核对，覆盖时点不推进"); return 1
    print(f"核对区间 {st['trade_coverage']['through']} → {now}：券商最新成交 {fill or '无'}")
    st2, err = _write(lambda cur: tc.verify_trades(cur, through=now, verified_at=now, source="人工核对", note=args.note))
    if err:
        print(err); return 1
    print(f"已确认：截至 {now} 的已平逻辑交易都已登记（{len(st2['closed_trades'])} 条）"); return 0


def _chunks(start_iso: str, now_iso: str, chunk_days: int):
    """账户本地日期分段（实测 start/end 是本地日期、end 含当天）：首尾各多留一天，段与段之间不重叠。"""
    from datetime import timedelta
    d0, d1 = tc.ts(start_iso).date() - timedelta(days=1), tc.ts(now_iso).date() + timedelta(days=1)
    a = d0
    while a <= d1:
        b = min(a + timedelta(days=chunk_days - 1), d1)
        yield a.isoformat(), b.isoformat()
        a = b + timedelta(days=1)


def _cash_flow_since(lb, start_iso: str, now_iso: str, chunk_days: int = 7, reads: list | None = None) -> list[dict]:
    """按 ≤7 天【互不重叠】分段读资金流水（长区间会报 internal server error，2026-10-15 实测）。任一段失败 → 抛出，不拼半截。
    不做整行去重：同一秒同金额的两笔真实成交（实测 8/19 一笔 +122.00 连续三行）不能被合并。每段行数记入 reads。"""
    rows = []
    for a, b in _chunks(start_iso, now_iso, chunk_days):
        got = lb.fetch_cash_flow(start=a, end=b)
        if reads is not None:
            reads.append({"what": "cash-flow", "start": a, "end": b, "n": len(got), "ok": True})
        rows += got
    return rows


def _executions_since(lb, start_iso: str, now_iso: str, chunk_days: int = 7, reads: list | None = None) -> list[dict]:
    """历史成交按 ≤7 天分段 + 当日接口（历史接口不含当天）。当日行若整体已出现在历史结果里 → 视为重叠、不重复计；
    部分重叠 → 读数记为未完成（同秒同价同量的重复行是真实成交，不能靠整行去重）。"""
    from collections import Counter
    rows = []
    for a, b in _chunks(start_iso, now_iso, chunk_days):
        got = lb.fetch_executions(start=a, end=b)
        if reads is not None:
            reads.append({"what": "executions", "start": a, "end": b, "n": len(got), "ok": True})
        rows += got
    today = lb.fetch_today_executions()
    key = lambda r: tuple(sorted((str(k), str(v)) for k, v in r.items()))
    hist, tod = Counter(map(key, rows)), Counter(map(key, today))
    inter = sum((hist & tod).values())
    ok = inter == 0 or inter == sum(tod.values())
    if reads is not None:
        reads.append({"what": "executions-today", "start": "today", "end": "today", "n": len(today), "ok": ok})
    return rows + (today if inter == 0 else [])


def _state_fingerprint(st) -> str:
    """提案绑定的状态指纹：rev、覆盖、已登记交易（含来源/结果）、暂停。锁内用最新状态重算，变了就拒绝。"""
    import hashlib
    import json
    core = {"rev": st.get("rev"), "coverage": st.get("trade_coverage"), "closed": st.get("closed_trades"),
            "latched": st.get("pause_latched")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def cmd_reconcile(args) -> int:
    """券商资金流水 + 成交 → 逻辑交易的自动对账（trial-reconcile-v2.1）。默认只打印候选；--apply 只在阻断项为零时登记
    （需 --note 写用户原话）。Codex 048：正确性未闭环前它不替代人工 trade/verify-trades 协议。"""
    from undertow.analyze import trial_reconcile as tr
    from undertow.collect import longbridge_account as lb
    st, prob = ts.load()
    if prob or st is None:
        print(prob or "试行未建立"); return 1
    if st["funded_at"] is None:
        print("试行尚未到账（undertow trial fund）：没有对账起点"); return 1
    now = _now()
    cov = st["trade_coverage"]["through"]
    fp = _state_fingerprint(st)
    reads: list = []
    try:
        flows = _cash_flow_since(lb, st["funded_at"], now, reads=reads)
        ex = _executions_since(lb, st["funded_at"], now, reads=reads)
        pos = {p.symbol: p.quantity for p in lb.fetch_positions()}
    except Exception as e:                                     # noqa: BLE001 —— 读不到 = 没对账，不是没有交易
        print(f"读不到券商数据，未对账、不登记、不推进：{str(e)[:200]}"); return 1
    out = tr.reconcile(cash_flow=flows, positions=pos, executions=ex, coverage_through=cov, trial_start=st["funded_at"],
                       now=now, known_trades=st["closed_trades"], reads=reads)
    print(f"对账 {out['schema']} · 窗口 {st['funded_at']} → {now} · 覆盖时点 {cov}")
    lab = {"open": "在场", "closed": "已平", "ambiguous": "⚠️ 不确定"}
    for g in out["groups"]:
        pnl = "" if g["net_pnl_usd"] is None else f" · 含费净 ${g['net_pnl_usd']:,.2f}"
        fee = "" if g["state"] != "closed" else (" · 费用已配（一桶一条推定）" if g["fees_settled"] else " · 费用未齐")
        print(f"  [{lab[g['state']]}] {g['underlying']} {','.join(g['expiries'])} {' / '.join(g['contracts'])}{pnl}{fee}")
        for f in g["flags"]:
            print(f"      {f}")
    for x in out["external"]:
        print(f"  外部现金流候选（原币 {x['currency']} {x['amount']}，@ {x['time']}）：本命令不登记、不折算；"
              "美元到账额以券商记录为准，用 trial fund 人工登记；换汇是内部转换，不是第二次入金")
    for h in out["history_issues"]:
        print(f"  ·（覆盖时点之前，不阻断）{h}")
    for b in out["blockers"]:
        print(f"  ✋ {b}")
    ev = out.get("evidence") or {}
    print(f"  证据等级：读数 {ev.get('reads')}（只证明读完本次声明的日期段，不证明券商源绝对完整）")
    for a in ev.get("assumptions", []):
        print(f"  · 假设：{a}")
    new = [r for r in out["records"] if not r["already_recorded"]]
    print(f"可登记 {len(new)} 笔 · 覆盖时点{'可' if out['can_advance_coverage'] else '不可'}推进")
    if not args.apply:
        print("（只读候选；阻断项为零时才可 --apply --note \"用户原话\" 登记）"); return 0
    if not (args.note or "").strip():
        print("--apply 需要 --note（用户原话）"); return 1
    if not out["can_advance_coverage"]:
        print("有阻断项：不登记、不推进（部分可信时不部分登记）"); return 1

    def fn(cur):
        if _state_fingerprint(cur) != fp:
            raise ValueError("对账期间试行状态已改变（rev/覆盖/已登记交易/暂停）：拒绝，请重跑对账")
        manual = [k for k, t in cur["closed_trades"].items()
                  if t.get("source") not in tr.SOURCES_AUTO and tc.ts(t["closed_at"]) > tc.ts(cov)]
        if manual and new:
            raise ValueError(f"覆盖时点之后已有人工登记 {manual[:3]}：自动登记可能重复计数，拒绝；请人工核对后再 verify-trades")
        s2 = cur
        for r in new:
            s2 = tc.record_trade(s2, trade_id=r["trade_id"], net_pnl_usd=r["net_pnl_usd"], closed_at=r["closed_at"],
                                 complete=True, source=tr.SOURCE, note=args.note)
        return tc.verify_trades(s2, through=now, verified_at=now, source=tr.SOURCE, note=args.note)
    st2, err = _write(fn)
    if err:
        print(err); return 1
    print(f"已登记 {len(new)} 笔；覆盖时点 {st2['trade_coverage']['through']}")
    return 0


def cmd_resume(args) -> int:
    st, prob = ts.load()
    if prob or st is None:
        print(prob or "试行未建立"); return 1
    if not st["pause_latched"]:
        print("当前未暂停"); return 0
    st2, err = _write(lambda cur: tc.resume(cur, user_note=args.note, at=_now()) if cur["pause_latched"] else None)
    if err:
        print(err); return 1
    print("已按用户决定解除暂停；连续亏损从此刻重新计数")
    w = tc.relock_warning(st2, rp.POLICY_B)
    if w:
        print(w)
    return 0


def register(sub):
    p = sub.add_parser("trial", help="实盘试行（政策 B）状态、额度与暂停；只读，从不下单、不入金")
    ss = p.add_subparsers(dest="trial_cmd", required=True)
    ss.add_parser("status").set_defaults(func=cmd_status)
    i = ss.add_parser("init"); i.add_argument("--planned", type=float, required=True); i.set_defaults(func=cmd_init)
    fd = ss.add_parser("fund"); fd.add_argument("--amount", type=float, required=True); fd.add_argument("--note", required=True)
    fd.set_defaults(func=cmd_fund)
    t = ss.add_parser("trade", help="登记一笔逻辑持仓的含费净实现结果（来自券商流水，人工核对）")
    t.add_argument("--id", required=True); t.add_argument("--pnl", type=float, required=True)
    t.add_argument("--closed-at", required=True, help="带时区 ISO 时间，如 2026-10-20T15:30:00-04:00")
    g = t.add_mutually_exclusive_group(required=True)
    g.add_argument("--complete", action="store_true"); g.add_argument("--partial", dest="complete", action="store_false")
    t.add_argument("--note", required=True); t.add_argument("--revise", action="store_true")
    t.set_defaults(func=cmd_trade)
    v = ss.add_parser("verify-trades", help="确认截至现在所有已平逻辑交易都已登记"); v.add_argument("--note", required=True)
    v.set_defaults(func=cmd_verify_trades)
    rc = ss.add_parser("reconcile", help="券商流水+成交自动对账（默认只读候选；阻断项为零时 --apply 登记）")
    rc.add_argument("--apply", action="store_true"); rc.add_argument("--note", default="")
    rc.set_defaults(func=cmd_reconcile)
    r = ss.add_parser("resume"); r.add_argument("--note", required=True); r.set_defaults(func=cmd_resume)
