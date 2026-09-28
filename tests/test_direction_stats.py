"""方向族统计合同（Codex 020-01/03 与 conviction 分层边界）：只用合成数据验证，不碰市场结果。"""
import random
from datetime import date

import pytest

from undertow.analyze import direction_stats as ds


def test_lower_bound_uses_alpha_quantile_not_upper():
    vals = sorted(-0.02 + 0.04 * i / 9999 for i in range(10000))            # 对称于 0
    lo = ds.quantile(vals, ds.ALPHA)
    hi = ds.quantile(vals, 1 - ds.ALPHA)
    assert lo < 0 < hi and lo == pytest.approx(-0.0197, abs=2e-4)          # 020-01：下界在低端
    assert ds.quantile([1.0, 2.0, 3.0], 0.34) == 2.0 and ds.quantile([1.0], 0.001) == 1.0


def test_regime_boundaries():
    assert ds.regime([1.0] * 199) is None                                    # 不足 200 日
    assert ds.regime([1.0] * 200) == "up"                                    # 恰好等于均线 → up
    assert ds.regime([2.0] * 199 + [1.0]) == "down"
    assert ds.regime([1.0] * 199 + [None]) is None                           # 有缺失 → 不入分析
    assert ds.quarter(date(2026, 9, 30)) == "2026Q3" and ds.quarter(date(2026, 10, 1)) == "2026Q4"


def _row(inst, t, s, r, reg="up", q="2026Q4", eligible=True):
    return {"inst": inst, "t": t, "s": s, "r": r, "regime": reg, "quarter": q, "eligible": eligible}


def test_nonoverlap_fixed_on_original_axis():
    rows = [_row("gold", t, 1, 0.01) for t in (0, 2, 5, 9)]
    kept = [r["t"] for r in ds.nonoverlap(rows, 5) if r["kept"]]
    assert kept == [0, 5]


def test_d_reg_support_exclusion_and_count():
    base = [_row("gold", t, 0, 0.0) for t in range(10, 16)]                  # 6 个非事件日 → 有支持
    ev = [_row("gold", 0, 1, 0.02), _row("gold", 20, -1, -0.02)]
    lonely = [_row("silver", 0, 1, 0.05), _row("silver", 1, 0, 0.0)]        # 只有 1 个非事件日 → 无支持
    rows = ds.nonoverlap(base + ev + lonely, 5)
    d, diag = ds.d_reg(rows)
    mu = (0.02 - 0.02) / 8
    assert d == pytest.approx(((0.02 - mu) + (0.02 + mu)) / 2)
    assert diag["strata_no_support"] == 1 and diag["events_excluded_no_support"] == 1
    assert ds.final_event_count(rows) == 2                                   # 30/50 门槛按最终可比事件数


def test_unknown_or_ineligible_rows_not_counted():
    rows = [_row("gold", 0, 1, 0.01), _row("gold", 1, None, 0.01), _row("gold", 2, 1, None),
            _row("gold", 3, 1, 0.01, eligible=False)] + [_row("gold", t, 0, 0.0) for t in range(10, 16)]
    assert ds.final_event_count(ds.nonoverlap(rows, 1)) == 1


def _panel(effect, n_days=400, seed=1):
    rng = random.Random(seed)
    rows = []
    for t in range(n_days):
        for inst in ("gold", "silver", "qqq"):
            s = rng.choice([1, -1]) if rng.random() < 0.25 else 0
            r = rng.gauss(0, 0.02) + effect * s
            rows.append(_row(inst, t, s, r, reg="up" if (t // 50) % 2 else "down", q=f"Q{t // 63}"))
    return rows


def test_judge_detects_real_effect_and_not_null():
    yes = ds.judge(_panel(0.01), 5, min_events=30, iters=400)
    assert yes["verdict"] == "detected" and yes["lower_one_sided"] > 0
    assert yes["lower_one_sided"] <= yes["point"] <= yes["ci95"][1]
    no = ds.judge(_panel(0.0, seed=3), 5, min_events=30, iters=400)
    assert no["verdict"] == "not_detected"


def test_judge_insufficient_before_any_return_statistic():
    few = [_row("gold", 0, 1, 0.5)] + [_row("gold", t, 0, 0.0) for t in range(1, 10)]
    out = ds.judge(few, 5, min_events=30, iters=50)
    assert out["verdict"] == "insufficient" and "point" not in out            # 未计算收益统计


def test_judge_invalid_resamples_make_insufficient():
    # 事件都挤在一个很短的时段、其余日子无事件 → 大量重抽抽不到有支持的层
    rows = []
    for k in range(30):                                                      # 30 个品种各 1 个事件，挤在第 0–5 天
        rows += [_row(f"i{k}", 0, 1, 0.01)] + [_row(f"i{k}", t, 0, 0.0) for t in range(1, 6)]
    rows += [_row("i0", t, 0, 0.0, q="Qx") for t in range(6, 2000)]         # 其余 2000 天没有事件
    out = ds.judge(rows, 1, min_events=30, block=10, iters=300)
    assert out["verdict"] == "insufficient" and out["invalid"] / 300 > ds.MAX_INVALID_FRAC


def test_bootstrap_deterministic():
    rows = ds.nonoverlap(_panel(0.005, n_days=120), 5)
    a, _ = ds.block_bootstrap(rows, ds.d_reg, block=10, iters=50)
    b, _ = ds.block_bootstrap(rows, ds.d_reg, block=10, iters=50)
    assert a == b


# —— Codex 021-01：预测资格与结果资格分开 ——
@pytest.mark.parametrize("h", [5, 10])
def test_ineligible_direction_row_does_not_occupy_cooldown(h):
    base = [_row("gold", t, 0, 0.0) for t in range(20, 30)]
    rows = [_row("gold", 0, 1, None, eligible=False), _row("gold", 1, 1, 0.01)] + base
    assert ds.final_event_count(ds.nonoverlap(rows, h)) == 1               # 以前 t0 占冷却 → 0


@pytest.mark.parametrize("h", [5, 10])
def test_published_event_with_missing_result_still_occupies_cooldown(h):
    base = [_row("gold", t, 0, 0.0) for t in range(20, 30)]
    rows = [_row("gold", 0, 1, None), _row("gold", 2, 1, 0.01)] + base      # t0 已合格发布、结果缺价
    kept = ds.nonoverlap(rows, h)
    assert [r["t"] for r in kept if r["kept"]] == [0]                        # 事前政策：占冷却，不因缺价改写序列
    assert ds.final_event_count(kept) == 0                                   # t0 本身不进统计


# —— Codex 021-02：共同支持在原始唯一日期上冻结，重抽复制不能复活排除层 ——
def test_duplicated_nonevent_rows_cannot_revive_excluded_stratum():
    ok = [_row("gold", t, 0, 0.0) for t in range(10, 16)] + [_row("gold", 0, 1, 0.02)]
    thin = [_row("silver", 0, 1, 0.50), _row("silver", 1, 0, 0.0)]         # 仅 1 个唯一对照日 → 原始排除
    rows = ds.nonoverlap(ok + thin, 5)
    sup = ds.support(rows)
    assert ("silver", "up", "2026Q4") not in sup["valid"] and sup["no_support"] == 1
    dup = rows + [r for r in rows if r["inst"] == "silver" and r["s"] == 0] * 6   # 模拟重抽复制同一对照日
    d_frozen, _ = ds.d_reg(dup, sup)
    d_orig, _ = ds.d_reg(rows, sup)
    assert d_frozen == pytest.approx(d_orig)                                 # silver 那 +0.50 事件仍不进入
    d_refrozen, _ = ds.d_reg(dup)                                            # 若按副本重新冻结则会（旧行为）
    assert ds.support(dup)["no_support"] == 1                                # 唯一日期口径下复制也不算新对照
    assert d_refrozen == pytest.approx(d_orig)


def test_resample_without_events_or_baseline_is_invalid():
    rows = ds.nonoverlap([_row("gold", t, 0, 0.0) for t in range(10, 16)] + [_row("gold", 0, 1, 0.02)], 5)
    sup = ds.support(rows)
    only_base = [r for r in rows if r["s"] == 0]
    only_event = [r for r in rows if r["s"] == 1]
    assert ds.d_reg(only_base, sup)[0] is None and ds.d_reg(only_event, sup)[0] is None
