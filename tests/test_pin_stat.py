"""expiry-pin-v4r1 镜像统计量（undertow/analyze/pin_stat.py，Codex 044 E5）。纯合成。"""
import random

from undertow.analyze import pin_stat as ps


def test_codex_counterexample_old_d_negative_mirror_zero():
    """Codex 044：真实 U[−.5,.5]、错误零模型 U[−1,1]、无墙力：旧 u 差 = −.1025；镜像统计量（线性/核）均值 0。"""
    n = 20000
    cs = [-0.5 + (i + 0.5) / n for i in range(n)]
    assert abs(sum(ps.mirror_m(c, 0.0, 0.10, 1.0) for c in cs) / n) < 1e-9
    assert abs(sum(ps.mirror_k(c, 0.0, 0.10, 1.0) for c in cs) / n) < 1e-9
    from scripts.expiry_pin_v4_synth import codex_counterexample
    r = codex_counterexample(20000)
    assert r["old_d"] == -0.1025 and abs(r["mirror_mean_m"]) < 1e-6


def test_close_on_wall_is_negative_and_symmetric_inputs():
    assert ps.mirror_m(101.0, 100.0, 101.0, 2.0) == -1.0                 # |0| − |2| = −2 → /2
    assert ps.mirror_k(101.0, 100.0, 101.0, 2.0) < -0.9
    assert ps.mirror_k(99.0, 100.0, 101.0, 2.0) > 0.9                    # 收在镜像点上 → 正
    assert ps.mirror_m(100.0, 100.0, 100.0, 2.0) == 0.0                  # 墙恰在参考价：不提供信息
    for bad in ((None, 100, 101, 2), (101, 100, 101, 0), (101, 100, float("nan"), 2)):
        assert ps.mirror_m(*bad) is None and ps.mirror_k(*bad) is None


def test_side_balanced_mean_cancels_linear_drift_and_needs_both_sides():
    rows = [{"m": 0.3, "side": 1}, {"m": 0.3, "side": 1}, {"m": -0.3, "side": -1}]
    assert abs(ps.side_balanced_mean(rows)) < 1e-12                       # 两侧等权：漂移一阶抵消
    assert ps.side_balanced_mean([{"m": 0.1, "side": 1}]) is None         # 缺一侧不退回单侧


def test_bootstrap_requires_enough_weeks_and_is_seeded():
    rng = random.Random(1)
    rows = [{"week": w, "side": rng.choice((1, -1)), "m": rng.gauss(0, 1)} for w in range(13) for _ in range(8)]
    a = ps.block_bootstrap(rows, block_weeks=2, n_boot=300, seed=7)
    b = ps.block_bootstrap(rows, block_weeks=2, n_boot=300, seed=7)
    assert a == b and a["lo"] < a["est"] < a["hi"] and a["n_weeks"] == 13
    few = [r for r in rows if r["week"] < 3]
    assert ps.block_bootstrap(few, block_weeks=2, n_boot=100, seed=7)["lo"] is None
    assert ps.verdict({"lo": None, "hi": None}) == "本数据不能识别"
    assert ps.verdict({"lo": -0.2, "hi": -0.01}).startswith("收盘离墙比离镜像点近")
    assert ps.verdict({"lo": -0.2, "hi": 0.1}) == "未检出，也未排除"


def test_null_batch_coverage_is_checked_in_batch_not_single_run():
    """Codex 044：单次随机游走要求 CI 必含 0 不是有效单测；这里批量看假检出率在合理范围（宽松界，正式数字见合成校准产物）。"""
    from scripts.expiry_pin_v4_synth import run_scenario
    r = run_scenario("t", {"_stat": "kernel"}, weeks=13, n_sims=60, n_boot=150, block=2, seed=1)
    assert r["unidentifiable"] == 0 and r["detect_rate"] <= 0.15 and r["coverage_of_0"] >= 0.75


def test_signflip_exact_matches_sampled_and_flipping_week_flips_m():
    rng = random.Random(3)
    rows = [{"week": w, "side": rng.choice((1, -1)), "m": rng.gauss(0, 1)} for w in range(10) for _ in range(6)]
    a = ps.signflip_test(rows)
    b = ps.signflip_test(rows, exact_max_weeks=0, n_perm=40000, seed=1)
    assert a["exact"] and not b["exact"] and abs(a["p_lower"] - b["p_lower"]) < 0.01
    # 一整周增量取反 → 该周每行 C_E − r 取反 → m 恰好变号（核版与线性版都是）
    ref, k, atr = 100.0, 101.0, 2.0
    for c in (99.3, 100.4, 101.2, 103.0):
        assert abs(ps.mirror_k(c, ref, k, atr) + ps.mirror_k(2 * ref - c, ref, k, atr)) < 1e-12
        assert abs(ps.mirror_m(c, ref, k, atr) + ps.mirror_m(2 * ref - c, ref, k, atr)) < 1e-12
    assert ps.verdict_signflip({"p_lower": 0.01, "p_upper": 0.99}).startswith("收盘离墙")
    assert ps.verdict_signflip({"p_lower": None}) == "本数据不能识别"
