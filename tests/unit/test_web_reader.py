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
    ("192.0.2.226 admin page", "private network"), ("100.64.0.7", "private network"),
    ("example.org", "own addresses"), ("@greg:example.org", "Matrix"),
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
    assert svc.mcp_agent("th") == (None, False)               # no Docmost: the token opens nothing here
    page = FakePage()
    svc.requests = ws.Requests(page, {"household": "the household chat"}, log=lambda m: None)
    assert svc.mcp_agent("th") == ("household", False) and svc.mcp_agent("tg") == ("greg", True)
    listed = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, False)
    assert [t["name"] for t in listed["result"]["tools"]] == ["file_request"]
    search = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "search_web", "arguments": {"query": "boiler"}}}, False)
    assert search["error"]["message"] == "unknown tool: search_web" and not svc.seen
    filed = ws.handle_rpc(svc, "household", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "file_request", "arguments": ASK}}, False)
    assert "filed" in filed["result"]["content"][0]["text"] and len(page.entries) == 1
    tools = ws.handle_rpc(svc, "greg", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["search_web", "add_private_name", "file_request"]


def test_recent_changes_tool_for_every_reader_agent(svc, git_dir):
    svc.changes_agents = {"household": {"token": "th"}}
    assert svc.mcp_agent("th") == (None, False)               # neither changes nor requests: nothing here
    get, _ = fake_github()
    svc.changes = ws.Changes(repos(git_dir), "ghtok", get=get, log=lambda m: None)
    assert svc.mcp_agent("th") == ("household", False)
    for agent, search, want in (("household", False, ["recent_changes"]),
                                ("greg", True, ["search_web", "add_private_name", "recent_changes"])):
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
