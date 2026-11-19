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
    assert out["isError"] and "Not done" in out["content"][0]["text"]
    assert not [u for u in svc.seen if isinstance(u, str)]           # nothing was sent to SearXNG
    tools = ws.handle_rpc(svc, "greg", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_web", "add_private_name"]
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


def test_agents_can_add_private_names_but_not_junk(svc, tmp_path):
    added = tmp_path / "added.txt"
    s = ws.Service("http://x", {"greg": {"token": "t"}}, ws.Filter(NAMES), fetch=lambda u: {"results": []},
                   log=lambda m: None, added_file=str(added))
    assert s.add_private_name("greg", "Aunt  Clara") == {"added": "Aunt Clara", "note": "Searches containing it are now refused."}
    with pytest.raises(ws.Refused):
        s.search_web("greg", "birthday ideas for aunt clara")
    assert s.add_private_name("greg", "aunt clara") == {"already_private": "aunt clara"}
    for junk in ("x", "1234", "drop table; --", "a b c d e f g", "", "x" * 70):
        with pytest.raises(ws.Refused):
            s.add_private_name("greg", junk)
    assert added.read_text().startswith("Aunt Clara\tgreg\t")
    again = ws.Service("http://x", {}, ws.Filter([]), log=lambda m: None, added_file=str(added))   # kept across restarts
    with pytest.raises(ws.Refused):
        again.filter.check("aunt clara")
    out = ws.handle_rpc(s, "greg", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "add_private_name", "arguments": {"name": "St Mary's School"}}})
    assert "added" in out["result"]["content"][0]["text"]
    tools = ws.handle_rpc(s, "greg", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_web", "add_private_name"]


# ── weather for the morning briefing (story C4) ─────────────────────────────

FORECAST = {"daily": {"weather_code": [61], "temperature_2m_min": [6.6], "temperature_2m_max": [11.6],
                      "precipitation_probability_max": [70], "wind_speed_10m_max": [31.2]}}


def test_location_is_rounded_to_about_ten_km():
    assert wh.parse_location("# where we are\n51.5074, -0.1278\n") == {"lat": 51.5, "lon": -0.1}
    assert wh.parse_location("51.46 -0.21") == {"lat": 51.5, "lon": -0.2}
    assert wh.parse_location("London") is None and wh.parse_location("95, 0") is None
    url = ws.weather_url(51.5074, -0.1278)
    assert "latitude=51.5&" in url and "longitude=-0.1&" in url and url.startswith(ws.OPEN_METEO)
    cfg = wh.reader_config({"greg": {"web": {"enabled": True}}}, {"greg": "t"}, [], [], {"lat": 51.5, "lon": -0.1})
    assert cfg["weather"] == {"lat": 51.5, "lon": -0.1}
    assert "weather" not in wh.reader_config({}, {}, [], [])


def test_weather_line():
    assert ws.weather_line(FORECAST) == "Light rain, 7 to 12°C, 70% chance of rain, windy (up to 31 mph)"
    calm = {"daily": {**FORECAST["daily"], "weather_code": [0], "wind_speed_10m_max": [8]}}
    assert ws.weather_line(calm) == "Clear sky, 7 to 12°C, 70% chance of rain"


def test_weather_is_cached_for_an_hour_and_survives_an_outage():
    calls, logs = [], []

    def fetch(url):
        calls.append(url)
        if len(calls) == 3:
            raise OSError("down")
        return FORECAST

    s = ws.Service("http://x", {"greg": {"token": "t"}}, ws.Filter([]), fetch=fetch, log=logs.append)
    assert s.weather("greg") is None and not calls          # no place set: nothing asked
    s.place = {"lat": 51.5, "lon": -0.1}
    assert s.weather("greg", now=1000).startswith("Light rain")
    assert s.weather("greg", now=2000).startswith("Light rain") and len(calls) == 1
    assert s.weather("greg", now=5000) and len(calls) == 2
    assert s.weather("greg", now=9000).startswith("Light rain")   # Open-Meteo down: the last forecast
    assert any("weather FAILED" in m for m in logs)


def test_weather_endpoint_needs_an_agent_token(svc):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    svc.fetch = lambda url: FORECAST
    server = ThreadingHTTPServer(("127.0.0.1", 0), ws.make_weather_handler(svc))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path, token=None):
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert get("/weather")[0] == 401 and get("/weather", "wrong")[0] == 401
        assert get("/weather", "tg")[0] == 503                  # no place configured yet
        svc.place = {"lat": 51.5, "lon": -0.1}
        assert get("/weather", "tg") == (200, {"today": "Light rain, 7 to 12°C, 70% chance of rain, windy (up to 31 mph)"})
        assert get("/other", "tg")[0] == 404
    finally:
        server.shutdown()
