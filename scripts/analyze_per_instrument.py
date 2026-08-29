"""单品种时序检验 —— 与跨品种合并用【不同】的统计方法。

用户 2026-08-29 质疑：「增仓在几个黄金大涨大跌节点都有效，这是事实吧？」
是事实。而我此前的全盘撤回过度了 —— 错在把两种情形套了同一把尺子。

## 方法论：什么时候该用日聚类，什么时候不该

  跨品种合并  →  必须用日期聚类 bootstrap。
                 同一天的金银 QQQ 不是独立样本（金银 0.89、QQQ/TQQQ 0.99），
                 直接加总会把 138 个品种日当成 138 份独立证据。
                 全样本只有 35 个日期聚类 < 50，所以【不能下结论】。

  单品种时序  →  日聚类修正【不适用】，也不该用。
                 同品种不同日期、前瞻 1 日、收益窗口不重叠，
                 独立性假设是合理的。此时二项检验就是对的工具。

把单品种也按 35 个聚类去卡，等于用为跨品种设计的修正去惩罚一个
根本没有那种相关问题的样本 —— 这是我上一轮的错。
"""
import json
import math
from pathlib import Path

ROWS = json.loads(Path("data/history/strength_backtest.json").read_text())
ALPHA = 0.05


def binom_p(k, n, p=0.5):
    return min(1.0, 2 * sum(math.comb(n, i) * p**i * (1 - p)**(n - i)
                            for i in range(k, n + 1)))


def wilson_lo(k, n, z=1.96):
    if not n:
        return 0.0
    ph = k / n
    d = 1 + z * z / n
    return (ph + z * z / (2 * n)
            - z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))) / d


insts = sorted({r["inst"] for r in ROWS})
tested = []
print("单品种时序检验（增仓层方向 vs 当日去趋势收益）")
print("=" * 78)
print(f"{'品种':<8}{'样本':>5}{'命中':>5}{'命中率':>8}{'二项双尾p':>11}{'Wilson下界':>11}  判定")
print("-" * 78)
for k in insts:
    g = [r for r in ROWS if r["inst"] == k
         and r.get("flow") is not None and abs(r["flow"]) > 1e-9]
    if len(g) < 10:
        continue
    avg = sum(r["ret"] for r in g) / len(g)
    hit = sum(1 for r in g if r["flow"] * (r["ret"] - avg) > 0)
    n = len(g)
    p, lo = binom_p(hit, n), wilson_lo(hit, n)
    tested.append((k, hit, n, p))
    v = "✅ p<0.05 且下界>50%" if (p < ALPHA and lo > 0.5) else "❌"
    print(f"{k:<8}{n:>5}{hit:>5}{hit/n*100:>7.0f}%{p:>11.4f}{lo*100:>10.1f}%  {v}")

print()
print("=" * 78)
print(f"多重比较：一共测了 {len(tested)} 个品种，不能只挑赢的说")
print("=" * 78)
adj = ALPHA / len(tested)
print(f"Bonferroni 校正后阈值 = {ALPHA}/{len(tested)} = {adj:.4f}")
for k, hit, n, p in tested:
    print(f"  {k:<8} p={p:.4f}　原始阈值 {'✅' if p < ALPHA else '❌'}　"
          f"校正后 {'✅' if p < adj else '❌'}")
print()
print(f"纯噪音下 {len(tested)} 次独立检验至少 1 次 p<0.05 的概率 = "
      f"{1-(1-ALPHA)**len(tested):.1%}")
print("⚠️ 但金银相关 0.89，它俩不是两次独立检验 —— 更接近「1.x 次」。")
print("   所以 2/4 显著既不能当两份独立证据，也不能说是碰巧。")
print()
print("=" * 78)
print("结论边界（写清楚才不会被自己误读）")
print("=" * 78)
print("✅ 是事实：黄金增仓层 23/32=72%(p=0.020)、白银 20/28=71%(p=0.036)，")
print("   黄金大波动日 9/11=82%，含 8/19(+3.84%) 与 8/28(-3.24%)。")
print("⚠️ 但仍未达本项目验证门槛，三条都差着：")
print("   1. Bonferroni 校正后（测了 4 个品种）阈值 0.0125，黄金 p=0.020 过不了；")
print("   2. n=32 < 项目自己定的 MIN_N=50；")
print("   3. 只覆盖 2026-06-25~08-28 两个月、一种市场环境。")
print("→ 定性：比「探索性观察」强，但够不上「已验证」。可以用来加权判断，")
print("   不足以据此重仓。原油 53%、QQQ 58% 说明它不是放之四海皆准。")
