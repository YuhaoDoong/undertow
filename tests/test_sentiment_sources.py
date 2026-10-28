"""情绪面板 v0 来源/时间包：COT 严格序列（真实类别、缺失不折零）与 CBOE 原文身份。"""
from __future__ import annotations

from datetime import date

import pytest


def test_strict_cot_series_real_category_and_no_zero_fold():
    from types import SimpleNamespace
    from undertow.collect.cftc_cot import strict_category_series
    reps = [SimpleNamespace(report_date=date(2026, 10, 20),
                            raw={"noncomm_positions_long_all": "86099", "noncomm_positions_short_all": "",
                                 "open_interest_all": "272462"})]
    s = strict_category_series(reps, "legacy_fut")[0]
    assert s["category_actual"] == "noncommercial" and s["long"] == 86099
    assert s["short"] is None                       # 缺失不折零
    reps = [SimpleNamespace(report_date=date(2026, 10, 20),
                            raw={"m_money_positions_long_all": "16180", "m_money_positions_short_all": "12675",
                                 "open_interest_all": "113992", "change_in_m_money_long_all": "378"})]
    s = strict_category_series(reps, "disaggregated_fut")[0]
    assert s["category_actual"] == "managed_money" and s["change_long"] == 378 and s["change_short"] is None


def test_cboe_fetch_with_identity_hash_and_time(monkeypatch, tmp_path):
    from undertow.collect import cboe_vol
    from undertow.collect.cache import FileCache
    text = "DATE,VALUE\n10/26/2026,18.39\n10/27/2026,17.52\n"
    monkeypatch.setattr(cboe_vol, "http_get_text", lambda url, headers=None: text)
    src = cboe_vol.CboeVolSource(cache=FileCache(root=tmp_path))
    a = src.fetch_with_identity("VIX", use_cache=False)
    assert a["status"] == "fresh_fetch" and a["series"][-1] == (date(2026, 10, 27), 17.52)
    b = src.fetch_with_identity("VIX", use_cache=True)
    assert b["status"] == "cache_hit" and b["sha256"] == a["sha256"]
    assert b["fetched_at"] == pytest.approx(a["fetched_at"], abs=1.0)
