"""Analytics import quality: data-origin labels, dates and time zones, duplicates, invalid values, reconciliation.
All inputs are synthetic test data (labelled simulated_test_data where stored). No network."""
from pathlib import Path

import pytest

from nexus.intelligence.publishing import analytics

FIX = Path(__file__).parent / "fixtures_publishing"
HDR = "Content,Video title,Video publish time,Views,Watch time (hours),Average percentage viewed (%),Subscribers,Likes\n"


def test_every_record_says_where_it_came_from(home):
    r = analytics.import_csv(FIX / "youtube_studio_table.csv", "youtube_studio", period="2026-10", origin="simulated_test_data")
    assert r["data_origin"] == "simulated_test_data" and {x["data_origin"] for x in r["records"]} == {"simulated_test_data"}
    assert analytics.real_only(analytics.records("youtube")) == []                 # simulated rows never count as real
    assert analytics.origin_summary(analytics.records()) == {"simulated_test_data": 2}
    with pytest.raises(ValueError):
        analytics.import_csv(FIX / "youtube_studio_table.csv", "youtube_studio", origin="probably_real")
    owner = analytics.import_csv(HDR + "v1,t,2026-10-03,5,,,,\n", "youtube_studio", period="2026-11", persist=False)
    assert owner["records"][0]["data_origin"] == "user_uploaded_data"           # an owner export is the default


@pytest.mark.parametrize("raw, iso", [("2026-10-03", "2026-10-03"), ("2026/10/03", "2026-10-03"), ("Oct 3, 2026", "2026-10-03"),
                                      ("3 Oct 2026", "2026-10-03"), ("01/02/2026", None), ("2026-02-30", None), ("", None)])
def test_dates_are_normalised_only_when_unambiguous(raw, iso):
    assert analytics.norm_date(raw) == iso


def test_publish_times_convert_to_utc_only_with_a_stated_time_zone():
    la = analytics.to_utc("2026-10-03T20:00:00", "America/Los_Angeles")
    if analytics.zone("America/Los_Angeles") is None:      # e.g. Windows without tzdata: no tz database -> not converted
        assert la is None
    else:
        assert la == "2026-10-04T03:00:00Z"
    assert analytics.to_utc("2026-10-03T20:00:00", "Not/AZone") is None
    assert analytics.to_utc("2026-10-03T20:00:00+02:00", None) == "2026-10-03T18:00:00Z"
    assert analytics.to_utc("2026-10-03T20:00:00", None) is None                   # unknown zone: not guessed
    r = analytics.import_csv(HDR + 'v1,t,"Oct 3, 2026",5,,,,\n', "youtube_studio", period="2026-10", persist=False)
    assert r["records"][0]["published_date"] == "2026-10-03" and r["records"][0]["published_utc"] is None
    assert r["records"][0]["published_tz"].startswith("unknown")


def test_duplicates_invalid_and_missing_values_are_reported_not_fixed(home):
    text = HDR + "v1,t,,10,1.5,45,3,2\nv1,t,,99,,,,\nv2,t,,-5,abc,140,-2,\nv3,t,,,,,,\n"
    r = analytics.import_csv(text, "youtube_studio", period="2026-10", origin="simulated_test_data")
    assert r["imported"] == 3 and r["duplicates"] == [{"row": 3, "video_id": "v1", "date": "2026-10", "first_row": 2, "kept": "first"}]
    bad = {(x["row"], x["column"], x["reason"]) for x in r["invalid"]}
    assert bad == {(4, "views", "out of range"), (4, "watch time (hours)", "not a number"), (4, "average percentage viewed (%)", "out of range")}
    v = {x["video_id"]: x["metrics"] for x in r["records"]}
    assert v["v1"]["views"] == 10 and v["v2"] == {"subscribers_net": -2} and v["v3"] == {}   # missing stays missing
    again = analytics.import_csv(text, "youtube_studio", period="2026-10", origin="simulated_test_data")
    assert again["replaced_existing"] == 3 and len(analytics.records("youtube")) == 3


def test_reconciliation_against_the_exports_total_row():
    r = analytics.import_csv(FIX / "youtube_studio_table.csv", "youtube_studio", period="2026-10", persist=False, origin="simulated_test_data")
    rec = r["reconciliation"]
    assert rec["metrics"]["views"] == {"rows_sum": 1234, "total_row": 1234, "diff": 0, "ok": True}
    assert rec["status"] == "ok"
    short = analytics.import_csv("Content,Views\nTotal,500\nv1,100\n", "youtube_studio", period="2026-10", persist=False)
    assert short["reconciliation"]["status"] == "mismatch" and short["reconciliation"]["metrics"]["views"]["diff"] == -400
    assert analytics.import_csv("Content,Views\nv1,1\n", "youtube_studio", persist=False)["reconciliation"] == {"status": "no_total_row"}


def test_api_rows_vs_export_reconciliation():
    api = [{"metrics": {"views": 400}}, {"metrics": {"views": 590}}]
    assert analytics.reconcile_api_vs_export(api, {"metrics": {"views": 1000}})["status"] == "ok"           # within 2 %
    assert analytics.reconcile_api_vs_export(api, {"metrics": {"views": 2000}})["status"] == "mismatch"
    assert analytics.reconcile_api_vs_export(api, {"metrics": {}})["status"] == "missing_in_export"


def test_simulated_rows_never_conclude_an_experiment():
    from nexus.intelligence.growth import experiments as ex
    import importlib
    g = importlib.import_module("test_intel_growth")
    e = ex.create("hook", "hook", [{"id": "A"}, {"id": "B", "params": {"hook": "q"}}], "youtube", "avg_view_pct", baseline=0.0, mde=0.8)
    rows = [dict(g.rec(f"{arm}{i}", variant=arm, avg_view_pct=(60.0 if arm == "B" else 40.0) + i % 3), data_origin="simulated_test_data")
            for arm in "AB" for i in range(60)]
    r = ex.evaluate(e, rows)
    assert r["results"]["real_data"] is False and r["results"]["data_origin"] == ["simulated_test_data"]
    assert r["status"] not in ("concluded", "inconclusive") and "SIMULATED" in r["results"]["note"]
