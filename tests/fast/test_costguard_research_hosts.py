"""Cost Guard declarations for Account 3's research hosts (integration request A1-4). No network.

Wikidata is declared by Account 1 (registry/components.yaml, id `wikidata`). GDELT is declared by Account 3 on its own
branch (id `gdelt-doc`); these tests hold on Account 1's branch alone AND after the three-account merge.
"""
import pytest
import yaml

from nexus import config, costguard


def _raw():
    return yaml.safe_load((config.REGISTRY_DIR / "components.yaml").read_text(encoding="utf-8"))["components"]


def test_component_ids_are_unique():
    ids = [c["id"] for c in _raw()]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})   # manifest() would silently keep the last


def test_wikidata_is_a_declared_zero_cost_host(home):
    c = costguard.manifest()["wikidata"]
    assert costguard.violations(c) == [] and c["monetary_cost_usd"] == 0 and c["payment_method_required"] is False
    assert costguard.check_host("https://www.wikidata.org/w/api.php?action=wbsearchentities") == "wikidata"
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://query.wikidata.org/sparql")                       # only the host that is used


def test_gdelt_if_declared_is_declared_once_and_zero_cost(home):
    owners = [c for c in costguard.manifest().values() if "api.gdeltproject.org" in c.get("network_hosts", [])]
    assert len(owners) <= 1
    if owners:                                                                           # after the Account 3 merge
        assert costguard.violations(owners[0]) == []
        assert costguard.check_host("https://api.gdeltproject.org/api/v2/doc/doc") == owners[0]["id"]
    else:                                                                                # Account 1 branch alone
        with pytest.raises(costguard.CostGuardError):
            costguard.check_host("https://api.gdeltproject.org/api/v2/doc/doc")


def test_paid_hosts_stay_blocked_even_if_declared(home):
    bad = {**costguard.manifest()["wikidata"], "network_hosts": ["www.wikidata.org", "api.openai.com"]}
    assert any("paid endpoint" in v for v in costguard.violations(bad))
    with pytest.raises(costguard.CostGuardError):
        costguard.check_host("https://api.openai.com/v1/chat/completions")
