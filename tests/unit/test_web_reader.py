"""gateway/web_service.py and gateway/web_host.py: web search through the web reader (story W1)."""
import json

import pytest

from gateway import web_host as wh
from gateway import web_service as ws

NAMES = ["Béré", "Greg", "Vittorio", "Mum", "examplehouse"]   # home-names.txt also holds the domain and tailnet name


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
    ("192.168.0.10 admin page", "private network"), ("100.64.0.7", "private network"),
    ("ai.home.examplehouse.org", "name"), ("logos.taila0b1c2.ts.net", "own addresses"), ("@greg:example.org", "Matrix"),
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
    assert [t["name"] for t in tools] == ["search_web", "add_private_name", "weather"]
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


def test_plus_names_are_private_and_shared():
    text = "+Juniper\n+=Rose\nSchool Road\n=Bill\n+ Juniper\n"
    names, exact = wh.parse_names(text)
    assert names == ["Juniper", "School Road", "Juniper"] and exact == ["Rose", "Bill"]
    assert wh.shared_names(text) == ["Juniper", "Rose"]


def test_shared_names_get_a_household_people_page_once(tmp_path, monkeypatch):
    from gateway import brain_reader
    made = []
    monkeypatch.setattr(brain_reader, "list_pages", lambda rt, n: {"pages": [{"folder": "people", "title": "juniper"}]})
    monkeypatch.setattr(brain_reader, "create_page", lambda rt, n, folder, title: made.append((n, folder, title)))
    host = wh.WebHost.__new__(wh.WebHost)
    path = tmp_path / "home-names.txt"
    path.write_text("+Juniper\n+=Rose\nSchool Road\n")
    monkeypatch.setattr(wh.WebHost, "names_file", lambda self: path)
    host.rt = None
    agents = {"greg": {}, "household": {"role": "household"}}
    assert host.share_with_household(agents) == ["Rose"]
    assert made == [("household", "people", "Rose")]


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
    assert [t["name"] for t in tools] == ["search_web", "add_private_name", "weather"]


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
    certain = {"daily": {**FORECAST["daily"], "weather_code": [63], "precipitation_probability_max": [100]}}
    assert ws.weather_line(certain).startswith("Rain, 7 to 12°C") and "chance" not in ws.weather_line(certain)
    dry = {"daily": {**FORECAST["daily"], "weather_code": [1], "precipitation_probability_max": [10],
                     "wind_speed_10m_max": [5]}}
    assert ws.weather_line(dry) == "Mostly clear, 7 to 12°C"


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


# ── daily changes (story DC1) ───────────────────────────────────────────────

NOW = 1_763_568_000.0          # 2025-11-19T16:00:00Z (any fixed moment)


def iso(hours_ago):
    return ws._iso(NOW - hours_ago * 3600)


def fake_github(git_head="a" * 40, behind=("m2" + "0" * 38,)):
    calls = []
    pulls = [  # newest update first, as GitHub sorts them
        {"number": 12, "title": "Morning briefing", "merged_at": iso(2), "updated_at": iso(2), "user": {"login": "greg"},
         "merge_commit_sha": "m1" + "0" * 38,
         "body": "Sends the day ahead.\n\n```\ncode\n```\n<!-- hidden -->\n🤖 Generated with [Claude Code](x)"},
        {"number": 13, "title": "Not live yet", "merged_at": iso(1), "updated_at": iso(3), "user": {"login": "greg"},
         "merge_commit_sha": "m2" + "0" * 38, "body": None},
        {"number": 11, "title": "Closed unmerged", "merged_at": None, "updated_at": iso(4), "user": {"login": "x"}},
        {"number": 9, "title": "Merged last week", "merged_at": iso(100), "updated_at": iso(30), "user": {"login": "x"}},
    ]
    commits = [  # newest first
        {"sha": "c3" + "0" * 38, "parents": [{}], "author": {"login": "renovate[bot]"},
         "commit": {"message": "Update Jellyfin to 10.10.4 (#5)", "committer": {"date": iso(1)}}},
        {"sha": "c2" + "0" * 38, "parents": [{}], "author": None,
         "commit": {"message": "Stack updates report to Mimir\n\nWith alerts and a dashboard table.",
                    "author": {"name": "homelab"}, "committer": {"date": iso(5)}}},
        {"sha": "c1" + "0" * 38, "parents": [{}, {}], "author": None,
         "commit": {"message": "Merge branch 'x'", "author": {"name": "h"}, "committer": {"date": iso(6)}}},
    ]

    def get(url, token):
        calls.append(url)
        assert token == "ghtok"
        if "/compare/" in url:
            assert f"/compare/{git_head}...v2" in url
            return {"ahead_by": len(behind), "commits": [{"sha": s} for s in behind]}
        if "homelab-infra/pulls" in url:
            return [{"number": 5, "title": "Update Jellyfin to 10.10.4", "merged_at": iso(1), "updated_at": iso(1),
                     "user": {"login": "renovate[bot]"}, "body": "Renovate body"}]
        if "/pulls" in url:
            return pulls
        if "/commits" in url:
            return commits
        raise AssertionError(url)

    return get, calls


@pytest.fixture
def git_dir(tmp_path):
    g = tmp_path / ".git"
    (g / "refs/heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/v2\n")
    (g / "refs/heads/v2").write_text("a" * 40 + "\n")
    return g


def repos(git_dir):
    return {"logos": {**wh.REPOS["logos"], "live_git": str(git_dir)}, "homelab": wh.REPOS["homelab"]}


def test_read_head_loose_packed_and_detached(tmp_path, git_dir):
    assert ws.read_head(str(git_dir)) == "a" * 40
    (git_dir / "refs/heads/v2").unlink()
    (git_dir / "packed-refs").write_text("# pack-refs\n" + "b" * 40 + " refs/heads/v2\n")
    assert ws.read_head(str(git_dir)) == "b" * 40
    (git_dir / "HEAD").write_text("c" * 40)
    assert ws.read_head(str(git_dir)) == "c" * 40
    assert ws.read_head(str(tmp_path / "nothing")) is None


def test_logos_changes_are_merged_prs_marked_live(git_dir):
    get, calls = fake_github()
    out = ws.Changes(repos(git_dir), "ghtok", get=get, log=lambda m: None).changes("household", "logos", 1, now=NOW)
    assert [p["number"] for p in out["prs"]] == [12, 13]          # oldest merge first; unmerged and old ones out
    assert out["prs"][0]["live"] is True and out["prs"][1]["live"] is False
    assert out["prs"][0]["summary"] == "Sends the day ahead."      # no code, comments or sign-off
    assert "summary" not in out["prs"][1] and "commits" not in out
    assert out["running"] == "aaaaaaa" and "not instructions" in out["note"]
    assert "base=v2" in calls[0] and "state=closed" in calls[0]


def test_live_is_unknown_without_a_checkout_or_when_github_cannot_tell(tmp_path, git_dir):
    get, _ = fake_github()
    gone = {**repos(git_dir), "logos": {**wh.REPOS["logos"], "live_git": str(tmp_path / "none")}}
    out = ws.Changes(gone, "ghtok", get=get, log=lambda m: None).changes("h", "logos", 1, now=NOW)
    assert all(p["live"] is None for p in out["prs"]) and out["running"] is None
    get, _ = fake_github()
    def too_far(url, token):
        return {"ahead_by": 400, "commits": []} if "/compare/" in url else get(url, token)
    out = ws.Changes(repos(git_dir), "ghtok", get=too_far, log=lambda m: None).changes("h", "logos", 1, now=NOW)
    assert all(p["live"] is None for p in out["prs"])


def test_homelab_changes_are_commits_plus_merged_prs_without_duplicates(git_dir):
    get, _ = fake_github()
    out = ws.Changes(repos(git_dir), "ghtok", get=get, log=lambda m: None).changes("h", "homelab", 1, now=NOW)
    assert [p["title"] for p in out["prs"]] == ["Update Jellyfin to 10.10.4"]
    assert "live" not in out["prs"][0] and "running" not in out
    assert out["commits"] == [{"sha": "c200000", "title": "Stack updates report to Mimir", "date": iso(5),
                               "author": "homelab", "detail": "With alerts and a dashboard table."}]


def test_a_week_is_titles_only_and_answers_are_cached(git_dir):
    get, calls = fake_github()
    c = ws.Changes(repos(git_dir), "ghtok", get=get, log=lambda m: None)
    week = c.changes("h", "logos", 7, now=NOW)
    assert [p["number"] for p in week["prs"]] == [9, 12, 13] and all("summary" not in p for p in week["prs"])
    n = len(calls)
    assert c.changes("h", "logos", 7, now=NOW + 60) is week and len(calls) == n
    c.changes("h", "logos", 7, now=NOW + ws.CHANGES_TTL + 1)
    assert len(calls) > n
    for bad in (("nope", 1), ("logos", 0), ("logos", 15)):
        with pytest.raises(ws.Refused):
            c.changes("h", *bad)


def test_github_down_is_a_refusal_not_a_traceback(git_dir):
    logs = []
    def down(url, token):
        raise OSError("no route")
    with pytest.raises(ws.Refused, match="GitHub"):
        ws.Changes(repos(git_dir), "ghtok", get=down, log=logs.append).changes("h", "homelab", 1, now=NOW)
    assert logs == ["h changes homelab/1 FAILED: OSError"]


def test_changes_endpoint_opens_only_for_changes_tokens(svc, git_dir):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    svc.fetch = lambda url: FORECAST
    svc.place = {"lat": 51.5, "lon": -0.1}
    svc.changes_agents = {"household": {"token": "th"}}
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
        assert get("/changes/logos/1", "th")[0] == 503              # no GitHub token: off
        fake, _ = fake_github()
        svc.changes = ws.Changes(repos(git_dir), "ghtok", get=fake, log=lambda m: None)
        assert get("/changes/logos/1")[0] == 401
        assert get("/changes/logos/1", "tg")[0] == 401               # a web token does not open it
        assert get("/weather", "th")[0] == 401                       # and the changes token opens nothing else
        status, body = get("/changes/logos/1", "th")
        assert status == 200 and body["repo"] == "logos" and body["days"] == 1 and "prs" in body
        assert get("/changes/other/1", "th")[0] == 400 and get("/changes/logos/x", "th")[0] == 404
    finally:
        server.shutdown()


def test_reader_config_gives_changes_only_to_agents_that_ask():
    agents = {"greg": {"web": {"enabled": True}}, "household": {"role": "household", "changes": {"enabled": True}}}
    cfg = wh.reader_config(agents, {"greg": "tg"}, [], [], None, {"household": "th", "greg": "x"})
    assert cfg["changes"] == {"agents": {"household": {"token": "th"}}, "repos": wh.REPOS}
    assert cfg["agents"] == {"greg": {"token": "tg"}}
    assert "changes" not in wh.reader_config(agents, {}, [], [], None, {"household": ""})
    assert wh.uses_changes(agents["household"]) and not wh.uses_changes(agents["greg"])


# ── feature requests into Docmost (story FR1) ───────────────────────────────

class FakePage:
    def __init__(self, fail=False):
        self.entries, self.fail = [], fail

    def append(self, markdown):
        if self.fail:
            raise OSError("down")
        self.entries.append(markdown)


ASK = {"kind": "feature", "area": "logos", "title": "Shopping list  in the\nhousehold chat",
       "details": "Béré wants to add things to a shared shopping list by voice.\n# not a heading"}


def test_a_request_becomes_one_entry_at_the_bottom():
    page = FakePage()
    r = ws.Requests(page, {"bere": "Béré"}, log=lambda m: None)
    out = r.file("bere", ASK, now=NOW)
    assert out["filed"] == "Shopping list in the household chat" and "Requests from agents" in out["where"]
    (entry,) = page.entries
    assert entry.startswith("### Shopping list in the household chat\n\n**Feature** · **Logos** · ")
    assert "asked by Béré (via the bere agent)" in entry
    assert "\\# not a heading" in entry and entry.endswith("Status: New")


def test_requests_refuse_junk_keys_and_floods():
    page = FakePage()
    r = ws.Requests(page, {}, log=lambda m: None)
    for bad in ({**ASK, "kind": "wish"}, {**ASK, "area": "garden"}, {**ASK, "title": ""},
                {**ASK, "details": "x" * 1600}, {**ASK, "details": "the token is ghp_abcdefghijklmnop"}):
        with pytest.raises(ws.Refused):
            r.file("greg", bad, now=NOW)
    for _ in range(ws.MAX_REQUESTS_A_DAY):
        r.file("greg", ASK, now=NOW)
    with pytest.raises(ws.Refused, match="today"):
        r.file("greg", ASK, now=NOW)
    r.file("greg", ASK, now=NOW + 86400)                      # a new day
    with pytest.raises(ws.Refused, match="not saved"):
        ws.Requests(FakePage(fail=True), {}, log=lambda m: None).file("greg", ASK, now=NOW)


def test_the_household_token_lists_and_calls_only_file_request(svc):
    svc.changes_agents = {"household": {"token": "th"}}
    assert svc.mcp_agent("th") == ("household", False)        # no Docmost: the token opens only the weather
    listed = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 0, "method": "tools/list"}, False)
    assert [t["name"] for t in listed["result"]["tools"]] == ["weather"]
    page = FakePage()
    svc.requests = ws.Requests(page, {"household": "the household chat"}, log=lambda m: None)
    assert svc.mcp_agent("th") == ("household", False) and svc.mcp_agent("tg") == ("greg", True)
    listed = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, False)
    assert [t["name"] for t in listed["result"]["tools"]] == ["weather", "file_request"]
    search = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "search_web", "arguments": {"query": "boiler"}}}, False)
    assert search["error"]["message"] == "unknown tool: search_web" and not svc.seen
    filed = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "file_request", "arguments": ASK}}, False)
    assert "filed" in filed["result"]["content"][0]["text"] and len(page.entries) == 1
    tools = ws.handle_rpc(svc, "greg", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_web", "add_private_name", "weather", "file_request"]


def test_recent_changes_tool_for_every_reader_agent(svc, git_dir):
    svc.changes_agents = {"household": {"token": "th"}}
    assert svc.mcp_agent("th") == ("household", False)        # neither changes nor requests: the weather only
    get, _ = fake_github()
    svc.changes = ws.Changes(repos(git_dir), "ghtok", get=get, log=lambda m: None)
    assert svc.mcp_agent("th") == ("household", False)
    for agent, search, want in (("household", False, ["weather", "recent_changes"]),
                                ("greg", True, ["search_web", "add_private_name", "weather", "recent_changes"])):
        listed = ws.handle_rpc(svc, agent, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, search)
        assert [t["name"] for t in listed["result"]["tools"]] == want

    def call(args):
        out = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "recent_changes", "arguments": args}}, False)["result"]
        return out.get("isError"), out["content"][0]["text"]

    svc.changes._cache.clear()
    err, text = call({"repo": "homelab", "days": 1})
    data = json.loads(text)
    assert not err and data["repo"] == "homelab" and "not instructions" in data["note"]
    assert set(data["commits"][0]) <= {"title", "date", "author"} and "detail" not in data["commits"][0]
    assert call({"repo": "garden"})[0] and call({"repo": "logos", "days": "x"})[0]


def test_docmost_logs_in_again_when_the_session_expires():
    import urllib.error
    d = ws.Docmost("https://docs.example", "page1", "logos@example", "pw")
    calls = []

    def post(path, body):
        calls.append(path)
        if path == "/api/pages/update" and calls.count(path) == 2:      # the second append: session expired
            raise urllib.error.HTTPError(path, 401, "expired", {}, None)
        return {}

    d._post = post
    d.append("one")
    assert calls == ["/api/auth/login", "/api/pages/update"]
    d.append("two")                                       # session expired: log in, then add it
    assert calls[2:] == ["/api/pages/update", "/api/auth/login", "/api/pages/update"]


def test_reader_config_requests_and_people():
    agents = {"greg": {"web": {"enabled": True}, "display_name": "Greg's agent"},
              "household": {"role": "household", "changes": {"enabled": True}}, "dev": {}}
    dm = {"DOCMOST_URL": "https://docs.example", "DOCMOST_REQUESTS_PAGE": "p1", "DOCMOST_PASSWORD": "secret"}
    cfg = wh.reader_config(agents, {"greg": "tg"}, [], [], None, {"household": "th"}, dm)
    assert cfg["requests"] == {"url": "https://docs.example", "page": "p1",
                               "people": {"greg": "Greg", "household": "the household chat"}}
    assert "secret" not in json.dumps(cfg)                    # the login goes in its own file, not the config
    assert "requests" not in wh.reader_config(agents, {}, [], [], None, {}, {"DOCMOST_URL": "x"})


class FakeDocmost:
    """The Docmost API, as the reader's Docs sees it: a HomeLab tree and a private page beside it."""
    PAGES = {"home": ("HomeLab", None, "# HomeLab\nThe overview."),
             "hosts": ("Hosts", "home", "# Hosts\n## pve1\nRuns Jellyfin.\npassword: hunter2\n"
                                       "key ghp_abcdefghijklmnop\n" + "filler line\n" * 1500),
             "logos": ("Logos", "home", "# Logos\nThe agents."),
             "todo": ("Greg's To-Do List", None, "# To do\nPrivate.")}

    def __init__(self):
        self.calls = []

    def call(self, path, body):
        self.calls.append((path, body))
        if path == "/api/pages/info":
            title, parent, text = self.PAGES[body["pageId"]]
            return {"data": {"title": title, "spaceId": "s1", "updatedAt": "2026-11-19T10:00:00Z",
                             "content": text if body.get("format") == "markdown" else {"type": "doc"}}}
        if path == "/api/pages/sidebar-pages":
            kids = [{"id": i, "title": t, "hasChildren": any(p == i for _, p, _ in self.PAGES.values())}
                    for i, (t, p, _) in self.PAGES.items() if p == body["pageId"]]
            if body.get("cursor") is None and len(kids) > 1:                 # two pages of results
                return {"data": {"items": kids[:1], "meta": {"nextCursor": "c2"}}}
            return {"data": {"items": kids[1:] if body.get("cursor") else kids, "meta": {"nextCursor": None}}}
        if path == "/api/search":
            return {"data": {"items": [{"id": "todo", "highlight": "<b>Jellyfin</b> to do"},
                                       {"id": "hosts", "highlight": "Runs <b>Jellyfin</b>. password: hunter2"}]}}
        raise AssertionError(path)


def test_docs_read_only_and_only_under_the_roots():
    dm = FakeDocmost()
    docs = ws.Docs(dm, ["home"], log=lambda m: None)
    listed = docs.list("greg")["pages"]
    assert [(p["page"], p["where"]) for p in listed] == [
        ("home", "HomeLab"), ("hosts", "HomeLab › Hosts"), ("logos", "HomeLab › Logos")]
    found = docs.search("greg", "jellyfin")["results"]
    assert [h["page"] for h in found] == ["hosts"]                    # the to-do list is not under HomeLab
    assert "<b>" not in found[0]["snippet"] and "hunter2" not in found[0]["snippet"]
    page = docs.read("bere", {"page": "hosts"})
    assert page["parts"] > 1 and page["part"] == 1 and page["headings"] == ["# Hosts", "## pve1"]
    assert "Runs Jellyfin" in page["text"] and "hunter2" not in page["text"] and "ghp_" not in page["text"]
    assert page["updated"] == "2026-11-19"
    assert docs.read("bere", {"page": "hosts", "part": 2})["text"].startswith("filler")
    assert docs.read("bere", {"page": "logos"})["title"] == "Logos"   # by id
    assert docs.read("bere", {"page": "  logos "})["page"] == "logos" # by title, any case
    for bad in ({"page": "todo"}, {"page": "Greg's To-Do List"}, {"page": "nope"}, {"page": "hosts", "part": 99}):
        with pytest.raises(ws.Refused):
            docs.read("bere", bad)
    with pytest.raises(ws.Refused):
        docs.search("greg", " ")
    writes = [p for p, _ in dm.calls if not p.startswith(("/api/pages/info", "/api/pages/sidebar", "/api/search"))]
    assert writes == []                                                # nothing but reads
    n = len(dm.calls)
    docs.pages()
    assert len(dm.calls) == n                                          # the page list is cached


def test_compact_tables():
    assert ws.compact("| a    |     b |\n|  --- | --- |\ntext  two  spaces  \n") == "| a | b |\n| --- | --- |\ntext  two  spaces"


def test_parts_of_keeps_everything():
    text = "".join(f"line {i}\n" for i in range(3000)) + "x" * 13000
    parts = ws.parts_of(text, 6000)
    assert "".join(parts) == text and all(len(p) <= 6000 for p in parts)


def test_docs_tools_reach_every_reader_agent():
    svc = ws.Service("http://sx", {"greg": {"token": "tg"}}, ws.Filter([]), log=lambda m: None)
    svc.changes_agents = {"household": {"token": "th"}}
    assert svc.mcp_agent("th") == ("household", False)                 # the weather only, so far
    svc.docs = ws.Docs(FakeDocmost(), ["home"], log=lambda m: None)
    assert svc.mcp_agent("th") == ("household", False)
    for search in (True, False):
        names = [t["name"] for t in svc.tools_for(search)]
        assert names[-3:] == ["search_docs", "read_doc", "list_docs"]
    out = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "read_doc", "arguments": {"page": "Logos"}}}, False)["result"]
    assert not out.get("isError") and "The agents." in json.loads(out["content"][0]["text"])["text"]
    out = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "read_doc", "arguments": {"page": "todo"}}}, False)["result"]
    assert out["isError"] and "no homelab doc" in out["content"][0]["text"]


def test_reader_config_docs_roots():
    agents = {"greg": {"web": {"enabled": True}}}
    dm = {"DOCMOST_URL": "https://docs.example", "DOCMOST_READ_ROOTS": "r1, r2,", "DOCMOST_PASSWORD": "secret"}
    cfg = wh.reader_config(agents, {"greg": "tg"}, [], [], None, {}, dm)
    assert cfg["docs"] == {"url": "https://docs.example", "roots": ["r1", "r2"]}
    assert "secret" not in json.dumps(cfg)
    assert "docs" not in wh.reader_config(agents, {"greg": "tg"}, [], [], None, {}, {"DOCMOST_URL": "x"})


@pytest.mark.parametrize("line", [
    "**Password:** hunter2", "password: hunter2", "| password | hunter2 |", "the password is 'hunter2'",
    "Wi-Fi key - hunter2", "pw = hunter2", "PIN: hunter2", "api_key=hunter2", "Root pass: hunter2"])
def test_docs_leave_out_passwords_however_they_are_written(line):
    assert "hunter2" not in ws.redact(line) and ws.REDACTED in ws.redact(line)


def test_docs_leave_out_a_password_column_and_keep_ordinary_text():
    table = "| Host | User | Password |\n|---|---|---|\n| pve1 | root | hunter2 |"
    out = ws.redact(table)
    assert "hunter2" not in out and "pve1 | root" in out and "| Password |" in out
    for keep in ("Runs Jellyfin on pve1.", "pin the image to 1.2", "| Host | Port |\n|---|---|\n| pve1 | 8006 |"):
        assert ws.redact(keep) == keep


# ── the weather anywhere (story WX1) ────────────────────────────────────────

CLACTON = {"name": "Clacton-on-Sea", "latitude": 51.79, "longitude": 1.15, "country_code": "GB",
           "country": "United Kingdom", "admin1": "England", "admin2": "Essex"}
PARIS_FR = {"name": "Paris", "latitude": 48.85, "longitude": 2.35, "country_code": "FR", "country": "France",
            "admin1": "Île-de-France", "admin2": "Paris"}
PARIS_TX = {"name": "Paris", "latitude": 33.66, "longitude": -95.56, "country_code": "US",
            "country": "United States", "admin1": "Texas", "admin2": "Lamar"}
SPRING_US = {"name": "Springfield", "latitude": 39.8, "longitude": -89.64, "country_code": "US",
             "country": "United States", "admin1": "Illinois", "admin2": "Sangamon"}
SPRING_GB = {"name": "Springfield", "latitude": 55.6, "longitude": -3.1, "country_code": "GB",
             "country": "United Kingdom", "admin1": "Scotland", "admin2": "Fife"}
WEEK = {"current": {"temperature_2m": 9.4, "weather_code": 2, "wind_speed_10m": 12.3},
        "daily": {"time": ["2026-11-28", "2026-11-29", "2026-11-30"], "weather_code": [61, 3, 0],
                  "temperature_2m_min": [6.4, 5.0, 2.2], "temperature_2m_max": [11.2, 10.0, 8.0],
                  "precipitation_probability_max": [70, 20, 0], "wind_speed_10m_max": [18, 30, 5]}}
PLACES = {"Clacton-on-Sea": [CLACTON], "Paris": [PARIS_FR, PARIS_TX], "Springfield": [SPRING_US, SPRING_GB]}


def weather_svc(country="GB", down=False):
    calls, logs = [], []

    def fetch(url):
        calls.append(url)
        if down:
            raise OSError("down")
        if url.startswith(ws.GEOCODING):
            import urllib.parse
            name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["name"][0]
            return {"results": PLACES[name]} if name in PLACES else {"generationtime_ms": 0.1}
        assert url.startswith(ws.OPEN_METEO)
        return WEEK

    s = ws.Service("http://x", {"greg": {"token": "t"}}, ws.Filter(["Mum", "Smalltown"]), fetch=fetch,
                   log=logs.append)
    s.country, s.calls, s.logs = country, calls, logs
    return s


def test_weather_text_names_the_place_found():
    s = weather_svc()
    out = s.weather_anywhere("bere", {"place": "Clacton-on-Sea"}, now=1000)
    assert out == ("Clacton-on-Sea, Essex: now 9°C, partly cloudy, wind 12 mph. "
                   "Today: Light rain, 6 to 11°C, 70% chance of rain. Tomorrow: Overcast, 5 to 10°C, "
                   "windy (up to 30 mph).")
    geo, fc = s.calls
    assert "count=5" in geo and "language=en" in geo and "format=json" in geo and "name=Clacton-on-Sea" in geo
    assert "latitude=51.79" in fc and "current=" in fc and "forecast_days=7" in fc
    three = s.weather_anywhere("bere", {"place": "Clacton-on-Sea", "days": 3}, now=1000)
    assert three.endswith("Monday 30 Nov: Clear sky, 2 to 8°C.")
    assert s.weather_anywhere("bere", {"place": "Clacton-on-Sea", "days": 99}, now=1000) == three


def test_weather_prefers_the_home_country_unless_another_is_named():
    s = weather_svc("GB")
    assert s.weather_anywhere("greg", {"place": "Springfield"}, now=0).startswith("Springfield, Fife:")
    assert s.weather_anywhere("greg", {"place": "Springfield, Illinois"}, now=0).startswith(
        "Springfield, Sangamon, Illinois, United States:")
    assert s.weather_anywhere("greg", {"place": "Paris"}, now=0).startswith("Paris, Île-de-France, France:")
    assert s.weather_anywhere("greg", {"place": "Paris, Texas"}, now=0).startswith("Paris, Lamar, Texas")
    assert s.weather_anywhere("greg", {"place": "Paris, USA"}, now=0).startswith("Paris, Lamar, Texas")
    assert s.weather_anywhere("greg", {"place": "Paris Texas"}, now=0).startswith("Paris, Lamar, Texas")
    none = weather_svc("")                                    # no preferred country: Open-Meteo's first
    assert none.weather_anywhere("greg", {"place": "Springfield"}, now=0).startswith("Springfield, Sangamon")
    assert ws.choose_place([], "", "GB") is None


def test_weather_place_not_found_asks_to_spell_it():
    s = weather_svc()
    for args in ({"place": "Clactn"}, {"place": "Nowhere, Atlantis"}):
        result = ws.handle_rpc(s, "greg", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "weather", "arguments": args}})["result"]
        assert not result.get("isError") and "was found" in result["content"][0]["text"]
        assert "spell" in result["content"][0]["text"]
    assert not [u for u in s.calls if u.startswith(ws.OPEN_METEO)]


def test_weather_is_cached_for_fifteen_minutes_per_place():
    s = weather_svc()
    s.weather_anywhere("greg", {"place": "Clacton-on-Sea"}, now=1000)
    s.weather_anywhere("greg", {"place": "clacton-on-sea", "days": 5}, now=1500)
    assert len(s.calls) == 2                                       # one lookup, one forecast
    s.weather_anywhere("greg", {"place": "Paris"}, now=1500)       # another place: asked
    assert len(s.calls) == 4
    s.weather_anywhere("greg", {"place": "Clacton-on-Sea"}, now=1000 + 901)
    assert len(s.calls) == 5 and s.calls[-1].startswith(ws.OPEN_METEO)   # the name is still known


def test_weather_errors_are_plain_sentences():
    s = weather_svc(down=True)
    result = ws.handle_rpc(s, "greg", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "weather", "arguments": {"place": "Paris"}}})["result"]
    assert result["isError"] and result["content"][0]["text"] == (
        "Not done: the weather service did not answer just now; try again in a minute.")
    assert any("FAILED (OSError)" in m for m in s.logs) and not any("Paris" in m for m in s.logs)
    ok = weather_svc()
    with pytest.raises(ws.Refused, match="private list"):          # a private name never leaves the house
        ok.weather_anywhere("greg", {"place": "Smalltown"})
    with pytest.raises(ws.Refused, match="home location"):
        ok.weather_anywhere("greg", {"place": "home"})
    with pytest.raises(ws.Refused):
        ok.weather_anywhere("greg", {"place": " "})
    with pytest.raises(ws.Refused):
        ok.weather_anywhere("greg", {"place": "Paris", "days": "lots"})
    assert not ok.calls
    ok.place = {"lat": 51.5, "lon": -0.1}
    assert ok.weather_anywhere("greg", {"place": "home"}).startswith("At home: now 9°C")
    assert len(ok.calls) == 1 and ok.calls[0].startswith(ws.OPEN_METEO)   # no name sent


def test_weather_tool_is_listed_for_every_reader_agent():
    s = weather_svc()
    s.changes_agents = {"household": {"token": "th"}}
    assert s.mcp_agent("th") == ("household", False)
    for search in (True, False):
        tool = [t for t in s.tools_for(search) if t["name"] == "weather"]
        assert tool and tool[0]["inputSchema"]["required"] == ["place"]
        assert tool[0]["inputSchema"]["properties"]["days"]["maximum"] == 7
    out = ws.handle_rpc(s, "household", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "weather", "arguments": {"place": "Clacton-on-Sea"}}}, False)["result"]
    assert out["content"][0]["text"].startswith("Clacton-on-Sea, Essex: now")


def test_preferred_country_comes_from_the_location_file():
    text = "# where we are\ncountry GB\n51.5074, -0.1278\n"
    assert wh.parse_location(text) == {"lat": 51.5, "lon": -0.1}
    assert wh.parse_country(text) == "GB" and wh.parse_country("country: fr") == "FR"
    assert wh.parse_country("51.5, -0.1") == "" and wh.parse_country("country Britain") == ""
    cfg = wh.reader_config({"greg": {"web": {"enabled": True}}}, {"greg": "t"}, [], [], None, None, None, "GB")
    assert cfg["country"] == "GB"
    assert "country" not in wh.reader_config({}, {}, [], [])


def test_weather_instructions_and_policy_name_the_tool():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    assert "use the `weather` tool (it says which place it found)" in (root / "souls/_shared/weather.md").read_text()
    assert "weather," in (root / "infra/agent-image/policy.yaml").read_text()
