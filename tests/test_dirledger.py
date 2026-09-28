"""方向判断台账：读数规则、前一交易日缺失不顶替、计分基准为开盘价、发布时刻对应的交易日、作者文件私有。"""
from datetime import date, datetime, timedelta, timezone

import pytest

from undertow.analyze import skew_reading as skr
from undertow.core.models import OptionContract

EXP = date(2026, 11, 6)


def _chain(put_bump=0.0, call_bump=0.0):
    cs = []
    for d in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60):
        cs.append(OptionContract(expiry=EXP, strike=100 + d, kind="C", open_interest=100, volume=1, gamma=0.01,
                                 delta=d, iv=0.20 + call_bump))
        cs.append(OptionContract(expiry=EXP, strike=100 - d, kind="P", open_interest=100, volume=1, gamma=0.01,
                                 delta=-d, iv=0.21 + put_bump))
    return cs


ASOF = date(2026, 9, 25)


@pytest.mark.parametrize("pb,cb,want", [(0.005, -0.001, "防守化"), (-0.005, 0.001, "进攻化"),
                                        (0.001, 0.0, "中性")])
def test_reading_rule(pb, cb, want):
    r = skr.read(_chain(), _chain(pb, cb), asof=ASOF)
    assert r["reading"] == want and r["rule_version"] == skr.RULE["version"]
    assert r["features"]["expiry"] == EXP.isoformat()


def test_no_expiry_in_range_is_insufficient():
    r = skr.read(_chain(), _chain(), asof=EXP - timedelta(days=5))
    assert r["reading"] == "数据不足"


def test_forward_returns_use_session_open_and_trading_days():
    bars = [(date(2026, 9, 24), 90, 91), (date(2026, 9, 25), 100, 101), (date(2026, 9, 28), 102, 99)]
    fr = skr.forward_returns(bars, date(2026, 9, 25), horizons=(1, 2, 5))
    assert fr["base_open"] == 100 and fr["ret_1d"] == pytest.approx(0.01) and fr["ret_2d"] == pytest.approx(-0.01)
    assert fr["ret_5d"] is None                                   # 未成熟，不折零


def test_session_after_posted_time():
    from undertow.dirledger_cli import session_after
    et = timezone(timedelta(hours=-4))
    assert session_after(datetime(2026, 9, 25, 7, 33, tzinfo=et)) == date(2026, 9, 25)      # 开盘前发布 → 当日
    assert session_after(datetime(2026, 9, 25, 10, 0, tzinfo=et)) == date(2026, 9, 28)     # 开盘后 → 下一交易日
    assert session_after(datetime(2026, 9, 26, 12, 0, tzinfo=et)) == date(2026, 9, 28)     # 周末 → 周一


def test_missing_previous_snapshot_is_not_substituted(tmp_path):
    from undertow.dirledger_cli import build_row

    class Store:
        def load(self, kind, sym, d):
            return {"x": 1} if d == date(2026, 9, 24) else None
        def captured_at(self, kind, sym, d):
            return None
        def path_of(self, kind, sym, d):
            return tmp_path / f"{d}.gz"
    # 只有认证到当日的快照，前一交易日没有 → 数据不足，不以更早快照顶替
    row = build_row("gold", "GLD", date(2026, 9, 24), Store(), now=datetime(2026, 9, 24, 10, tzinfo=timezone.utc),
                    replay=True, index={date(2026, 9, 24): date(2026, 9, 24), date(2026, 9, 21): date(2026, 9, 21)})
    assert row["reading"] == "数据不足" and "不以更早快照顶替" in row["reason"] and row["prev_file"] is None


def test_session_index_uses_capture_time_not_file_name():
    """周六文件（盘后抓）应认证到下一交易日；盘中抓剔除；同一交易日取抓取最晚的一份。"""
    from undertow.dirledger_cli import session_index
    et = timezone(timedelta(hours=-4))
    caps = {date(2026, 7, 11): datetime(2026, 7, 11, 10, 0, tzinfo=et).timestamp(),    # 周六 → 周一 7/13
            date(2026, 7, 12): datetime(2026, 7, 12, 20, 0, tzinfo=et).timestamp(),    # 周日晚 → 周一 7/13（更晚）
            date(2026, 7, 14): datetime(2026, 7, 14, 11, 0, tzinfo=et).timestamp(),    # 盘中 → 剔除
            date(2026, 7, 15): datetime(2026, 7, 15, 6, 0, tzinfo=et).timestamp()}     # 盘前 → 7/15

    class Store:
        def dates(self, kind, sym):
            return sorted(caps)
        def captured_at(self, kind, sym, d):
            return caps[d]
    idx = session_index(Store(), "GLD")
    assert idx == {date(2026, 7, 13): date(2026, 7, 12), date(2026, 7, 15): date(2026, 7, 15)}


def test_author_calls_are_private_and_prereg_exists():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    rc = subprocess.run(["git", "check-ignore", "-q", "data/soul/author_calls.jsonl"], cwd=root).returncode
    assert rc == 0, "外部作者判断（付费内容概括）必须在 gitignore 路径下"
    assert (root / "docs" / "prereg" / "2026-09-28_skew_reading_v1.md").exists()
    du = (root / "scripts" / "daily_update.sh").read_text("utf-8")
    assert du.index("undertow report gold") < du.index("dirledger record") < du.index("publish_dirs \"每日自动更新")


# —— 台账 v2（Codex 017 D17-01～04）——
ET_ = timezone(timedelta(hours=-4))


def test_forward_returns_calendar_endpoint_missing_bar_not_shifted():
    bars = [(date(2026, 9, 28), 100, 101), (date(2026, 9, 30), 102, 103)]          # 9/29 缺
    fr = skr.forward_returns(bars, date(2026, 9, 28), horizons=(1, 2, 3), closed_through=date(2026, 9, 30))
    assert fr["end_2d"] == "2026-09-29" and fr["status_2d"] == "missing_price" and fr["ret_2d"] is None
    assert fr["ret_3d"] == pytest.approx(0.03) and fr["status_3d"] == "ok"


def test_forward_returns_unclosed_day_is_immature():
    bars = [(date(2026, 9, 28), 100, 101), (date(2026, 9, 29), 102, 110)]           # 9/29 的 bar 盘中
    fr = skr.forward_returns(bars, date(2026, 9, 28), horizons=(1, 2), closed_through=date(2026, 9, 28))
    assert fr["status_1d"] == "ok" and fr["status_2d"] == "immature" and fr["ret_2d"] is None


class _Store:
    def __init__(self, tmp, caps, files):
        self.tmp, self.caps, self.files = tmp, caps, files
        for d in files:
            (tmp / f"{d}.gz").write_bytes(str(d).encode())
    def load(self, kind, sym, d):
        return {"x": 1} if d in self.files else None
    def captured_at(self, kind, sym, d):
        return self.caps.get(d)
    def path_of(self, kind, sym, d):
        return self.tmp / f"{d}.gz"


S0, S1 = date(2026, 9, 25), date(2026, 9, 28)
IDX = {S1: S1, S0: S0}


def _row(tmp, caps, now, monkeypatch):
    from undertow import dirledger_cli as dl
    monkeypatch.setattr(dl.skr, "read", lambda p, c, asof: {"reading": "中性", "features": {}})
    monkeypatch.setattr("undertow.collect.cboe_options.snapshot_from_payload",
                        lambda p, i, s: type("S", (), {"contracts": []})())
    return dl.build_row("gold", "GLD", S1, _Store(tmp, caps, {S0, S1}), now=now, replay=False, index=IDX)


def test_identity_requires_known_capture_times_before_record(tmp_path, monkeypatch):
    now = datetime(2026, 9, 28, 6, 0, tzinfo=ET_)
    r = _row(tmp_path, {}, now, monkeypatch)                                        # 两份抓取时刻都未知
    assert not r["identity_ok"] and "curr_captured_at_unknown" in r["identity_problems"]
    late_cap = datetime(2026, 9, 28, 7, 0, tzinfo=ET_).timestamp()                 # 抓取晚于记录时刻
    r = _row(tmp_path, {S0: late_cap - 86400 * 3, S1: late_cap}, now, monkeypatch)
    assert not r["identity_ok"] and "curr_captured_after_record" in r["identity_problems"]
    ok = datetime(2026, 9, 28, 5, 0, tzinfo=ET_).timestamp()
    r = _row(tmp_path, {S0: ok - 86400 * 3, S1: ok}, now, monkeypatch)
    assert r["identity_ok"] and r["before_open"]


def test_decide_policy_first_eligible_before_cutoff(tmp_path, monkeypatch):
    from undertow import dirledger_cli as dl
    ok = datetime(2026, 9, 28, 5, 0, tzinfo=ET_).timestamp()
    early = datetime(2026, 9, 28, 4, 0, tzinfo=ET_)
    r_missing = _row(tmp_path, {S0: ok - 86400 * 3}, early, monkeypatch)            # 当日快照尚未抓到
    assert dl.decide(None, r_missing, early) == ("not_ready", None)                 # 不占正式 key
    now = datetime(2026, 9, 28, 6, 0, tzinfo=ET_)
    r = _row(tmp_path, {S0: ok - 86400 * 3, S1: ok}, now, monkeypatch)
    st, formal = dl.decide(None, r, now)
    assert st == "eligible" and formal["status"] == "eligible"
    assert dl.decide(formal, r, now)[0] == "exists"                                 # 重跑同输入
    changed = dict(r, curr_sha="deadbeef")
    assert dl.decide(formal, changed, now)[0] == "changed_after_freeze"
    after = datetime(2026, 9, 28, 10, 0, tzinfo=ET_)
    st, miss = dl.decide(None, r, after)
    assert st == "missing_at_cutoff" and miss["reading"] is None


def test_record_rerun_is_idempotent_and_versioned(tmp_path, monkeypatch):
    from undertow import dirledger_cli as dl
    monkeypatch.setattr(dl, "DIR", tmp_path / "dl")
    ok = datetime(2026, 9, 28, 5, 0, tzinfo=ET_).timestamp()
    store = _Store(tmp_path, {S0: ok - 86400 * 3, S1: ok}, {S0, S1})
    monkeypatch.setattr(dl, "session_index", lambda st, sym: IDX)
    monkeypatch.setattr(dl.skr, "read", lambda p, c, asof: {"reading": "中性", "features": {}})
    monkeypatch.setattr("undertow.collect.cboe_options.snapshot_from_payload",
                        lambda p, i, s: type("S", (), {"contracts": []})())
    s1, _ = dl.record_one("gold", "GLD", S1, store, datetime(2026, 9, 28, 6, 0, tzinfo=ET_))
    s2, _ = dl.record_one("gold", "GLD", S1, store, datetime(2026, 9, 28, 7, 0, tzinfo=ET_))   # 仅记录时刻不同
    assert (s1, s2) == ("eligible", "exists")
    p = dl._path("gold", "prospective")
    assert skr.RULE["version"] in str(p)
    rows = dl.jl.load(p, dl.KEY)
    assert len(rows) == 1 and rows[0]["recorded_at"].startswith("2026-09-28T10:00")          # 保留首次记录时刻
    att = (tmp_path / "dl" / skr.RULE["version"] / "attempts" / "gold.jsonl").read_text().splitlines()
    assert len(att) == 2


def test_summary_excludes_non_eligible_rows():
    from undertow.dirledger_cli import _summary
    rows = [{"status": "missing_at_cutoff", "reading": None},
            {"status": "eligible", "reading": "防守化", "outcome": {"ret_1d": -0.01, "status_1d": "ok"}}]
    s = _summary("gold（前瞻）", rows, key="reading")
    assert "missing_at_cutoff 1" in s and "命中 1/1" in s


def test_score_keeps_outcome_history():
    from undertow.dirledger_cli import score_rows
    r = {"status": "eligible", "session": "2026-09-28",
         "outcome": {"ret_1d": 0.5, "base_open": 1}, "scored_at": "old"}
    bars = [(date(2026, 9, 28), 100, 101)]
    n = score_rows([r], bars, date(2026, 9, 28), "sha", datetime(2026, 9, 29, tzinfo=timezone.utc))
    assert n == 1 and r["outcome"]["ret_1d"] == pytest.approx(0.01) and r["outcome_history"][0]["scored_at"] == "old"


def test_author_defensive_calls_are_not_directional():
    from undertow.dirledger_cli import _hit
    assert _hit("防守", -0.02) is None and _hit("区间", 0.01) is None
    assert _hit("偏空", -0.02) is True and _hit("防守化", -0.02) is True


def test_level_recorded_even_when_previous_snapshot_missing(tmp_path, monkeypatch):
    from undertow import dirledger_cli as dl
    monkeypatch.setattr("undertow.collect.cboe_options.snapshot_from_payload",
                        lambda p, i, s: type("S", (), {"contracts": []})())
    ok = datetime(2026, 9, 28, 5, 0, tzinfo=ET_).timestamp()
    store = _Store(tmp_path, {S1: ok}, {S1})                                        # 只有当日快照
    row = dl.build_row("gold", "GLD", S1, store, now=datetime(2026, 9, 28, 6, 0, tzinfo=ET_), replay=False,
                       index={S1: S1}, level_fn=lambda snap, q: {"skew10_pp": 0.1, "expiry": "2026-10-30"})
    assert row["reading"] == "数据不足" and row["curr_level"]["skew10_pp"] == 0.1
