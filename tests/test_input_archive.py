"""研报输入存档：各格式取尾部、同日内容不变不重复、月度全量只存一次、跳过期权链、坏文件隔离、未知格式整份存。"""
import gzip
import json

from undertow.collect import input_archive as ia


def _w(d, name, obj):
    (d / f"{name}.json").write_text(json.dumps(obj))


def _read(p):
    return json.loads(gzip.decompress(p.read_bytes()))


def _cache(tmp):
    c = tmp / "cache"; c.mkdir()
    _w(c, "cboehist_GLD", {"fetched_at": 1.0, "data": {"symbol": "GLD", "data": [{"date": f"d{i}", "close": i} for i in range(100)]}})
    _w(c, "fred_DFII10", {"fetched_at": 1.0, "data": "DATE,VAL\n" + "\n".join(f"2026-01-{i:02d},{i}" for i in range(1, 61))})
    _w(c, "cboevol_GVZ", {"fetched_at": 1.0, "data": "DATE,OPEN\n2026-09-25,22.4"})
    _w(c, "cot_legacy_fut_1", {"fetched_at": 1.0, "data": [{"report_date_as_yyyy_mm_dd": f"2026-{m:02d}-01"} for m in (3, 1, 2)]})
    _w(c, "yahoo_GC_F_1y", {"fetched_at": 1.0, "data": {"chart": {"result": [{"meta": {"symbol": "GC=F"},
        "timestamp": list(range(80)), "indicators": {"quote": [{"close": list(range(80))}]}}]}}})
    _w(c, "cboe_GLD", {"fetched_at": 1.0, "data": {"big": "chain"}})
    _w(c, "weird_thing", {"x": 1})
    return c


def test_tails_by_format_and_skip_options(tmp_path):
    c = _cache(tmp_path)
    r = ia.archive("2026-09-28", cache_dir=c, out_dir=tmp_path / "out")
    assert r["issues"] == [] and r["stats"]["skipped_options"] == 1 and r["stats"]["unknown_full"] == 1
    d = _read(tmp_path / "out" / "daily" / "2026-09-28.json.gz")["files"]
    assert "cboe_GLD" not in d
    assert d["cboehist_GLD"][0]["n_rows"] == 100 and len(d["cboehist_GLD"][0]["tail"]) == ia.TAIL
    assert d["cboehist_GLD"][0]["tail"][-1]["close"] == 99
    assert d["fred_DFII10"][0]["tail"][0] == "DATE,VAL" and d["fred_DFII10"][0]["tail"][-1] == "2026-01-60,60"
    assert [x["report_date_as_yyyy_mm_dd"] for x in d["cot_legacy_fut_1"][0]["tail"]] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    y = d["yahoo_GC_F_1y"][0]
    assert y["kind"] == "yahoo" and y["n_rows"] == 80 and y["tail"]["indicators"]["quote"][0]["close"][-1] == 79
    assert d["weird_thing"][0]["kind"] == "unknown_full" and d["weird_thing"][0]["tail"] == {"x": 1}
    m = _read(tmp_path / "out" / "monthly" / "2026-09" / "cboehist_GLD.json.gz")
    assert len(m["raw"]["data"]["data"]) == 100


def test_same_day_only_new_versions_and_monthly_once(tmp_path):
    c = _cache(tmp_path)
    out = tmp_path / "out"
    ia.archive("2026-09-28", cache_dir=c, out_dir=out)
    r2 = ia.archive("2026-09-28", cache_dir=c, out_dir=out)
    assert r2["stats"]["new_versions"] == 0 and r2["stats"]["monthly_new"] == 0
    _w(c, "fred_DFII10", {"fetched_at": 2.0, "data": "DATE,VAL\n2026-01-01,1.5"})    # 修订
    r3 = ia.archive("2026-09-28", cache_dir=c, out_dir=out)
    assert r3["stats"]["new_versions"] == 1
    v = _read(out / "daily" / "2026-09-28.json.gz")["files"]["fred_DFII10"]
    assert len(v) == 2 and v[0]["sha256"] != v[1]["sha256"]                          # 修订前后两个版本都在
    m = _read(out / "monthly" / "2026-09" / "fred_DFII10.json.gz")
    assert "2026-01-60" in m["raw"]["data"]                                           # 月度全量不被覆盖


def test_corrupt_daily_is_quarantined_not_overwritten(tmp_path):
    c = _cache(tmp_path)
    out = tmp_path / "out"
    (out / "daily").mkdir(parents=True)
    bad = out / "daily" / "2026-09-28.json.gz"
    bad.write_bytes(b"not gzip")
    r = ia.archive("2026-09-28", cache_dir=c, out_dir=out)
    assert any("已隔离" in i for i in r["issues"])
    assert list((out / "daily").glob("2026-09-28.json.gz.corrupt-*"))[0].read_bytes() == b"not gzip"


def test_unreadable_cache_file_is_an_issue(tmp_path):
    c = _cache(tmp_path)
    (c / "fred_BROKEN.json").write_text("{not json")
    r = ia.archive("2026-09-28", cache_dir=c, out_dir=tmp_path / "out")
    assert any("fred_BROKEN" in i for i in r["issues"])


def test_daily_update_runs_archive_after_report():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "daily_update.sh").read_text("utf-8")
    assert src.index("undertow report gold") < src.index("archive-inputs") < src.index("publish_dirs \"每日自动更新")
