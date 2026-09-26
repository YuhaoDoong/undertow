"""卖方价差推荐台账 —— 逐日记录研报给出的候选，事后回填结果。

用户 2026-09-02：「记录每次研报里的推荐，收集数据」。

为什么要有：策略的三步规则是在 43 个可交易日的历史上定出来的，
样本期 SLV +10.6%（含末段 −7.7% 的下跌）。要判断它在真实行情里成不成立，
必须有【事前记录、事后回填】的前瞻台账 —— 回测再怎么做都是事后的。

与 signal_ledger 同构：
  record()   每天研报生成时调用，把当日候选（或"无候选"及其原因）落盘
  backfill() 事后用真实收盘价回填：到期收盘、是否破卖腿、假设持有到期的模型损益
  summarize() 分别统计破卖腿与模型盈亏；候选条数不等于独立样本数

⚠️ 记录的是【推荐】，不是【成交】。用户是否下单、以什么价成交，
   要以券商成交回报为准（那属于 journal 模块）。这里只回答一个问题：
   "按候选报价假设成交并持有到期，模型结果会怎样"。
   它不验证逐日破卖腿退出规则，也不是实盘损益。
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import pathlib
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone

DIR = pathlib.Path("data/history/wall_spread")


def _path(inst: str) -> pathlib.Path:
    return DIR / f"{inst}.jsonl"


class LedgerCorruptError(ValueError):
    """原始台账已保留，另存隔离副本；禁止把损坏历史当成空表继续写。"""


class LedgerConflictError(ValueError):
    """同日首份事前记录已冻结；新输入与原记录不同，不能静默改写。"""


@contextmanager
def _locked_path(inst: str, root: pathlib.Path | None):
    """锁独立于被原子替换的数据 inode，涵盖整个读—改—写事务。"""
    path = (root or DIR) / f"{inst}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".jsonl.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _iso_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _validate_rows(rows: list[dict], inst: str) -> None:
    """接受无 context/recorded_at 的旧行，但不容忍缺少计算必需字段。"""
    def check_finite(value):
        if isinstance(value, dict):
            for child in value.values():
                check_finite(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                check_finite(child)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("台账含非有限数值")

    check_finite(rows)
    seen = set()
    for line_no, row in enumerate(rows, 1):
        def require(ok, reason):
            if not ok:
                raise ValueError(f"第 {line_no} 条记录：{reason}")

        require(isinstance(row, dict), "记录必须是对象")
        require(_iso_date(row.get("date")), "date 缺失或不是 ISO 日期")
        require(row["date"] not in seen, "同一决策日重复记录")
        seen.add(row["date"])
        require(row.get("inst") == inst, "inst 与文件品种不一致")
        require(isinstance(row.get("sym"), str), "sym 缺失或不是字符串")
        require(_finite_number(row.get("spot")), "spot 缺失或不是有限数值")
        require(type(row.get("ok")) is bool, "ok 缺失或不是布尔值")
        require(isinstance(row.get("reason"), str), "reason 缺失或不是字符串")
        require(isinstance(row.get("params"), dict), "params 缺失或不是对象")
        require(isinstance(row.get("context", {}), dict), "context 必须是对象")
        require(isinstance(row.get("candidates"), list), "candidates 缺失或不是列表")
        for c in row["candidates"]:
            require(isinstance(c, dict), "候选必须是对象")
            require(c.get("kind") in ("P", "C"), "候选 kind 无效")
            require(_iso_date(c.get("expiry")), "候选 expiry 缺失或无效")
            for key in ("sell", "buy", "credit", "width"):
                require(_finite_number(c.get(key)), f"候选 {key} 缺失或不是有限数值")
            require(c["width"] > 0, "候选 width 必须为正")
            for key in ("settle", "pnl"):
                require(c.get(key) is None or _finite_number(c[key]),
                        f"候选 {key} 不是有限数值或 null")
            require(c.get("broke") is None or type(c["broke"]) is bool,
                    "候选 broke 不是布尔值或 null")
            if c.get("pnl") is not None:
                require(c.get("settle") is not None and c.get("broke") is not None,
                        "已回填损益缺少 settle/broke")


def _decode_rows(raw: bytes, inst: str) -> list[dict]:
    def reject_constant(value):
        raise ValueError(f"非有限 JSON 数值 {value}")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"重复 JSON 字段 {key}")
            obj[key] = value
        return obj

    rows = [json.loads(line, parse_constant=reject_constant,
                       object_pairs_hook=unique_object)
            for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("已有台账为空，可能被截断；不能当作未建账")
    _validate_rows(rows, inst)
    return rows


def _load_path(path: pathlib.Path, inst: str) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    try:
        return _decode_rows(raw, inst)
    except (ValueError, UnicodeError) as exc:
        # 保留原文件以阻止下一次唤醒误把台账当作空表；副本只供人工恢复。
        fd, backup = tempfile.mkstemp(prefix=path.name + ".corrupt-", dir=path.parent)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        raise LedgerCorruptError(
            f"{path} 损坏：{exc}；原文件未改，隔离副本 {backup}；本次操作中止"
        ) from exc


def _atomic_write(path: pathlib.Path, rows: list[dict], inst: str) -> None:
    """在同目录完整写入并回读验证后才提交，任何提交前失败都保留原文件。"""
    _validate_rows(rows, inst)
    raw = ("\n".join(json.dumps(row, ensure_ascii=False, allow_nan=False)
                     for row in rows) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                                dir=path.parent)
    tmp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        reread = tmp.read_bytes()
        if reread != raw or _decode_rows(reread, inst) != rows:
            raise ValueError(f"{path} 临时文件回读验证失败；原文件未改")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _decision_fields(row: dict) -> dict:
    """回填结果和记录时刻不属于事前输入；旧 schema 的缺省 context 等同空对象。"""
    out = {key: value for key, value in row.items()
           if key not in ("recorded_at", "candidates")}
    out.setdefault("context", {})
    out["candidates"] = [{key: value for key, value in c.items()
                          if key not in ("settle", "broke", "pnl")}
                         for c in row["candidates"]]
    return out


def decision_context(highs, lows, closes, dates, session: date) -> dict:
    """决策日的波动率状态 —— 只记录，不过滤（用户 2026-09-25：「先不带入期权实盘」）。

    第四步在 15 品种 20 年日线上测出：唯一跨簇复现的破墙提示是 **ATR 短回看扩张**
    （5% 口径约 2×，ATR 口径仍 >1）；ATR 分位在 % 口径有效但 ATR 口径反号（缩放效应）；
    布林带宽扩张无效。它们都没过 Bonferroni，所以不能进过滤器 —— 但要攒前瞻样本，
    就得从今天起把决策日的读数记下来。

    ⚠️ 只用 date < session 的 bar：决策日是可交易日的上一交易日，含 session 当天就是前视。
    """
    from undertow.analyze.stretch import _atr_series
    from undertow.analyze.stretch_backtest import _pct_rank_series
    from undertow.analyze.technicals import bb_width_series
    idx = [i for i, d in enumerate(dates) if d < session]
    if len(idx) < 30:
        return {"asof": None, "note": "日线不足 30 根，无法算上下文"}
    n = idx[-1] + 1
    h, l, c = list(highs[:n]), list(lows[:n]), list(closes[:n])
    atr = _atr_series(h, l, c, 14)
    bw = bb_width_series(c, 20, 2.0)
    pct = _pct_rank_series(atr)
    def _ratio(xs, k=5):
        a, b = xs[-1], xs[-1 - k] if len(xs) > k else None
        return round(a / b, 4) if a and b else None
    def _r(x, nd=4):
        return round(x, nd) if x is not None else None
    return {"asof": dates[n - 1].isoformat(), "close_prev": _r(c[-1]),
            "atr14": _r(atr[-1]), "atr_expand_5": _ratio(atr),
            "atr_pct_250": _r(pct[-1]),
            "bb_width_20": _r(bw[-1]), "bb_expand_5": _ratio(bw)}


def record(inst: str, sym: str, session: date, spot: float, verdict,
           *, root: pathlib.Path | None = None, context: dict | None = None) -> pathlib.Path:
    """冻结某品种某可交易日的首份推荐。相同输入幂等，变化必须显式解决。

    spot 必须是决策价（C[可交易日前一交易日] 收盘），与回测口径一致。
    context 是决策日的波动率状态与近墙（decision_context + gamma.local_wall），
    只记录不参与判定；旧行没有这个字段，load/backfill 照常。
    """
    row = {
        "date": session.isoformat(), "inst": inst, "sym": sym,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "spot": round(float(spot), 4),
        "ok": bool(verdict.ok), "reason": verdict.reason,
        "context": dict(context or {}),
        "params": {k: (list(v) if isinstance(v, tuple) else v)
                   for k, v in (verdict.params or {}).items()},
        "candidates": [
            {"kind": c.kind, "expiry": c.expiry.isoformat(), "dte": c.dte,
             "sell": c.sell, "buy": c.buy, "wall": c.wall, "offset": c.offset,
             "width_n": c.width_n, "wall_rule": c.wall_rule,
             "credit": round(c.credit, 2), "width": round(c.width, 2),
             "occ": round(c.occupancy, 2), "buf_pct": round(c.buffer_pct, 3),
             "net_credit": round(c.net_credit, 2),
             "breakeven_rate": round(c.breakeven_rate, 4),
             # 事后回填
             "settle": None, "broke": None, "pnl": None}
            for c in verdict.all],
    }
    _validate_rows([row], inst)
    with _locked_path(inst, root) as path:
        rows = _load_path(path, inst)
        for old in rows:
            if old["date"] == row["date"]:
                if _decision_fields(old) != _decision_fields(row):
                    raise LedgerConflictError(
                        f"{inst} {row['date']} 首份候选已冻结；新输入与原记录冲突，"
                        "原候选及回填结果未改；需人工核对，禁止事后改写前瞻样本"
                    )
                return path
        rows.append(row)
        rows.sort(key=lambda r: r["date"])
        _atomic_write(path, rows, inst)
        return path


def load(inst: str, *, root: pathlib.Path | None = None) -> list[dict]:
    """严格读取；损坏时保留原件和隔离副本并抛错，不跳过任何记录。"""
    with _locked_path(inst, root) as path:
        return _load_path(path, inst)


def backfill(inst: str, closes: dict[str, float],
             *, root: pathlib.Path | None = None) -> tuple[int, int]:
    """用收盘价回填每个候选的到期模型结果。返回 (回填条数, 仍待填条数)。

    破卖腿 = 到期日收盘越过卖腿，不等于持有期内曾经越过或净亏损。
    损益 = 候选权利金 − 内在价值 − 手续费；不是实际成交损益，
    也不回测逐日破卖腿提前平仓。调用方必须提供已完成的日线收盘价。
    """
    from undertow.analyze.wall_spread import FEE_PER_TRADE
    with _locked_path(inst, root) as path:
        rows = _load_path(path, inst)
        filled = pending = 0
        for r in rows:
            for c in r["candidates"]:
                if c.get("pnl") is not None:
                    continue
                se = closes.get(c["expiry"])
                if se is None:
                    pending += 1
                    continue
                if not _finite_number(se) or se < 0:
                    raise ValueError(f"{inst} {c['expiry']} 到期收盘无效；台账未改")
                w = c["width"] / 100.0
                itr = (max(0.0, min(c["sell"] - se, w)) if c["kind"] == "P"
                       else max(0.0, min(se - c["sell"], w))) * 100
                c["settle"] = round(se, 4)
                c["broke"] = bool(se < c["sell"]) if c["kind"] == "P" else bool(se > c["sell"])
                c["pnl"] = round(c["credit"] - itr - FEE_PER_TRADE, 2)
                filled += 1
        if filled:
            _atomic_write(path, rows, inst)
        return filled, pending


def summarize(inst: str, *, root: pathlib.Path | None = None) -> dict:
    """候选到期模型统计：分开盈亏与破腿，并披露日期数和未回填数。"""
    rows = load(inst, root=root)
    days = len({r["date"] for r in rows})
    with_cand = sum(1 for r in rows if r["candidates"])
    done = [c for r in rows for c in r.get("candidates", [])
            if c.get("pnl") is not None]
    settled_days = {r["date"] for r in rows
                    if any(c.get("pnl") is not None for c in r["candidates"])}
    pending = sum(c.get("pnl") is None for r in rows for c in r["candidates"])
    broke = sum(1 for c in done if c.get("broke"))
    return {
        "basis": "hypothetical_candidates_hold_to_expiry",
        "note": ("候选报价假设成交、持有到期的模型损益，非实盘；不含逐日破腿退出。"
                 "同日多候选不是独立样本；不同决策日的持有期也可能重叠。"
                 + ("尚无已回填的候选。" if not done else "")),
        "days": days, "days_with_candidate": with_cand,
        "coverage": round(with_cand / days, 3) if days else 0.0,
        "settled_decision_dates": len(settled_days), "pending": pending,
        "complete": pending == 0,
        "settled": len(done), "broke": broke,
        "break_rate": round(broke / len(done), 4) if done else None,
        "wins": sum(c["pnl"] > 0 for c in done),
        "losses": sum(c["pnl"] < 0 for c in done),
        "flat": sum(c["pnl"] == 0 for c in done),
        "win_rate": round(sum(c["pnl"] > 0 for c in done) / len(done), 4) if done else None,
        "total_pnl": round(sum(c["pnl"] for c in done), 2) if done else None,
        "avg_pnl": round(sum(c["pnl"] for c in done) / len(done), 2) if done else None,
        "put": sum(1 for c in done if c["kind"] == "P"),
        "call": sum(1 for c in done if c["kind"] == "C"),
    }
