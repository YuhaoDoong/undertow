import gzip
import json

from scripts import flow_accuracy_availability as fa


def test_classify_preopen_after_open_unknown():
    T = "2026-10-15"                                             # 09:30 EDT = 13:30Z
    assert fa.classify(["2026-10-15T05:00:00+00:00", "2026-10-14T06:00:00+00:00"], T) == "preopen"
    assert fa.classify(["2026-10-15T13:31:00+00:00", "2026-10-14T06:00:00+00:00"], T) == "after_open"
    assert fa.classify(["2026-10-15T05:00:00+00:00", None], T) == "unknown"
    assert fa.classify([], T) == "unknown"                       # 缺 prev_date：不猜


def test_open_et_tracks_dst():
    assert fa.open_et("2026-10-15").utcoffset().total_seconds() == -4 * 3600
    assert fa.open_et("2026-12-15").utcoffset().total_seconds() == -5 * 3600


def test_captured_at_parsing():
    raw = gzip.compress(json.dumps({"captured_at": 1792057969.7}).encode())
    assert fa.captured_at(raw).startswith("2026-10-15T")
    assert fa.captured_at(gzip.compress(b'{"captured_at": null}')) is None
    assert fa.captured_at(b"not gzip") is None and fa.captured_at(None) is None
