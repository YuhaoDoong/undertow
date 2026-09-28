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
            return {"x": 1} if d == date(2026, 9, 24) else None     # 只有当日，前一交易日缺
        def captured_at(self, kind, sym, d):
            return None
        def path_of(self, kind, sym, d):
            return tmp_path / f"{d}.gz"
    row = build_row("gold", "GLD", date(2026, 9, 24), Store(), now=datetime(2026, 9, 24, 10, tzinfo=timezone.utc),
                    replay=True)
    assert row["reading"] == "数据不足" and "不以更早快照顶替" in row["reason"] and row["prev_file"] == "2026-09-23"


def test_author_calls_are_private_and_prereg_exists():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    rc = subprocess.run(["git", "check-ignore", "-q", "data/soul/author_calls.jsonl"], cwd=root).returncode
    assert rc == 0, "外部作者判断（付费内容概括）必须在 gitignore 路径下"
    assert (root / "docs" / "prereg" / "2026-09-28_skew_reading_v1.md").exists()
    du = (root / "scripts" / "daily_update.sh").read_text("utf-8")
    assert du.index("undertow report gold") < du.index("dirledger record") < du.index("publish_dirs \"每日自动更新")
