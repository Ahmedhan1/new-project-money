"""Research sources that are unreachable, undeclared or keyless degrade to an explicit unavailable/unknown result.
No network: the context's fetchers raise the errors a real outage produces."""
from nexus.intelligence.market import scoring
from nexus.intelligence.market import sources as src


def ctx(exc=None, host_ok=lambda u: (True, "declared (test)"), key=None):
    def boom(url, params=None):
        raise exc or src.net.RemoteUnavailable("connection reset by peer")
    return src.Ctx(fetch_json=boom, fetch_text=boom, host_ok=host_ok, youtube_key=lambda: key, settings={}, min_interval=0,
                   rate_limit_wait=0)


def test_unreachable_gdelt_is_an_error_result_and_freshness_is_unknown():
    g = src.gdelt_timeline(ctx(), "aswan dam")
    assert g["status"] == "error" and "connection reset" in g["reason"] and g["items"] == []
    f = scoring.freshness(g, None, "2026-11-13")
    assert f["value"] is None


def test_undeclared_host_is_skipped_not_called():
    c = ctx(host_ok=lambda u: (False, "host not declared in components.yaml"))
    g = src.gdelt_timeline(c, "x")
    assert g["status"] == "skipped" and c.calls == []


def test_youtube_competition_without_owner_key_is_unknown_never_estimated():
    yt = src.youtube_competition(ctx(), "x")
    assert yt["status"] == "disabled"
    comp = scoring.competition(yt)
    assert comp["value"] is None and "never guessed" in comp["method"]


def test_youtube_with_key_but_host_down_is_an_error_not_zero_competition():
    yt = src.youtube_competition(ctx(key="k"), "x")
    assert yt["status"] == "error" and scoring.competition(yt)["value"] is None


def test_wikipedia_outage_leaves_demand_unknown():
    pv = src.wiki_pageviews(ctx(OSError("network unreachable")), "Aswan Dam")
    assert pv["status"] == "error"
    assert scoring.demand(pv, {})["value"] is None
