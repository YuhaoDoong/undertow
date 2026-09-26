"""VRP 研究的纯计算工具；描述样本与不重叠推断样本分别汇报。

IV − 实现波动只是波动率点差，不是信用价差损益。不重叠只消除共享的
日收益，不能保证跨期独立；这里不输出显著性或策略启用结论。
"""
from __future__ import annotations

import math
import statistics as st
from datetime import date

from undertow.analyze.vrp_history import forward_realized_vol


def describe(vals):
    """全样本描述；小样本 p5 是顺序统计量，不是尾部损失概率。"""
    if not vals:
        return None
    s = sorted(vals)
    return {"n": len(vals), "mean": st.mean(vals), "median": st.median(vals),
            "pos": sum(v > 0 for v in vals) / len(vals),
            "p5": s[int(0.05 * (len(s) - 1))],
            "p95": s[int(0.95 * (len(s) - 1))]}


def one_sample_t(xs):
    """至少三条且方差非零才返回 t；不可估计不是 t=0。"""
    if len(xs) < 3:
        return None
    sd = st.stdev(xs)
    return st.mean(xs) / (sd / math.sqrt(len(xs))) if sd > 0 else None


def welch(a, b):
    """Welch t（不替代自由度、p 值或多重检验校正）。"""
    if len(a) < 3 or len(b) < 3:
        return None
    den = math.sqrt(st.variance(a) / len(a) + st.variance(b) / len(b))
    return (st.mean(a) - st.mean(b)) / den if den > 0 else None


def compare_samples(selected, rest):
    """同一组样本同时产生均值差、两侧 n 和检验统计量。"""
    return {"nonoverlap": describe(selected), "rest_nonoverlap": describe(rest),
            "mean_diff_nonoverlap": st.mean(selected) - st.mean(rest)
            if selected and rest else None,
            "welch_vs_rest": welch(selected, rest)}


def realized_expiry_window(dates, closes, session, expiry):
    """返回 T−1 收盘至到期收盘的已成熟窗口及审计字段。

    只接受日线中确实存在的到期日，不猜测缺行是休市还是数据遗漏。
    日线序列应为已完成的收盘数据；缺少交易所日历时无法认证中间缺行。
    使用与长历史一致的总体标准差定义（短窗去均值可能低估单向移动风险）。
    """
    if len(dates) != len(closes) or dates != sorted(set(dates)):
        raise ValueError("日线日期须唯一、升序且与收盘价一一对应")
    if not dates or session not in dates:
        return None, "missing_decision_session"
    i = dates.index(session)
    if i == 0:
        return None, "missing_base_close"
    if expiry > dates[-1]:
        return None, "unmatured_expiry"
    if expiry not in dates:
        return None, "missing_expiry_close"
    end = dates.index(expiry)
    n = end - i + 1
    if n < 2:
        return None, "insufficient_returns"
    ds, cs = dates[i - 1:end + 1], closes[i - 1:end + 1]
    if any(not math.isfinite(v) or v <= 0 for v in cs):
        return None, "invalid_close"
    rv = forward_realized_vol(ds, cs, n)[ds[0]]
    return {"base_date": ds[0].isoformat(), "decision_price": cs[0],
            "return_start": session.isoformat(), "return_end": expiry.isoformat(),
            "return_dates": [d.isoformat() for d in ds[1:]],
            "n_returns": n, "rv": rv}, None


def nonoverlapping_rows(rows):
    """按决策时间依次保留，下一条收益起日必须晚于上条到期日。

    入选仅取决于日期，不看 IV、RV 或收益；两个区间可以共享边界收盘价，
    但不能共享任意一天的收盘到收盘收益。
    """
    selected, last_end = [], None
    for row in sorted(rows, key=lambda r: (r["return_start"], r["return_end"])):
        start = date.fromisoformat(row["return_start"])
        end = date.fromisoformat(row["return_end"])
        if end < start:
            raise ValueError("收益窗口终点早于起点")
        if last_end is None or start > last_end:
            selected.append(row)
            last_end = end
    return selected


def short_summary(rows):
    sub = nonoverlapping_rows(rows)
    vals = [r["vrp"] for r in sub]
    return {"all": describe([r["vrp"] for r in rows]),
            "nonoverlap": describe(vals), "n_nonoverlap": len(vals),
            "t_nonoverlap": one_sample_t(vals),
            "nonoverlap_sessions": [r["T"] for r in sub]}
