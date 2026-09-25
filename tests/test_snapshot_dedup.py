"""同日重复抓取的去重 —— 回归测试。

daily_update.sh 每天四个时点各跑一次（盘前 ET 04:00~06:45）。实测 GLD
2026-09-22 在 git 里留下的四个版本：

    Σ|ΔOI| = 0 · Σ|Δvolume| = 0 · 合约数差 = 0 · 源 ts 全为 2026-09-21T15:59:59
    唯一差别：current_price 398.38 → 395.89（延迟报价随盘前走）

后三份没有任何物质增量，却各占一个 417KB 的 git blob；全库因此多出 71MB
（快照工作区 117MB vs git 历史 188MB）。

⚠️ 去重的危险边界在**反方向**：早时点可能撞上 OCC 结算只落地一半的残缺链。
若把规则写成"今天存过就不再写"，那条残缺链会被永久钉死在当天，而期权链
不可再生。所以判据必须是"物质内容是否相同"，不是"今天是否存过"。
"""
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow import cli                                        # noqa: E402
from undertow.collect.cboe_options import (materially_same,     # noqa: E402
                                           snapshot_from_payload)

TODAY = date(2026, 9, 24)


def _payload(contracts, spot=400.0, ts="2026-09-23T15:59:59"):
    """contracts = [(strike, kind, oi, volume), ...]"""
    return {
        "timestamp": f"{ts}+00:00",
        "data": {"current_price": spot, "last_trade_time": ts, "options": [
            {"option": f"GLD261016{k}{int(st * 1000):08d}",
             "open_interest": oi, "volume": vol, "iv": 0.2, "delta": 0.5,
             "gamma": 0.01, "bid": 1.0, "ask": 1.1, "bid_size": 1, "ask_size": 1,
             "theta": 0.0, "vega": 0.0, "rho": 0.0,
             "last_trade_price": 1.05, "prev_day_close": 1.0}
            for st, k, oi, vol in contracts]},
    }


BASE = [(400.0, "C", 1000, 55), (390.0, "P", 800, 31)]


def _snap(p):
    return snapshot_from_payload(p, "gold", "GLD")


# ── materially_same 的语义 ──────────────────────────────────────────────

def test_spot_change_alone_is_not_material():
    """spot 变了不算变 —— 这是去重能不能生效的关键。

    spot 是"抓取时刻的延迟报价，介于 D−1 收盘与 D 盘前之间，不精确等于任一
    收盘"（flow.py 时序约定）。四个时点的 spot 没有哪个更对；把它算进判据，
    去重就永远不会触发，等于没做。
    """
    a, b = _snap(_payload(BASE, spot=398.38)), _snap(_payload(BASE, spot=395.89))
    assert a.spot != b.spot
    assert materially_same(a, b), "只有 spot 变化必须判为相同"
    print("PASS test_spot_change_alone_is_not_material")


@pytest.mark.parametrize("changed,label", [
    ([(400.0, "C", 1200, 55), (390.0, "P", 800, 31)], "OI 变化"),
    ([(400.0, "C", 1000, 77), (390.0, "P", 800, 31)], "volume 变化"),
    ([(400.0, "C", 1000, 55)], "合约消失（到期滚出）"),
    ([(400.0, "C", 1000, 55), (390.0, "P", 800, 31),
      (380.0, "P", 5, 1)], "新挂合约"),
])
def test_material_changes_are_detected(changed, label):
    """三样物质内容任一变化都必须判为不同 —— 它们是分析层真正吃进去的东西。"""
    assert not materially_same(_snap(_payload(BASE)), _snap(_payload(changed))), label
    print(f"PASS test_material_changes_are_detected[{label}]")


# ── 落盘路径的行为 ──────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    # root 必须隔离：写进真实 data/snapshots 会污染不可再生的历史数据
    return cli.SnapshotStore(root=tmp_path)


class _Inst:
    key = "gold"


def test_same_day_identical_skips_rewrite(store):
    """同日重复且物质内容一致 → 不重写文件，但**路径照常回报**。

    路径不能返回 None：CLI 会把它打出来，None 看着像出错了。
    """
    p1, sk1 = cli._save_snapshot_dedup(store, _Inst, "GLD", _payload(BASE), TODAY)
    assert p1 and not sk1
    mtime1 = p1.stat().st_mtime_ns

    p2, sk2 = cli._save_snapshot_dedup(
        store, _Inst, "GLD", _payload(BASE, spot=395.89), TODAY)
    assert sk2 is True, "同日物质内容一致必须跳过"
    assert p2 == p1, "必须回报既有路径，不能是 None"
    assert p1.stat().st_mtime_ns == mtime1, "文件被重写了 → git 仍会产生新 blob"
    print("PASS test_same_day_identical_skips_rewrite")


def test_same_day_material_change_must_overwrite(store):
    """同日但物质内容变了 → **必须**覆盖。

    这是去重最危险的反方向：早时点可能撞上 OCC 结算只落地一半的残缺链，
    晚时点拿到完整的若写不进去，那条残缺链就被永久钉死在当天 —— 而期权链
    不可再生。规则必须是"内容是否相同"，不是"今天是否存过"。
    """
    partial = [(400.0, "C", 0, 0), (390.0, "P", 0, 0)]        # 结算未落地
    cli._save_snapshot_dedup(store, _Inst, "GLD", _payload(partial), TODAY)
    p, sk = cli._save_snapshot_dedup(store, _Inst, "GLD", _payload(BASE), TODAY)
    assert sk is False, "物质内容变了却被去重跳过 —— 残缺链会被永久钉死"
    got = _snap(store.load("options", "GLD", TODAY))
    assert sum(c.open_interest for c in got.contracts) == 1800, "完整链必须覆盖残缺链"
    print("PASS test_same_day_material_change_must_overwrite")


def test_cross_day_rule_is_unchanged(store):
    """跨日去重走的仍是 oi_change_total，与同日去重是两个问题。

    跨日问的是"OCC 结算到了没有"（Σ|ΔOI|=0 → 没到，不落盘）；
    同日问的是"这次抓的有没有新东西"。两者不能混用同一个判据：
    跨日若用 materially_same，会把"到期滚出但存活合约没动"的残缺链放行。
    """
    yesterday = date(2026, 9, 23)
    store.save("options", "GLD", _payload(BASE), on_date=yesterday)
    # 存活合约一张没动 → 跨日必须判未结算、不落盘（返回 None）
    p, sk = cli._save_snapshot_dedup(
        store, _Inst, "GLD", _payload(BASE, spot=401.0), TODAY)
    assert sk is True and p is None, "跨日未结算必须返回 (None, True)"
    assert store.load("options", "GLD", TODAY) is None, "不得落盘"

    # OI 动了 → 结算到位，落盘
    moved = [(400.0, "C", 1300, 60), (390.0, "P", 800, 31)]
    p2, sk2 = cli._save_snapshot_dedup(store, _Inst, "GLD", _payload(moved), TODAY)
    assert sk2 is False and p2 is not None
    print("PASS test_cross_day_rule_is_unchanged")


def test_dedup_is_announced_not_silent(capsys, store, monkeypatch):
    """去重必须出声 —— 否则"今天到底抓到没有"无从分辨（AGENTS.md 第四节）。"""
    store.save("options", "GLD", _payload(BASE), on_date=TODAY)

    class _Src:
        def fetch_raw(self, inst, *, use_cache=True):
            return _payload(BASE, spot=395.89)

    monkeypatch.setattr(cli, "market_today", lambda: TODAY)
    monkeypatch.setattr(cli, "SnapshotStore", lambda *a, **k: store)
    monkeypatch.setattr(cli, "CboeOptionsSource", _Src)

    class _Args:
        instruments = ["gold"]; no_cache = True; status_file = None
        no_fallback = True; fallback_expiries = None
    cli.cmd_snapshot(_Args())
    err = capsys.readouterr().err
    assert "去重" in err and "gold" in err, f"同日去重必须打印出来：{err!r}"
    print("PASS test_dedup_is_announced_not_silent")
