"""命令行入口：拉数据 -> 分析 -> 渲染报告。

用法示例（在项目根 /Users/yhdong/Trading 下运行）:
    python -m undertow.cli analyze                 # 默认全部品种
    python -m undertow.cli analyze gold silver     # 指定品种
    python -m undertow.cli analyze --lookback 104  # 自定义回看周数
    python -m undertow.cli analyze --json          # 输出结构化 JSON（喂给上层/LLM）
    python -m undertow.cli list                    # 列出已配置品种
    python -m undertow.cli --no-cache analyze gold # 强制绕过缓存
"""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import subprocess
import json
import sys
from datetime import date, timedelta

from undertow.core.config import load_config, DATA_DIR
from undertow.core.clock import market_today
from undertow.core.calendar import load_events, upcoming, merge as merge_events, CATEGORY_LABEL
from undertow.collect.store import SnapshotStore
from undertow.collect.faireconomy_cal import FairEconomyCalSource
from undertow.collect.cftc_cot import CftcCotSource
from undertow.collect.cboe_options import (CboeOptionsSource, snapshot_from_payload,
                                          chain_fingerprint, oi_change_total)
from undertow.collect.cboe_history import CboeHistorySource
from undertow.collect.yahoo_futures import YahooFuturesSource
from undertow.collect.fred_macro import FredMacroSource
from undertow.collect.cboe_vol import CboeVolSource
from undertow.analyze.vrp_history import assess_vrp_history, render_markdown as vrp_md
from undertow.analyze.positioning import analyze
from undertow.analyze.signals import generate_signals, net_bias
from undertow.analyze.gamma import (analyze_gamma, structure_delta,
                                   support_ladder, ladder_bands, wall_agreement)
from undertow.analyze.flow import _live as _flow_live
from undertow.analyze.credit_wall import propose as cw_propose
from undertow.analyze.backmonth import scan as backmonth_scan
from undertow.analyze.cost_gate import candidates as cost_candidates
from undertow.analyze.flow import (analyze_flow, counter_signals, tradeable_info, detect_ratio_spreads,
                                   flip_driver_summary, structural_moves,
                                   detect_strong_signal, probe_strong_signal)
from undertow.analyze.outlook import (build_outlook, macro_to_votes,
                                      plain_summary_blocks)
from undertow.analyze.strategy import build_strategy
from undertow.analyze.expiry_ladder import build_ladder
from undertow.analyze.fibonacci import build_fibonacci
from undertow.analyze.risk_reward import build_risk_reward
from undertow.analyze.verdict import build_verdict
from undertow.analyze.macro import analyze_macro, series_ids_for
from undertow.analyze.backtest import run_backtest
from undertow.report import markdown as report_mod
from undertow.report import viz
from undertow.analyze.family import check as _family_check
from undertow.analyze.indicators import build as _build_labels
from undertow.report.html import (render_report_html, render_index_html,
                          render_wall_layers_section,
                          render_wall_history,
                          render_tradeable_gate,
                          render_cost_gate,
                          render_backmonth,
                          render_ratio_spreads,
                          render_credit_wall,
                          render_flow_section, render_macro_section, render_events_section,
                          render_tldr_section, render_strategy_section,
                          render_concentration_html, render_vol_regime_section,
                          render_vol_analysis_section,
                          render_strategy_hub, render_condor_section,
                          render_credit_spread_section, render_expiry_ladder_section,
                          render_fib_rr_section, render_strong_signal_banner,
                          render_structure_section, render_vintage_banner,
                          render_verdict_section, render_technicals_section)
from undertow.analyze.volregime import assess_vol_regime
from undertow.analyze.condor import assess_condor
from undertow.analyze.credit_spread import assess_credit_spread
from undertow.analyze.strategy_hub import assemble_strategies


def _resolve_instruments(cfg, names: list[str]) -> list:
    if not names:
        return list(cfg.instruments.values())
    return [cfg.get(n) for n in names]


def cmd_list(args) -> int:
    cfg = load_config()
    print("已配置品种:")
    for inst in cfg.instruments.values():
        layers = []
        if inst.cot is not None:
            layers.append(f"COT {inst.cot.contract_market_code}/{inst.cot.report.split('_')[0]}")
        if inst.options is not None:
            layers.append(f"期权 {inst.options.symbol}")
        if inst.commodity is not None:
            layers.append(f"价 {inst.commodity.symbol}")
        if inst.vol_index:
            layers.append(f"波动率 {inst.vol_index}")
        print(f"  {inst.key:8s} {inst.display_name:28s} [{' · '.join(layers)}]")
    return 0


def _merged_events(no_live: bool, no_cache: bool):
    """手维护锚点 + 实时 feed（FairEconomy 公开 JSON）合并去重。feed 失败优雅降级。"""
    manual = load_events()
    if no_live:
        return manual, "（仅手维护锚点）"
    try:
        live = FairEconomyCalSource().fetch_events(use_cache=not no_cache)
    except Exception as e:  # 网络/解析任意异常都退回手维护
        print(f"[提示] 实时日历 feed 跳过，仅用手维护锚点: {e}", file=sys.stderr)
        return manual, "（feed 不可用，仅手维护锚点）"
    if not live:
        return manual, "（feed 本周无匹配事件，仅手维护锚点）"
    return merge_events(manual, live), f"（含 FairEconomy 实时 feed {len(live)} 条 + 手维护远期锚点）"


def cmd_calendar(args) -> int:
    """事件雷达：未来关键节点（FOMC/数据/COT/到期），美东日历，带预测/前值。"""
    today = market_today()
    events, src_note = _merged_events(args.no_live, args.no_cache)
    if not events:
        print("事件表为空（config/calendar.json 缺失或无条目）。", file=sys.stderr)
        return 1
    inst = args.instruments[0] if args.instruments else None
    evs = upcoming(events, today=today, within_days=args.within, instrument=inst)
    scope = f"·{inst}" if inst else ""
    print(f"事件雷达（美东 · 未来 {args.within} 天{scope}） · 今日 {today.isoformat()}")
    print(f"  来源 {src_note}")
    if not evs:
        print("  （窗口内无登记事件）")
        return 0
    for e in evs:
        cat = CATEGORY_LABEL.get(e.category, e.category)
        when = e.date.isoformat() + (f" {e.time_et}ET" if e.time_et and e.time_et != "—" else "")
        tag = " (FF)" if e.source == "ff" else ""
        line = f"  {e.mark} {e.tminus(today):>5s}  {when:20s} [{cat}] {e.name}{tag}"
        print(line)
        cons = e.consensus()
        if cons:
            print(f"            ├ {cons}")
        if e.note:
            print(f"            └ {e.note}")
    print("\n临近 FOMC/CPI/非农请主动降置信、警惕跳空；OPEX 前 Gamma/OI 墙失真。")
    print("预测/前值/影响来自 ForexFactory/FairEconomy 公开日历 feed，仅供参考。")
    return 0


def _analyze_one(source, inst, lookback, use_cache):
    history = source.fetch_history(inst, lookback=lookback, use_cache=use_cache)
    an = analyze(history)
    signals = generate_signals(an)
    return history, an, signals


def cmd_analyze(args) -> int:
    cfg = load_config()
    lookback = args.lookback or cfg.lookback_weeks
    source = CftcCotSource()

    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    results = []
    for inst in instruments:
        if inst.cot is None:  # 无期货持仓的品种（如个股）—— 持仓命令不适用
            print(f"[跳过] {inst.key} 无 COT 持仓数据（个股等无期货持仓）", file=sys.stderr)
            continue
        try:
            results.append((inst, *_analyze_one(source, inst, lookback, not args.no_cache)))
        except Exception as e:  # 单品种失败不影响其它
            print(f"[警告] {inst.key} 分析失败: {e}", file=sys.stderr)

    if not results:
        print("没有可用结果。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([_to_jsonable(inst, an, sigs) for inst, _, an, sigs in results],
                         ensure_ascii=False, indent=2, default=str))
        return 0

    blocks = [report_mod.render(an, sigs, inst.display_name,
                                report_kind=inst.cot.report if inst.cot else "disaggregated_fut")
              for inst, _, an, sigs in results]
    print(report_mod.render_all(blocks))
    return 0


def _realtime_multiplier(inst, spot, *, no_cache):
    """真实期货价/ETF现价的【实时比值】——与 report 口径统一，免静态乘数漂移。

    取不到真实期货价时回退静态乘数（inst.options.approx_commodity_multiplier）。
    """
    static = inst.options.approx_commodity_multiplier if inst.options else None
    if inst.commodity is not None and spot and spot > 0:
        try:
            _s, real_price, _a = YahooFuturesSource().fetch_for(inst, use_cache=not no_cache)
            if real_price and real_price > 0:
                return real_price / spot
        except Exception:
            pass
    return static


def cmd_gamma(args) -> int:
    cfg = load_config()
    source = CboeOptionsSource()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    results = []
    for inst in instruments:
        if inst.options is None:
            print(f"[跳过] {inst.key} 未配置期权数据源", file=sys.stderr)
            continue
        try:
            snap = source.fetch_snapshot(inst, use_cache=not args.no_cache)
            ga = analyze_gamma(
                snap,
                multiplier=_realtime_multiplier(inst, snap.spot, no_cache=args.no_cache),
                proxy_quality=inst.options.proxy_quality,
                horizon_days=args.horizon,
            )
            results.append((inst, ga))
        except Exception as e:
            print(f"[警告] {inst.key} 期权分析失败: {e}", file=sys.stderr)

    if not results:
        print("没有可用结果。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([_gamma_jsonable(inst, ga) for inst, ga in results],
                         ensure_ascii=False, indent=2, default=str))
        return 0

    blocks = [report_mod.render_gamma(ga, inst.display_name) for inst, ga in results]
    print(report_mod.render_gamma_all(blocks))
    return 0


def cmd_backtest(args) -> int:
    cfg = load_config()
    lookback = args.lookback or cfg.lookback_weeks
    cot_src = CftcCotSource()
    px_src = CboeHistorySource()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    results = []
    for inst in instruments:
        if inst.cot is None or inst.price is None:
            print(f"[跳过] {inst.key} 缺 COT 或 price 数据源（回测需两者）", file=sys.stderr)
            continue
        try:
            history = cot_src.fetch_history(inst, lookback=lookback, use_cache=not args.no_cache)
            price = px_src.fetch_series(inst, use_cache=not args.no_cache)
            bt = run_backtest(history, price, horizons=tuple(args.horizons))
            results.append((inst, bt))
        except Exception as e:
            print(f"[警告] {inst.key} 回测失败: {e}", file=sys.stderr)

    if not results:
        print("没有可用结果。", file=sys.stderr)
        return 1

    if args.json:
        import dataclasses
        print(json.dumps([dataclasses.asdict(bt) | {"instrument": inst.key} for inst, bt in results],
                         ensure_ascii=False, indent=2, default=str))
        return 0

    blocks = [report_mod.render_backtest(bt, inst.display_name, inst.price.quality)
              for inst, bt in results]
    print(report_mod.render_backtest_all(blocks))
    return 0


def _flow_facts(fa, ga, ga_prev, snap_prev, snap_curr, spot: float, ref) -> dict:
    """索引页要的【具体事实】：墙在哪、变了多少、call/put 谁在建仓、最大的几笔是什么。

    动机（用户 2026-08-28）：索引页原来八个品种全是「不做空·回调买·长线拿住」，
    模板化车轱辘话，等于什么都没说。用户要的是：
      近端/中期方向 + 是否强信号 + call/put 主要变动。
    这里只出**原始事实**（张数、行权价、Delta），不出自造术语。
    """
    out: dict = {}
    if ga is not None:
        pm = {}
        for x in snap_prev.contracts:
            pm[(x.kind, x.strike)] = pm.get((x.kind, x.strike), 0) + (x.open_interest or 0)
        cm = {}
        for x in snap_curr.contracts:
            cm[(x.kind, x.strike)] = cm.get((x.kind, x.strike), 0) + (x.open_interest or 0)
        for side, kind, wall in (("put", "P", ga.put_wall), ("call", "C", ga.call_wall)):
            if wall and wall > 0:
                oi = cm.get((kind, wall), 0)
                out[f"{side}_wall"] = wall
                out[f"{side}_wall_oi"] = oi
                out[f"{side}_wall_chg"] = oi - pm.get((kind, wall), 0)
                # 墙【搬家】比墙变厚更重要：420P→413P 说明承接位整体后撤，
                # 只报「413 加厚 38,449」会让人以为支撑变强了。
                pw = getattr(ga_prev, f"{side}_wall", None) if ga_prev is not None else None
                if pw and abs(pw - wall) > 1e-9:
                    out[f"{side}_wall_from"] = pw
    if fa is not None and fa.changes:
        ch = fa.changes
        out["call_add"] = sum(x.d_oi for x in ch if x.kind == "C" and x.d_oi > 0)
        out["put_add"] = sum(x.d_oi for x in ch if x.kind == "P" and x.d_oi > 0)
        # ⚠️ 只报 call/put 张数比会【误导】：白银 2026-08-28 就是活例子——
        # index 写「call 是 put 的 2.2 倍」看着像看涨，可那批 call（69C/67C，
        # 纯净度 0.95+）全是【卖方压制】，卖上方 call 是看跌动作。当天白银 -4.38%。
        # 必须按买卖方判定归类：买put/卖call = 看跌侧，买call/卖put = 看涨侧。
        # 用 flow 已有的 bias（bearish/bullish/neutral）而不是匹配 judgment 文案——
        # judgment 有「买方保护」「买方轻微保护」「买方保护(新建·主动方未知)」等多种
        # 变体，字符串匹配必漏。bias 本来就是给聚合用的粗方向。
        out["bear_add"] = sum(x.d_oi for x in ch if x.d_oi > 0 and x.bias == "bearish")
        out["bull_add"] = sum(x.d_oi for x in ch if x.d_oi > 0 and x.bias == "bullish")
        # 按剩余到期分桶：pressure 是 45 天内加总的，加总会掩盖"近月看跌、
        # 远月看涨"这类结构（用户 2026-08-29 追问）
        from undertow.analyze.flow import (expiry_split, expiry_split_conflict,
                                            dominant_expiry)
        _sp = expiry_split(fa)
        out["exp_split"] = _sp
        out["exp_conflict"] = expiry_split_conflict(_sp)
        from undertow.analyze.flow import dte_agreement as _dte_agree
        out["exp_agreement"] = _dte_agree(_sp)   # agree/conflict/insufficient 三态
        # 主力到期：由数据决定哪个到期日占大头，不预先划桶（用户 2026-08-29）
        out["dominant"] = dominant_expiry(ch, getattr(fa, "curr_date", None))
        # 保护迁移的结构描述 —— 只陈述"钱从哪撤到哪、哪档涨价最急"，不做预测。
        # 这是用户 2026-08-29 点名要置顶高亮的那段描述。
        try:
            from undertow.analyze.flow import wall_structure
            pw_ = getattr(ga, "put_wall", None) if ga is not None else None
            # wall_structure 取代旧的 migration_text：后者只认「保护向下搬家」
            # 这一种形态，白银 2026-08-28「就地加固、守住 60」什么都输出不了。
            out["migration"] = wall_structure(fa, pw_, spot)
        except Exception as e:
            print(f"⚠️ 保护迁移描述失败：{type(e).__name__}: {e}", file=sys.stderr)
        # 卖方价差候选：index 只带最优腿位，明细在品种报告
        try:
            from undertow.analyze.credit_wall import propose as _cwp
            from undertow.analyze.flow import tradeable_info as _ti_fn
            _t = _ti_fn(fa)
            # ref 是 OI 所属日；可执行日是快照日 = ref 的下一个工作日
            _exec_ref = getattr(snap_curr, "asof_date", None) or _next_weekday(ref)
            if _t.get("side") in ("看涨", "看跌") and ga is not None:
                _cw = {}
                for _tier in ("conservative", "aggressive"):
                    _v = _cwp(snap_curr, ref, spot, _t["side"], _t["ratio"],
                              tier=_tier, execution_date=_exec_ref)
                    if _v.ok and _v.spreads:
                        _s0 = _v.spreads[0]
                        _cw[_tier] = {"sell": _s0.sell_strike, "buy": _s0.buy_strike,
                                      "kind": _s0.kind, "expiry": _s0.expiry.isoformat(),
                                      "dte": _s0.dte, "credit": _s0.credit,
                                      "occ": _s0.occupancy, "buffer": _s0.buffer_pct}
                    elif not _v.ok and _tier == "conservative":
                        out["credit_wall_blocked"] = _v.reason[:80]
                if _cw:
                    out["credit_wall"] = _cw
        except Exception as e:
            print(f"⚠️ 卖方价差候选失败：{type(e).__name__}: {e}", file=sys.stderr)
    # 持续墙：排除 <7 天到期后的承接/压制区 —— 这才是"跌到哪有人接"的答案。
    # 现行墙位会被 0DTE 劫持：2026-08-28 黄金 put 墙报 413，其 42,388 张里
    # 40,394 张（95%）当天到期，收盘即归零；排除后第一大是 400（≈金价 4416），
    # 那才是真正多到期分布的承接区。
    try:
        from undertow.analyze.gamma import persistent_walls
        out["persist"] = persistent_walls(snap_curr, ref)
    except Exception as e:
        print(f"⚠️ 持续墙计算失败：{type(e).__name__}: {e}", file=sys.stderr)
        # 最大的几笔新建仓（含 Delta，供判断是尾部险还是贴身防御）
        big = sorted([x for x in ch if x.d_oi > 0], key=lambda x: -x.d_oi)[:3]
        out["big_legs"] = [{
            "expiry": x.expiry.isoformat()[5:], "strike": x.strike, "kind": x.kind,
            "d_oi": x.d_oi, "delta": x.delta,
            # ⚠️ 参照点必须是【快照描述的交易日】，不是今天：这批 OI 变化发生在
            # 那一天，"还剩几天到期" 只有相对那天才有意义。用 today 在数据过期时
            # 会算出「-1天后到期」（2026-08-29 实际出现过）。
            "dte": (x.expiry - ref).days,
            "pct": (x.strike / spot - 1) * 100 if spot else 0.0,
        } for x in big]
    return out


def _write_status(path: str | None, payload: dict) -> None:
    """把本次运行的机器可读状态原子写盘。**自动化只读它，不许 grep 人读文案。**

    动机（codex review 2026-08-28）：daily_update.sh 原先靠 grep 中文提示串
    （'快照失败'/'没有保存任何快照'/'研判报告失败'）来判断成败 —— 脆弱耦合，
    改一句文案告警就静默失效，而我们恰恰在修"静默失败"。
    人读输出保持不变，另写一份 JSON 供脚本消费。

    写不出来不抛异常（不能因为状态文件拖垮主流程），但会打到 stderr —— 不静默。
    """
    if not path:
        return
    import json as _json
    import os as _os
    try:
        tmp = f"{path}.tmp.{_os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump({"schema": 1, **payload}, f, ensure_ascii=False, indent=1)
        _os.replace(tmp, path)      # 原子改名：读方永远看到完整文件
    except Exception as e:
        print(f"[警告] 状态文件写入失败 {path}: {type(e).__name__} {e}", file=sys.stderr)


def cmd_snapshot(args) -> int:
    """把当前期权链【原始 payload 全字段】按日落盘——攒 flow 层所需的历史。"""
    cfg = load_config()
    source = CboeOptionsSource()
    store = SnapshotStore()
    today = market_today()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    saved = []
    items: list[dict] = []          # 逐品种状态，供 --status-file
    for inst in instruments:
        if inst.options is None:
            print(f"[跳过] {inst.key} 未配置期权数据源", file=sys.stderr)
            items.append({"instrument": inst.key, "status": "skipped_no_source"})
            continue
        sym = inst.options.symbol
        try:
            payload = source.fetch_raw(inst, use_cache=not args.no_cache)
            already = store.load("options", sym, today) is not None
            path, skipped = _save_snapshot_dedup(store, inst, sym, payload, today)
            if skipped and not already:
                print(f"[提示] {inst.key} 期权数据与上一交易日逐行相同（休市重复），跳过落盘",
                      file=sys.stderr)
                items.append({"instrument": inst.key, "status": "unchanged"})
                continue
            if skipped and already:
                items.append({"instrument": inst.key, "status": "already_present"})
            snap = snapshot_from_payload(payload, inst.key, sym)
            n_oi = len(snap.with_oi())
            n_dates = len(store.dates("options", sym))
            saved.append((inst, sym, path, len(snap.contracts), n_oi, n_dates))
            items.append({"instrument": inst.key, "status": "saved",
                          "contracts": len(snap.contracts), "with_oi": n_oi})
        except Exception as e:
            print(f"[警告] {inst.key} 快照失败: {e}", file=sys.stderr)
            items.append({"instrument": inst.key, "status": "failed",
                          "error_type": type(e).__name__, "error": str(e)[:200]})

    n_fail = sum(1 for x in items if x["status"] == "failed")
    n_saved = len(saved)
    # overall 四态：complete(全部处理妥当) / partial(有成功也有失败) /
    #              failed(一个都没成且有失败) / unchanged(全部因 OI 未结算跳过，属正常)
    if n_fail == 0:
        overall = "complete" if n_saved else "unchanged"
    elif n_saved:
        overall = "partial"
    else:
        overall = "failed"
    _write_status(getattr(args, "status_file", None), {
        "command": "snapshot", "date": str(today), "overall": overall,
        "n_saved": n_saved, "n_failed": n_fail, "items": items})

    if not saved:
        print("没有保存任何快照。", file=sys.stderr)
        return 1

    print(f"已落盘 {today} 期权链快照（原始全字段，纳入 git 永久留存）:")
    for inst, sym, path, n_all, n_oi, n_dates in saved:
        print(f"  {inst.key:7s} {sym}: {n_all:,} 合约（{n_oi:,} 有OI）  "
              f"→ 已累计 {n_dates} 天  ·  {path}")
    if any(nd < 2 for *_, nd in saved):
        print("\n提示：日对日 ΔOI/ΔIV 异动需要 ≥2 天快照。明天再跑一次 snapshot，"
              "之后 `flow` 即可出那种「近月大单异动」。")
    return 0


def cmd_flow(args) -> int:
    """期权资金流/持仓异动：单快照异常活跃 + 两日 ΔOI/ΔIV diff。"""
    cfg = load_config()
    source = CboeOptionsSource()
    store = SnapshotStore()
    today = market_today()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    results = []
    for inst in instruments:
        if inst.options is None:
            print(f"[跳过] {inst.key} 未配置期权数据源", file=sys.stderr)
            continue
        sym = inst.options.symbol
        try:
            # 当前快照：优先用刚落盘的今日数据；若今日未落盘则现拉一份（不落盘，仅分析）
            if not args.no_snapshot and store.load("options", sym, today) is None:
                payload = source.fetch_raw(inst, use_cache=not args.no_cache)
                _, skipped = _save_snapshot_dedup(store, inst, sym, payload, today)
                if skipped:
                    print(f"[提示] {inst.key} 期权数据与上一交易日逐行相同（休市重复），跳过落盘",
                          file=sys.stderr)

            stored = store.latest_two("options", sym, on_or_before=today if replay else None)
            if stored:
                curr_date, curr_payload = stored[-1]
                curr = snapshot_from_payload(curr_payload, inst.key, sym)
                prev = None
                prev_date = None
                if len(stored) == 2:
                    prev_date_d, prev_payload = stored[0]
                    prev = snapshot_from_payload(prev_payload, inst.key, sym)
                    prev_date = prev_date_d.isoformat()
                curr_date_s = curr_date.isoformat()
            else:
                # 一份都没有：现拉一份分析（提示去 snapshot 攒历史）
                payload = source.fetch_raw(inst, use_cache=not args.no_cache)
                curr = snapshot_from_payload(payload, inst.key, sym)
                prev, prev_date, curr_date_s = None, None, today.isoformat()

            # 拿静态墙位叠加（乘数用实时比值，与 report 口径统一）
            ga = analyze_gamma(curr, multiplier=_realtime_multiplier(inst, curr.spot, no_cache=args.no_cache),
                               proxy_quality=inst.options.proxy_quality, today=today,
                               horizon_days=args.horizon)
            fa = analyze_flow(prev, curr, today=today, horizon_days=args.horizon,
                              call_wall=ga.call_wall, put_wall=ga.put_wall,
                              prev_date=prev_date, curr_date=curr_date_s)
            results.append((inst, fa))
        except Exception as e:
            print(f"[警告] {inst.key} 资金流分析失败: {e}", file=sys.stderr)

    if not results:
        print("没有可用结果。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([dataclasses.asdict(fa) | {"instrument": inst.key} for inst, fa in results],
                         ensure_ascii=False, indent=2, default=str))
        return 0

    blocks = [report_mod.render_flow(fa, inst.display_name) for inst, fa in results]
    print(report_mod.render_flow_all(blocks))
    return 0


def cmd_expiry(args) -> int:
    """近周到期阶梯：逐周五/月度独立墙位 + 买卖方（短线定到期价差用）。"""
    cfg = load_config()
    source = CboeOptionsSource()
    store = SnapshotStore()
    today = market_today()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    blocks = []
    for inst in instruments:
        if inst.options is None:
            print(f"[跳过] {inst.key} 未配置期权数据源", file=sys.stderr)
            continue
        try:
            curr, prev, prev_date, curr_date_s = _load_curr_prev_snapshot(
                store, source, inst, today, no_cache=args.no_cache, no_snapshot=args.no_snapshot)
            # 用真实今天锚定周次/倒计时（墙位纯 OI，不涉 theta 权重，无需 obs_day）
            ladder = build_ladder(prev, curr, today=today,
                                  multiplier=inst.options.approx_commodity_multiplier,
                                  proxy_quality=inst.options.proxy_quality)
            blocks.append(report_mod.render_expiry_ladder(
                ladder, inst.display_name, curr.spot,
                prev_date=prev_date, curr_date=curr_date_s))
        except Exception as e:
            print(f"[警告] {inst.key} 到期阶梯失败: {e}", file=sys.stderr)

    if not blocks:
        print("没有可用结果。", file=sys.stderr)
        return 1
    print(report_mod.render_expiry_ladder_all(blocks))
    return 0


def _fib_key_levels(ga, ratio):
    """把 gamma 的 call/put 墙 + 零伽马翻转位收敛成 R:R 目标用的 KeyLevel 列表。"""
    from undertow.analyze.outlook import KeyLevel
    def comm(v):
        return round(v * ratio, 2) if ratio else v
    lv = []
    if ga.call_wall_oi > 0:
        lv.append(KeyLevel(f"看涨墙 {comm(ga.call_wall):.1f}", ga.call_wall,
                           comm(ga.call_wall) if ratio else None, "resistance", ""))
    if ga.put_wall_oi > 0:
        lv.append(KeyLevel(f"看跌墙 {comm(ga.put_wall):.1f}", ga.put_wall,
                           comm(ga.put_wall) if ratio else None, "support", ""))
    if ga.zero_gamma:
        lv.append(KeyLevel(f"零伽马 {comm(ga.zero_gamma):.1f}", ga.zero_gamma,
                           comm(ga.zero_gamma) if ratio else None, "flip", ""))
    return lv


def cmd_fib(args) -> int:
    """斐波那契回撤 + 盈亏比闸门：「先看盈亏比、别追、等回调」这套交易纪律的确定性落地。"""
    cfg = load_config()
    opt_src = CboeOptionsSource()
    fut_src = YahooFuturesSource()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    blocks = []
    for inst in instruments:
        if inst.commodity is None:
            print(f"[跳过] {inst.key} 未配置真实期货价（斐波摆动腿需日线）", file=sys.stderr)
            continue
        try:
            series, real_price, _asof = fut_src.fetch_for(inst, use_cache=not args.no_cache)
            ratio, walls = None, []
            if inst.options is not None:
                try:
                    snap = opt_src.fetch_snapshot(inst, use_cache=not args.no_cache)
                    ratio = real_price / snap.spot if snap.spot else None
                    ga = analyze_gamma(snap, multiplier=(ratio or inst.options.approx_commodity_multiplier),
                                       proxy_quality=inst.options.proxy_quality)
                    walls = _fib_key_levels(ga, ratio)
                except Exception as e:
                    print(f"[提示] {inst.key} 墙位取用失败（R:R 目标退回斐波扩展）: {e}", file=sys.stderr)
            fib = build_fibonacci(series, ratio=ratio, spot=real_price, lookback=args.lookback)
            plan = build_risk_reward(fib, o=None, key_levels=walls)
            blocks.append(report_mod.render_fib_rr(fib, plan, inst.display_name))
        except Exception as e:
            print(f"[警告] {inst.key} 斐波/盈亏比失败: {e}", file=sys.stderr)

    if not blocks:
        print("没有可用结果。", file=sys.stderr)
        return 1
    print(report_mod.render_fib_rr_all(blocks))
    return 0


def _save_snapshot_dedup(store, inst, sym, payload, today):
    """落盘今日期权快照，但若内容与上一份完全相同则跳过（休市/数据未刷新的重复）。
    返回 (path|None, skipped_bool)。跳过可避免 flow 层日对日 diff 退化成全 0。"""
    try:
        curr = snapshot_from_payload(payload, inst.key, sym)
        latest = store.latest("options", sym)
        if latest is not None:
            ld, lpayload = latest
            if ld != today and lpayload is not None:
                prev = snapshot_from_payload(lpayload, inst.key, sym)
                # 判据是【已建仓合约的 OI 变动总量】，不是指纹是否相同。
                # 指纹只看单份快照，判不了"到期合约滚出导致行集合变化、
                # 而存活合约一张没动"的情形 —— 那正是 OI 未结算的残缺快照
                # （现价新、OI 旧），落盘后会让次日 diff 把两天变动记成一天。
                if oi_change_total(prev, curr) == 0:
                    return None, True
    except Exception:
        pass  # 判定失败不应阻断落盘（宁可多存）
    return store.save("options", sym, payload, on_date=today), False


def _load_curr_prev_snapshot(store, source, inst, today, *, no_cache, no_snapshot,
                             replay: bool = False):
    """取当前+上一份期权快照。今日未落盘则按需落盘（除非 --no-snapshot）。
    返回 (curr_snap, prev_snap|None, prev_date|None, curr_date_str)。

    replay=True 时：只读 today 及之前的快照，且**禁止实时抓取兜底** ——
    历史那天没有快照就该明确失败，不能拿今天的链冒充（codex 2026-08-29 P0）。
    """
    sym = inst.options.symbol
    if not no_snapshot and store.load("options", sym, today) is None:
        payload = source.fetch_raw(inst, use_cache=not no_cache)
        _, skipped = _save_snapshot_dedup(store, inst, sym, payload, today)
        if skipped:
            print(f"[提示] {inst.key} 期权数据与上一交易日逐行相同（休市重复），跳过落盘",
                  file=sys.stderr)
    stored = store.latest_two("options", sym, on_or_before=today if replay else None)
    if stored:
        curr_d, curr_payload = stored[-1]
        curr = snapshot_from_payload(curr_payload, inst.key, sym)
        prev, prev_date = None, None
        if len(stored) == 2:
            prev_d, prev_payload = stored[0]
            prev = snapshot_from_payload(prev_payload, inst.key, sym)
            prev_date = prev_d.isoformat()
        return curr, prev, prev_date, curr_d.isoformat()
    if replay:
        raise FileNotFoundError(
            f"{inst.key} 在 {today} 及之前没有已落盘的期权快照 —— "
            f"回放不得用当前实时链冒充历史")
    payload = source.fetch_raw(inst, use_cache=not no_cache)
    return snapshot_from_payload(payload, inst.key, sym), None, None, today.isoformat()


def _next_weekday(d: date) -> date:
    """下一个工作日 —— OI 所属日 → 可执行日的换算（只跳周末）。"""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def _prev_weekday(d: date) -> date:
    """前一个工作日（链日/观察日推导；只跳周末，节假日误差由快照去重兜底）。"""
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


def _truncate_before(series, day: date):
    """把价格序列掐到【严格早于 day】—— 回放时防止吃到未来价格。

    与 _drop_incomplete_bar 的分工：那个管"当日盘中未完成的半根 K"，
    这个管"回放时序列比 as-of 长出来的一整段"。两者都要，缺一不可。
    """
    if series is None or not getattr(series, "dates", None):
        return series
    n = sum(1 for d in series.dates if d < day)
    if n == len(series.dates):
        return series
    from undertow.core.models import PriceSeries as _PS
    return _PS(symbol=series.symbol,
               dates=series.dates[:n], closes=series.closes[:n],
               highs=series.highs[:n] if series.highs else [],
               lows=series.lows[:n] if series.lows else [])


def _drop_incomplete_bar(series, today: date):
    """剔掉尾部那根【当日未完成】的盘中 bar，再喂给任何窗口型指标。

    Yahoo 日线的最后一根在盘中是"进行时"——高低价还没走完、收盘价随时在变。
    直接喂给 MA/ATR/RSI/滚动分位，会让同一天里读数随盘面来回跳，而且不同命令
    在不同时刻跑会得出互相矛盾的结论（实测 `tech` 报过热分 +5、`report` 报 +2）。
    统一由本函数处理，保证各命令口径一致。
    """
    if series is None or not getattr(series, "dates", None):
        return series
    if series.dates[-1] < today:
        return series
    from undertow.core.models import PriceSeries as _PS
    return _PS(symbol=series.symbol,
               dates=series.dates[:-1],
               closes=series.closes[:-1],
               highs=series.highs[:-1] if series.highs else [],
               lows=series.lows[:-1] if series.lows else [])


def _score_trend(inst_key: str, date_s: str, score: float) -> str:
    """记录每日综合分并给出对昨趋势短句（同向比强弱、异向报翻转）。入 git 留痕。"""
    hist_path = DATA_DIR / "history" / "outlook_scores.json"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if hist_path.exists():
        try:
            data = json.loads(hist_path.read_text())
        except Exception:
            data = {}
    hist = data.setdefault(inst_key, {})
    prev_dates = sorted(d for d in hist if d < date_s)
    trend = ""
    if prev_dates:
        pv = hist[prev_dates[-1]]
        arrow = f"综合分较上日 {pv:+.1f} → {score:+.1f}"
        if pv * score > 0:
            side = "空" if score < 0 else "多"
            if abs(score) > abs(pv) + 0.05:
                trend = f"{arrow}，看{side}增强"
            elif abs(score) < abs(pv) - 0.05:
                trend = f"{arrow}，看{side}减弱"
            else:
                trend = f"{arrow}，强度持平"
        elif score == 0 or pv == 0 or pv * score < 0:
            trend = f"{arrow}，方向较昨发生翻转/中性化"
    hist[date_s] = round(score, 2)
    hist_path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    return trend


def _archive_existing(path) -> None:
    """同日重复生成报告时不覆盖：把旧文件按其生成时刻改名留档（数据尽量多留原则）。

    gold_2026-07-09.html → gold_2026-07-09_r1405.html（r+时分 = 旧版生成时间，本地时区）。
    """
    if not path.exists():
        return
    from datetime import datetime as _dt
    stamp = _dt.fromtimestamp(path.stat().st_mtime).strftime("%H%M")
    # 归档写进 archive/YYYY-MM/，不留在 reports/ 根目录。
    # 2026-08-31 一天开发重跑十几次就堆了 90 个 _rXXXX 文件淹掉当日报告
    # （用户当天：「全部整理一下」）。归档目录本来就是按月分的，
    # 只是旧逻辑把新文件丢在了根目录。
    import re as _re
    m = _re.search(r"(\d{4}-\d{2})", path.stem)
    adir = path.parent / "archive" / (m.group(1) if m else "misc")
    adir.mkdir(parents=True, exist_ok=True)
    backup = adir / f"{path.stem}_r{stamp}{path.suffix}"
    n = 1
    while backup.exists():   # 同一分钟内多次生成 → 追加序号
        backup = adir / f"{path.stem}_r{stamp}-{n}{path.suffix}"
        n += 1
    path.rename(backup)


def _persist_vrp(key: str, h) -> None:
    """VRP 跨周期结果落盘存档 —— 长周期数据「记录」，纳入 git 备份，不进每日报告分析。"""
    d = DATA_DIR / "history" / "vrp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(
        json.dumps(dataclasses.asdict(h), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")


def _persist_signal_probe(key: str, fa, strong_sig, outlook_bias: str) -> None:
    """强信号候选落台账（含被逆向闸门拦下的）。见 signal_ledger 模块 docstring。

    ⚠️ 绝不能让台账写入失败拖垮当日报告——报告是每天要用的，台账是攒给未来的。
    但也不静默吞掉：出错打到 stderr，否则会像 2026-08-26 那次"以为修了其实没生效"。
    """
    try:
        from undertow.analyze import signal_ledger as sl
        sl.record(key, on_date=fa.curr_date, prev_date=fa.prev_date, spot=fa.spot,
                  probe=probe_strong_signal(fa), signal=strong_sig,
                  outlook_bias=outlook_bias or "")
    except Exception as e:
        print(f"[警告] {key} 强信号台账写入失败：{type(e).__name__} {e}", file=sys.stderr)


def _persist_resonance(row: dict) -> None:
    """共振层每日联合状态落盘（append，按品种一个文件，同日覆盖）。

    共振能不能用，只能靠自己攒数据回答——期权结构快照目前只有 22~46 天，
    远不够回测。与事件快照同一思路：不可再生的横截面，先落盘再说。
    forward_* 留空，日后由校准脚本按真实价格回填。
    """
    d = DATA_DIR / "history" / "resonance"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{row['instrument']}.json"
    rows = []
    if p.exists():
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            rows = []
    rows = [r for r in rows if r.get("date") != row["date"]]
    rows.append(row)
    rows.sort(key=lambda r: r.get("date", ""))
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str),
                 encoding="utf-8")


def cmd_live(args) -> int:
    """持仓实时体检：长桥实时盘口 → 真实可平仓价（只读，绝不下单）。

    与 `account` 的分工：account 做【理论评价】（结构/顺逆/墙位/被指派风险），
    本命令只回答一个问题——**现在就走，能拿回多少**。两者口径不同：
    account 用 last/BS 估值（跟券商 App 一致），live 用 bid/ask 算真实出场价。
    """
    from undertow.collect import longbridge_quote as lq
    from undertow.collect import longbridge_account as lb
    from dataclasses import replace
    from undertow.analyze.livecheck import LegQuote, check_position, render_md
    from undertow.soul.plan import load_plans
    if not lq.available():
        print("未找到 longbridge CLI —— 实时体检需要它", file=sys.stderr)
        return 1
    try:
        positions = lb.fetch_positions()
        assets = lb.fetch_assets()
    except Exception as e:
        print(f"读取账户失败：{e}", file=sys.stderr)
        return 1
    # RawPosition 是 dataclass，不是 dict；成本价也一并留下，供组合成本兜底
    held = {p.symbol: int(p.quantity) for p in positions if p.quantity}
    cost_px = {p.symbol: p.cost_price for p in positions}
    if not held:
        print("当前无持仓。")
        return 0
    syms = list(held)
    # ⚠️ fetch_depth 在【全部代码都取不到】时会抛 LiveQuotesUnavailable（这是刻意的：
    # 返回空字典会让自动化把「行情全挂」当成「一切正常」）。但调用方必须接住并给出
    # 清晰结论 + 非零退出码，而不是抛一脸 traceback。
    # （第一轮修复引入抛异常、却漏了这里的接应——本身就是一次回归。）
    try:
        depth = lq.fetch_depth(syms)
    except lq.LiveQuotesUnavailable as e:
        print(f"⚠️ 实时盘口全部取不到（{e}）——无法计算真实可平仓价，本次体检中止。\n"
              f"   这不是「持仓没问题」，是「拿不到行情」。请先确认长桥连通性再重跑。",
              file=sys.stderr)
        return 1
    try:
        quotes = lq.fetch_option_quotes([s for s in syms if "C" in s or "P" in s])
    except Exception as e:
        print(f"[提示] 期权 last 价取失败（{type(e).__name__}），App 口径列将缺失；"
              f"可平仓价不受影响", file=sys.stderr)
        quotes = {}

    def mk(sym):
        d = depth.get(sym)
        q = quotes.get(sym)
        return LegQuote(symbol=sym, qty=held[sym],
                        bid=(d.bid if d else None), ask=(d.ask if d else None),
                        last=(q.last if q else None))

    # 按计划单里的腿分组：同一计划的腿算作一个组合；未被计划覆盖的腿单独成组
    try:
        plans = [p for p in load_plans() if p.status == "active"]
    except Exception:
        plans = []
    grouped, seen, cost_note = [], set(), []
    for pl in plans:
        legs = [mk(l.symbol) for l in pl.legs if l.symbol in held]
        if not legs:
            continue
        # ⚠️ 多个 active 计划若覆盖同一合约，每个都会建一组 → 敞口被重复计算。
        # 先到先得：已被前一个计划认领的合约不再参与。（codex review 2026-08-27）
        if any(l.symbol in seen for l in legs):
            print(f"[提示] 计划 {pl.id} 的腿已被其它计划认领，跳过以免重复计敞口",
                  file=sys.stderr)
            continue
        seen.update(l.symbol for l in legs)
        # 成本优先用计划里记录的【实际成交价】；缺失则回退券商成本价。
        # ⚠️ 必须乘 l.qty，且计划腿与当前实际持仓数量必须完全一致才显示成本——
        # 部分平仓后价值按当前数量算、成本却按原计划算，会得出错误浮盈亏。
        # （codex review 2026-08-26）
        cost = None
        # 计划腿与实际持仓数量/方向是否完全一致。不一致 = 已部分平仓/改过腿，
        # 此时【成本与出场阈值都不能沿用】——市值按残余腿算、阈值却按完整组合算，
        # 会产生假的止损/止盈告警。（codex review 2026-08-27）
        held_match = all(held.get(l.symbol, 0) == (l.qty if l.action == "buy" else -l.qty)
                         for l in pl.legs)
        if pl.legs and held_match and all(l.filled is not None for l in pl.legs):
            cost = sum((l.filled or 0) * l.qty * (1 if l.action == "buy" else -1) * 100
                       for l in pl.legs)
        elif legs and held_match:
            # ⚠️ 回退到券商成本价 = 回到被摊销改写过的那个数（部分减仓后它是该轮打平价，
            # 不是实付价）。能算但要标出来，别让人当成真实成本。
            cost = sum(cost_px.get(l.symbol, 0.0) * l.qty * 100 for l in legs)
            cost_note.append(pl.structure[:20])
        grouped.append((pl.structure[:34], legs, cost, pl if held_match else None,
                        "" if held_match else "持仓与计划不符（已部分平仓或改腿）：成本与出场阈值均不适用"))
    for sym, qty in held.items():
        if sym not in seen:
            grouped.append((sym, [mk(sym)], cost_px.get(sym, 0.0) * qty * 100, None, ""))

    checks = []
    for name, legs, cost, pl, mismatch in grouped:
        stop = target = None
        if pl is not None:                      # pl 为 None 即已判定不匹配
            stop, target = pl.exits.stop_value, pl.exits.target_value
        c = check_position(name, legs, cost=cost, stop=stop, target=target)
        if mismatch:
            c = replace(c, warnings=list(c.warnings) + [f"⚠️ {mismatch}"])
        checks.append(c)
    net = assets.net_assets or None
    print(render_md(checks, net_assets=net))
    if cost_note:
        print(f"\n> ⚠️ 以下持仓的成本用了**券商成本价**（计划里缺实际成交价）："
              f"{'、'.join(cost_note)}。券商成本价在部分减仓后会被改写成该轮打平价，"
              f"非实付价——盈亏列仅供参考，以生命周期台账为准。")

    # 品种累计台账：只有现金流水不会骗人（券商成本价在部分减仓后会被改写）
    try:
        from undertow.analyze.livecheck import build_ledger, render_ledger_md
        import datetime as _dt
        start = (_dt.date.today() - _dt.timedelta(days=90)).isoformat()
        rows = lb.fetch_cash_flow(start=start) or []
        # ⚠️ 按【当前持有的具体合约】聚合，不按品种。
        # 按品种会把该标的 90 天内所有已了结的其它仓位也算进来——实测 SLV 会得到
        # -1,871（整个 SLV 交易史），对「这个仓亏了多少」毫无意义。
        # 按合约则得到这几张合约的完整生命周期（含同一合约的更早轮次），才是要的数。
        by_group: dict = {}
        for sym in held:
            root = sym.split("2")[0] if len(sym) > 6 else sym       # SLV260918C70000.US → SLV
            by_group.setdefault(root, []).append(sym)
        ledgers = []
        for u, syms_u in sorted(by_group.items()):
            sub = [r for r in rows if (r.get("symbol") or "") in syms_u]
            if not sub:
                continue
            legs_u = [mk(s) for s in syms_u]
            cl = 0.0
            for lg in legs_u:
                px = lg.exit_price()
                if px is None:
                    cl = None; break
                cl += px * lg.qty * 100
            # 手续费按【张数】计，不是按合约代码数——两腿各 4 张要收 8 笔
            n_contracts = sum(abs(lg.qty) for lg in legs_u)
            label = (f"{u}（{n_contracts}张在场："
                     f"{', '.join(x.split('.')[0][-7:] for x in syms_u)}）")
            # ⚠️ 原样传 cl（可能是 None），绝不折成 0.0 —— 折零会把「某条腿拿不到盘口」
            # 显示成「持仓已归零」，并据此算出一个假的生命周期亏损。
            # （codex review 二轮高危#1：模型层已传播 None，真实 CLI 路径却又折回 0.0）
            ledgers.append(build_ledger(label, sub, cl, exit_fee=0.80 * n_contracts))
        if ledgers:
            print(render_ledger_md(ledgers))
    except Exception as e:
        print(f"\n> 台账跳过：{type(e).__name__}: {str(e)[:60]}", file=sys.stderr)
    return 0



def _persist_containment(acc: dict, *, horizon: int, mode: str, spans: list) -> None:
    """不突破率校准表落盘存档 —— 卖方选行权价距离时直接查这张表。

    与 VRP 同类：这是"回测产出的参数"，纳入 git 备份，改指标后重跑覆盖。
    """
    d = DATA_DIR / "history" / "containment"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"h{horizon}_{mode}.json").write_text(json.dumps({
        "asof": market_today().isoformat(), "horizon": horizon, "mode": mode,
        "panel": spans, "unit": "ATR14 倍数",
        "note": ("终值=到期收盘在行权价内；路径=期间一次都没碰到。"
                 "超买档看向上不突破(卖call)，超卖档看向下不突破(卖put)。"
                 "提升才是 edge，绝对值高只是因为虚值远。"),
        "bands": acc,
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def cmd_vol(args) -> int:
    """波动率溢价（VRP）跨周期检验：这个卖方 edge 能不能穿越牛熊。"""
    cfg = load_config()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    vol_src, px_src = CboeVolSource(), CboeHistorySource()
    rc = 0
    for inst in instruments:
        if not inst.vol_index or not inst.price:
            print(f"[跳过] {inst.key}: 未配置波动率指数或价格源", file=sys.stderr)
            continue
        try:
            iv = vol_src.fetch_series(inst.vol_index, use_cache=not args.no_cache)
            ser = px_src.fetch_series(inst, use_cache=not args.no_cache)
        except Exception as e:
            print(f"[失败] {inst.key}: {type(e).__name__}: {e}", file=sys.stderr)
            rc = 1
            continue
        h = assess_vrp_history(iv_series=iv, px_dates=ser.dates, px_closes=ser.closes,
                               index_name=inst.vol_index, window=args.window)
        print(vrp_md(h, inst.display_name))
        print()
    return rc


def _index_summary(o) -> str:
    """index 卡片一句话摘要。

    ⚠️ 方向已经在 pill（近X/中Y）里、墙已经在事实块里，这里【不要重复】——
    重复正是用户说的「车轱辘话」。只留一件别处没有的事：伽马环境。
    """
    if "正伽马" in o.regime or ("正" in o.regime and "Gamma" in o.regime):
        return "正伽马环境：做市商压波动，突破容易假、回归中枢的概率高"
    if "负伽马" in o.regime or ("负" in o.regime and "Gamma" in o.regime):
        return "负伽马环境：做市商追单，一旦启动容易走出趋势、别逆势接"
    return ""


def cmd_report(args) -> int:
    """综合研判报告：四层情报聚合 + 可视化 + 情景推演 → 自包含 HTML。"""
    cfg = load_config()
    lookback = args.lookback or cfg.lookback_weeks
    cot_src, opt_src, px_src = CftcCotSource(), CboeOptionsSource(), CboeHistorySource()
    fut_src = YahooFuturesSource()
    fred_src = FredMacroSource()
    vol_src = CboeVolSource()
    store = SnapshotStore()
    # --as-of：把"今天"钉在历史某天，用当时的数据重放研报（复盘验证用）。
    # ⚠️ 回放时一律不落新快照，也不写进当日报告目录的常规文件名 —— 回放产物
    # 必须能和真实当日产物区分开，否则复盘会污染台账与归档。
    replay = getattr(args, "as_of", None)
    if replay:
        try:
            today = date.fromisoformat(replay)
        except ValueError:
            print(f"--as-of 需要 YYYY-MM-DD，收到 {replay!r}", file=sys.stderr)
            return 2
        args.no_snapshot = True
    else:
        today = market_today()
    all_events, _ = _merged_events(getattr(args, "no_live", False), args.no_cache)
    reports_dir = DATA_DIR / "reports" / "replay" if replay else DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    written = []

    failed: list = []
    for inst in instruments:
        if inst.options is None or inst.price is None:
            # 综合 HTML 报告以期权 Gamma/Flow 为骨架；无期权代理的品种（如美元指数）
            # 暂走持仓分析命令。接入真·期货期权数据源（如 IBKR）后即可出完整报告。
            hint = "持仓分析请用 `analyze`" if inst.cot is not None else "暂无可分析层"
            print(f"[跳过] {inst.key} 无期权数据源，不出综合 HTML 报告（{hint}）", file=sys.stderr)
            continue
        try:
            history = cot_src.fetch_history(inst, lookback=lookback, use_cache=not args.no_cache)
            an = analyze(history)
            signals = generate_signals(an)

            curr, prev, prev_date, curr_date_s = _load_curr_prev_snapshot(
                store, opt_src, inst, today, no_cache=args.no_cache,
                no_snapshot=args.no_snapshot, replay=bool(replay))

            # —— 真实商品期货价：用【当日实时比值】= 期货价/ETF价 换算所有位点（免乘数漂移）——
            real_series, real_price, real_asof, ratio = None, None, "", None
            if inst.commodity is not None:
                try:
                    real_series, real_price, real_asof = fut_src.fetch_for(
                        inst, use_cache=not args.no_cache)
                    if curr.spot > 0:
                        ratio = real_price / curr.spot
                except Exception as e:
                    print(f"[提示] {inst.key} 真实期货价获取失败，回退静态乘数: {e}", file=sys.stderr)
            # ⚠️ 回放必须掐断未来价格 —— 否则整份重放都不可信。
            # 研报在【可交易日 D 的开盘前】生成，那时能看到的最后一根完整日线是 D−1。
            # _drop_incomplete_bar 只剔"≥today 的最后一根"，实时跑时刚好对（today 那根
            # 是盘中未完成的）；但回放时序列早已长到 today 之后，只剔一根会留下 today
            # 当天的收盘价 —— 那是当时还不存在的信息。
            # 2026-08-29 实测：回放 8/28 的黄金，因为吃进了 8/28 当天 −3.24% 的收盘价，
            # 超买超卖从当时真实的"偏超买 78%"变成"中性 57%"，直接改写了结论。
            if replay and real_series is not None and real_series.dates:
                real_series, real_price = _truncate_before(real_series, today), None
                if real_series.closes:
                    real_price = real_series.closes[-1]
                    ratio = (real_price / curr.spot) if curr.spot > 0 else None
            mult = ratio if ratio is not None else inst.options.approx_commodity_multiplier

            # 观察日 = 链交易日（快照日前一工作日）：报告的期权结构是"昨收快照"，
            # 计时按观察日锚定——否则当日到期(0DTE)合约会被当成已过期剔除，
            # 而自动报告恰在 ET 凌晨生成、这些合约当天仍在交易（Codex P0-4）
            obs_day = _prev_weekday(date.fromisoformat(curr_date_s)) if curr_date_s else _prev_weekday(today)

            ga = analyze_gamma(curr, multiplier=mult,
                               proxy_quality=inst.options.proxy_quality, today=obs_day,
                               horizon_days=args.horizon)
            fa = analyze_flow(prev, curr, today=obs_day, horizon_days=args.horizon,
                              call_wall=ga.call_wall, put_wall=ga.put_wall,
                              prev_date=prev_date, curr_date=curr_date_s)
            if ratio is not None:
                etf_sym = inst.options.symbol
                fut_sym = inst.commodity.symbol
                basis = f"实时比值 {fut_sym}/{etf_sym}={ratio:.3f}（{real_asof[:10]}）"
                comm_sym = fut_sym
            else:
                basis = "静态乘数近似（真实期货价不可用）"
                comm_sym = ""

            # —— 宏观背景层（FRED 真实利率/美元/通胀预期）——
            ma, macro_votes = None, []
            try:
                sids = series_ids_for(inst.asset_class)
                smap = {sid: fred_src.fetch_series(sid, use_cache=not args.no_cache) for sid in sids}
                vol_series = None
                if inst.vol_index:
                    try:
                        vol_series = vol_src.fetch_series(inst.vol_index, use_cache=not args.no_cache)
                    except Exception as ve:
                        print(f"[提示] {inst.key} 波动率 {inst.vol_index} 跳过: {ve}", file=sys.stderr)
                ma = analyze_macro(smap, asset_class=inst.asset_class,
                                   vol_name=inst.vol_index, vol_series=vol_series)
                macro_votes = macro_to_votes(ma)
            except Exception as e:
                print(f"[提示] {inst.key} 宏观层跳过: {e}", file=sys.stderr)

            outlook = build_outlook(an, signals, ga, fa, display_name=inst.display_name,
                                    commodity_symbol=comm_sym, commodity_basis=basis,
                                    extra_votes=macro_votes)
            # —— 近端资金流强信号：一边倒时置顶告警，独立于综合投票（复盘 8/19 黄金埋没案）——
            # 对照【近端】层：强信号本就是 Flow 子层，拿它比被中期主导的综合分
            # 会把"与近端一致"误报成"与综合背离"（见 _diverges docstring）。
            strong_sig = detect_strong_signal(
                fa, outlook_bias=(getattr(outlook, "near_bias", "") or outlook.bias),
                mid_bias=getattr(outlook, "mid_bias", ""))
            if not replay:      # ⚠️ 回放绝不得写正式台账（codex 2026-08-29 P0）
                _persist_signal_probe(inst.key, fa, strong_sig, outlook.bias)
            _stale = ("" if (curr_date_s or "") >= today.isoformat() else
                      f"它描述 {prev_date} 交易日，本该在 {curr_date_s} 开盘交易，今天已是 {today}")
            strong_html = render_strong_signal_banner(strong_sig, inst.display_name, _stale)

            # —— 结构读数（机构口径，不输出方向票）——
            # 与投票层正交：只描述"防守强度/位置/逐腿可靠度/证伪清单"，
            # 因此不会和 偏多/偏空 打架。见 structure_read 模块 docstring。
            struct_html = ""
            try:
                from undertow.analyze import structure_read as _sr
                _vols = []
                for _d in store.dates("options", inst.options.symbol)[-11:-1]:
                    try:
                        _sn = snapshot_from_payload(store.load("options", inst.options.symbol, _d),
                                                    inst.key, inst.options.symbol)
                        _vols.append(sum(c.volume for c in _flow_live(_sn, _d, 60)))
                    except Exception:
                        pass
                _read = _sr.analyze_structure(
                    fa, _flow_live(prev, today, 60) if prev is not None else [],
                    _flow_live(curr, today, 60), recent_volumes=_vols or None)
                struct_html = render_structure_section(_read, inst.display_name)
            except Exception as e:
                print(f"[警告] {inst.key} 结构读数失败：{type(e).__name__} {e}", file=sys.stderr)

            # —— 价格图：优先真实期货价 + 关键位换算到商品价；否则回退 ETF 日线 ——
            def lvl(etf_v):
                return ga.to_commodity(etf_v) if ratio is not None else etf_v
            levels = []
            if ga.call_wall_oi > 0:
                levels.append(("call墙", lvl(ga.call_wall), viz.C_RES))
            if ga.put_wall_oi > 0:
                levels.append(("put墙", lvl(ga.put_wall), viz.C_SUP))
            if ga.zero_gamma is not None:
                levels.append(("零伽马", lvl(ga.zero_gamma), viz.C_FLIP))
            if real_series is not None:
                px_dates, px_closes = real_series.dates, real_series.closes
                px_spot = real_price
                px_title = f"真实期货价日线 + 关键位（{real_series.symbol}）"
            else:
                price = px_src.fetch_series(inst, use_cache=not args.no_cache)
                px_dates, px_closes, px_spot = price.dates, price.closes, ga.spot
                px_title = f"价格日线 + 关键位（{price.symbol} ETF）"
            price_svg = viz.price_levels_svg(px_dates, px_closes, levels, px_spot, title=px_title)
            # OI 墙图：行权价换算到商品价（有真实比值时）
            oi_rows = [(lvl(r.strike), r.call_oi, r.put_oi) for r in ga.strike_rows]
            oi_svg = viz.oi_walls_svg(
                oi_rows, lvl(ga.spot), lvl(ga.call_wall), lvl(ga.put_wall),
                title="近价 OI 墙（按" + ("商品价" if ratio is not None else "ETF行权价") + "）")
            cot_svg = viz.cot_net_history_svg(
                [r.report_date for r in history], [r.managed_money.net for r in history],
                percentile=an.categories["managed_money"].net_percentile,
                title="投机资金 Managed Money 净持仓历史")

            # migration（put 墙三区域读数）从 index 移到品种报告的资金流一节
            # （用户 2026-08-31：index 太复杂）——研究性内容不占索引页，但不消失
            _mig = None
            try:
                from undertow.analyze.flow import wall_structure as _ws
                _mig = _ws(fa, getattr(ga, "put_wall", None), curr.spot)
            except Exception as e:
                print(f"⚠️ {inst.key} 墙区读数失败：{type(e).__name__}: {e}", file=sys.stderr)
            flow_html = render_flow_section(fa, migration=_mig)
            macro_html = render_macro_section(ma)
            evs = upcoming(all_events, today=today, within_days=21, instrument=inst.key)
            events_html = render_events_section(evs, today)
            # —— 大白话速读：日涨跌用真实期货最近两根收盘；期权确认取波动率面判读 ——
            day_chg = None
            if real_series is not None and len(real_series.closes) >= 2 and real_series.closes[-2]:
                day_chg = 100.0 * (real_series.closes[-1] / real_series.closes[-2] - 1.0)
            vv = fa.vol.verdict if (fa.vol is not None and fa.vol.prev is not None) else ""
            # —— 逐日结构历史：用已落盘快照逐日重算 gamma（快照日=链日+1，比值按当日期货收盘）——
            struct_hist, timeline_rows = [], []
            if ratio is not None and real_series is not None:
                closes_m = dict(zip(real_series.dates, real_series.closes))
                highs_m = dict(zip(real_series.dates, real_series.highs)) if real_series.highs else {}
                lows_m = dict(zip(real_series.dates, real_series.lows)) if real_series.lows else {}

                by_day = {}
                opt_sym = inst.options.symbol
                for snap_d in store.dates("options", opt_sym)[-14:]:
                    try:
                        h_snap = snapshot_from_payload(store.load("options", opt_sym, snap_d),
                                                       inst.key, opt_sym)
                        if h_snap.spot <= 0:
                            continue
                        t = _prev_weekday(snap_d)
                        # 当日比值：有该日期货收盘用之；缺根时退用实时比值（仅影响展示口径）
                        r_h = (closes_m[t] / h_snap.spot) if t in closes_m else ratio
                        g_h = analyze_gamma(h_snap, multiplier=r_h,
                                            proxy_quality=inst.options.proxy_quality,
                                            today=t, horizon_days=args.horizon)
                        by_day[t] = (
                            g_h.zero_gamma * r_h if g_h.zero_gamma is not None else None,
                            g_h.call_wall * r_h if g_h.call_wall_oi > 0 else None,
                            g_h.put_wall * r_h if g_h.put_wall_oi > 0 else None)
                    except Exception:
                        continue
                for t in sorted(by_day):
                    f_v, cw, pw = by_day[t]
                    struct_hist.append((t, f_v, cw, pw))
                    timeline_rows.append((t, closes_m.get(t), highs_m.get(t),
                                          lows_m.get(t), f_v, cw, pw))
            if timeline_rows and ratio is not None:
                # 末点 = 今日（盘中口径）：结构线与策略票同一换算，消除 58.2 vs 60.6 的双口径困惑
                timeline_rows.append((today, None, None, None,
                                      ga.to_commodity(ga.zero_gamma),
                                      ga.to_commodity(ga.call_wall) if ga.call_wall_oi > 0 else None,
                                      ga.to_commodity(ga.put_wall) if ga.put_wall_oi > 0 else None))
            timeline_svg = viz.strategy_timeline_svg(timeline_rows, real_price) \
                if len(timeline_rows) >= 3 else ""
            # —— 策略情景参数化（期货）先算：其否决票 = 现成的对手盘证据 ——
            series_done = _drop_incomplete_bar(real_series, today)
            # —— 技术面价格源：优先长桥 K 线（最实时），失败才退回原序列 ——
            # 2026-08-27 实测：CBOE 历史日线在 8/27 当天仍止于 8/25，滞后两天，
            # 于是报告里的 KDJ/RSI/MACD 全是两天前的读数（KDJ-J -9.6「深度超卖」），
            # 而当天 QQQ 已 KDJ 金叉、RSI6 拉到 55 —— 用户在券商 App 上一眼看出矛盾。
            # 长桥 K 线与用户看盘同源，数值实测可对上（KDJ 差 0.1 以内）。
            tech_series, tech_src = series_done, "价格序列"
            if inst.options:
                try:
                    from undertow.collect.longbridge_kline import fetch_series as _lb_kline
                    _lb = _lb_kline(f"{inst.options.symbol}.US", count=400)
                    if _lb.closes and (series_done is None
                                       or _lb.dates[-1] >= series_done.dates[-1]):
                        tech_series, tech_src = _lb, "长桥K线"
                except Exception as e:
                    print(f"[提示] {inst.key} 长桥 K 线不可用（{type(e).__name__}），"
                          f"技术面退回原价格源", file=sys.stderr)
            # ⚠️ 统一出口截断：回放时未来价格可能从【多个入口】渗入 ——
            # real_series 已在上面掐过，但长桥 K 线这一支是"谁更新用谁"，
            # 回放时它正好把未来数据又接了回来（2026-08-29 实测：截断后分位
            # 仍是被 8/28 收盘价污染的 57%）。所以在所有来源汇合之后再掐一次。
            if replay:
                tech_series = _truncate_before(tech_series, today)
                series_done = _truncate_before(series_done, today)

            plan = build_strategy(outlook, vol=fa.vol, series=series_done,
                                  struct_history=struct_hist or None)
            strategy_html = render_strategy_section(plan, timeline_svg=timeline_svg)
            # —— 分块速读：方向 / 关键位 / 持仓异动(ΔOI) / 对手盘警示 ——
            tilt = fa.flow_tilt if not fa.flow_tilt.startswith("—") else ""
            conv = ga.to_commodity if ratio is not None else None
            moves = structural_moves(fa, conv=conv)
            counters = counter_signals(fa, plan.direction, conv=conv)
            if plan.vetoes:  # 全文在策略卡，速读只留短标签
                labels = "、".join(v.split("：")[0] for v in plan.vetoes)
                counters.append(f"实时层否决票 ×{len(plan.vetoes)}（{labels}，详见策略卡）")
            # —— 结构对昨变化（墙增/削、零伽马位移）+ 综合分趋势 ——
            struct_notes = []
            ga_prev = None
            if prev is not None:
                try:
                    # 昨日结构必须用昨日的日期锚定（到期时间权重随 today 变，
                    # 用今天的日期算昨日链会把零伽马算歪）
                    prev_obs = _prev_weekday(date.fromisoformat(prev_date)) if prev_date \
                        else _prev_weekday(obs_day)
                    ga_prev = analyze_gamma(prev, multiplier=mult,
                                            proxy_quality=inst.options.proxy_quality,
                                            today=prev_obs, horizon_days=args.horizon)
                    # 墙厚基准 = 昨日快照中按【今日观察窗口】仍存活的 OI（防 R8b 到期滚落假削弱）
                    end = obs_day + timedelta(days=args.horizon)
                    surv: dict = {}
                    for c_ in prev.contracts:
                        if obs_day < c_.expiry <= end:   # 与 gamma 的 0<T 同语义
                            key_ = (c_.strike, c_.kind)
                            surv[key_] = surv.get(key_, 0) + c_.open_interest
                    struct_notes = structure_delta(ga_prev, ga, prev_surviving=surv)
                    driver = flip_driver_summary(fa)
                    if driver:
                        struct_notes.append(driver)
                except Exception:
                    pass
            trend = ("" if replay
                     else _score_trend(inst.key, today.isoformat(), outlook.bias_score))
            tldr_html = render_tldr_section(plain_summary_blocks(
                outlook, day_chg_pct=day_chg, vol_verdict=vv,
                flow_tilt=tilt, flow_moves=moves, counter_notes=counters,
                bias_trend=trend, struct_notes=struct_notes))
            # —— 波动率环境：期权偏贵/偏便宜 → 波段级买方/卖方倾向 ——
            vr_closes = series_done.closes if series_done is not None else (
                real_series.closes if real_series is not None else None)
            vr = assess_vol_regime(
                iv_reading=(ma.vol if ma is not None else None),
                atm_iv_pp=(fa.vol.curr.atm_iv_pp if fa.vol is not None else None),
                closes=vr_closes)
            volregime_html = render_vol_regime_section(vr)
            # —— 波动率速览：聚焦最近（复用 vr）+ 近1年波动率曲线 ——
            #    VRP「穿越牛熊」属长周期，不进每日报告，仅落盘存档（data/history/vrp/）。
            vol_svg = ""
            if inst.vol_index and inst.price:
                try:
                    iv_ser = vol_src.fetch_series(inst.vol_index, use_cache=not args.no_cache)
                    recent = [v for _, v in sorted(iv_ser)[-252:]]
                    mean_ref = sum(recent) / len(recent) if recent else None
                    vol_svg = viz.vol_history_svg(
                        iv_ser, title=f"波动率指数 {inst.vol_index} 近1年（年化 IV，pp）",
                        mean_ref=mean_ref)
                    # ⚠️ 回放绝不得写正式台账（codex 2026-08-29 P0：实测跑一次
                    # --as-of 就改写了 outlook_scores.json 与 5 个 resonance/*.json）
                    if not replay:
                        px_ser = px_src.fetch_series(inst, use_cache=not args.no_cache)
                        _persist_vrp(inst.key, assess_vrp_history(
                            iv_series=iv_ser, px_dates=px_ser.dates,
                            px_closes=px_ser.closes, index_name=inst.vol_index))
                except Exception as ve:
                    print(f"[提示] {inst.key} 波动率历史跳过: {type(ve).__name__}: {ve}",
                          file=sys.stderr)
            vol_analysis_html = render_vol_analysis_section(vr, vol_svg)
            # —— 铁鹰策略子模块 + 策略统筹（多子模块调度）——
            condor_plan = assess_condor(snap=curr, vr=vr, today=today, fa=fa)
            cs_plan = assess_credit_spread(snap=curr, vr=vr, outlook=outlook, today=today, fa=fa)
            strategy_props = assemble_strategies(directional=plan, condor=condor_plan,
                                                 credit_spread=cs_plan)
            strategy_html = (render_strategy_hub(strategy_props) + strategy_html
                             + render_credit_spread_section(cs_plan)
                             + render_condor_section(condor_plan))
            # —— 可交易信息闸门（压力倍数 <2× = 今天没信息，见 flow.tradeable_info）——
            gate_html = ""
            try:
                _ti = tradeable_info(fa)
                gate_html = render_tradeable_gate(_ti, inst.display_name)
            except Exception as e:
                print(f"⚠️ {inst.key} 可交易闸门失败：{type(e).__name__}: {e}", file=sys.stderr)

            # —— 比例价差（playbook R15）：逐腿判定会读反的结构 ——
            ratio_html = ""
            try:
                _rs = detect_ratio_spreads(fa.changes, curr.spot)
                ratio_html = render_ratio_spreads(
                    _rs, conv=(ga.to_commodity if ratio is not None else None),
                    etf_symbol=inst.options.symbol)
            except Exception as e:
                print(f"⚠️ {inst.key} 比例价差检测失败：{type(e).__name__}: {e}", file=sys.stderr)

            # —— 远月结构异动（playbook R16）：近月窗口的盲区，只作长期背景 ——
            backmonth_html = ""
            try:
                _bm = backmonth_scan(prev, curr, obs_day, curr.spot)
                backmonth_html = render_backmonth(
                    _bm, spot=curr.spot,
                    conv=(ga.to_commodity if ratio is not None else None),
                    etf_symbol=inst.options.symbol)
            except Exception as e:
                print(f"⚠️ {inst.key} 远月扫描失败：{type(e).__name__}: {e}", file=sys.stderr)

            # —— 墙位卖方价差候选（analyze/credit_wall）——
            credit_wall_html = ""
            try:
                if _ti and _ti.get("side") in ("看涨", "看跌"):
                    _bp = _na = None
                    try:
                        from undertow.collect.longbridge_account import fetch_assets
                        _a = fetch_assets()
                        _bp, _na = _a.buy_power, _a.net_assets
                    except Exception:
                        pass
                    _ed = date.fromisoformat(curr_date_s) if curr_date_s else today
                    _vs = {t: cw_propose(curr, obs_day, curr.spot, _ti["side"],
                                         _ti["ratio"], tier=t, execution_date=_ed)
                           for t in ("conservative", "balanced", "aggressive")}
                    credit_wall_html = render_credit_wall(
                        _vs, spot=curr.spot,
                        conv=(ga.to_commodity if ratio is not None else None),
                        etf_symbol=inst.options.symbol, buying_power=_bp,
                        net_assets=_na)
            except Exception as e:
                print(f"⚠️ {inst.key} 卖方价差失败：{type(e).__name__}: {e}", file=sys.stderr)

            # —— 成本闸门：预期波动 vs 回本门槛（见 cost_gate 模块注释）——
            cost_html = ""
            try:
                if _ti and _ti.get("side") in ("看涨", "看跌"):
                    # 可执行日 = 快照日（不是 obs_day）：DTE 必须按下单日算
                    _exec_day = (date.fromisoformat(curr_date_s)
                                 if curr_date_s else today)
                    _cands = cost_candidates(curr, curr.spot, _ti["side"], _exec_day,
                                             decidable=_ti["decidable"])
                    cost_html = render_cost_gate(
                        _cands, spot=curr.spot, side=_ti["side"],
                        conv=(ga.to_commodity if ratio is not None else None),
                        etf_symbol=inst.options.symbol)
            except Exception as e:
                print(f"⚠️ {inst.key} 成本闸门失败：{type(e).__name__}: {e}", file=sys.stderr)

            # —— 期权结构按到期分层（近端置顶）——
            # 主报告的墙来自 analyze_gamma 的跨到期加总，会造出实盘不存在的位置；
            # 这一节把它拆回近/中/远三层，并标出近端与中端是否指向同一位置。
            layers_html = ""
            try:
                # expiring_on = 快照日 = 今天的交易日：obs_day 计时会把当日到期算进
                # 近端支撑，但它们今晚就消失，必须在阶梯上标出来。
                _texp = date.fromisoformat(curr_date_s) if curr_date_s else today
                _lad = support_ladder(curr, obs_day, curr.spot, expiring_on=_texp)
                _bands = ladder_bands(curr, obs_day, curr.spot)
                _agree = {sd: wall_agreement(ga.layers, sd) for sd in ("put", "call")}
                layers_html = render_wall_layers_section(
                    ga, ladder=_lad, bands=_bands, agree=_agree,
                    conv=(ga.to_commodity if ratio is not None else None),
                    unit="", etf_symbol=inst.options.symbol)
            except Exception as e:
                # 分层失败要出声：主墙位已改用近端口径，这一节缺失会让读者
                # 无从判断墙属于哪个到期层。
                print(f"⚠️ {inst.key} 期权结构分层失败：{type(e).__name__}: {e}", file=sys.stderr)

            # —— 近周到期阶梯：逐周五/月度独立墙位+买卖方（短线定到期价差用）——
            expiry_html = ""
            try:
                # 阶梯用【真实今天】锚定周次/倒计时（墙位是纯 OI，不涉 theta 权重，
                # 无需 obs_day；否则周一跑会把"本周五"错标成"下周五"）
                ladder = build_ladder(prev, curr, today=today, multiplier=mult,
                                      proxy_quality=inst.options.proxy_quality)
                expiry_html = render_expiry_ladder_section(
                    ladder, conv=(ga.to_commodity if ratio is not None else None),
                    etf_symbol=inst.options.symbol)
            except Exception as e:
                print(f"[提示] {inst.key} 到期阶梯跳过: {e}", file=sys.stderr)
            # —— 斐波那契回撤 + 盈亏比闸门（"先看盈亏比、别追、等回调"纪律落地）——
            fib_html = ""
            fib_an = rr_plan = None
            try:
                if real_series is not None:
                    # ⚠️ 必须用 series_done（已剔掉当日未完成 bar）：超买超卖用的是它，
                    # 斐波若用生 real_series，同一份报告里两层就有两个截止时点 ——
                    # 盘中半根 K 会改变摆动腿方向，于是"偏超卖"和"短线等回调"
                    # 基于不同的日线截止日，这正是假矛盾的来源之一。
                    # 现价仍用实时价（只影响"距位点多少"，不改已确认的摆动腿）。
                    fib_an = build_fibonacci(series_done if series_done is not None else real_series,
                                             ratio=ratio,
                                             spot=(real_price if real_price else curr.spot))
                    rr_plan = build_risk_reward(fib_an, o=outlook)
                    fib_html = render_fib_rr_section(
                        fib_an, rr_plan, etf_symbol=(inst.options.symbol if inst.options else ""))
            except Exception as e:
                print(f"[提示] {inst.key} 斐波/盈亏比跳过: {e}", file=sys.stderr)
            # —— 技术面 · 超买超卖（拉伸度，带回测校准）——
            #    用 series_done（剔掉未完成的当日盘中 bar），否则 MA/ATR/分位会被半根 K 污染。
            tech_html = ""
            tech_read = stretch_read = res_read = None
            try:
                from undertow.analyze.technicals import analyze_technicals
                from undertow.analyze.stretch import analyze_stretch
                from undertow.analyze.resonance import assess_resonance, snapshot_row
                if tech_series is not None:
                    tech_read = analyze_technicals(tech_series)
                    stretch_read = analyze_stretch(tech_series)
                    from undertow.analyze.technicals import crossovers as _cx
                    cross_read = _cx(tech_series.highs, tech_series.lows, tech_series.closes)
                    tech_asof = tech_series.dates[-1].isoformat() if tech_series.dates else ""
                    # 4H 层：只展示不判定；取不到就不显示，绝不用日线顶替
                    h4_read = None
                    if inst.options:
                        try:
                            from undertow.analyze.technicals import read_4h as _r4
                            h4_read = _r4(f"{inst.options.symbol}.US")
                        except Exception:
                            h4_read = None
                    # 共振：期权结构（近端 bias = Gamma墙位+资金流）为主，超买超卖为辅
                    res_read = assess_resonance(outlook.near_bias, stretch_read)
                    tech_html = render_technicals_section(
                        tech_read, stretch_read, res_read,
                        cross=cross_read, asof=tech_asof, src=tech_src,
                        today=today.isoformat(), h4=h4_read)
                    # 落盘当日联合状态——共振能不能用只能靠自己攒数据回答
                    if not replay:
                        _persist_resonance(snapshot_row(
                            inst.key, today.isoformat(), res_read, stretch_read,
                            spot=(real_price if real_price else ga.spot)))
            except Exception as e:
                print(f"[提示] {inst.key} 技术面跳过: {e}", file=sys.stderr)
            # —— 当日决策研判：规则化合成 近中分层＋资金流＋强信号＋盈亏比闸门（无 LLM）——
            verdict = None
            verdict_html = ""
            try:
                verdict = build_verdict(outlook, fa, strong_sig, fib_an, rr_plan)
                verdict_html = render_verdict_section(verdict, inst.display_name)
            except Exception as e:
                print(f"[提示] {inst.key} 决策研判跳过: {e}", file=sys.stderr)
            # 指标分组要在渲染之前算好：品种研报里也有「指标说明」栏目
            try:
                _labels = _build_labels(outlook, fa=fa, stretch=stretch_read)
                from undertow.analyze.strength import (collect as _st_collect,
                                                       weighted_score as _st_w,
                                                       near_weighted as _st_near)
                _sts = _st_collect(outlook, fa=fa, stretch=stretch_read)
                # 波动压缩（观察项，不进综合分 —— 区间跨 0，样本不足）
                from undertow.analyze.squeeze import assess as _sq_assess
                # ⚠️ VolRegime 没有 history 字段 —— 上一版从它取历史，_ivh 恒为空，
                # tight 在生产里【永远触发不了】（codex 2026-08-29 P1）。
                # 它本来就有算好的 iv_pct（IV 在自身历史里的分位），直接用。
                _sq = _sq_assess(
                    iv_pctile=(getattr(vr, "iv_pct", None) / 100.0
                               if vr is not None and getattr(vr, "iv_pct", None) is not None
                               else None),
                    highs=(tech_series.highs if tech_series else None),
                    lows=(tech_series.lows if tech_series else None),
                    closes=(tech_series.closes if tech_series else None))
                _scores = {"legacy": getattr(outlook, "bias_score", None),
                           "weighted": _st_w(_sts)[0], "near": _st_near(_sts),
                           "squeeze": _sq}
                from undertow.analyze.indicators import render_section as _ind_sec
                from undertow.report.html import _esc as _e
                _indicators_html = _ind_sec(_labels, _e)
                # 到期分桶明细：index 上只留一个标记，明细在品种研报里
                from undertow.analyze.flow import (expiry_split as _exp_sp,
                                                   expiry_split_html as _exp_html)
                _expiry_html = _exp_html(_exp_sp(fa), _e)
                # 研报顶部的综合研判卡：与 index 卡片同源同内容
                from undertow.report.html import render_summary_card as _sum_card
                _summary_html = _sum_card({
                    "near_bias": outlook.near_bias, "mid_bias": outlook.mid_bias,
                    "near_score": getattr(outlook, "near_score", None),
                    "mid_score": getattr(outlook, "mid_score", None),
                    "labels": _labels, "scores": _scores,
                    "facts": (_flow_facts(fa, ga, ga_prev, prev, curr, curr.spot,
                                          date.fromisoformat(curr_date_s)
                                          if curr_date_s else today)
                              | {"bias": outlook.bias, "mid_bias": outlook.mid_bias}),
                })
            except Exception as e:      # 指标分组失败要出声，不能静默少一栏
                print(f"⚠️ {inst.key} 指标分组失败：{type(e).__name__}: {e}", file=sys.stderr)
                _labels, _indicators_html, _scores = [], "", {}
                _expiry_html = _summary_html = ""
            # 墙位历史图（用户 2026-08-31 要求「放进研报，期权结构下面」）。
            # 单独 try：图挂了不能拖垮整份研报，但要出声 —— 静默少一张图，
            # 下次就没人记得它本该在那里（这正是它被漏了两天的原因）。
            _wall_hist_html = ""
            try:
                _wh_rows = _wall_history_rows(
                    inst, inst.options.symbol if inst.options else "",
                    date.fromisoformat(curr_date_s) if curr_date_s else today)
                _wall_hist_html = render_wall_history(_wh_rows, inst.display_name)
            except Exception as e:
                print(f"⚠️ {inst.key} 墙位历史图失败：{type(e).__name__}: {e}",
                      file=sys.stderr)
            html = render_report_html(outlook, price_svg, oi_svg, cot_svg,
                                      flow_html, macro_html, events_html, tldr_html,
                                      strategy_html,
                                      conc_html=render_concentration_html(an.concentration),
                                      volregime_html=volregime_html,
                                      vol_analysis_html=vol_analysis_html,
                                      expiry_html=expiry_html, fib_html=fib_html,
                                      strong_html=strong_html, struct_html=struct_html,
                                      vintage_html=render_vintage_banner(
                                          prev_date or "", curr_date_s or "", today.isoformat()),
                                      verdict_html=verdict_html,
                                      tech_html=tech_html, stretch_read=stretch_read,
                                      indicators_html=_indicators_html,
                                      expiry_html2=_expiry_html,
                                      summary_html=_summary_html,
                                      layers_html=layers_html,
                                      gate_html=gate_html, cost_html=cost_html,
                                      backmonth_html=backmonth_html,
                                      ratio_html=ratio_html,
                                      wall_hist_html=_wall_hist_html,
                                      credit_wall_html=credit_wall_html)
            # ⚠️ 文件名用【可交易日】（= 快照日期），不是生成日期。
            # 时点约定：快照 D 于 D 凌晨捕获，OI 是 D−1 收盘的 OCC 结算，
            # diff 描述交易日 D−1，**D 开盘才可执行** —— D 就是这份研报的身份。
            # 工作日两者相同看不出来；周末/数据延迟时就错位：2026-08-29（周六）
            # 生成的报告装着描述 8/27 的数据，却被命名成 gold_2026-08-29.html
            # （用户 2026-08-29 指出）。研报的名字必须回答"这份东西哪天能用"。
            fn = f"{inst.key}_{curr_date_s or today.isoformat()}.html"
            _archive_existing(reports_dir / fn)
            (reports_dir / fn).write_text(html, encoding="utf-8")
            try:
                _facts = _flow_facts(fa, ga, ga_prev, prev, curr, curr.spot,
                                     date.fromisoformat(curr_date_s) if curr_date_s else today)
            except Exception as e:   # 出错要出声，不能静默变空卡片
                print(f"⚠️ {inst.key} 索引事实块生成失败：{type(e).__name__}: {e}",
                      file=sys.stderr)
                _facts = {}
            written.append((inst, outlook, fn, strong_sig, verdict, stretch_read,
                            curr_date_s or "", _facts, _labels, _scores))
        except Exception as e:
            failed.append(inst.key)
            print(f"[警告] {inst.key} 研判报告失败: {e}", file=sys.stderr)

    # 机器可读状态：区分 complete / partial / failed，供定时脚本按名分流告警。
    # ⚠️ codex review 指出：cmd_report 只要有一个品种失败就 return 1，
    # 于是脚本侧的"个别品种失败"分支永远不可达 —— 必须靠 overall 区分。
    _ok = [inst.key for inst, *_ in written]
    _ov = ("complete" if not failed else ("partial" if written else "failed"))
    _write_status(getattr(args, "status_file", None), {
        "command": "report", "date": str(today), "overall": _ov,
        "n_ok": len(_ok), "n_failed": len(failed),
        "ok": _ok, "failed": failed})

    if not written:
        print("没有生成任何报告。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([dataclasses.asdict(o) | {"instrument": inst.key} for inst, o, _, _, _, _, _, _, _, _ in written],
                         ensure_ascii=False, indent=2, default=str))
        return 0

    # —— 品种对比值观察（只记录，不判定；用户 2026-08-30「顺手就记了」）——
    _ratio_rows = []
    try:
        from undertow.analyze.ratio_watch import build as _rw_build, save as _rw_save
        _snaps, _futs, _etfs, _mults = {}, {}, {}, {}
        for _inst, _o, _fn, _ss, _v, _sr, _td2, _fx, _lb, _sc in written:
            k = _inst.key
            _cs = _td2 or today.isoformat()
            _p = store.load("options", _inst.options.symbol, date.fromisoformat(_cs))
            if _p is not None:
                _snaps[k] = snapshot_from_payload(_p, k, _inst.options.symbol)
            # ⚠️ ETF 价必须用【当日收盘】，不能用快照的 spot ——
            # 后者是抓取时刻（盘前）的延迟报价，与期货收盘不同时点，
            # 算出来的换算比会错位（2026-08-30 首版就混了：
            # num_etf 取 422.54 而当日实际收 408.89）。
        # 期货价与换算比区间：用近 25 日的比值范围，不用单点
        from undertow.collect.longbridge_kline import fetch_series as _lbk
        for k, _inst in [(i.key, i) for i, *_ in written]:
            if _inst.commodity is None:
                continue
            try:
                _fs = fut_src.fetch_series(_inst.commodity.symbol,
                                           use_cache=not args.no_cache)
                _es = _lbk(f"{_inst.options.symbol}.US", period="day", count=60)
                _em = {str(d): c for d, c in zip(_es.dates, _es.closes)}
                _cs2 = _td2 or today.isoformat()
                if _cs2 in _em:
                    _etfs[k] = _em[_cs2]          # 当日 ETF 收盘
                _rs = [c / _em[str(d)] for d, c in zip(_fs.dates, _fs.closes)
                       if str(d) in _em and _em[str(d)]]
                if _rs:
                    _rs = sorted(_rs[-25:])
                    _mults[k] = (_rs[0], _rs[-1])
                    _futs[k] = _fs.closes[-1]
            except Exception:
                pass
        _ratio_rows = _rw_build(today, _snaps, _futs, _etfs, _mults)
        n = _rw_save(_ratio_rows)
        for r in _ratio_rows:
            if r.ratio is not None:
                _in = ("区间内" if r.inside else
                       (f"区间外 {r.dist_pct:+.1f}%" if r.dist_pct is not None else "—"))
                print(f"  📐 {r.pair} {r.ratio:.2f}"
                      + (f"　墙隐含 {r.implied_lo:.1f}~{r.implied_hi:.1f}（{_in}）"
                         if r.implied_lo else "　（墙位缺失，只记比值）"))
    except Exception as e:      # 观察项失败不阻断，但必须出声
        print(f"⚠️ 比值观察失败：{type(e).__name__}: {e}", file=sys.stderr)


    index_path = None
    if len(written) > 1:
        idx_items = [{"name": o.display_name, "fn": fn, "bias": o.bias,
                      "conf": o.confidence, "summary": _index_summary(o), "signal": ss,
                      "verdict_head": (v.headline if v and getattr(v, "ok", False) else ""),
                      "stretch": sr,
                      "near_bias": getattr(o, "near_bias", ""),
                      "mid_bias": getattr(o, "mid_bias", ""),
                      "trade_date": td, "today": today.isoformat(),
                      "near_score": getattr(o, "near_score", None),
                      "mid_score": getattr(o, "mid_score", None),
                      "facts": _fx | {"bias": o.bias, "mid_bias": o.mid_bias},
                      "spot": o.spot,
                      "labels": _lb, "scores": _sc}
                     for _, o, fn, ss, v, sr, td, _fx, _lb, _sc in written]
        # 同族一致性：金银同向、QQQ/TQQQ 同向 —— 不一致时并排摆出来（用户 2026-08-29）
        _views = {}
        for _inst, _o, _fn, _ss, _v, _sr, _td2, _fx, _lb, _sc in written:
            _stale = bool(_td2 and _td2 < today.isoformat())
            _views[_inst.key] = {
                "near": _o.near_bias or "", "mid": _o.mid_bias or "", "bias": _o.bias,
                "signal_dir": (_ss.direction if (_ss and not _stale) else ""),
                "signal_level": (_ss.level if (_ss and not _stale) else ""),
            }
        idx_family = _family_check(_views)

        # 索引页同理：用各品种里最新的可交易日，不用生成日期
        _idx_day = max((w[6] for w in written if w[6]), default="") or today.isoformat()
        _ratio_html = ""
        if _ratio_rows:
            from undertow.analyze.ratio_watch import render as _rw_render
            from undertow.report.html import _esc as _e2
            _ratio_html = _rw_render(_ratio_rows, _e2)
        index_html = render_index_html(idx_items, _idx_day, family_notes=idx_family,
                                       ratio_html=_ratio_html)
        index_path = reports_dir / f"index_{_idx_day}.html"
        _archive_existing(index_path)
        index_path.write_text(index_html, encoding="utf-8")

    # 标题写【可交易日】不写生成日期 —— 否则周六生成的报告写着 2026-08-29，
    # 装的却是 8/28 可交易的数据（用户 2026-08-29 指出的同一个坑）。
    _hd = max((w[6] for w in written if w[6]), default="") or today.isoformat()
    _gen = f"（生成于 {today}）" if _hd != today.isoformat() else ""
    if replay:
        print("⚠️ 回放模式的已知限制（codex 2026-08-29 P0-2）：")
        print("   只有【期权快照】与【日线价格】按 as-of 截断了。")
        print("   COT 持仓、FRED 宏观、波动率指数、事件日历、4H 技术面")
        print("   仍取【当前值】—— 没有历史 vintage 可用。")
        print("   → 🏦大资金 / 🌍宏观 两层、以及事件雷达，在回放里含未来信息，不可信。")
        print("   → 💰增仓 / 🧱结构 / 🌊波动 / 📈价格 四层是干净的。")
        print()
    print(f"已生成综合研判报告 · 可交易日 {_hd}{_gen}:")
    for inst, o, fn, ss, v, _sr, _td, _fx, _lb, _sc in written:
        # 低置信 / 已过期 的强信号在摘要里也必须降级，不能和可执行告警长得一样。
        # ⚠️ 报告横幅、索引页、CLI 摘要**三处口径必须同步** —— 2026-08-28 实测：
        # SPY 的 ⚡强看涨 在报告里已正确标注"本告警已过期"，CLI 摘要却仍是满格 ⚡，
        # 与昨天修低置信时"渲染层改了、摘要层漏了"是同一类错。
        flag = ""
        if ss:
            _stale_flag = bool(_td and _td < today.isoformat())
            if _stale_flag:
                flag = f"  ·{ss.level}{ss.direction}(已过期)"
            elif getattr(ss, "low_confidence", False):
                flag = f"  ·{ss.level}{ss.direction}(低置信)"
            else:
                flag = f"  ⚡{ss.level}{ss.direction}"
        vh = f"  · {v.headline}" if v and getattr(v, "ok", False) else ""
        # 与索引页一致：不报综合，只报近端/中期两层（用户 2026-08-29）
        _nb = (o.near_bias or "—")
        _mb = (o.mid_bias or "—")
        print(f"  {inst.key:7s} 近{_nb:9s}中{_mb:9s}(可信度{o.confidence})"
              f"{flag}{vh}  → {reports_dir / fn}")
    if index_path:
        print(f"  索引页 → {index_path}")

    # 数据是不是"今天的"，必须说清楚 —— 否则周末生成的报告看着像当日研报。
    _days = sorted({w[6] for w in written if w[6]})
    _tstr = today.isoformat()
    if _days and all(d < _tstr for d in _days):
        print(f"\n📅 今天（{_tstr}）没有新数据：最新快照止于 {_days[-1]}，"
              f"上面这批研报的可交易日是 {_days[-1]}，不是今天。")
        print("   下一份新研报要等新快照落地（OCC 隔夜结算 → 次日凌晨抓取）。")
    elif len(_days) > 1:
        print(f"\n📅 ⚠️ 各品种数据不同龄：{', '.join(_days)} —— 混龄比较需谨慎。")

    # —— 持仓 × 信号冲突告警 ——————————————————————————————————————
    # 用户 2026-08-28 的直接批评：那天黄金亮 ⚡极强看跌（53.5×），他手上持白银多头，
    # 金银相关 0.89 —— 信号没被提、相反结论没被质疑、持仓没被告警。次日 SLV -4.38%。
    # ⚠️ 结果只上终端 + 通知 + data/account/（已 gitignore），**绝不进 HTML**：
    #    研报是入公开库的，账户持仓不能出现在里面。
    try:
        _sig_by_sym = {}
        for _inst, _o, _fn, _ss, _v, _sr, _td2, _fx, _lb, _sc in written:
            if _ss is not None and not (_td2 and _td2 < today.isoformat()):
                _sig_by_sym[_inst.options.symbol.upper()] = _ss
        if _sig_by_sym and not replay:
            # 回放不查持仓：那是"当时"的持仓未知，拿今天的仓位比历史信号毫无意义
            _alert_position_conflicts(_sig_by_sym, today.isoformat())
    except Exception as e:      # 告警失败要出声，不能静默变"没有冲突"
        print(f"⚠️ 持仓冲突检查失败：{type(e).__name__}: {e}", file=sys.stderr)

    if failed:
        print(f"[部分失败] {', '.join(failed)} 未生成——退出码置 1，避免自动化误提交残缺报告集",
              file=sys.stderr)
        return 1
    print(f"\n用浏览器打开即可（macOS: open '{reports_dir / written[0][2]}'）")
    return 0


def _alert_position_conflicts(signals: dict, today: str) -> None:
    """有持仓的品种（或与之高相关的品种）出现反向强信号 → 告警。

    ⚠️ 只上终端 + 系统通知 + data/account/（已 gitignore）。研报 HTML 入公开库，
    账户持仓绝不能写进去。取不到持仓时静默跳过是【可以的】——那是"没连账户"，
    不是"没有冲突"；但取到了却检查失败必须出声（调用方已接住并打印）。
    """
    from undertow.analyze.position_alert import check_conflicts, render, unparsed
    from undertow.collect import longbridge_account as lb
    from undertow.collect import longbridge_quote as lq
    if not lq.available():
        print("⚠️ 持仓冲突检查未能执行：找不到 longbridge CLI —— "
              "这【不是】「没有冲突」，是「没查成」", file=sys.stderr)
        return
    try:
        positions = lb.fetch_positions()
    except Exception as e:
        # ⚠️ 「没连上账户」必须与「没有冲突」可区分（codex 2026-08-29）：
        # 静默返回时，真实告警系统失效的那天，用户看到的仍是"什么都没发生"。
        print(f"⚠️ 持仓冲突检查未能执行：读取账户失败（{type(e).__name__}）——"
              f"这【不是】「没有冲突」，是「没查成」", file=sys.stderr)
        try:
            out = pathlib.Path("data/account/live")
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{today}_conflict.md").write_text(
                f"# 持仓 × 信号冲突 {today}\n\n"
                f"⚠️ **未能执行检查**：读取账户失败（{type(e).__name__}: {e}）。\n"
                f"这不是「没有冲突」，是「没查成」。\n", encoding="utf-8")
        except Exception:
            pass
        return
    held = {p.symbol: int(p.quantity) for p in positions if p.quantity}
    if not held:
        return
    # 解析失败必须出声 —— 解析不出来会让持仓被当成不存在，告警静默消失。
    # 2026-08-29 实测就栽在这：真实代码带 .US 后缀，四条腿一条都没解析出来。
    bad = unparsed(held)
    if bad:
        print(f"⚠️ {len(bad)} 条持仓代码无法解析，冲突检查会漏掉它们："
              f"{', '.join(bad[:4])}", file=sys.stderr)
    conflicts = check_conflicts(held, signals)
    if not conflicts:
        return
    txt = render(conflicts)
    print("\n" + txt)
    out = pathlib.Path("data/account/live")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{today}_conflict.md").write_text(
        f"# 持仓 × 信号冲突 {today}\n\n{txt}\n", encoding="utf-8")
    head = conflicts[0]
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification "{head.headline()[:180]}" '
                        f'with title "⚠️ 持仓与信号方向冲突" sound name "Glass"'],
                       check=False, capture_output=True, timeout=10)
    except Exception:
        pass


def _gamma_jsonable(inst, ga) -> dict:
    return {
        "instrument": inst.key,
        "proxy_symbol": ga.proxy_symbol,
        "spot": ga.spot,
        "asof": ga.asof,
        "multiplier": ga.multiplier,
        "put_call_ratio": ga.put_call_ratio,
        "net_gex": ga.net_gex,
        "gex_regime": ga.gex_regime,
        "zero_gamma": ga.zero_gamma,
        "call_wall": ga.call_wall, "call_wall_oi": ga.call_wall_oi,
        "put_wall": ga.put_wall, "put_wall_oi": ga.put_wall_oi,
        "nearest_expiry": ga.nearest_expiry,
        "nearest_call_wall": ga.nearest_call_wall,
        "nearest_put_wall": ga.nearest_put_wall,
        "commodity_levels": {
            "call_wall": ga.to_commodity(ga.call_wall),
            "put_wall": ga.to_commodity(ga.put_wall),
            "zero_gamma": ga.to_commodity(ga.zero_gamma),
        },
    }


def _account_context(inst, store, sources, today, *, no_cache, live_quotes=None):
    """为某标的组装 InstrumentContext（现价+Gamma墙+近中研判+当日决策+链上greeks）。

    复用 report 的四层聚合，但只取持仓评价需要的字段。失败抛异常，调用方降级跳过。
    live_quotes：{ETF符号: {spot, spot_kind, options:{occ:(last,iv)}}} 实时价（可选）。
    """
    from undertow.analyze.portfolio import InstrumentContext
    cot_src, opt_src, fut_src, fred_src, vol_src = sources
    lookback = load_config().lookback_weeks

    an = analyze(cot_src.fetch_history(inst, lookback=lookback, use_cache=not no_cache))
    signals = generate_signals(an)
    curr, prev, prev_date, curr_date_s = _load_curr_prev_snapshot(
        store, opt_src, inst, today, no_cache=no_cache, no_snapshot=True)

    ratio, real_series, real_price = None, None, None
    if inst.commodity is not None:
        try:
            real_series, real_price, _asof = fut_src.fetch_for(inst, use_cache=not no_cache)
            if curr.spot > 0:
                ratio = real_price / curr.spot
        except Exception:
            pass
    from undertow.analyze.technicals import analyze_technicals
    from undertow.analyze.stretch import analyze_stretch
    # 与 report/tech 同口径：剔掉当日未完成的盘中 bar，否则同一天不同命令读数会打架
    tech_series = _drop_incomplete_bar(real_series, today)
    technicals = analyze_technicals(tech_series) if tech_series is not None else None
    stretch = analyze_stretch(tech_series) if tech_series is not None else None
    mult = ratio if ratio is not None else inst.options.approx_commodity_multiplier
    obs_day = _prev_weekday(date.fromisoformat(curr_date_s)) if curr_date_s else _prev_weekday(today)

    ga = analyze_gamma(curr, multiplier=mult, proxy_quality=inst.options.proxy_quality,
                       today=obs_day, horizon_days=45)
    fa = analyze_flow(prev, curr, today=obs_day, horizon_days=45,
                      call_wall=ga.call_wall, put_wall=ga.put_wall,
                      prev_date=prev_date, curr_date=curr_date_s)

    macro_votes = []
    try:
        sids = series_ids_for(inst.asset_class)
        smap = {sid: fred_src.fetch_series(sid, use_cache=not no_cache) for sid in sids}
        vseries = vol_src.fetch_series(inst.vol_index, use_cache=not no_cache) if inst.vol_index else None
        macro_votes = macro_to_votes(analyze_macro(smap, asset_class=inst.asset_class,
                                                    vol_name=inst.vol_index, vol_series=vseries))
    except Exception:
        pass

    outlook = build_outlook(an, signals, ga, fa, display_name=inst.display_name,
                            extra_votes=macro_votes)
    strong_sig = detect_strong_signal(
        fa, outlook_bias=(getattr(outlook, "near_bias", "") or outlook.bias),
        mid_bias=getattr(outlook, "mid_bias", ""))
    # ⚠️ 此处【不】写台账：_account_context 走盘中实时链，report 走结算后链。
    # 两者同日同方向会互相覆盖，谁赢取决于当天先跑 live 还是先跑 report。
    # 台账只认结算后口径（report 路径），否则攒出来的样本是混口径的，回测作废。
    verdict = None
    try:
        fib_an = build_fibonacci(real_series, ratio=ratio,
                                 spot=(real_price if real_price else curr.spot)) if real_series is not None else None
        rr_plan = build_risk_reward(fib_an, o=outlook) if fib_an is not None else None
        verdict = build_verdict(outlook, fa, strong_sig, fib_an, rr_plan)
    except Exception:
        pass

    # 链上 greeks 查询（ETF 口径行权价；持仓也是 ETF 行权价，直接匹配）
    lut = {}
    for c in curr.with_oi():
        lut[(c.kind, round(c.strike, 3), c.expiry)] = (c.delta, c.iv)

    def greeks(kind, strike, expiry):
        return lut.get((kind, round(strike, 3), expiry))

    # —— 实时价覆盖：有则用最新股价当现价（修快照收盘过期）、期权实时 last/IV 供估值 ——
    spot = curr.spot
    spot_source = "snapshot"
    price_note = f"ETF 快照收盘 {curr.spot:.2f}（{curr_date_s}）"
    lq = (live_quotes or {}).get(inst.options.symbol.upper())
    if lq is not None:
        if lq.get("spot"):
            spot = lq["spot"]
            spot_source = lq.get("spot_kind", "实时")
            price_note = f"实时{spot_source}股价 {spot:.2f}"
    live_opt = (lq or {}).get("options") if lq else None
    if live_opt:
        price_note += f" · {len(live_opt)} 个持仓期权用实时价估值"
    elif lq is not None:
        price_note += " · 无期权行情权限，期权用 BS 理论估值"

    return InstrumentContext(
        etf_symbol=inst.options.symbol, display_name=inst.display_name,
        spot=spot, call_wall=ga.call_wall, put_wall=ga.put_wall,
        zero_gamma=ga.zero_gamma, bias=outlook.bias,
        near_bias=outlook.near_bias or "", mid_bias=outlook.mid_bias or "",
        verdict_head=(verdict.headline if verdict else ""),
        proxy_quality=inst.options.proxy_quality, greeks=greeks,
        spot_source=spot_source, live_opt=live_opt, price_note=price_note,
        technicals=technicals, stretch=stretch)


def _fetch_live_quotes(positions):
    """取实时报价：每个持仓 ETF 的最新股价 + 各期权合约的实时 last/IV。

    两级降级：有 OPRA 订阅→期权也实时；无订阅/非长桥→只股价；全失败→返回 {}（回退快照）。
    返回 {ETF符号大写: {spot, spot_kind, options:{occ:(last,iv)}}}。
    """
    from undertow.collect import longbridge_quote as lq
    from undertow.analyze.portfolio import parse_symbol
    if not lq.available():
        return {}
    # 归类：ETF 标的 + 各标的的期权合约
    etf_syms, opt_by_etf = set(), {}
    for p in positions:
        ps = parse_symbol(p.symbol)
        etf = ps.underlying
        etf_syms.add(f"{etf}.US")
        if ps.is_option:
            opt_by_etf.setdefault(etf, []).append(p.symbol)
    out = {}
    try:
        sq = lq.fetch_stock_quotes(sorted(etf_syms))
    except lq.LiveQuotesUnavailable as e:
        print(f"[提示] 实时股价不可用，回退快照价：{str(e)[:80]}", file=sys.stderr)
        return {}
    for full, q in sq.items():
        root = full.split(".")[0].upper()
        out[root] = {"spot": q.freshest, "spot_kind": q.freshest_kind, "options": None}
    # 期权实时价（需订阅；失败则只保留股价）
    for etf, occs in opt_by_etf.items():
        try:
            oq = lq.fetch_option_quotes(occs)
            om = {s: (o.last, o.iv) for s, o in oq.items()}
            if etf in out:
                out[etf]["options"] = om
        except lq.LiveQuotesUnavailable as e:
            print(f"[提示] {etf} 期权实时报价不可用（未订阅？），期权用 BS 估值：{str(e)[:70]}",
                  file=sys.stderr)
    return out


def _build_contexts(positions, no_cache, live_quotes=None):
    """为持仓涉及的品种构建 {ETF根: InstrumentContext}（复用四层聚合）。"""
    from undertow.analyze.portfolio import parse_symbol
    cfg = load_config()
    etf_to_inst = {i.options.symbol.upper(): i for i in cfg.instruments.values()
                   if i.options is not None}
    held_roots = {parse_symbol(p.symbol).underlying for p in positions}
    store = SnapshotStore()
    today = market_today()
    sources = (CftcCotSource(), CboeOptionsSource(), YahooFuturesSource(),
               FredMacroSource(), CboeVolSource())
    contexts = {}
    for root in sorted(held_roots):
        inst = etf_to_inst.get(root)
        if inst is None:
            continue
        try:
            contexts[root] = _account_context(inst, store, sources, today,
                                              no_cache=no_cache, live_quotes=live_quotes)
        except Exception as e:
            print(f"[提示] {root} 研判上下文构建失败，该标的仅列出不评方向: {str(e)[:120]}",
                  file=sys.stderr)
    return contexts, today


def _load_account_review(no_cache):
    """读实盘持仓 → 构建研判上下文 + 资金 → 评价 + 体检。**只读**。

    返回 dict：positions/contexts/review/health/capital/assets/today；无持仓时 review=None。
    """
    from undertow.collect import longbridge_account as lb
    from undertow.analyze.portfolio import review_portfolio, AccountCapital
    from undertow.analyze.healthcheck import run_healthcheck

    positions = lb.fetch_positions()
    if not positions:
        return {"positions": [], "review": None, "today": market_today()}
    live_quotes = {}
    try:
        live_quotes = _fetch_live_quotes(positions)
    except Exception as e:
        print(f"[提示] 实时报价获取跳过，回退快照价：{str(e)[:100]}", file=sys.stderr)
    contexts, today = _build_contexts(positions, no_cache, live_quotes=live_quotes)
    assets = None
    try:
        assets = lb.fetch_assets()
    except lb.LongbridgeUnavailable:
        pass
    capital = None
    if assets is not None:
        capital = AccountCapital(buy_power=assets.buy_power, net_assets=assets.net_assets,
                                 cash_usd=assets.cash_by_ccy.get("USD", 0.0))
    review = review_portfolio(positions, contexts, asof=today, capital=capital)
    health = run_healthcheck(review, capital)
    return {"positions": positions, "contexts": contexts, "review": review,
            "health": health, "capital": capital, "assets": assets, "today": today}


def cmd_account(args) -> int:
    """实盘持仓理论评价：读长桥账户当前持仓 → 逐笔对 undertow 研判做复盘。

    **只读**（绝不下单）；持仓/资金属敏感数据，HTML 落 gitignore 的 data/account/。
    """
    from undertow.collect import longbridge_account as lb
    from undertow.report.html import render_account_html
    from undertow.report.markdown import render_account_md

    try:
        bundle = _load_account_review(args.no_cache)
    except lb.LongbridgeUnavailable as e:
        print(f"[长桥账户不可用] {e}", file=sys.stderr)
        return 2
    if bundle["review"] is None:
        print("账户当前无持仓。")
        return 0
    positions = bundle["positions"]
    review, health, assets = bundle["review"], bundle["health"], bundle["assets"]
    today = bundle["today"]

    print(render_account_md(review, assets, health))

    # —— 每次评价落一份数据快照：持仓+资产+资金流水+成交，为将来历史复盘攒数据 ——
    # 全部 gitignore（data/account/），不入公开仓库；失败不阻断评价。
    if not getattr(args, "no_save", False):
        try:
            _save_account_snapshot(lb, positions, assets, today, no_cache=args.no_cache)
        except Exception as e:
            print(f"[提示] 账户数据快照保存失败（不影响评价）: {str(e)[:120]}", file=sys.stderr)

    if not getattr(args, "no_html", False):
        out_dir = DATA_DIR / "account"      # gitignore：敏感数据不入公开仓库
        out_dir.mkdir(parents=True, exist_ok=True)
        fn = out_dir / f"account_{today.isoformat()}.html"
        fn.write_text(render_account_html(review, assets, health), encoding="utf-8")
        print(f"\n实盘评价 HTML（本地私有，未入 git）→ {fn}")
    return 0


def _parse_pretrade_spec(spec: str):
    """解析拟开仓 spec → 合成持仓对象列表。

    格式：`合约代码:数量:成本` 逗号分隔，如
      SLV260919P60000.US:-4:0.5,SLV260919P58000.US:4:0.25
    数量正=买/负=卖。合约代码用长桥格式（行权价×1000 不补零）。
    """
    from undertow.collect.longbridge_account import RawPosition
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        f = part.split(":")
        if len(f) != 3:
            raise ValueError(f"spec 段格式应为 代码:数量:成本，收到：{part}")
        sym, qty, cost = f[0].strip(), float(f[1]), float(f[2])
        out.append(RawPosition(symbol=sym, name=sym, quantity=qty, cost_price=cost,
                               currency="USD", market="US"))
    return out


def _news_symbol(inst):
    """品种取新闻用的标的代码：优先期权 ETF 代理（GLD/SLV/USO/QQQ）。"""
    if inst.options is not None and inst.options.symbol:
        return f"{inst.options.symbol}.US"
    return None


def _build_news_digest(inst, events, today, *, limit=12):
    """为某品种组装事件感知 digest（新闻 + 临近事件）。新闻失败则只出事件。"""
    from undertow.analyze.newsfeed import build_news_digest
    items = []
    sym = _news_symbol(inst)
    if sym:
        try:
            from undertow.collect import longbridge_news as ln
            items = ln.fetch_news(sym, limit=limit)
        except Exception as e:
            print(f"[提示] {inst.key} 新闻获取跳过：{str(e)[:80]}", file=sys.stderr)
    return build_news_digest(inst.key, inst.display_name, items, events, today)


def cmd_soul(args) -> int:
    """交易灵魂档案：显示你的交易体系/纪律/弱点；--check 用它核当前持仓。"""
    from undertow.soul.profile import (load_profile, render_profile_md,
                                       check_against_profile, init_from_template)
    if getattr(args, "init", False):
        try:
            path, created = init_from_template()
        except FileNotFoundError as e:
            print(f"[失败] {e}", file=sys.stderr)
            return 2
        if created:
            print(f"已从模板生成本地私有档案 → {path}\n（该文件已 gitignore，绝不进公开仓库；"
                  f"请按自己的情况改写 rules / limits，再跑 `undertow soul` 查看）")
        else:
            print(f"档案已存在，未覆盖 → {path}")
        return 0
    prof = load_profile()
    if getattr(args, "json", False):
        import dataclasses as _dc
        print(json.dumps(_dc.asdict(prof) if prof else {}, ensure_ascii=False, indent=2))
        return 0
    print(render_profile_md(prof))
    if not getattr(args, "check", False):
        return 0
    if prof is None:
        print("\n（无档案，跳过持仓核查）", file=sys.stderr)
        return 0
    from undertow.collect import longbridge_account as lb
    try:
        bundle = _load_account_review(args.no_cache)
    except lb.LongbridgeUnavailable as e:
        print(f"\n[长桥账户不可用] {e}", file=sys.stderr)
        return 2
    if bundle["review"] is None:
        print("\n当前无持仓，无需核查。")
        return 0
    vios = check_against_profile(bundle["review"], bundle.get("capital"), prof)
    print("\n## 🧭 纪律核查（对照你自己的规则）\n")
    if not vios:
        print("- ✅ 当前持仓未触碰你设定的任何限额。")
        return 0
    for v in vios:
        print(f"- **[{v.severity}] {v.title}** —— {v.detail}")
        if v.scope:
            print(f"  - 涉及：{v.scope}")
    return 0


def cmd_journal(args) -> int:
    """交易日记：记录成交明细/复盘/盖棺定论/心情。--capture 从券商自动抓当日成交。**只读。**"""
    from undertow.soul.journal import (load_journal, save_journal, capture_trades,
                                       JournalEntry, render_journal_md, render_entry_md,
                                       load_theses, render_theses_md)
    entries = load_journal()
    if getattr(args, "theses", False):
        print(render_theses_md(load_theses()))
        return 0
    if getattr(args, "capture", False):
        from undertow.collect import longbridge_account as lb
        day = market_today().isoformat()
        try:
            ex = lb.fetch_today_executions() or lb.fetch_executions(start=day)
            cf = lb.fetch_cash_flow(start=day)
        except Exception as e:
            print(f"[抓取失败] {str(e)[:120]}", file=sys.stderr)
            return 2
        trades = capture_trades(ex, cf, day=day)
        if not trades:
            print(f"{day} 无成交记录。")
            return 0
        fees = sum(t.fee for t in trades)
        assets = None
        try:
            assets = lb.fetch_assets()
        except Exception:
            pass
        e = JournalEntry(date=day, title="（待补标题）", trades=trades, fees=fees,
                         net_assets_after=(assets.net_assets if assets else None),
                         buy_power_after=(assets.buy_power if assets else None),
                         analysis="（待复盘）", verdict="", mood="")
        entries = [x for x in entries if x.date != day] + [e]
        save_journal(sorted(entries, key=lambda x: x.date, reverse=True))
        print(render_entry_md(e))
        print("\n> 已抓取落盘。复盘/定论/心情可直接编辑 data/soul/journal.json，或让我帮你写。")
        return 0
    if getattr(args, "date", None):
        hit = [e for e in entries if e.date == args.date]
        if not hit:
            print(f"[无记录] {args.date}", file=sys.stderr)
            return 2
        print(render_entry_md(hit[0]))
        return 0
    print(render_journal_md(entries, limit=getattr(args, "limit", 0) or 0))
    return 0


def cmd_event(args) -> int:
    """事件影响捕捉：数据/事件落地【前】与【后约10分钟】各捕一次横截面快照，再对比。

    事件时点不可再生——错过就没了。攒够后可用自己的数据统计各类事件的真实冲击。**只读。**
    """
    import datetime as _dt
    from undertow.analyze.event_impact import (EventSnapshot, InstrumentSnap, compare,
                                               render_compare_md, render_snap_md)
    from undertow.analyze.technicals import analyze_technicals
    out_dir = DATA_DIR / "history" / "events"
    out_dir.mkdir(parents=True, exist_ok=True)

    if getattr(args, "compare", None):
        import glob as _g
        files = sorted(_g.glob(str(out_dir / f"*{args.compare}*.json")))
        if len(files) < 2:
            print(f"[需要两份快照] 匹配 '{args.compare}' 的只有 {len(files)} 份", file=sys.stderr)
            return 2
        def _load(fp):
            raw = json.loads(pathlib.Path(fp).read_text(encoding="utf-8"))
            raw["instruments"] = [InstrumentSnap(**i) for i in raw.get("instruments", [])]
            return EventSnapshot(**raw)
        print(render_compare_md(compare(_load(files[0]), _load(files[-1]))))
        return 0

    cfg = load_config()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    fut_src, store, opt_src = YahooFuturesSource(), SnapshotStore(), CboeOptionsSource()
    today = market_today()
    snaps, heads = [], []
    # 实时价（含盘前/盘后场次）
    live, sessions = {}, {}
    try:
        from undertow.collect import longbridge_quote as lq
        syms = [f"{i.options.symbol}.US" for i in instruments if i.options]
        live = lq.fetch_stock_quotes(syms)
        # 各场次原始值（freshest 会丢信息；且非盘前时段 pre_market 是陈旧值）
        import subprocess as _sp, json as _j
        raw = _sp.run(["longbridge", "quote", *syms, "--format", "json"],
                      capture_output=True, text=True, timeout=20)
        if raw.returncode == 0:
            for r in _j.JSONDecoder().raw_decode(raw.stdout.lstrip())[0]:
                def _p(d):
                    try:
                        return float((d or {}).get("last")) if (d or {}).get("last") else None
                    except (TypeError, ValueError):
                        return None
                sessions[r["symbol"]] = {
                    "regular": _p(r), "overnight": _p(r.get("overnight")),
                    "pre": _p(r.get("pre_market")), "post": _p(r.get("post_market"))}
    except Exception as e:
        print(f"[提示] 实时价跳过：{str(e)[:70]}", file=sys.stderr)

    for inst in instruments:
        if inst.options is None:
            continue
        q = live.get(f"{inst.options.symbol}.US")
        atm_iv, cw, pw, bias = None, None, None, ""
        try:
            snap = store.load("options", inst.options.symbol, today)
            snap = snapshot_from_payload(snap, inst.key, inst.options.symbol) if snap else \
                   opt_src.fetch_snapshot(inst, use_cache=True)
            ga = analyze_gamma(snap, multiplier=None,
                               proxy_quality=inst.options.proxy_quality, horizon_days=45)
            cw, pw = ga.call_wall, ga.put_wall
            ivs = [c.iv for c in snap.with_oi()
                   if c.iv > 0 and 0.35 <= abs(c.delta) <= 0.65]
            atm_iv = sum(ivs) / len(ivs) if ivs else None
        except Exception:
            pass
        # 期货实时价——主信号（≈23h 交易，数据公布瞬间最真实）
        heat, trend, fut_px, fut_asof, fut_sym = None, "", None, "", ""
        try:
            ser, fut_px, fut_asof = fut_src.fetch_for(inst, use_cache=False)
            fut_sym = inst.commodity.symbol if inst.commodity else ""
            tr = analyze_technicals(ser)
            if tr.ok:
                heat, trend = tr.heat_score, tr.trend
        except Exception as e:
            print(f"[提示] {inst.key} 期货价跳过：{str(e)[:60]}", file=sys.stderr)
        kind = q.freshest_kind if q else ""
        ss = sessions.get(f"{inst.options.symbol}.US", {})
        snaps.append(InstrumentSnap(
            key=inst.key, display_name=inst.display_name,
            fut_symbol=fut_sym, fut_price=fut_px, fut_asof=fut_asof,
            spot=(q.freshest if q else None), spot_kind=kind,
            etf_regular=ss.get("regular"), etf_overnight=ss.get("overnight"),
            etf_pre=ss.get("pre"), etf_post=ss.get("post"),
            prev_close=(q.prev_close if q else None),
            change_pct=(q.change_pct * 100 if q else None),
            atm_iv=atm_iv, iv_stale=(kind in ("盘前", "夜盘")),
            call_wall=cw, put_wall=pw, heat_score=heat, trend=trend, bias=bias))
    try:
        from undertow.collect import longbridge_news as ln
        for inst in instruments[:1]:
            if inst.options:
                heads = [x.title for x in ln.fetch_news(f"{inst.options.symbol}.US", limit=5)]
    except Exception:
        pass

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    es = EventSnapshot(label=args.label, at=now, phase=(args.phase or ""),
                       event_name=(args.event or ""), instruments=snaps, headlines=heads)
    fn = out_dir / f"{today.isoformat()}_{args.label}.json"
    fn.write_text(json.dumps(asdict_es(es), ensure_ascii=False, indent=2), encoding="utf-8")
    print(render_snap_md(es))
    print(f"\n快照已存 → {fn}")
    return 0


def asdict_es(es):
    import dataclasses as _dc
    return _dc.asdict(es)


def cmd_plan(args) -> int:
    """计划交易：记录/监控触发与出场条件，输出可照抄的下单参数。**只读，绝不下单。**"""
    from undertow.soul.plan import (load_plans, check_plans, render_plans_md, render_orders)
    plans = load_plans()
    if getattr(args, "orders", None):
        hit = [p for p in plans if p.id == args.orders]
        if not hit:
            print(f"[未找到] 计划 id={args.orders}；现有：{[p.id for p in plans]}", file=sys.stderr)
            return 2
        print(render_orders(hit[0]))
        return 0

    alerts = []
    if getattr(args, "check", False) and plans:
        spots = {}
        try:
            from undertow.collect import longbridge_quote as lq
            syms = sorted({f"{p.underlying}.US" for p in plans if p.status == "waiting"})
            for full, q in lq.fetch_stock_quotes(syms).items():
                spots[full.split(".")[0].upper()] = q.freshest
        except Exception as e:
            print(f"[提示] 实时价获取失败，跳过触发核查：{str(e)[:80]}", file=sys.stderr)
        alerts = check_plans(plans, spots)
    print(render_plans_md(plans, alerts))
    if getattr(args, "notify", False) and alerts:
        fired = [a for a in alerts if a.kind == "触发"]
        if fired:
            msg = "；".join(a.detail[:60] for a in fired[:2])
            try:
                subprocess.run(["/usr/bin/osascript", "-e",
                                f'display notification "{msg}" with title "🎯 undertow 计划触发" sound name "Glass"'],
                               check=False, timeout=10)
            except Exception:
                pass
            out = DATA_DIR / "account"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"PLAN_ALERT_{market_today().isoformat()}.txt").write_text(
                "\n".join(a.detail for a in fired), encoding="utf-8")
    return 0


def cmd_tech(args) -> int:
    """技术面：短线过热度 + 趋势结构（从真实价序确定性算 RSI/KDJ/MACD/布林/均线）。"""
    from undertow.analyze.technicals import analyze_technicals, render_md
    cfg = load_config()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    fut_src = YahooFuturesSource()
    px_src = CboeHistorySource()
    blocks = []
    for inst in instruments:
        ser = None
        if inst.commodity is not None:
            try:
                ser, _p, _a = fut_src.fetch_for(inst, use_cache=not args.no_cache)
            except Exception as e:
                print(f"[提示] {inst.key} 期货价序取失败：{str(e)[:70]}", file=sys.stderr)
        if ser is None and inst.price is not None:
            try:
                ser = px_src.fetch_series(inst, use_cache=not args.no_cache)
            except Exception:
                pass
        ser = _drop_incomplete_bar(ser, market_today())   # 与 report 同口径
        block = render_md(analyze_technicals(ser), inst.display_name)
        # 拉伸度：过热分的回测校准替代品（超卖侧强 67%）。两者并列展示，
        # 分歧时以拉伸度为准——过热分五个分量彼此相关 0.79~0.93，是同一信息数了四遍。
        try:
            from undertow.analyze.stretch import analyze_stretch, render_md as stretch_md
            block += "\n\n" + stretch_md(analyze_stretch(ser))
        except Exception as e:
            block += f"\n\n- 拉伸度：计算失败 {str(e)[:60]}"
        blocks.append(block)
    print("# 技术面（短线过热度 + 趋势结构 · 与期权结构层正交交叉印证）\n")
    print("\n\n---\n\n".join(blocks))
    return 0


# 校准面板：长历史、跨资产类别。用 ETF 而非期货，因为期货连续合约在 Yahoo 上
# 只回溯到 2004 年前后且有展期跳空；ETF 日线干净且能覆盖 2000/2008/2022 三轮熊市。
CALIB_PANEL = [("GLD", "黄金GLD"), ("SLV", "白银SLV"), ("USO", "原油USO"),
               ("QQQ", "QQQ"), ("SPY", "SPY")]


def cmd_signals(args) -> int:
    """强信号台账：重建 / 回填真实走势 / 出统计。

    这一层是全报告唯一顶红色告警却从未回测过的部分，核心三闸门需历史逐行 OI、
    免费源拿不到，只能向前累积。详见 signal_ledger 模块 docstring。
    """
    from undertow.analyze import signal_ledger as sl
    from undertow.collect.cboe_options import snapshot_from_payload

    cfg = load_config()
    keys = args.instruments or [k for k, v in cfg.instruments.items() if v.options]
    unknown = [k for k in keys if k not in cfg.instruments]
    if unknown:
        print(f"未知品种：{', '.join(unknown)}", file=sys.stderr)
        return 2

    if args.horizon not in sl.HORIZONS:
        print(f"--horizon 必须是 {sl.HORIZONS} 之一（台账只回填这几个格子），"
              f"收到 {args.horizon}", file=sys.stderr)
        return 2

    if args.rebuild:
        store = SnapshotStore()
        for key in keys:
            inst = cfg.instruments[key]
            if not inst.options:
                continue
            sym = inst.options.symbol
            # 真重建：先清空。否则改了阈值/修了算法后旧定义的行会残留，
            # 统计就成了不同版本定义的混合，不可解释。
            dropped = sl.clear(key)
            dates = store.dates("options", sym)
            snaps = {}
            for d in dates:
                try:
                    snaps[d] = snapshot_from_payload(store.load("options", sym, d), key, sym)
                except Exception as e:
                    print(f"[警告] {key} {d} 快照解析失败：{type(e).__name__} {e}",
                          file=sys.stderr)
            dates = [d for d in dates if d in snaps]
            got = 0
            for i in range(1, len(dates)):
                pv, cu = dates[i - 1], dates[i]
                try:
                    fa = analyze_flow(snaps[pv], snaps[cu], today=cu,
                                      prev_date=pv.isoformat(), curr_date=cu.isoformat())
                except Exception as e:
                    print(f"[警告] {key} {cu} 资金流分析失败：{type(e).__name__} {e}",
                          file=sys.stderr)
                    continue
                # ⚠️ 重建时不带 outlook_bias：综合研判依赖 COT/宏观等【当时】的数据，
                # 事后重跑拿到的是修订后的版本，会造成前视偏差。diverges 因此留空，
                # 只有日常管线实时写入的那些才有该字段。
                row = sl.record(key, on_date=cu.isoformat(), prev_date=pv.isoformat(),
                                spot=snaps[cu].spot, probe=probe_strong_signal(fa),
                                signal=detect_strong_signal(fa))
                got += bool(row["fired"])
            print(f"  {key:<8} 清旧 {max(dropped,0)} 行 → 回放 {len(dates)} 份快照，"
                  f"记录 {max(len(dates)-1,0)} 天，其中开火 {got} 次")

    if args.backfill:
        price_src = CboeHistorySource()
        for key in keys:
            inst = cfg.instruments[key]
            try:
                ser = price_src.fetch_series(inst, use_cache=not args.no_cache)
            except Exception as e:
                print(f"[警告] {key} 价格序列获取失败，跳过回填：{type(e).__name__} {e}",
                      file=sys.stderr)
                continue
            filled, pending = sl.backfill(key, ser.dates, ser.closes)
            print(f"  {key:<8} 回填 {filled} 行，仍有 {pending} 个前瞻格未到期")

    rows = sl.load_all(keys)
    if not rows:
        print("台账为空。先跑 `undertow signals --rebuild --backfill`。")
        return 0
    if not any(r.get("fired") for r in rows):
        print(f"台账有 {len(rows)} 天记录，但尚无开火信号，无从统计。")
        return 0
    print()
    print(sl.render_md(rows, horizon=args.horizon))
    return 0


def cmd_backtest_stretch(args) -> int:
    """重跑拉伸度校准表。改了指标参数就跑这个，别手改 stretch.py 里的数字。"""
    from undertow.analyze import stretch_backtest as sb
    from undertow.analyze.stretch import CALIB_ASOF
    # ⚠️ --horizon 必须落在 --horizons 里，否则 calibrate 会 KeyError 崩溃
    # （样本只算 horizons 中的收益）。同时校验为正整数。（codex review 2026-08-26）
    if any(h <= 0 for h in args.horizons) or args.horizon <= 0:
        print("前瞻天数必须为正整数", file=sys.stderr)
        return 2
    if args.horizon not in args.horizons:
        args.horizons = sorted(set(args.horizons) | {args.horizon})
        print(f"[提示] --horizon {args.horizon} 不在 --horizons 中，已自动并入："
              f"{args.horizons}", file=sys.stderr)
    src = YahooFuturesSource()
    syms = [(s, s) for s in args.symbols] if args.symbols else CALIB_PANEL
    samples, spans, contain = [], [], []
    for sym, name in syms:
        try:
            ser = src.fetch_series(sym, rng="max", use_cache=not args.no_cache)
        except Exception as e:
            print(f"[跳过] {sym}: {str(e)[:70]}", file=sys.stderr)
            continue
        got = sb.build_samples(name, ser.highs, ser.lows, ser.closes,
                               horizons=tuple(args.horizons), mode=args.mode)
        if not got:
            print(f"[跳过] {sym}: 历史不足（{len(ser.closes)} 根）", file=sys.stderr)
            continue
        samples += got
        # 不突破率必须按品种算（要用各自的 ATR 与高低价），再加权合并
        contain.append(sb.containment_stats(name, ser.highs, ser.lows, ser.closes, got,
                                            horizon=args.horizon))
        spans.append(f"{name} {ser.dates[0]}→{ser.dates[-1]} ({len(ser.closes)}根)")
        print(f"  {name:<10} {len(ser.closes):>6} 根 → {len(got):>6} 样本", file=sys.stderr)
    if not samples:
        print("无可用样本", file=sys.stderr)
        return 1

    cal = sb.calibrate(samples, horizon=args.horizon)
    print(f"# 超买超卖校准回测（口径 {args.mode}）\n")
    print("数据：" + "；".join(spans) + "\n")
    print(sb.render_table_md(cal, horizon=args.horizon, total=len(samples)))
    print()
    prof = sb.horizon_profile(samples, horizons=tuple(args.horizons))
    print("### 信号持续多久（全样本，相对中性桶）\n")
    print("| 档位 | " + " | ".join(f"+{k}日" for k in args.horizons) + " |")
    print("|---|" + "---:|" * len(args.horizons))
    for band, row in prof.items():
        print(f"| {band} | " + " | ".join(f"{row.get(k, 0):+.3f}pp" for k in args.horizons) + " |")

    # 卖方口径：不突破率（卖方赢的条件不是猜对方向，是价格别再朝反方向走出去）
    acc = sb.merge_containment(contain)
    if acc:
        print()
        print(sb.render_containment_md(acc, horizon=args.horizon))
        _persist_containment(acc, horizon=args.horizon, mode=args.mode, spans=spans)

    # 两维分歧：分歧时该信谁？（答案应是"都别太信"）
    dv = sb.diverge_stats(samples, horizon=args.horizon)
    if dv["rows"]:
        print(f"\n### 两维一致 vs 分歧（+{args.horizon} 日）\n")
        print("| 组合 | 触发 | 边缘 | 跑赢漂移 | t |")
        print("|---|---:|---:|---:|---:|")
        for r in dv["rows"]:
            print(f"| {r['label']} | {r['n']} | **{r['edge_pp']:+.3f}pp** | "
                  f"{r.get('beat_drift', 0):.0f}% | {r['t']:+.2f} |")

    if args.compare:
        print(f"\n### 三种口径对照（最低/最高 10%，+{args.horizon} 日）\n")
        print("| 口径 | 超卖边缘 | t | 超买边缘 | t |")
        print("|---|---:|---:|---:|---:|")
        for m in sb.SIGNAL_MODES:
            s2 = []
            for sym, name in syms:
                try:
                    ser = src.fetch_series(sym, rng="max", use_cache=True)
                except Exception:
                    continue
                s2 += sb.build_samples(name, ser.highs, ser.lows, ser.closes,
                                       horizons=tuple(args.horizons), mode=m)
            c2 = sb.calibrate(s2, horizon=args.horizon)
            def pick(bands):
                vals = [c2[(b, rg)] for b in bands for rg in ("牛", "熊") if (b, rg) in c2]
                if not vals:
                    return 0.0, 0.0
                w = sum(v["n"] for v in vals)
                return (sum(v["edge_pp"] * v["n"] for v in vals) / w,
                        sum(v["t"] * v["n"] for v in vals) / w)
            le, lt = pick(("极超卖", "强超卖"))
            he, ht = pick(("极超买", "强超买"))
            print(f"| {m} | {le:+.3f}pp | {lt:+.2f} | {he:+.3f}pp | {ht:+.2f} |")
    if args.emit:
        print(f"\n### 粘回 undertow/analyze/stretch.py（当前表 asof {CALIB_ASOF}）\n")
        print("```python")
        print(sb.render_calib_literal(cal))
        print("```")
    return 0


def cmd_news(args) -> int:
    """事件感知：品种相关新闻 + 临近关键事件（高影响事件临近置顶告警）。只读。"""
    from undertow.analyze.newsfeed import render_digest_md
    cfg = load_config()
    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2
    events, _src = _merged_events(getattr(args, "no_live", False), args.no_cache)
    today = market_today()
    blocks = []
    for inst in instruments:
        dg = _build_news_digest(inst, events, today)
        blocks.append(render_digest_md(dg))
    print("# 事件感知（新闻 + 临近关键事件 · 只读背景层）\n")
    print("\n\n---\n\n".join(blocks))
    return 0


def cmd_consult(args) -> int:
    """咨询：把研判+持仓评价+体检+你的问题装成"咨询上下文包"，供 AI 给意见。

    默认打印可投喂任意 LLM 的 prompt；--json 打印机器可读的完整包（供其它 AI 接入）。
    --pre-trade 给拟开仓 spec 做开仓前问诊。**只读，不下单。**
    """
    from undertow.collect import longbridge_account as lb
    from undertow.analyze.portfolio import review_portfolio
    from undertow.analyze.healthcheck import run_healthcheck
    from undertow.consult.packet import build_consult_packet

    try:
        bundle = _load_account_review(args.no_cache)
    except lb.LongbridgeUnavailable as e:
        print(f"[长桥账户不可用] {e}", file=sys.stderr)
        return 2
    if bundle["review"] is None:
        print("账户当前无持仓（可仍用 --pre-trade 做开仓前问诊）。", file=sys.stderr)

    contexts = bundle.get("contexts", {})
    capital = bundle.get("capital")
    today = bundle["today"]
    review = bundle["review"]
    health = bundle.get("health", [])

    mode = "review"
    pre_trade = None
    if getattr(args, "pre_trade", None):
        mode = "pre_trade"
        try:
            pt_positions = _parse_pretrade_spec(args.pre_trade)
        except ValueError as e:
            print(f"[spec 解析失败] {e}", file=sys.stderr)
            return 2
        # 拟开仓涉及的品种也要有上下文
        pt_contexts, _ = _build_contexts(pt_positions, args.no_cache)
        merged = {**pt_contexts, **contexts}
        contexts = merged
        pt_review = review_portfolio(pt_positions, contexts, asof=today, capital=capital)
        pt_health = run_healthcheck(pt_review, capital)
        pre_trade = {"spec": args.pre_trade, "review": pt_review, "health": pt_health}

    # 无持仓且仅问诊：用空评价占位
    if review is None:
        review = review_portfolio([], contexts, asof=today, capital=capital)
        health = []

    # 事件感知：为涉及的品种拉新闻 + 临近事件（失败不阻断）
    news = []
    try:
        cfg = load_config()
        etf_to_inst = {i.options.symbol.upper(): i for i in cfg.instruments.values()
                       if i.options is not None}
        events, _src = _merged_events(getattr(args, "no_live", False), args.no_cache)
        for root in contexts:
            inst = etf_to_inst.get(root)
            if inst is not None:
                news.append(_build_news_digest(inst, events, today))
    except Exception as e:
        print(f"[提示] 事件感知跳过：{str(e)[:80]}", file=sys.stderr)

    # 灵魂档案：用户专属交易体系 + 当前纪律核查（优先于一切建议）
    soul = None
    try:
        from undertow.soul.profile import load_profile, check_against_profile
        prof = load_profile()
        if prof is not None:
            target = pre_trade["review"] if pre_trade else review
            soul = (prof, check_against_profile(target, capital, prof))
    except Exception as e:
        print(f"[提示] 灵魂档案跳过：{str(e)[:80]}", file=sys.stderr)

    # 事前判断：--thesis ID 从 journal 取，供 AI 逐条检验（先独立判读再对照）
    thesis = None
    tid = getattr(args, "thesis", None)
    if tid:
        try:
            from undertow.soul.journal import load_theses
            hit = [t for t in load_theses() if t.id == tid]
            if hit:
                thesis = hit[0]
            else:
                print(f"[提示] 未找到事前判断 id={tid}", file=sys.stderr)
        except Exception as e:
            print(f"[提示] 事前判断读取跳过：{str(e)[:80]}", file=sys.stderr)

    packet = build_consult_packet(
        review=review, health=health, contexts=contexts, capital=capital,
        question=(args.question or ""), mode=mode, pre_trade=pre_trade,
        news=news, soul=soul, thesis=thesis, asof=today)

    if getattr(args, "json", False):
        print(json.dumps(packet, ensure_ascii=False, indent=2))
    else:
        print(packet["prompt"])
    return 0


def cmd_serve(args) -> int:
    """本地只读 HTTP API：把咨询上下文包暴露给其它 AI 接入（localhost，无下单端点）。"""
    from undertow.collect import longbridge_account as lb
    from undertow.analyze.portfolio import review_portfolio
    from undertow.analyze.healthcheck import run_healthcheck
    from undertow.consult.packet import build_consult_packet, _portfolio_brief
    from undertow.consult.server import serve

    def _bundle():
        return _load_account_review(args.no_cache)

    def build_positions():
        b = _bundle()
        if b["review"] is None:
            return {"headline": "账户当前无持仓", "groups": []}
        return _portfolio_brief(b["review"])

    def build_packet(question, pre_trade):
        b = _bundle()
        contexts = b.get("contexts", {})
        capital = b.get("capital")
        today = b["today"]
        review = b["review"]
        health = b.get("health", [])
        mode, pt = "review", None
        if pre_trade:
            mode = "pre_trade"
            pos = _parse_pretrade_spec(pre_trade)
            ptc, _ = _build_contexts(pos, args.no_cache)
            contexts = {**ptc, **contexts}
            pr = review_portfolio(pos, contexts, asof=today, capital=capital)
            pt = {"spec": pre_trade, "review": pr, "health": run_healthcheck(pr, capital)}
        if review is None:
            review = review_portfolio([], contexts, asof=today, capital=capital)
            health = []
        return build_consult_packet(review=review, health=health, contexts=contexts,
                                    capital=capital, question=question or "", mode=mode,
                                    pre_trade=pt, asof=today)

    try:
        httpd = serve(build_packet, build_positions, host=args.host, port=args.port)
    except OSError as e:
        print(f"[启动失败] {e}（端口可能被占用，换 --port）", file=sys.stderr)
        return 2
    url = f"http://{args.host}:{args.port}"
    print(f"undertow 咨询 API 已启动（只读，仅本机）：{url}", file=sys.stderr)
    print(f"  {url}/            端点清单", file=sys.stderr)
    print(f"  {url}/consult?q=这个价差该平还是展期   完整咨询包", file=sys.stderr)
    print(f"  {url}/prompt?q=...  只取 prompt 文本（喂给任意 LLM）", file=sys.stderr)
    print("  Ctrl-C 停止。数字均由确定性引擎算好，接入的 AI 只解读、不臆算。", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        httpd.shutdown()
    return 0


def _save_account_snapshot(lb, positions, assets, today, *, no_cache, lookback_days=120):
    """把当次账户全貌（持仓+资产+近 N 天资金流水+成交）落一份 JSON 到本地私有目录。

    这是"每次评价即攒一份不可再生历史"的习惯（对齐期权快照思路）——CBOE/券商都不
    保证长期回溯，日后做历史成交复盘（进场时点 vs 当时信号、真实费用校准、已实现盈亏）
    要靠这些逐次快照拼起来。全部 gitignore，绝不进公开仓库。
    """
    import dataclasses as _dc
    from datetime import timedelta
    snap_dir = DATA_DIR / "account" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    start = (today - timedelta(days=lookback_days)).isoformat()

    cash_flow, executions = [], []
    try:
        cash_flow = lb.fetch_cash_flow(start=start)
    except lb.LongbridgeUnavailable as e:
        print(f"[提示] 资金流水拉取跳过: {str(e)[:80]}", file=sys.stderr)
    try:
        executions = lb.fetch_executions(start=start)
    except lb.LongbridgeUnavailable as e:
        print(f"[提示] 历史成交拉取跳过: {str(e)[:80]}", file=sys.stderr)

    payload = {
        "asof": today.isoformat(),
        "saved_at": market_today().isoformat(),
        "window_start": start,
        "positions": [_dc.asdict(p) for p in positions],
        "assets": _dc.asdict(assets) if assets is not None else None,
        "cash_flow": cash_flow,
        "executions": executions,
    }
    fn = snap_dir / f"account_{today.isoformat()}.json"
    fn.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"账户数据快照（本地私有，未入 git；供将来历史复盘）→ {fn}"
          f"（持仓 {len(positions)} · 流水 {len(cash_flow)} · 成交 {len(executions)}）")
    return fn


def _to_jsonable(inst, an, signals) -> dict:
    return {
        "instrument": inst.key,
        "display_name": inst.display_name,
        "report_date": an.report_date,
        "prev_date": an.prev_date,
        "open_interest": an.open_interest,
        "open_interest_change": an.open_interest_change,
        "lookback_used": an.lookback_used,
        "bias": net_bias(signals),
        "categories": {n: dataclasses.asdict(c) for n, c in an.categories.items()},
        "signals": [dataclasses.asdict(s) for s in signals],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="undertow", description="期货持仓(COT)情报分析")
    p.add_argument("--no-cache", action="store_true", help="绕过本地缓存强制拉取")
    sub = p.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("analyze", help="分析品种持仓并出报告")
    pa.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pa.add_argument("--lookback", type=int, default=None, help="回看周数（默认读配置）")
    pa.add_argument("--json", action="store_true", help="输出结构化 JSON")
    pa.set_defaults(func=cmd_analyze)

    pg = sub.add_parser("gamma", help="分析期权 Gamma/OI 结构与关键位点")
    pg.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pg.add_argument("--horizon", type=int, default=45, help="近月窗口天数（默认45）")
    pg.add_argument("--json", action="store_true", help="输出结构化 JSON")
    pg.set_defaults(func=cmd_gamma)

    pv = sub.add_parser("vol", help="波动率溢价 VRP 跨周期检验（这个卖方 edge 能否穿越牛熊）")
    pv.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pv.add_argument("--window", type=int, default=21, help="前视已实现波动窗口（默认21个交易日）")
    pv.set_defaults(func=cmd_vol)

    psn = sub.add_parser("snapshot", help="落盘期权链原始快照（攒 flow 所需历史，纳入 git）")
    psn.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    psn.add_argument("--status-file", help="把本次运行的机器可读状态原子写入该 JSON 文件"
                                          "（供定时脚本消费，避免 grep 人读文案）")
    psn.set_defaults(func=cmd_snapshot)

    pf = sub.add_parser("flow", help="期权资金流/持仓异动：单快照异常活跃 + 两日 ΔOI/ΔIV")
    pf.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pf.add_argument("--horizon", type=int, default=60, help="近月窗口天数（默认60）")
    pf.add_argument("--no-snapshot", action="store_true",
                    help="不自动落盘今日快照（仅用已落盘数据分析）")
    pf.add_argument("--json", action="store_true", help="输出结构化 JSON")
    pf.set_defaults(func=cmd_flow)

    pe = sub.add_parser("expiry", help="近周到期阶梯：逐周五/月度独立墙位+买卖方（定到期价差用）")
    pe.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pe.add_argument("--no-snapshot", action="store_true",
                    help="不自动落盘今日快照（仅用已落盘数据分析）")
    pe.set_defaults(func=cmd_expiry)

    pfib = sub.add_parser("fib", help="斐波那契回撤+盈亏比闸门：先看盈亏比、别追、等回调（波段交易纪律落地）")
    pfib.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pfib.add_argument("--lookback", type=int, default=90, help="摆动腿检测回看的交易日数（默认 90）")
    pfib.set_defaults(func=cmd_fib)

    pb = sub.add_parser("backtest", help="回测 COT 信号的历史前瞻收益（校准阈值）")
    pb.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pb.add_argument("--lookback", type=int, default=None, help="COT 回看周数（默认读配置）")
    pb.add_argument("--horizons", type=int, nargs="+", default=[5, 10, 20],
                    help="前瞻交易日（默认 5 10 20 ≈1/2/4周）")
    pb.add_argument("--json", action="store_true", help="输出结构化 JSON")
    pb.set_defaults(func=cmd_backtest)

    pr = sub.add_parser("report", help="综合研判：四层聚合+可视化+情景推演 → HTML 报告")
    pr.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pr.add_argument("--lookback", type=int, default=None, help="COT 回看周数（默认读配置）")
    pr.add_argument("--horizon", type=int, default=45, help="期权近月窗口天数（默认45）")
    pr.add_argument("--no-snapshot", action="store_true", help="不自动落盘今日快照")
    pr.add_argument("--no-live", action="store_true", help="事件雷达不拉实时 feed，仅用手维护锚点")
    pr.add_argument("--json", action="store_true", help="输出 outlook 结构化 JSON")
    pr.add_argument("--status-file", help="机器可读状态 JSON（同 snapshot）")
    pr.add_argument("--as-of", metavar="YYYY-MM-DD",
                    help="回放历史某天的研报（复盘验证用）。产物写 data/reports/replay/，"
                         "不写任何台账。⚠️ 只有期权快照与日线价格能按 as-of 截断；"
                         "COT/宏观/波动率指数/事件日历/4H 仍是当前值，"
                         "带这些层的结论在回放里不可信")
    pr.set_defaults(func=cmd_report)

    pl = sub.add_parser("list", help="列出已配置品种")
    pl.set_defaults(func=cmd_list)

    pac = sub.add_parser("account", help="实盘持仓理论评价：读长桥账户当前持仓，逐笔对 undertow 研判复盘（只读，不下单）")
    pac.add_argument("--no-html", action="store_true", help="只出终端，不写本地 HTML")
    pac.add_argument("--no-save", action="store_true", help="不落账户数据快照（默认每次评价都攒一份供将来历史复盘）")
    pac.set_defaults(func=cmd_account)

    pcs = sub.add_parser("consult", help="咨询：把研判+持仓+体检+你的问题装成上下文包供 AI 给意见（只读）")
    pcs.add_argument("question", nargs="?", default="", help="你的问题，如 '这个价差该平还是展期?'")
    pcs.add_argument("--pre-trade", dest="pre_trade", metavar="SPEC",
                     help="开仓前问诊：拟开仓 spec，如 'SLV260919P60000.US:-4:0.5,SLV260919P58000.US:4:0.25'")
    pcs.add_argument("--thesis", metavar="ID", help="带上你的事前判断（journal 里的 thesis id），让 AI 先独立判读再逐条对照")
    pcs.add_argument("--json", action="store_true", help="打印机器可读的完整咨询包（供其它 AI 接入）")
    pcs.set_defaults(func=cmd_consult)

    psv = sub.add_parser("serve", help="本地只读 HTTP API：把咨询上下文包暴露给其它 AI 接入（localhost）")
    psv.add_argument("--port", type=int, default=8787, help="端口（默认 8787）")
    psv.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1，仅本机）")
    psv.set_defaults(func=cmd_serve)

    psl = sub.add_parser("soul", help="交易灵魂档案：你的交易体系/铁律/弱点；--check 核当前持仓是否破戒")
    psl.add_argument("--init", action="store_true",
                     help="从公开模板 config/soul.template.json 生成本地私有档案（不覆盖已有）")
    psl.add_argument("--check", action="store_true", help="用档案的限额核查当前实盘持仓")
    psl.add_argument("--json", action="store_true", help="输出结构化档案")
    psl.set_defaults(func=cmd_soul)

    pjr = sub.add_parser("journal", help="交易日记：成交明细+复盘+盖棺定论+心情（--capture 自动抓当日成交）")
    pjr.add_argument("--capture", action="store_true", help="从券商抓当日成交与费用，落盘成一条日记")
    pjr.add_argument("--date", metavar="YYYY-MM-DD", help="只看某天")
    pjr.add_argument("--limit", type=int, default=0, help="最多显示几条")
    pjr.add_argument("--theses", action="store_true", help="看【事前判断】记录与命中率（判断对错 vs 交易盈亏分开统计）")
    pjr.set_defaults(func=cmd_journal)

    pev = sub.add_parser("event", help="事件影响捕捉：数据落地前后各捕一次横截面快照并对比（只读）")
    pev.add_argument("label", nargs="?", default="snap", help="标签，如 PCE-before / PCE-after")
    pev.add_argument("instruments", nargs="*", help="品种，留空=全部")
    pev.add_argument("--event", help="事件名，如 'Core PCE'")
    pev.add_argument("--phase", help="before / after / open / close")
    pev.add_argument("--compare", metavar="KEY", help="对比同名的两份快照（如 PCE）")
    pev.set_defaults(func=cmd_event)

    ppl = sub.add_parser("plan", help="计划交易：记录/监控触发与出场条件，输出可照抄的下单参数（只读，绝不下单）")
    ppl.add_argument("--check", action="store_true", help="抓实时价核查触发/接近")
    ppl.add_argument("--notify", action="store_true", help="触发时弹 macOS 通知并落 ALERT 文件")
    ppl.add_argument("--orders", metavar="ID", help="打印该计划的下单参数（供你自己执行）")
    ppl.set_defaults(func=cmd_plan)

    pt = sub.add_parser("tech", help="技术面：短线过热度 + 趋势结构（RSI/KDJ/MACD/布林/均线，确定性）")
    pt.add_argument("instruments", nargs="*", help="品种，留空=全部")
    pt.set_defaults(func=cmd_tech)

    plv = sub.add_parser("live", help="持仓实时体检：长桥实时盘口 → 真实可平仓价（只读）")
    plv.set_defaults(func=cmd_live)

    psig = sub.add_parser("signals",
                          help="强信号台账：重建/回填/统计（这层从未回测过，靠向前累积）")
    psig.add_argument("instruments", nargs="*", help="品种键（留空=全部有期权源的）")
    psig.add_argument("--rebuild", action="store_true",
                      help="用已落盘的历史快照回放重建台账（同日同方向覆盖）")
    psig.add_argument("--backfill", action="store_true",
                      help="用真实价格序列回填前瞻收益/漂移/牛熊制度")
    psig.add_argument("--horizon", type=int, default=5, help="统计用前瞻天数（默认 5）")
    psig.add_argument("--no-cache", action="store_true", help="回填时不用本地缓存")
    psig.set_defaults(func=cmd_signals)

    pbs = sub.add_parser("backtest-stretch",
                         help="重跑拉伸度(超买超卖)校准表：长历史+regime分层+不重叠t检验")
    pbs.add_argument("symbols", nargs="*",
                     help="Yahoo 符号（留空=默认面板 GLD/SLV/USO/QQQ/SPY）")
    pbs.add_argument("--horizon", type=int, default=5, help="校准表用的前瞻天数（默认 5）")
    pbs.add_argument("--horizons", type=int, nargs="+", default=[2, 3, 5, 10],
                     help="持续性剖面的前瞻天数（默认 2 3 5 10）")
    pbs.add_argument("--mode", choices=["combo", "stretch", "drawdown"], default="combo",
                     help="信号口径：combo=两维分位均值(默认) / stretch=只用偏离度 / "
                          "drawdown=只用回撤度")
    pbs.add_argument("--compare", action="store_true",
                     help="额外跑三种口径的横向对照（慢一些）")
    pbs.add_argument("--emit", action="store_true",
                     help="额外输出可粘回 stretch.py 的 CALIB 字面量")
    pbs.set_defaults(func=cmd_backtest_stretch)

    pn = sub.add_parser("news", help="事件感知：品种相关新闻 + 临近关键事件（高影响临近置顶告警，只读）")
    pn.add_argument("instruments", nargs="*", help="品种，留空=全部")
    pn.add_argument("--no-live", action="store_true", help="事件仅用手维护锚点，不拉实时 feed")
    pn.set_defaults(func=cmd_news)

    pc = sub.add_parser("calendar", help="事件雷达：未来关键节点（FOMC/数据/COT/到期）+ 实时预测")
    pc.add_argument("instruments", nargs="*", help="品种 key（留空=全部/全局事件）")
    pc.add_argument("--within", type=int, default=21, help="未来天数窗口（默认21）")
    pc.add_argument("--no-live", action="store_true", help="不拉实时 feed，仅用手维护锚点")
    pc.set_defaults(func=cmd_calendar)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())


#: 墙位历史图的**显示**裁剪半径。structural_walls 本身不设距离上限
#: （它按近端到期占比认墙，见 gamma 文件头），但图上要是画一条距现价 +73% 的
#: SLV call 100，Y 轴会被拉爆、其余全部压成一条线。这里只裁显示，不改墙定义 ——
#: 被裁掉的墙仍然存在于结构分析里，只是不进这张图。
WALL_HIST_MAX_DIST = 0.25


def _wall_history_rows(inst, sym: str, upto: date, *, days: int = 60) -> list[dict]:
    """构建墙位历史图的数据（用户 2026-08-31 要求，2026-09-02 接线）。

    每行 = 一个【可交易日】：日 K + 三道结构墙 + 当日信号标记。

    三个口径都走已定版的那一套，不另立：
      · 可交易日由 captured_at 推导（clock.decision_session），不是文件名；
      · 墙用 gamma.structural_walls（全范围 + 近端到期占比门槛），不是 band 内最大；
      · 信号的"开火"取自台账 fired 字段，与报告告警同源 —— 压力比 ≥10× 但
        未开火的只画空心标记，因为它们从没在报告里弹过告警。
    """
    from datetime import datetime as _dt
    from undertow.analyze.gamma import structural_walls
    from undertow.collect.longbridge_kline import fetch_bars
    from undertow.core.clock import decision_session

    try:
        bars = fetch_bars(f"{sym}.US", period="day", count=260)
    except Exception:
        return []
    ohlc = {str(b["ts"])[:10]: b for b in bars}
    tdays = [_dt.strptime(x, "%Y-%m-%d").date() for x in sorted(ohlc)]
    if not tdays:
        return []

    # 台账里的信号（fired 与压力比），按可交易日索引
    sig_by_day: dict[str, list] = {}
    try:
        from undertow.analyze import signal_ledger as sl
        for r in sl.load_all([inst.key]):
            br = r.get("bear_pressure_ratio") or 0
            bl = r.get("bull_pressure_ratio") or 0
            ratio = max(br, bl)
            if ratio < 10:
                continue
            side = "看跌" if br >= bl else "看涨"
            fired = bool(r.get("fired")) and r.get("direction") == side
            sig_by_day[r["date"]] = [side, round(ratio, 1), fired]
    except Exception:
        pass

    st = SnapshotStore()
    # 同一 decision_session 取 captured_at 最新的那份（codex 2026-09-02 P0）
    cand: dict[date, tuple[float, date]] = {}
    for fd in st.dates("options", sym):
        sess = st.decision_session("options", sym, fd, tdays)
        if sess is None or sess > upto:
            continue
        ca = st.captured_at("options", sym, fd) or 0.0
        if sess not in cand or ca > cand[sess][0]:
            cand[sess] = (ca, fd)

    rows = []
    for sess in sorted(cand)[-days:]:
        _, fd = cand[sess]
        k = sess.isoformat()
        bar = ohlc.get(k)
        if not bar:
            continue
        payload = st.load("options", sym, fd)
        if payload is None:
            continue
        try:
            snap = snapshot_from_payload(payload, inst.key, sym)
        except Exception:
            continue
        spot = float(bar["close"])
        def _near(ws):
            return [[w["strike"], w["oi"]] for w in ws
                    if abs(w["dist_pct"]) <= WALL_HIST_MAX_DIST * 100][:3]
        wp = structural_walls(snap, sess, spot, "P", top_n=8)
        wc = structural_walls(snap, sess, spot, "C", top_n=8)
        rows.append({
            "date": k, "o": float(bar["open"]), "h": float(bar["high"]),
            "l": float(bar["low"]), "c": spot,
            "topP": _near(wp), "topC": _near(wc),
            "sig": sig_by_day.get(k),
        })
    return rows
