from tv_aggregator.normalizer import canonical_api, extract_sites, merge_sites, parse_config


def test_canonical_api_removes_fragment_and_normalizes_query():
    assert canonical_api("HTTPS://Example.COM/api/?b=2&a=1#fragment") == "https://example.com/api?a=1&b=2"


def test_extracts_tvbox_and_json5_sites_without_breaking_urls():
    payload = """
    {
      // comment
      sites: [
        {"key": "a", "name": "A", "api": "https://example.com/a",},
        {"key": "b", "name": "B", "api": "csp_B"}
      ]
    }
    """
    sites = extract_sites(parse_config(payload))
    assert [site["api"] for site in sites] == ["https://example.com/a", "csp_B"]


def test_merge_keeps_provenance_for_same_api():
    first = {"key": "a", "name": "源 A", "api": "https://EXAMPLE.com/api/", "source": {"id": "one", "name": "一"}}
    second = {"key": "b", "name": "源 B", "api": "https://example.com/api", "source": {"id": "two", "name": "二"}}
    merged = merge_sites([first, second])
    assert len(merged) == 1
    assert merged[0]["sourceCount"] == 2
    assert {source["id"] for source in merged[0]["sources"]} == {"one", "two"}
