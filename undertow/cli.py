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
import json
import sys
from datetime import date

from undertow.core.config import load_config, DATA_DIR
from undertow.core.clock import market_today
from undertow.core.calendar import load_events, upcoming, merge as merge_events, CATEGORY_LABEL
from undertow.collect.store import SnapshotStore
from undertow.collect.faireconomy_cal import FairEconomyCalSource
from undertow.collect.cftc_cot import CftcCotSource
from undertow.collect.cboe_options import CboeOptionsSource, snapshot_from_payload, chain_fingerprint
from undertow.collect.cboe_history import CboeHistorySource
from undertow.collect.yahoo_futures import YahooFuturesSource
from undertow.collect.fred_macro import FredMacroSource
from undertow.collect.cboe_vol import CboeVolSource
from undertow.analyze.positioning import analyze
from undertow.analyze.signals import generate_signals, net_bias
from undertow.analyze.gamma import analyze_gamma, structure_delta
from undertow.analyze.flow import (analyze_flow, counter_signals,
                                   flip_driver_summary, structural_moves)
from undertow.analyze.outlook import (build_outlook, macro_to_votes,
                                      plain_summary_blocks)
from undertow.analyze.strategy import build_strategy
from undertow.analyze.macro import analyze_macro, series_ids_for
from undertow.analyze.backtest import run_backtest
from undertow.report import markdown as report_mod
from undertow.report import viz
from undertow.report.html import (render_report_html, render_index_html,
                          render_flow_section, render_macro_section, render_events_section,
                          render_tldr_section, render_strategy_section,
                          render_concentration_html)


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
                multiplier=inst.options.approx_commodity_multiplier,
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
    for inst in instruments:
        if inst.options is None:
            print(f"[跳过] {inst.key} 未配置期权数据源", file=sys.stderr)
            continue
        sym = inst.options.symbol
        try:
            payload = source.fetch_raw(inst, use_cache=not args.no_cache)
            already = store.load("options", sym, today) is not None
            path, skipped = _save_snapshot_dedup(store, inst, sym, payload, today)
            if skipped and not already:
                print(f"[提示] {inst.key} 期权数据与上一交易日逐行相同（休市重复），跳过落盘",
                      file=sys.stderr)
                continue
            snap = snapshot_from_payload(payload, inst.key, sym)
            n_oi = len(snap.with_oi())
            n_dates = len(store.dates("options", sym))
            saved.append((inst, sym, path, len(snap.contracts), n_oi, n_dates))
        except Exception as e:
            print(f"[警告] {inst.key} 快照失败: {e}", file=sys.stderr)

    if not saved:
        print("没有保存任何快照。", file=sys.stderr)
        return 1

    print(f"已落盘 {today} 期权链快照（原始全字段，纳入 git 永久留存）:")
    for inst, sym, path, n_all, n_oi, n_dates in saved:
        print(f"  {inst.key:7s} {sym}: {n_all:,} 合约（{n_oi:,} 有OI）  "
              f"→ 已累计 {n_dates} 天  ·  {path}")
    if any(nd < 2 for *_, nd in saved):
        print("\n提示：日对日 ΔOI/ΔIV 异动需要 ≥2 天快照。明天再跑一次 snapshot，"
              "之后 `flow` 即可出作者那种「近月大单异动」。")
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

            stored = store.latest_two("options", sym)
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

            # 拿静态墙位叠加
            ga = analyze_gamma(curr, multiplier=inst.options.approx_commodity_multiplier,
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


def _save_snapshot_dedup(store, inst, sym, payload, today):
    """落盘今日期权快照，但若内容与上一份完全相同则跳过（休市/数据未刷新的重复）。
    返回 (path|None, skipped_bool)。跳过可避免 flow 层日对日 diff 退化成全 0。"""
    try:
        fp = chain_fingerprint(snapshot_from_payload(payload, inst.key, sym))
        latest = store.latest("options", sym)
        if latest is not None:
            ld, lpayload = latest
            if ld != today and lpayload is not None:
                lfp = chain_fingerprint(snapshot_from_payload(lpayload, inst.key, sym))
                if lfp == fp:
                    return None, True   # 与上一交易日逐行相同 → 休市重复，不落盘
    except Exception:
        pass  # 指纹失败不应阻断落盘（宁可多存）
    return store.save("options", sym, payload, on_date=today), False


def _load_curr_prev_snapshot(store, source, inst, today, *, no_cache, no_snapshot):
    """取当前+上一份期权快照。今日未落盘则按需落盘（除非 --no-snapshot）。
    返回 (curr_snap, prev_snap|None, prev_date|None, curr_date_str)。"""
    sym = inst.options.symbol
    if not no_snapshot and store.load("options", sym, today) is None:
        payload = source.fetch_raw(inst, use_cache=not no_cache)
        _, skipped = _save_snapshot_dedup(store, inst, sym, payload, today)
        if skipped:
            print(f"[提示] {inst.key} 期权数据与上一交易日逐行相同（休市重复），跳过落盘",
                  file=sys.stderr)
    stored = store.latest_two("options", sym)
    if stored:
        curr_d, curr_payload = stored[-1]
        curr = snapshot_from_payload(curr_payload, inst.key, sym)
        prev, prev_date = None, None
        if len(stored) == 2:
            prev_d, prev_payload = stored[0]
            prev = snapshot_from_payload(prev_payload, inst.key, sym)
            prev_date = prev_d.isoformat()
        return curr, prev, prev_date, curr_d.isoformat()
    payload = source.fetch_raw(inst, use_cache=not no_cache)
    return snapshot_from_payload(payload, inst.key, sym), None, None, today.isoformat()


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
    backup = path.with_name(f"{path.stem}_r{stamp}{path.suffix}")
    n = 1
    while backup.exists():   # 同一分钟内多次生成 → 追加序号
        backup = path.with_name(f"{path.stem}_r{stamp}-{n}{path.suffix}")
        n += 1
    path.rename(backup)


def cmd_report(args) -> int:
    """综合研判报告：四层情报聚合 + 可视化 + 情景推演 → 自包含 HTML。"""
    cfg = load_config()
    lookback = args.lookback or cfg.lookback_weeks
    cot_src, opt_src, px_src = CftcCotSource(), CboeOptionsSource(), CboeHistorySource()
    fut_src = YahooFuturesSource()
    fred_src = FredMacroSource()
    vol_src = CboeVolSource()
    store = SnapshotStore()
    today = market_today()
    all_events, _ = _merged_events(getattr(args, "no_live", False), args.no_cache)
    reports_dir = DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    try:
        instruments = _resolve_instruments(cfg, args.instruments)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 2

    written = []  # (inst, outlook, filename)
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
                store, opt_src, inst, today, no_cache=args.no_cache, no_snapshot=args.no_snapshot)

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
            mult = ratio if ratio is not None else inst.options.approx_commodity_multiplier

            ga = analyze_gamma(curr, multiplier=mult,
                               proxy_quality=inst.options.proxy_quality, today=today,
                               horizon_days=args.horizon)
            fa = analyze_flow(prev, curr, today=today, horizon_days=args.horizon,
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

            flow_html = render_flow_section(fa)
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
                        t = max((dd for dd in closes_m if dd < snap_d), default=None)
                        if t is None or h_snap.spot <= 0:
                            continue
                        g_h = analyze_gamma(h_snap, multiplier=closes_m[t] / h_snap.spot,
                                            proxy_quality=inst.options.proxy_quality,
                                            today=snap_d, horizon_days=args.horizon)
                        r_h = closes_m[t] / h_snap.spot
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
            timeline_svg = viz.strategy_timeline_svg(timeline_rows, real_price) \
                if len(timeline_rows) >= 2 else ""
            # —— 策略情景参数化（期货）先算：其否决票 = 现成的对手盘证据 ——
            plan = build_strategy(outlook, vol=fa.vol, series=real_series,
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
            if prev is not None:
                try:
                    # 昨日结构必须用昨日的日期锚定（到期时间权重随 today 变，
                    # 用今天的日期算昨日链会把零伽马算歪）
                    prev_d = date.fromisoformat(prev_date) if prev_date else today
                    ga_prev = analyze_gamma(prev, multiplier=mult,
                                            proxy_quality=inst.options.proxy_quality,
                                            today=prev_d, horizon_days=args.horizon)
                    struct_notes = structure_delta(ga_prev, ga)
                    driver = flip_driver_summary(fa)
                    if driver:
                        struct_notes.append(driver)
                except Exception:
                    pass
            trend = _score_trend(inst.key, today.isoformat(), outlook.bias_score)
            tldr_html = render_tldr_section(plain_summary_blocks(
                outlook, day_chg_pct=day_chg, vol_verdict=vv,
                flow_tilt=tilt, flow_moves=moves, counter_notes=counters,
                bias_trend=trend, struct_notes=struct_notes))
            html = render_report_html(outlook, price_svg, oi_svg, cot_svg,
                                      flow_html, macro_html, events_html, tldr_html,
                                      strategy_html,
                                      conc_html=render_concentration_html(an.concentration))
            fn = f"{inst.key}_{today.isoformat()}.html"
            _archive_existing(reports_dir / fn)
            (reports_dir / fn).write_text(html, encoding="utf-8")
            written.append((inst, outlook, fn))
        except Exception as e:
            print(f"[警告] {inst.key} 研判报告失败: {e}", file=sys.stderr)

    if not written:
        print("没有生成任何报告。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([dataclasses.asdict(o) | {"instrument": inst.key} for inst, o, _ in written],
                         ensure_ascii=False, indent=2, default=str))
        return 0

    index_path = None
    if len(written) > 1:
        idx_items = [(o.display_name, fn, o.bias, o.confidence) for _, o, fn in written]
        index_html = render_index_html(idx_items, today.isoformat())
        index_path = reports_dir / f"index_{today.isoformat()}.html"
        _archive_existing(index_path)
        index_path.write_text(index_html, encoding="utf-8")

    print(f"已生成综合研判报告（{today}）:")
    for inst, o, fn in written:
        print(f"  {inst.key:7s} {o.bias:8s}(可信度{o.confidence})  → {reports_dir / fn}")
    if index_path:
        print(f"  索引页 → {index_path}")
    print(f"\n用浏览器打开即可（macOS: open '{reports_dir / written[0][2]}'）")
    return 0


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

    psn = sub.add_parser("snapshot", help="落盘期权链原始快照（攒 flow 所需历史，纳入 git）")
    psn.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    psn.set_defaults(func=cmd_snapshot)

    pf = sub.add_parser("flow", help="期权资金流/持仓异动：单快照异常活跃 + 两日 ΔOI/ΔIV")
    pf.add_argument("instruments", nargs="*", help="品种 key（留空=全部）")
    pf.add_argument("--horizon", type=int, default=60, help="近月窗口天数（默认60）")
    pf.add_argument("--no-snapshot", action="store_true",
                    help="不自动落盘今日快照（仅用已落盘数据分析）")
    pf.add_argument("--json", action="store_true", help="输出结构化 JSON")
    pf.set_defaults(func=cmd_flow)

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
    pr.set_defaults(func=cmd_report)

    pl = sub.add_parser("list", help="列出已配置品种")
    pl.set_defaults(func=cmd_list)

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
