"""重复日线不得变成额外交易日，冲突不可任意覆盖。"""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace as N

import pytest

from undertow.collect.base import DataSourceError
from undertow.collect.cboe_history import CboeHistorySource


def fetch(rows):
    cache = N(get=lambda key, ttl: {"data": rows})
    inst = N(key="test", price=N(symbol="TEST"))
    return CboeHistorySource(cache).fetch_series(inst)


def row(day="2024-12-31"):
    return {"date": day, "open": 26.22, "high": 26.44,
            "low": 26.22, "close": 26.33, "volume": 8002260}


def test_identical_ohlcv_duplicate_is_one_session():
    original = row()
    raw = [row("2025-01-02"), original, deepcopy(original)]
    before = deepcopy(raw)
    result = fetch(raw)
    assert result.dates == [date(2024, 12, 31), date(2025, 1, 2)]
    assert result.closes == [26.33, 26.33]
    assert raw == before, "保留源记录，不改缓存 payload"


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
def test_conflicting_same_day_ohlcv_fails_loudly(field):
    conflict = row()
    conflict[field] += 1
    with pytest.raises(DataSourceError, match="同日 OHLCV 冲突.*2024-12-31"):
        fetch([row(), conflict])


def test_conflict_is_not_hidden_by_a_malformed_duplicate():
    conflict = row()
    conflict["close"] = "invalid"
    with pytest.raises(DataSourceError, match="同日 OHLCV 冲突"):
        fetch([conflict, row()])
