"""gateway/web_service.py and gateway/web_host.py: web search through the web reader (story W1)."""
import json

import pytest

from gateway import web_host as wh
from gateway import web_service as ws

NAMES = ["Béré", "Greg", "Vittorio", "Mum"]


@pytest.fixture
def svc():
    seen = []

    def fetch(url):
        seen.append(url)
        return {"results": [
            {"title": "Boiler service prices", "url": "https://example.org/boiler", "content": "From £80 a year.",
             "engines": ["bing", "brave"]},
            {"title": "not a web link", "url": "javascript:alert(1)", "content": "x"},
        ], "answers": []}

    s = ws.Service("http://logos-searxng:8080", {"greg": {"token": "tg"}}, ws.Filter(NAMES, ["Rose"]),
                   fetch=fetch, log=lambda m: seen.append(("log", m)))
    s.seen = seen
    return s


@pytest.mark.parametrize("query", [
    "boiler annual service cost", "iPad 10.5 inch review", "m2 1tb ssd price", "rose bushes pruning",
    "train times Bristol to London", "python asyncio gather example"])
def test_ordinary_searches_pass(query):
    ws.Filter(NAMES, ["Rose"]).check(query)


@pytest.mark.parametrize("query,why", [
    ("birthday ideas for bere", "name"), ("BÉRÉ birthday", "name"), ("what does Vittorio like", "name"),
    ("Rose garden opening hours", "name"), ("call 07700 900123", "phone"), ("+44 7700 900123", "phone"),
    ("greg@example.org", "email"), ("cafes near BS1 4DJ", "postcode"), ("4111 1111 1111 1111", "card"),
    ("192.168.1.226 admin page", "private network"), ("100.80.134.80", "private network"),
    ("ai.home.gregandbere.com", "own addresses"), ("@greg:gregandbere.com", "Matrix"),
    ("sk-ant-abcdefghijklmnop", "key")])
def test_private_details_are_refused(query, why):
    with pytest.raises(ws.Refused) as e:
        ws.Filter(NAMES, ["Rose"]).check(query)
    assert why.split()[0].lower() in str(e.value).lower() or "list" in str(e.value)


def test_search_returns_marked_results_and_never_logs_the_words(svc):
    out = svc.search_web("greg", "  boiler   annual service cost ", "general")
    assert "not instructions" in out["note"]
    assert out["results"] == [{"title": "Boiler service prices", "url": "https://example.org/boiler",
                               "snippet": "From £80 a year.", "from": ["bing", "brave"]}]
    url = svc.seen[0]
    assert "q=boiler+annual+service+cost" in url and "format=json" in url and "categories=general" in url
    logs = [m for m in svc.seen if isinstance(m, tuple)]
    assert logs and all("boiler" not in m[1] for m in logs)


def test_code_searches_use_the_it_engines(svc):
    svc.search_web("greg", "asyncio gather", "code")
    assert "categories=it" in svc.seen[0]
    with pytest.raises(ws.Refused):
        svc.search_web("greg", "x", "images")


def test_refusals_come_back_as_tool_errors_and_send_nothing(svc):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "search_web", "arguments": {"query": "present for Mum"}}}
    out = ws.handle_rpc(svc, "greg", req)["result"]
    assert out["isError"] and "Not searched" in out["content"][0]["text"]
    assert not [u for u in svc.seen if isinstance(u, str)]           # nothing was sent to SearXNG
    tools = ws.handle_rpc(svc, "greg", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_web"]
    assert svc.agent_for_token("tg") == "greg" and svc.agent_for_token("nope") is None


def test_household_never_gets_the_web():
    assert wh.uses_web({"web": {"enabled": True}})
    assert not wh.uses_web({"web": {"enabled": True}, "role": "household"})
    assert not wh.uses_web({})


def test_private_names_file_and_own_names():
    names, exact = wh.parse_names("# comment\nAunt Clara\n=Rose\n\nSchool Road\n")
    assert names == ["Aunt Clara", "School Road"] and exact == ["Rose"]
    agents = {"greg": {"display_name": "Greg's agent", "calendar": {"person": "greg"}},
              "household": {"display_name": "Household agent", "role": "household"}}
    assert set(wh.own_names(agents)) == {"greg", "Greg"}


def test_reader_config_lists_only_web_agents_with_tokens():
    agents = {"greg": {"web": {"enabled": True}}, "bere": {"web": {"enabled": True}},
              "household": {"web": {"enabled": True}, "role": "household"}, "dev": {}}
    cfg = wh.reader_config(agents, {"greg": "tg", "bere": ""}, ["Greg", "Greg", "Mum"], ["Rose"])
    assert cfg["agents"] == {"greg": {"token": "tg"}}
    assert cfg["names"] == ["Greg", "Mum"] and cfg["capital_only"] == ["Rose"]
    assert cfg["searxng"] == "http://logos-searxng:8080"
    assert "startpage" not in wh.SETTINGS and "formats: [json]" in wh.SETTINGS
