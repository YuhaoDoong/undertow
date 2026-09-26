"""W05/W06 前瞻配对影子账：选腿、报价、结算、冻结、统计的口径锁。"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from undertow.analyze import shadow as sh                      # noqa: E402
from undertow.analyze import wall_spread as ws                 # noqa: E402
from undertow.collect import jsonl_ledger as jl                # noqa: E402

T = date(2026, 9, 14)
EXP = date(2026, 9, 16)


class _C:
    def __init__(self, kind, strike, expiry, bid=None, ask=None, oi=0):
        self.kind, self.strike, self.expiry, self.bid, self.ask, self.open_interest = kind, strike, expiry, bid, ask, oi


class _Snap:
    def __init__(self, cs): self.contracts = cs


def _snap():
    cs = []
    for k in (54.0, 55.0, 56.0, 57.0, 58.0, 59.0, 60.0, 61.0, 62.0):
        cs.append(_C("P", k, EXP, 0.10, 0.12)); cs.append(_C("C", k, EXP, 0.10, 0.12))
    cs.append(_C("P", 57.0, date(2026, 9, 30), 0.5, 0.6))        # DTE 16：不是目标到期
    return _Snap(cs)


def test_config_frozen_and_fee_single_source():
    assert sh.CONFIG["fee_round_trip"] == ws.FEE_PER_TRADE, "费用只能有一个来源"
    assert sh.CONFIG["primary_b"] == "B1" and sh.CONFIG["b_rules"] == {"B1": 1.0, "B2": 2.0}
    assert sh.config_hash() == sh.config_hash(dict(sh.CONFIG)), "hash 只取决于内容"


def test_pick_rules():
    strikes = [54.0, 55.0, 56.0, 57.0, 58.0, 59.0, 60.0]
    assert sh.pick_a({"strike": 57.0}, strikes, 58.12, "P", 2) == ((57.0, 55.0), None)
    assert sh.pick_a({"strike": 59.0}, strikes, 58.12, "P", 2) == (None, "wall_not_otm")
    assert sh.pick_a(None, strikes, 58.12, "P", 2) == (None, "no_wall")
    assert sh.pick_a({"strike": 56.5}, strikes, 58.12, "P", 2)[1] == "wall_strike_not_listed_for_target_expiry"
    # B：P 侧取 ≤ spot−m·ATR 的最高档；C 侧取 ≥ spot+m·ATR 的最低档
    assert sh.pick_b(strikes, 58.12, 1.0, "P", 1.0, 2) == ((57.0, 55.0), None)
    assert sh.pick_b(strikes, 58.12, 1.0, "C", 1.0, 2) == (None, "no_protective_strike")   # 60 之外只剩 0 档
    assert sh.pick_b(strikes, 58.12, None, "P", 1.0, 2) == (None, "atr_unavailable")


def test_credit_scenarios_and_invalid_quotes():
    c = sh._credit(0.60, 0.63, 0.34, 0.35, 0.25)
    assert c["conservative"] == pytest.approx(25.0) and c["mid"] == pytest.approx(27.0)
    assert c["mid_give"] == pytest.approx(26.5)
    assert sh._credit(None, 0.63, 0.34, 0.35, 0.25) is None, "缺价不能用 last 补"
    assert sh._credit(0.70, 0.63, 0.34, 0.35, 0.25) is None, "倒挂盘口无效"
    assert sh.exit_cost({"ask": 0.9, "bid": 0.8}, {"bid": 0.3, "ask": 0.31}, 100.0) == pytest.approx(60.0)
    assert sh.exit_cost({"ask": 2.0, "bid": 1.9}, {"bid": 0.1, "ask": 0.2}, 100.0) is None, "越界不裁剪"


def _row():
    return sh.build_opportunity(inst="silver", sym="SLV", snap=_snap(), session=T, spot=58.12, atr=1.0,
                                wall_fn=lambda k: {"strike": 57.0 if k == "P" else 60.0, "oi": 9000, "buf_pct": 1.9,
                                                   "n_exp": 2, "oi_by_expiry": {"2026-09-16": 9000}},
                                labels={"base_date": "2026-09-11"}, identity={"mode": "prospective", "status": "provisional"})


def test_build_opportunity_records_all_legs_and_pairing():
    r = _row()
    assert r["decision"]["target_expiry"] == "2026-09-16" and len(r["legs"]) == 6
    pa = next(l for l in r["legs"] if l["leg_id"] == "P-A"); pb = next(l for l in r["legs"] if l["leg_id"] == "P-B1")
    assert (pa["sell"], pa["buy"]) == (57.0, 55.0) and pb["same_as_A"] is True, "同一行权价要标同腿，不能当独立对照"
    assert all("reason" in l for l in r["legs"]), "无候选也要有原因"


def test_frozen_part_ignores_post_hoc_fields():
    import copy
    r = _row(); f0 = copy.deepcopy(sh.frozen_part(r))
    r["entry"] = {"x": 1}; r["monitor"].append({"leg_id": "P-A"}); r["outcome"] = {"a": 1}
    r["settled_at"] = "x"; r["recorded_at"] = "y"; r["identity"]["status"] = "certified"; r["identity"]["certified_at"] = "z"
    assert sh.frozen_part(r) == f0
    r["legs"][0]["sell"] = 99.0
    assert sh.frozen_part(r) != f0


def _bars(closes, lows=None, highs=None):
    ds = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    return [(d, (highs or closes)[i], (lows or closes)[i], closes[i]) for i, d in enumerate(ds)]


def test_settle_three_breaches_are_distinct():
    leg = next(l for l in _row()["legs"] if l["leg_id"] == "P-A")        # 卖 57P / 买 55P，宽 $200
    ent = {"credit": {"conservative": 20.0, "mid": 24.0, "mid_give": 23.0}, "valid": True}
    # 期间收盘跌破 57、到期收回 58：窗末未破、期间收盘破
    o = sh.settle_leg(leg, session=T, bars=_bars([58.0, 56.9, 58.0], lows=[57.5, 56.5, 57.8]), entry_leg=ent, exit_info=None)
    assert (o["endpoint_breach"], o["any_close_breach"], o["intraday_breach"]) == (False, True, True)
    assert o["pnl"]["hold_quote_conservative"] == pytest.approx(20.0 - 3.20)
    assert o["trigger_date"] == "2026-09-15" and o["pnl"]["exit_rule_quote_conservative"] is None, \
        "触发后缺退出报价 = 未知，不偷换成持有到期盈利"
    assert o["exit_status"] == "triggered_unpriced"
    o2 = sh.settle_leg(leg, session=T, bars=_bars([58.0, 56.9, 58.0]), entry_leg=ent, exit_info={"cost_conservative": 90.0})
    assert o2["pnl"]["exit_rule_quote_conservative"] == pytest.approx(20.0 - 90.0 - 3.20)
    # 盘中碰到但收盘都没破
    o3 = sh.settle_leg(leg, session=T, bars=_bars([58.0, 57.5, 58.0], lows=[57.5, 56.8, 57.9]), entry_leg=ent, exit_info=None)
    assert (o3["endpoint_breach"], o3["any_close_breach"], o3["intraday_breach"]) == (False, False, True)


def test_settle_refuses_immature_and_unpriced():
    leg = next(l for l in _row()["legs"] if l["leg_id"] == "P-A")
    assert sh.settle_leg(leg, session=T, bars=_bars([58.0, 58.0, 58.0])[:2], entry_leg=None, exit_info=None) is None
    o = sh.settle_leg(leg, session=T, bars=_bars([58.0, 58.0, 58.0]), entry_leg=None, exit_info=None)
    assert o["pnl"]["hold_quote_conservative"] is None, "没有盘中入场报价就没有可执行收益"


def test_price_legs_marks_off_hours_not_executable():
    r = _row()
    depth = {(l["side"], K): {"bid": 0.6, "ask": 0.62, "bid_size": 10, "ask_size": 10, "error": ""}
             for l in r["legs"] if l["status"] == "candidate" for K in (l["sell"], l["buy"])}
    e = sh.price_legs(r, depth, observed_at="t", phase="off_hours")
    assert e["executable"] is False and e["phase"] == "off_hours"


def test_bootstrap_and_bounds():
    assert sh.date_block_bootstrap({})[0] is None
    m, lo, hi = sh.date_block_bootstrap({f"d{i}": [0.1, 0.1] for i in range(4)})
    assert m == pytest.approx(0.1) and lo is None, "少于 5 个日期不给区间"
    m, lo, hi = sh.date_block_bootstrap({f"d{i}": [i / 10] for i in range(20)})
    assert lo < m < hi
    assert sh.zero_event_upper(50) == pytest.approx(0.0582, abs=1e-3), "50 笔零事件的 95% 上界 5.8%"
    assert sh.judge(0.01, 0.2) == "支持" and sh.judge(-0.2, -0.01) == "不支持" and sh.judge(-0.1, 0.1) == "未决"


def test_jsonl_ledger_freeze_update_and_quarantine(tmp_path):
    p = tmp_path / "silver.jsonl"; r = _row(); r["recorded_at"] = "2026-09-14T10:00:00+00:00"
    assert jl.insert_frozen(p, r, key_field="key", frozen=sh.frozen_part) == "inserted"
    assert jl.insert_frozen(p, dict(r, recorded_at="later"), key_field="key", frozen=sh.frozen_part) == "exists"
    bad = json.loads(json.dumps(r)); bad["legs"][0]["sell"] = 50.0
    with pytest.raises(jl.LedgerConflictError):
        jl.insert_frozen(p, bad, key_field="key", frozen=sh.frozen_part)
    assert jl.update(p, lambda x: x.update(entry={"ok": 1}) or True, key_field="key", frozen=sh.frozen_part) == 1
    with pytest.raises(jl.LedgerConflictError):
        jl.update(p, lambda x: x["legs"][0].update(sell=1.0) or True, key_field="key", frozen=sh.frozen_part)
    assert jl.load(p, "key")[0]["legs"][0]["sell"] == 57.0, "拒绝的更新不得落盘"
    p.write_text(p.read_text() + "{坏行\n")
    with pytest.raises(jl.LedgerCorruptError):
        jl.load(p, "key")
    assert list(tmp_path.glob("silver.jsonl.corrupt-*")), "损坏必须另存隔离副本"


def test_prospective_requires_certified_and_before_open():
    from undertow.shadow_cli import prospective_ok
    base = {"session": "2026-09-14", "identity": {"mode": "prospective", "status": "certified"}}
    assert prospective_ok(dict(base, recorded_at="2026-09-14T12:00:00+00:00"))          # 08:00 ET
    assert not prospective_ok(dict(base, recorded_at="2026-09-14T14:00:00+00:00"))      # 10:00 ET 迟到
    assert not prospective_ok({**base, "identity": {"mode": "prospective", "status": "provisional"},
                               "recorded_at": "2026-09-14T12:00:00+00:00"})
    assert not prospective_ok({**base, "identity": {"mode": "replay", "status": "certified"},
                               "recorded_at": "2026-09-14T12:00:00+00:00"})


def test_cli_is_read_only():
    """影子账从不下单：编排代码里不得出现任何下单/撤单子命令。"""
    src = (ROOT / "undertow" / "shadow_cli.py").read_text("utf-8")
    import re
    assert not re.search(r'\[\s*"order"', src), "不得调用 longbridge order 子命令"
    for bad in ("order buy", "order sell", "order cancel", "order replace", "submit_order", "place_order"):
        assert bad not in src, bad
    assert "fetch_depth" in src and "_run(" not in src, "盘口只经 longbridge_quote 的只读接口取"


def test_unattended_wiring():
    """接进现有任务（不新建 launchd）：盘前 capture+settle、盘中 ④ 窗口 quote；失败必须告警并可重试。"""
    du = (ROOT / "scripts" / "daily_update.sh").read_text("utf-8")
    assert "shadow capture" in du and "shadow settle" in du and "影子账采集/结算失败" in du
    assert du.index("shadow capture") < du.index("git add data/snapshots data/history"), "要在提交之前"
    sh_ = (ROOT / "scripts" / "session_hooks.sh").read_text("utf-8")
    i = sh_.index("④ 影子账两腿盘口")
    seg = sh_[i:i + 1200]
    assert "shadow quote" in seg and ".shadow_quote_" in seg and "notify" in seg
    assert "ET_MIN >= 600 && ET_MIN <= 608" in sh_[sh_.index("不在任何窗口"):], "心跳判断要包含新窗口"
