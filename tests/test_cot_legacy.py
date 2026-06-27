"""Legacy COT 报告映射的单元测试（合成记录，不依赖网络）。

验证金融期货（如美元指数）走 Legacy 报告时，非商业/商业/非报告 三类被正确
映射到模型槽位（managed_money/producer_merchant/nonreportable），且 Legacy
未细分的 swap/other 留空。锁住 CFTC 的历史拼写字段名（postions/spead）。
运行: python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from undertow.core.config import CotSpec, Instrument
from undertow.collect.cftc_cot import CftcCotSource

# 一条合成的 Legacy 记录（字段名照搬 CFTC 6dca-aqww，含历史拼写错误）
_LEGACY_REC = {
    "report_date_as_yyyy_mm_dd": "2026-06-23T00:00:00.000",
    "market_and_exchange_names": "USD INDEX - ICE FUTURES U.S.",
    "open_interest_all": "54908",
    "change_in_open_interest_all": "5497",
    "noncomm_positions_long_all": "34278",
    "noncomm_positions_short_all": "21350",
    "noncomm_postions_spread_all": "1551",          # 注意 CFTC 拼写 postions
    "comm_positions_long_all": "14069",
    "comm_positions_short_all": "30315",
    "nonrept_positions_long_all": "5010",
    "nonrept_positions_short_all": "1692",
    "change_in_noncomm_long_all": "3098",
    "change_in_noncomm_short_all": "3367",
    "change_in_noncomm_spead_all": "204",           # 注意 CFTC 拼写 spead
    "change_in_comm_long_all": "538",
    "change_in_comm_short_all": "1657",
    "change_in_nonrept_long_all": "1657",
    "change_in_nonrept_short_all": "269",
}


def _dxy_instrument() -> Instrument:
    return Instrument(
        key="dxy",
        display_name="美元指数 US Dollar Index (ICE)",
        asset_class="fx",
        cot=CotSpec(report="legacy_fut", contract_market_code="098662",
                    market_name="USD INDEX - ICE FUTURES U.S."),
    )


def test_legacy_maps_noncommercial_to_managed_money():
    rep = CftcCotSource()._parse(_dxy_instrument(), _LEGACY_REC)
    # 非商业(大投机) -> managed_money 槽位
    assert rep.managed_money.long == 34278 and rep.managed_money.short == 21350
    assert rep.managed_money.net == 34278 - 21350
    assert rep.managed_money.spread == 1551           # 历史拼写 postions 正确解析
    # 商业(套保) -> producer_merchant 槽位
    assert rep.producer_merchant.long == 14069 and rep.producer_merchant.short == 30315
    assert rep.producer_merchant.net == 14069 - 30315
    # 非报告小户
    assert rep.nonreportable.net == 5010 - 1692
    # Legacy 不细分的两类留空
    assert rep.other_reportables.net == 0 and rep.other_reportables.gross == 0
    assert rep.swap_dealers.net == 0 and rep.swap_dealers.gross == 0
    # OI 与周变化
    assert rep.open_interest == 54908 and rep.open_interest_change == 5497


def test_legacy_change_fields_parsed():
    rep = CftcCotSource()._parse(_dxy_instrument(), _LEGACY_REC)
    chg = rep.changes["managed_money"]
    assert chg.long == 3098 and chg.short == 3367
    assert chg.spread == 204                           # 历史拼写 spead 正确解析
    assert chg.net == 3098 - 3367
    # 空类别的变化也为 0，不报错
    assert rep.changes["swap_dealers"].net == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")
