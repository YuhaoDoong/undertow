"""到期类型 Q/M/W/D（用户 2026-09-29）：按规则与交易日历判定。"""
from datetime import date

import pytest

from undertow.analyze.expiry_type import classify, monthly_expiry, quarterly_expiry


@pytest.mark.parametrize("d,t", [("2026-09-30", "Q"), ("2026-06-30", "Q"), ("2026-12-31", "Q"),
                                 ("2026-09-18", "M"), ("2026-10-16", "M"), ("2026-12-18", "M"),
                                 ("2026-09-25", "W"), ("2026-10-02", "W"), ("2026-09-29", "D"),
                                 ("2027-03-25", "W")])                # 周五耶稣受难日休市 → 周四为周度
def test_classify(d, t):
    assert classify(date.fromisoformat(d))["type"] == t


def test_holiday_has_no_expiry_and_monthly_rolls_back():
    assert classify(date(2027, 3, 26))["type"] is None                # 耶稣受难日休市
    assert monthly_expiry(2026, 9) == date(2026, 9, 18)
    assert quarterly_expiry(2026, 9) == date(2026, 9, 30) and quarterly_expiry(2026, 10) is None


def test_expiry_profile_row_types_and_top_strikes():
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS
    from undertow import shadow_cli as sc
    C = lambda e, k, s, oi, v=1: NS(expiry=e, kind=k, strike=s, open_interest=oi, volume=v)
    snap = NS(spot=100.0, contracts=[C(date(2026, 9, 30), "P", 95, 700), C(date(2026, 9, 30), "P", 98, 400),
                                    C(date(2026, 9, 30), "C", 105, 500), C(date(2026, 9, 30), "P", 70, 9999),   # 远价外不进 top
                                    C(date(2026, 10, 16), "C", 110, 900), C(date(2026, 12, 18), "C", 110, 5)])  # >35 天不记
    row = sc.expiry_profile_row("gold", "GLD", date(2026, 9, 29), snap, {"sha256": "x", "captured_at": 1.0},
                                datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc))
    e = {x["expiry"]: x for x in row["expiries"]}
    assert set(e) == {"2026-09-30", "2026-10-16"} and e["2026-09-30"]["type"] == "Q" and e["2026-10-16"]["type"] == "M"
    assert e["2026-09-30"]["top_p"] == [[95, 700], [98, 400]] and isinstance(e["2026-09-30"]["top_p"][0], list)
    assert e["2026-09-30"]["oi_p"] == 700 + 400 + 9999 and row["before_open"] is True


def test_expiry_profile_row_survives_real_jsonl_write(tmp_path):
    """2026-09-29 首日：top_c/top_p 是元组，写成 JSON 读回是列表 → jsonl 回读校验恒失败，所有品种一行都没写进去。
    旧测试只比对内存里的行（还接受元组），没走真实写盘。"""
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS
    from undertow import shadow_cli as sc
    from undertow.collect import jsonl_ledger as jl
    C = lambda e, k, s, oi: NS(expiry=e, kind=k, strike=s, open_interest=oi, volume=1)
    snap = NS(spot=100.0, contracts=[C(date(2026, 9, 30), "P", 95, 700), C(date(2026, 9, 30), "C", 105, 500)])
    row = sc.expiry_profile_row("gold", "GLD", date(2026, 9, 29), snap, {"sha256": "x", "captured_at": 1.0},
                                datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc))
    p = tmp_path / "gold.jsonl"
    fz = lambda r: {k: v for k, v in r.items() if k not in ("recorded_at", "before_open")}
    assert jl.insert_frozen(p, row, key_field="key", frozen=fz) == "inserted"
    assert jl.insert_frozen(p, row, key_field="key", frozen=fz) == "exists"
    assert jl.load(p, "key")[0]["expiries"][0]["top_p"] == [[95, 700]]
