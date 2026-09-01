"""墙位卖方价差模块的行为锁。

这个模块直接产出交易候选，却长期零测试覆盖（2026-09-01 发现）。
下面每条测试都对应一次真实的口径错误，改错了就红。
"""
from datetime import date

import pytest

from undertow.analyze import wall_spread as ws


class FakeLeg:
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask


class FakeContract:
    def __init__(self, kind, strike, expiry, oi=0, bid=None, ask=None):
        self.kind, self.strike, self.expiry = kind, strike, expiry
        self.open_interest, self.bid, self.ask = oi, bid, ask


class FakeSnap:
    def __init__(self, contracts, spot):
        self.contracts, self.spot = contracts, spot


# ── should_exit：2026-09-01 改为两侧同规则 ──────────────────────────
def test_put侧不再一刀切拒绝平仓():
    """原实现 kind=='P' 直接 return False，理由（"从没破过墙"）已被证伪。"""
    ok, why = ws.should_exit("P", sell_strike=60.0, spot=59.0,
                             signal_side="看跌", signal_ratio=35.0)
    assert ok, why


def test_call侧反向极强信号且贴近卖腿则平仓():
    """卖 60C、现价 59：尚未破墙但已逼近，仍应平（距离 1.7% ≤ 5%）。"""
    ok, why = ws.should_exit("C", sell_strike=60.0, spot=59.0,
                             signal_side="看涨", signal_ratio=35.0)
    assert ok and "逼近" in why


def test_已破卖腿必须平仓_哪怕距离超过阈值():
    """codex 2026-09-01 P0：原实现在这里方向反了。

    卖 60P、现价 50 —— 已深度破墙 16.7%，旧代码却因 |60/50−1|=20% > 5%
    返回「离得远，不必平」，等于在风险最大时锁死出口。
    """
    ok, why = ws.should_exit("P", sell_strike=60.0, spot=50.0,
                             signal_side="看跌", signal_ratio=35.0)
    assert ok, why
    assert "已破卖腿" in why

    ok2, why2 = ws.should_exit("C", sell_strike=60.0, spot=72.0,
                               signal_side="看涨", signal_ratio=35.0)
    assert ok2 and "已破卖腿" in why2


def test_安全侧且离得远才不平():
    """距离条件只承担「尚在安全侧时别乱动」，不得用于拒绝已破墙的仓位。"""
    ok, why = ws.should_exit("P", sell_strike=60.0, spot=100.0,
                             signal_side="看跌", signal_ratio=35.0)
    assert not ok and "安全侧" in why


def test_信号方向不逆就不平():
    ok, _ = ws.should_exit("P", sell_strike=60.0, spot=59.0,
                           signal_side="看涨", signal_ratio=999.0)
    assert not ok


def test_信号不够极端就不平():
    ok, _ = ws.should_exit("P", sell_strike=60.0, spot=59.0,
                           signal_side="看跌", signal_ratio=ws.SIG_EXIT_RATIO - 0.1)
    assert not ok


def test_离卖腿远就不平():
    """用户原话：如果离得很远，其实也无所谓。"""
    ok, why = ws.should_exit("P", sell_strike=60.0, spot=100.0,
                             signal_side="看跌", signal_ratio=999.0)
    assert not ok and "离得远" in why


# ── _fill：窄价差的成交价假设 ───────────────────────────────────────
def test_成交价按中价让点差_不是两边吃满():
    """卖腿吃 bid、买腿吃 ask 会把真实有 $7 的价差算成 $0。"""
    sell, buy = FakeLeg(0.60, 0.80), FakeLeg(0.50, 0.70)
    got = ws._fill(sell, buy)
    mid = ((0.60 + 0.80) / 2 - (0.50 + 0.70) / 2) * 100      # = 10
    worst = (0.60 - 0.70) * 100                               # = -10
    assert got == pytest.approx(mid + (worst - mid) * 0.25)
    assert got > 0                                            # 两边吃满会得 -10


def test_让价比例越大成交价越差():
    sell, buy = FakeLeg(0.60, 0.80), FakeLeg(0.50, 0.70)
    assert ws._fill(sell, buy, give=0.5) < ws._fill(sell, buy, give=0.0)


# ── accum_wall：band 的语义 ────────────────────────────────────────
def test_band之外的大OI被排除():
    """这正是 2026-09-01 暴露的问题：band 内总能找到一个最大值，那不是墙。

    真墙 50（OI 180000）在 band=5% 之外，于是选出 OI 只有 30000 的 57。
    此测试锁住【当前行为】，并标明它是已知缺陷，不是期望行为。
    """
    exp = date(2026, 9, 11)
    cs = [FakeContract("P", 50.0, exp, oi=180_000),
          FakeContract("P", 57.0, exp, oi=30_000)]
    snap = FakeSnap(cs, 60.0)
    k, oi, _ = ws.accum_wall(snap, 60.0, "P", date(2026, 9, 1), cap=9999, band=0.05)
    assert k == 57.0 and oi == 30_000      # ⛔ 已知缺陷：真墙 50 被 band 挡在外面
    k2, oi2, _ = ws.accum_wall(snap, 60.0, "P", date(2026, 9, 1), cap=9999, band=0.30)
    assert k2 == 50.0 and oi2 == 180_000   # band 放大才找到真墙


def test_无合约时返回None而不是崩溃():
    snap = FakeSnap([], 60.0)
    k, oi, sh = ws.accum_wall(snap, 60.0, "P", date(2026, 9, 1), cap=9999)
    assert k is None and oi == 0 and sh == 0.0


# ── propose：激活闸门 ──────────────────────────────────────────────
def test_未激活品种明确拒绝并说明原因():
    snap = FakeSnap([], 400.0)
    v = ws.propose(snap, "gold", date(2026, 9, 1), date(2026, 9, 1), spot=400.0)
    assert not v.ok and "未激活" in v.reason


def test_ACTIVE为空_全品种停用():
    """2026-09-01（codex P0）：只清绩效数字、留着污染参数继续出候选，
    等于换个说法照做同一笔交易。重开需满足模块内列出的三项条件。"""
    assert ws.ACTIVE == set()


def test_propose的spot必填():
    """原来允许回退 snap.spot，而 74% 的快照 spot 不是当天的价。"""
    import inspect
    sig = inspect.signature(ws.propose)
    assert sig.parameters["spot"].default is inspect.Parameter.empty


def test_未激活时的理由不得格式化None():
    snap = FakeSnap([], 60.0)
    v = ws.propose(snap, "silver", date(2026, 9, 1), date(2026, 9, 1), spot=60.0)
    assert not v.ok and "停用" in v.reason


# ── PARAMS：作废标记不得被悄悄填回 ──────────────────────────────────
@pytest.mark.parametrize("inst", ["silver", "gold"])
@pytest.mark.parametrize("fld", ["n", "unbroken", "roi", "annual", "occ", "credit"])
def test_回测数字保持作废状态(inst, fld):
    """这些数字是用错位的 snapshot.spot 算的（74% 的快照 spot 不是当天价）。

    要填回来，必须先用 C[D−1] 重跑回测，并把本测试改成断言新值。
    在那之前保持 None，避免报告把错数字当结论输出。
    """
    assert ws.PARAMS[inst][fld] is None, (
        f"{inst}.{fld} 被填回了 —— 若已用正确基准重算，请同步更新本测试")


# ── Candidate 派生量 ──────────────────────────────────────────────
def test_candidate派生量():
    c = ws.Candidate(kind="P", expiry=date(2026, 9, 11), dte=10, sell=60.0,
                     buy=58.0, credit=40.0, width=200.0, occupancy=160.0,
                     wall=60.0, spot=61.0)
    assert c.roi == pytest.approx(0.25)
    assert c.buffer_pct == pytest.approx(abs(60 / 61 - 1) * 100)
    assert c.max_loss == pytest.approx(160.0 + ws.FEE_PER_LEG * 4)
    assert c.fee_share == pytest.approx(ws.FEE_PER_LEG * 4 / 40.0)


def test_零占用时roi不炸():
    c = ws.Candidate(kind="P", expiry=date(2026, 9, 11), dte=10, sell=60.0,
                     buy=60.0, credit=40.0, width=0.0, occupancy=0.0,
                     wall=60.0, spot=61.0)
    assert c.roi == 0.0
