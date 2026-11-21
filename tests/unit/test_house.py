"""gateway/house_service.py and gateway/house_host.py: the household agent's lights (story H1)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, REPO / "gateway" / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hs = load("logos_house_service_test", "house_service.py")
hh = load("logos_house_host_test", "house_host.py")


def light(eid, name, state="on", brightness=128, **attrs):
    a = {"friendly_name": name, **attrs}
    if state == "on":
        a.update(brightness=brightness, color_mode="color_temp", color_temp_kelvin=2700)
    return {"entity_id": eid, "state": state, "attributes": a}


STATES = [
    light("light.kitchen_kitchen", "Kitchen", is_hue_group=True, hue_type="room",
          entity_id=["light.kitchen_spot_1", "light.kitchen_spot_2"]),
    light("light.kitchen_spot_1", "Spotlight 1"),
    light("light.kitchen_spot_2", "Spotlight 2", state="off"),
    light("light.living_room", "Living Room", is_hue_group=True, hue_type="room",
          entity_id=["light.lounge_left", "light.lounge_right", "light.tv_gradient"]),
    light("light.tv_wall", "TV Wall", is_hue_group=True, hue_type="zone", entity_id=["light.tv_gradient"]),
    light("light.lounge_left", "Lounge Left"),
    light("light.lounge_right", "Lounge Right", state="off"),
    light("light.tv_gradient", "TV Gradient", state="unavailable"),
    light("light.balcony", "Balcony", state="off"),
    {"entity_id": "scene.living_room_relax", "state": "unknown",
     "attributes": {"friendly_name": "Living Room Relax", "name": "Relax", "group_name": "Living Room"}},
    {"entity_id": "scene.kitchen_relax", "state": "unknown",
     "attributes": {"friendly_name": "Kitchen Relax", "name": "Relax", "group_name": "Kitchen"}},
    {"entity_id": "scene.kitchen_bright", "state": "unknown",
     "attributes": {"friendly_name": "Kitchen Bright", "name": "Bright", "group_name": "Kitchen"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "switch.heater", "state": "off", "attributes": {"friendly_name": "Heater"}},
]


class FakeHA:
    def __init__(self, states=STATES, status=200):
        self.states = states
        self.status = status
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        if self.status != 200:
            return self.status, b""
        if url.endswith("/api/states"):
            return 200, json.dumps(self.states).encode()
        return 200, b"[]"

    def services(self):
        return [(c[1].split("/api/services/")[1], c[3]) for c in self.calls if "/api/services/" in c[1]]


def service(fake=None, domains=("light", "scene"), exclude=()):
    fake = fake or FakeHA()
    svc = hs.Service(hs.HomeAssistant("http://192.0.2.10", "ha-token", http=fake),
                     {"household": {"token": "tok", "domains": list(domains), "exclude": list(exclude)}},
                     log=lambda m: None, sleep=lambda s: None)
    return svc, fake


# ── the service ──────────────────────────────────────────────────────────────

def test_list_lights_by_room():
    svc, fake = service()
    out = svc.list_lights("household")
    rooms = {r["room"]: r for r in out["rooms"]}
    assert list(rooms) == ["Kitchen", "Living Room", "TV Wall"]          # rooms first, then zones
    assert rooms["Kitchen"]["on"] == "1 of 2"
    assert rooms["Kitchen"]["lights"][0] == {"name": "Spotlight 1", "state": "on", "brightness": 50, "kelvin": 2700}
    assert out["not_in_a_room"] == [{"name": "Balcony", "state": "off"}]
    assert fake.calls[0][2]["Authorization"] == "Bearer ha-token"
    assert svc.list_lights("household", "kitchen")["rooms"][0]["room"] == "Kitchen"
    with pytest.raises(hs.Refused, match="no room called 'garage'"):
        svc.list_lights("household", "garage")


def test_only_allowed_domains_are_ever_seen():
    svc, _ = service()
    h = svc.house("household")
    assert "lock.front_door" not in h.by_id and "switch.heater" not in h.by_id
    # Even a definition that names them gets nothing: they are not supported.
    h = hs.House(STATES, ["light", "lock", "switch"], [])
    assert all(e.startswith("light.") for e in h.by_id)


def test_set_lights_room_names_and_values():
    svc, fake = service()
    svc.set_lights("household", ["the kitchen lights"], True, brightness=40, kelvin=9000, transition=2)
    assert fake.services() == [("light/turn_on", {"entity_id": ["light.kitchen_kitchen"], "brightness_pct": 40,
                                                  "color_temp_kelvin": 6500, "transition": 2.0})]
    svc.set_lights("household", ["Lounge Left", "lounge right"], False)
    assert fake.services()[-1] == ("light/turn_off", {"entity_id": ["light.lounge_left", "light.lounge_right"]})
    svc.set_lights("household", ["TV Wall"], True, color="Warm White")
    assert fake.services()[-1][1] == {"entity_id": ["light.tv_wall"], "color_name": "warmwhite"}


def test_exact_names_win_and_a_room_beats_its_own_lamp():
    states = STATES + [
        light("light.bathroom", "Bathroom", is_hue_group=True, hue_type="room", entity_id=["light.bathroom_lamp"]),
        light("light.bathroom_lamp", "Bathroom lamp")]
    h = hs.House(states, ["light"], [])
    assert h.resolve_lights(["Bathroom lamp"]) == ["light.bathroom_lamp"]
    assert h.resolve_lights(["bathroom"]) == ["light.bathroom"]
    assert h.resolve_lights(["the bathroom lights"]) == ["light.bathroom"]
    assert h.resolve_lights(["light.lounge_left"]) == ["light.lounge_left"]


def test_everything_means_each_room_once_plus_loose_lights():
    svc, fake = service()
    svc.set_lights("household", ["everything"], False)
    assert fake.services()[-1][1]["entity_id"] == ["light.kitchen_kitchen", "light.living_room", "light.balcony"]


def test_set_lights_refusals():
    svc, fake = service()
    for args, why in (((["garage"], True), "no light or room called 'garage'"),
                      (([], True), "Say which lights"),
                      ((["Kitchen"], "yes"), "on: true or on: false"),
                      ((["Kitchen"], True, 0), "1 to 100"),
                      ((["Kitchen"], True, "loud"), "numbers"),
                      ((["Kitchen"], True, None, 3000, "red"), "not both"),
                      ((["Kitchen"], True, None, None, "red; rm -rf"), "one plain name")):
        with pytest.raises(hs.Refused, match=why):
            svc.set_lights("household", *args)
    with pytest.raises(hs.Refused, match="could mean several"):
        svc.set_lights("household", ["spotlight"], True)
    assert fake.services() == []                                      # nothing was sent


def test_excluded_lights_cannot_be_touched():
    svc, fake = service(exclude=["light.balcony"])
    with pytest.raises(hs.Refused, match="no light or room called 'Balcony'"):
        svc.set_lights("household", ["Balcony"], True)
    svc.set_lights("household", ["everything"], False)
    assert "light.balcony" not in fake.services()[-1][1]["entity_id"]


def test_a_room_with_an_excluded_light_is_switched_light_by_light_and_loses_its_scenes():
    svc, fake = service(exclude=["light.lounge_right"])
    svc.set_lights("household", ["Living Room"], True)
    assert fake.services()[-1][1]["entity_id"] == ["light.lounge_left", "light.tv_gradient"]  # not the room
    svc.set_lights("household", ["everything"], False)
    sent = fake.services()[-1][1]["entity_id"]
    assert "light.living_room" not in sent and "light.lounge_right" not in sent and "light.kitchen_kitchen" in sent
    assert [r["room"] for r in svc.list_scenes("household")["scenes"]] == ["Kitchen"]   # Hue scenes set the room
    with pytest.raises(hs.Refused):
        svc.activate_scene("household", "Relax", "living room")


def test_a_home_assistant_scene_that_sets_anything_but_allowed_lights_is_never_offered():
    states = STATES + [
        {"entity_id": "scene.leaving", "state": "unknown",
         "attributes": {"friendly_name": "Leaving home", "entity_id": ["light.kitchen_kitchen", "lock.front_door"]}},
        {"entity_id": "scene.evening", "state": "unknown",
         "attributes": {"friendly_name": "Evening", "entity_id": ["light.lounge_left", "light.kitchen_kitchen"]}}]
    svc, fake = service(FakeHA(states))
    with pytest.raises(hs.Refused):
        svc.activate_scene("household", "Leaving home")
    assert svc.activate_scene("household", "Evening")["activated"]["scene"] == "Evening"


def test_set_lights_reports_unreachable_lights():
    svc, _ = service()
    out = svc.set_lights("household", ["TV Gradient"], True)
    assert out["not_reachable"] == ["TV Gradient"]


def test_scenes():
    svc, fake = service()
    assert svc.list_scenes("household") == {"scenes": [{"room": "Kitchen", "scenes": ["Bright", "Relax"]},
                                                       {"room": "Living Room", "scenes": ["Relax"]}]}
    with pytest.raises(hs.Refused, match="several rooms"):
        svc.activate_scene("household", "relax")
    assert svc.activate_scene("household", "Relax", "living room") == {"activated": {"scene": "Relax",
                                                                                     "room": "Living Room"}}
    assert svc.activate_scene("household", "bright")["activated"]["room"] == "Kitchen"
    assert fake.services() == [("scene/turn_on", {"entity_id": "scene.living_room_relax"}),
                               ("scene/turn_on", {"entity_id": "scene.kitchen_bright"})]


def test_only_three_home_assistant_services_exist():
    ha = hs.HomeAssistant("http://192.0.2.10", "t", http=FakeHA())
    for domain, svc_name in (("lock", "unlock"), ("light", "toggle"), ("homeassistant", "restart"), ("script", "turn_on")):
        with pytest.raises(hs.Refused):
            ha.service(domain, svc_name, {})


def test_home_assistant_down_or_refusing_login():
    svc, _ = service(FakeHA(status=0))
    with pytest.raises(hs.Refused, match="not answering"):
        svc.list_lights("household")
    svc, _ = service(FakeHA(status=401))
    with pytest.raises(hs.Refused, match="refused the house service's login"):
        svc.list_lights("household")


def test_domains_decide_the_tools():
    svc, _ = service(domains=["light"])
    names = [t["name"] for t in hs.handle_rpc(svc, "household", {"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert names == ["list_lights", "set_lights"]
    answer = hs.handle_rpc(svc, "household", {"id": 2, "method": "tools/call",
                                              "params": {"name": "activate_scene", "arguments": {"scene": "Relax"}}})
    assert answer["error"]["message"] == "unknown tool: activate_scene"


def test_rpc_wraps_refusals_and_hides_errors():
    svc, _ = service()
    out = hs.handle_rpc(svc, "household", {"id": 1, "method": "tools/call",
                                           "params": {"name": "set_lights", "arguments": {"targets": ["garage"], "on": True}}})
    assert out["result"]["isError"] and out["result"]["content"][0]["text"].startswith("Not done: There is no light")
    svc.ha.states = lambda: (_ for _ in ()).throw(RuntimeError("secret detail"))
    out = hs.handle_rpc(svc, "household", {"id": 2, "method": "tools/call", "params": {"name": "list_lights"}})
    assert out["result"]["content"][0]["text"] == "Not done: the house service hit an error."
    assert hs.handle_rpc(svc, "household", {"method": "notifications/initialized"}) is None


def test_tokens():
    svc, _ = service()
    assert svc.agent_for_token("tok") == "household"
    assert svc.agent_for_token("nope") is None and svc.agent_for_token("") is None


# ── the host ─────────────────────────────────────────────────────────────────

def test_only_the_household_agent_uses_it():
    assert hh.uses_house({"role": "household", "house": {"enabled": True}})
    assert not hh.uses_house({"role": "household"})
    assert hh.uses_house({"house": {"enabled": True}})                      # a person's agent, when asked
    assert not hh.uses_house({"role": "person"})


def test_agent_settings():
    assert hh.agent_settings({}) == {"domains": ["light", "scene"], "exclude": []}
    assert hh.agent_settings({"house": {"domains": ["light"], "exclude": ["light.balcony"]}}) == {
        "domains": ["light"], "exclude": ["light.balcony"]}
    with pytest.raises(hh.HouseError, match="not supported"):
        hh.agent_settings({"name": "household", "house": {"domains": ["light", "lock"]}})
    assert hh.agent_settings({"house": {"domains": ["light", "scene", "todo"]}})["domains"] == ["light", "scene", "todo"]


def write_settings(tmp_path, text, mode=0o600):
    d = tmp_path / "logos"
    d.mkdir(exist_ok=True)
    p = d / "home-assistant"
    p.write_text(text)
    p.chmod(mode)
    return {"XDG_CONFIG_HOME": str(tmp_path)}


def test_ha_settings(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    with pytest.raises(hh.HouseError, match="no .*home-assistant"):
        hh.ha_settings(env)
    env = write_settings(tmp_path, "HA_URL=http://192.0.2.10/\nHA_TOKEN=abc\n")
    assert hh.ha_settings(env) == {"url": "http://192.0.2.10", "token": "abc"}
    with pytest.raises(hh.HouseError, match="IP address"):
        hh.ha_settings(write_settings(tmp_path, "HA_URL=http://ha.home.example\nHA_TOKEN=abc\n"))
    with pytest.raises(hh.HouseError, match="both"):
        hh.ha_settings(write_settings(tmp_path, "HA_URL=http://192.0.2.10\n"))
    with pytest.raises(hh.HouseError, match="chmod 600"):
        hh.ha_settings(write_settings(tmp_path, "HA_URL=http://192.0.2.10\nHA_TOKEN=abc\n", 0o644))


class Done:
    def __init__(self, rc=0):
        self.returncode = rc


class FakeRunner:
    def __init__(self, rt):
        self.rt = rt

    def run(self, cmd):
        self.rt.calls.append(cmd)
        return Done(1)                       # nothing exists yet

    def http(self, url, timeout=3):
        return b"{}"

    def sleep(self, s):
        pass


class FakeRuntime:
    def __init__(self, env, agents, home_env):
        self.env, self._agents, self.home_env = env, agents, home_env
        self.runner = FakeRunner(self)
        self.calls, self.inputs = [], []

    def defined(self):
        return list(self._agents)

    def load(self, name):
        return self._agents[name]

    def get(self, volume, rel):
        return self.home_env.get(volume, "")

    def run(self, cmd, *, input=None, check=True):
        self.calls.append(cmd)
        self.inputs.append(input)
        if cmd[:3] == ["docker", "network", "inspect"]:
            return "172.17.0.1\n"
        return ""


def test_up_starts_a_locked_down_container(tmp_path):
    env = write_settings(tmp_path, "HA_URL=http://192.0.2.10\nHA_TOKEN=ha-secret\n")
    agents = {"household": {"name": "household", "role": "household", "house": {"enabled": True}},
              "greg": {"name": "greg", "house": {"enabled": True}}}
    rt = FakeRuntime(env, agents, {"logos-home-household": "LOGOS_HOUSE_TOKEN=tok\n",
                                   "logos-home-greg": "LOGOS_HOUSE_TOKEN=gtok\n"})
    assert hh.HouseHost(rt).up() is True
    (config,) = [i for i in rt.inputs if i]
    cfg = json.loads(config)
    assert sorted(cfg["agents"]) == ["greg", "household"]               # each with its own token
    assert cfg["ha_token"] == "ha-secret" and cfg["agents"]["household"]["token"] == "tok"
    assert cfg["agents"]["greg"]["token"] == "gtok"
    (start,) = [c for c in rt.calls if c[:3] == ["docker", "run", "-d"]]
    for flag in ("--read-only", "--cap-drop", "ALL", "--user", "1000:1000", "--memory", "64m", "--dns", "127.0.0.1",
                 "--network", "logos-house", "172.17.0.1:8098:8098"):
        assert flag in start
    assert not any("ha-secret" in part for part in start)               # never on the command line


def test_up_without_a_household_token_does_nothing(tmp_path):
    rt = FakeRuntime({"XDG_CONFIG_HOME": str(tmp_path)},
                     {"household": {"role": "household", "house": {"enabled": True}}}, {})
    assert hh.HouseHost(rt).up() is False
    assert rt.calls == []


# ── the shared lists (todo) ──────────────────────────────────────────────────

class FakeTodoHA(FakeHA):
    """Home Assistant with to-do lists that answer todo.get_items and change on add/update/remove."""

    def __init__(self, lists):
        self.lists = {eid: [dict(i) for i in items] for eid, (_, items) in lists.items()}
        super().__init__(STATES + [{"entity_id": eid, "state": str(len(items)), "attributes": {
            "friendly_name": name, "supported_features": 15 if "shopping" in eid else 127}}
            for eid, (name, items) in lists.items()])

    def __call__(self, method, url, headers, body):
        status, raw = super().__call__(method, url, headers, body)
        data = json.loads(body) if body else {}
        if "/api/services/todo/get_items" in url:
            assert url.endswith("?return_response")
            eid = data["entity_id"]
            return 200, json.dumps({"changed_states": [], "service_response": {eid: {"items": self.lists[eid]}}}).encode()
        if url.endswith("/api/services/todo/add_item"):
            new = {"summary": data["item"], "status": "needs_action"}
            new.update({k: data[v] for k, v in (("description", "description"), ("due", "due_date")) if v in data})
            self.lists[data["entity_id"]].append(new)
        elif url.endswith("/api/services/todo/update_item"):
            for i in self.lists[data["entity_id"]]:
                if i["summary"] == data["item"]:
                    if "status" in data:
                        i["status"] = data["status"]
                    if "description" in data:
                        i["description"] = data["description"]
                    if "due_date" in data:
                        i["due"] = data["due_date"]
                    if "rename" in data:
                        i["summary"] = data["rename"]
        elif url.endswith("/api/services/todo/remove_item"):
            self.lists[data["entity_id"]] = [i for i in self.lists[data["entity_id"]] if i["summary"] not in data["item"]]
        return status, raw


def lists_service(lists=None, domains=("light", "scene", "todo")):
    fake = FakeTodoHA(lists if lists is not None else {
        "todo.shopping_list": ("Shopping list", [{"summary": "Peas", "status": "needs_action"},
                                                 {"summary": "Lemons", "status": "completed"}])})
    return service(fake, domains=domains)


def test_list_items_open_and_done():
    svc, _ = lists_service()
    assert svc.list_items("household") == {"lists": [{"list": "Shopping list", "open": ["Peas"]}]}
    assert svc.list_items("household", "the shopping list", done=True) == {
        "lists": [{"list": "Shopping list", "open": ["Peas"], "done": ["Lemons"]}]}


def test_add_items_skips_duplicates_and_reopens_ticked_ones():
    svc, fake = lists_service()
    got = svc.add_items("household", "shopping", ["pasta", " peas ", "lemons", "something sweet"])
    assert got == {"list": "Shopping list", "added": ["pasta", "something sweet"],
                   "back_on_the_list": ["Lemons"], "already_on_it": ["Peas"]}
    assert [i["summary"] for i in fake.lists["todo.shopping_list"] if i["status"] == "needs_action"] == [
        "Peas", "Lemons", "pasta", "something sweet"]


def test_tick_and_remove_match_names_loosely_but_never_guess():
    svc, fake = lists_service({"todo.shopping_list": ("Shopping list", [
        {"summary": "Oat milk", "status": "needs_action"}, {"summary": "Milk chocolate", "status": "needs_action"},
        {"summary": "Bread", "status": "needs_action"}])})
    assert svc.tick_items("household", "", ["bread"]) == {"list": "Shopping list", "ticked": ["Bread"]}
    with pytest.raises(hs.Refused, match="could be"):
        svc.tick_items("household", "", ["milk"])
    with pytest.raises(hs.Refused, match="not on the list"):
        svc.remove_items("household", "", ["eggs"])
    assert svc.remove_items("household", "", ["oat milk"])["removed"] == ["Oat milk"]
    assert [i["summary"] for i in fake.lists["todo.shopping_list"]] == ["Milk chocolate", "Bread"]


def test_which_list():
    svc, _ = lists_service({"todo.shopping_list": ("Shopping list", []), "todo.household": ("Household to-do", [])})
    with pytest.raises(hs.Refused, match="Say which list"):
        svc.add_items("household", "", ["x"])
    assert svc.add_items("household", "to-do", ["call the plumber"])["list"] == "Household to-do"
    for name in ("todo", "To Do", "the to-do list", "todo list"):
        assert svc.list_items("household", name)["lists"][0]["list"] == "Household to-do"
    with pytest.raises(hs.Refused, match="no list called 'gifts'"):
        svc.list_items("household", "gifts")
    svc, _ = lists_service({})
    with pytest.raises(hs.Refused, match="no shared lists"):
        svc.list_items("household")


def test_items_are_checked_and_lists_need_the_domain():
    svc, _ = lists_service()
    for bad in ([], [""], ["x" * 201], ["a"] * 31):
        with pytest.raises(hs.Refused):
            svc.add_items("household", "shopping", bad)
    svc, _ = lists_service(domains=("light",))
    with pytest.raises(hs.Refused, match="not set up"):
        svc.list_items("household")
    names = [t["name"] for t in hs.handle_rpc(svc, "household", {"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "add_items" not in names
    svc, _ = lists_service()
    names = [t["name"] for t in hs.handle_rpc(svc, "household", {"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert {"list_items", "add_items", "tick_items", "remove_items"} <= set(names)
    answer = hs.handle_rpc(svc, "household", {"id": 2, "method": "tools/call",
                                              "params": {"name": "add_items", "arguments": {"items": ["eggs"]}}})
    assert json.loads(answer["result"]["content"][0]["text"])["added"] == ["eggs"]


def test_only_the_list_services_are_added():
    ha = hs.HomeAssistant("http://192.0.2.10", "t", http=FakeHA())
    with pytest.raises(hs.Refused):
        ha.service("todo", "remove_completed_items", {})
    with pytest.raises(hs.Refused):
        ha.todo_items("light.kitchen")


# ── the copy of the lists in the household brain ─────────────────────────────

def test_lists_mirror_writes_only_when_the_lists_change(tmp_path):
    spec = importlib.util.spec_from_file_location("lists_mirror", REPO / "infra/agent-jobs/lists_mirror.py")
    lm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lm)
    page = tmp_path / "lists.md"
    lists = [{"list": "Shopping list", "open": ["Peas", "2 lemons"]}, {"list": "To-do", "open": []}]
    text = lm.render(lists)
    rich = lm.render([{"list": "To-do", "open": [{"item": "Ceremony", "details": "banns, hymns", "due": "2026-12-01"}]}])
    assert "- [ ] Ceremony (due 2026-12-01)\n    banns, hymns\n" in rich
    assert "- [ ] Peas\n- [ ] 2 lemons\n" in text and "## To-do\n\nNothing on it." in text
    assert "Do not edit it here" in text
    assert lm.write_if_changed(text, str(page)) is True
    first = page.read_text()
    assert "Last changed " in first
    assert lm.write_if_changed(text, str(page)) is False and page.read_text() == first
    assert lm.write_if_changed(lm.render([{"list": "Shopping list", "open": ["Peas"]}]), str(page)) is True
    assert "2 lemons" not in page.read_text()


def test_short_titles_with_details_and_due_dates():
    svc, fake = lists_service({"todo.to_do": ("To-do", []), "todo.shopping_list": ("Shopping list", [])})
    got = svc.add_items("household", "to-do", [
        {"item": "Ceremony arrangements", "details": "calling of banns, hymns, music and order of ceremony",
         "due": "2026-12-01"}, "Look for choreographer"])
    assert got == {"list": "To-do", "added": ["Ceremony arrangements", "Look for choreographer"]}
    assert fake.lists["todo.to_do"][0] == {"summary": "Ceremony arrangements", "status": "needs_action",
                                          "description": "calling of banns, hymns, music and order of ceremony",
                                          "due": "2026-12-01"}
    assert svc.list_items("household", "to-do")["lists"][0]["open"] == [
        {"item": "Ceremony arrangements", "details": "calling of banns, hymns, music and order of ceremony",
         "due": "2026-12-01"}, "Look for choreographer"]
    with pytest.raises(hs.Refused, match="too long for a title"):
        svc.add_items("household", "to-do", ["x" * 61])
    with pytest.raises(hs.Refused, match="YYYY-MM-DD"):
        svc.add_items("household", "to-do", [{"item": "Rings", "due": "next week"}])
    got = svc.add_items("household", "shopping", [{"item": "Milk", "details": "oat"}])   # no details on this list
    assert got["added"] == ["Milk"] and got["details_not_kept"] == ["Milk"]


def test_change_item_renames_and_sets_details():
    svc, fake = lists_service({"todo.to_do": ("To-do", [
        {"summary": "[Wedding] Book flat to give notice and prepare docs needed", "status": "needs_action"}]),
        "todo.shopping_list": ("Shopping list", [{"summary": "Peas", "status": "needs_action"}])})
    got = svc.change_item("household", "to-do", "book flat", "Book appointment to give notice",
                          "and prepare the documents needed")
    assert got == {"list": "To-do", "changed": "Book appointment to give notice"}
    assert fake.lists["todo.to_do"][0]["summary"] == "Book appointment to give notice"
    assert fake.lists["todo.to_do"][0]["description"] == "and prepare the documents needed"
    with pytest.raises(hs.Refused, match="Say what to change"):
        svc.change_item("household", "to-do", "book appointment")
    with pytest.raises(hs.Refused, match="cannot hold details"):
        svc.change_item("household", "shopping", "peas", details="frozen")
