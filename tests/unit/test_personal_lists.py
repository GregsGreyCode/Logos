"""Personal lists (Greg, 28 Nov): lists only their person sees, kept by the house service beside the
shared Home Assistant lists and worked with the same tools (gateway/house_service.py)."""
import json
import os
import stat

import pytest

from tests.unit.test_house import ADMIN, FakeFlowHA, FakeRuntime, hh, hs, write_settings


class FakeEntriesHA(FakeFlowHA):
    """Also lists Local To-do config entries and deletes one, as an admin would."""

    def __init__(self, lists, entries=None, **kw):
        super().__init__(lists, **kw)
        self.entries = list(entries if entries is not None else [])
        self.deleted = []

    def __call__(self, method, url, headers, body):
        path = "/" + url.split("/", 3)[3]
        if path.startswith("/api/config/config_entries/entry"):
            assert headers["Authorization"][7:] == ADMIN          # only the admin login removes lists
            self.calls.append((method, path, headers, None))
            if method == "GET":
                return 200, json.dumps(self.entries).encode()
            entry_id = path.rsplit("/", 1)[1]
            gone = [e for e in self.entries if e["entry_id"] == entry_id]
            self.entries = [e for e in self.entries if e["entry_id"] != entry_id]
            for e in gone:
                eid = "todo." + e["title"].lower().replace(" ", "_")
                self.lists.pop(eid, None)
                self.states = [s for s in self.states if s["entity_id"] != eid]
            self.deleted.append(entry_id)
            return 200, b'{"require_restart": false}'
        return super().__call__(method, url, headers, body)


AGENTS = {
    "household": {"token": "h-tok", "domains": ["light", "todo"], "exclude": []},
    "bere": {"token": "b-tok", "domains": ["light", "todo"], "exclude": [], "person": "bere"},
    "greg": {"token": "g-tok", "domains": ["light", "todo"], "exclude": [], "person": "greg"},
}


def make(tmp_path, lists=None, entries=None, admin=True, ha_status=200):
    fake = FakeEntriesHA(lists if lists is not None else {
        "todo.shopping_list": ("Shopping list", [{"summary": "Peas", "status": "needs_action"}])}, entries)
    fake.status = ha_status
    clock = [1_795_000_000.0]
    svc = hs.Service(hs.HomeAssistant("http://ha.example:8123", "ha-token", http=fake), json.loads(json.dumps(AGENTS)),
                     log=lambda m: None, sleep=lambda s: None, clock=lambda: clock[0],
                     lists_admin=hs.ListMaker("http://ha.example:8123", ADMIN, http=fake) if admin else None,
                     personal=hs.PersonalLists(str(tmp_path / "lists"), clock=lambda: clock[0]))
    return svc, fake


def saved(tmp_path, person):
    return json.loads((tmp_path / "lists" / f"{person}.json").read_text())


# ── storage ──────────────────────────────────────────────────────────────────

def test_storage_is_one_private_file_per_person_written_atomically(tmp_path):
    store = hs.PersonalLists(str(tmp_path / "lists"), clock=lambda: 1_795_000_000.0)
    assert store.load("bere") == []
    store.save("bere", [{"name": "Games", "owner": "bere", "created_at": store.now(), "items": []}])
    path = tmp_path / "lists" / "bere.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "lists").stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {"version": 1, "owner": "bere", "lists": [
        {"name": "Games", "owner": "bere", "created_at": "2026-11-18T11:06:40Z", "items": []}]}
    assert os.listdir(tmp_path / "lists") == ["bere.json"]            # no temporary file left behind
    for bad in ("", "../greg", "Bere", "a/b"):
        with pytest.raises(hs.Refused):
            store.load(bad)
    path.write_text("{not json")
    with pytest.raises(hs.Refused, match="could not be read"):
        store.load("bere")


def test_a_failed_write_leaves_the_old_file(tmp_path, monkeypatch):
    store = hs.PersonalLists(str(tmp_path / "lists"))
    store.save("bere", [{"name": "Games", "items": []}])
    monkeypatch.setattr(hs.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.save("bere", [])
    assert [l["name"] for l in store.load("bere")] == ["Games"]
    assert os.listdir(tmp_path / "lists") == ["bere.json"]


# ── who sees what ────────────────────────────────────────────────────────────

def test_create_list_is_personal_for_a_persons_agent_and_shared_for_the_household(tmp_path):
    svc, fake = make(tmp_path)
    assert svc.create_list("bere", "Video games") == {"list": "Video games", "kind": "personal", "created": True}
    assert not [c for c in fake.calls if "config_entries" in c[1]]           # Home Assistant never heard of it
    got = saved(tmp_path, "bere")["lists"][0]
    assert got["name"] == "Video games" and got["owner"] == "bere" and got["created_at"].endswith("Z")
    assert svc.create_list("bere", "video games list")["already_existed"] is True
    assert svc.create_list("household", "Wedding")["kind"] == "shared"         # the household agent: shared
    with pytest.raises(hs.Refused, match="Personal lists belong to one person"):
        svc.create_list("household", "Mine", shared=False)
    assert svc.create_list("greg", "Garden", shared=True)["kind"] == "shared"


def test_an_agent_never_sees_another_persons_lists(tmp_path):
    svc, _ = make(tmp_path)
    svc.create_list("bere", "Video games")
    svc.add_items("bere", "video games", ["Zelda"])
    greg = svc.list_items("greg")["lists"]
    assert [(l["list"], l["kind"]) for l in greg] == [("Shopping list", "shared")]
    for call in (lambda: svc.add_items("greg", "video games", ["Doom"]),
                 lambda: svc.tick_items("greg", "video games", ["Zelda"]),
                 lambda: svc.remove_items("greg", "video games", ["Zelda"]),
                 lambda: svc.delete_list("greg", "video games"),
                 lambda: svc.move_list("greg", "video games", "shared")):
        with pytest.raises(hs.Refused, match="no list called|no personal lists"):
            call()
    # The household agent gets no personal lists at all, and never Béré's.
    assert [l["list"] for l in svc.list_items("household")["lists"]] == ["Shopping list"]
    with pytest.raises(hs.Refused, match="no list called 'video games'"):
        svc.add_items("household", "video games", ["Doom"])
    for call in (lambda: svc.list_items("household", shared=False), lambda: svc.delete_list("household", "x"),
                 lambda: svc.move_list("household", "Shopping list", "personal")):
        with pytest.raises(hs.Refused, match="Personal lists belong to one person"):
            call()
    assert [i["text"] for i in saved(tmp_path, "bere")["lists"][0]["items"]] == ["Zelda"]
    assert not (tmp_path / "lists" / "greg.json").exists()


def test_the_person_comes_only_from_the_config(tmp_path):
    svc, _ = make(tmp_path)
    svc.create_list("bere", "Games")
    # Whatever the agent sends, the call works on its own person's lists.
    answer = hs.handle_rpc(svc, "greg", {"id": 1, "method": "tools/call", "params": {
        "name": "list_items", "arguments": {"list": "games", "person": "bere", "owner": "bere"}}})
    assert answer["result"]["isError"]
    # No personal lists without the volume, whatever the config says.
    plain = hs.Service(svc.ha, json.loads(json.dumps(AGENTS)), log=lambda m: None)
    assert plain._person("bere") == ""
    assert [l["kind"] for l in plain.list_items("bere")["lists"]] == ["shared"]


def test_tools_offered(tmp_path):
    svc, _ = make(tmp_path)
    names = lambda a: [t["name"] for t in hs.handle_rpc(svc, a, {"id": 1, "method": "tools/list"})["result"]["tools"]]
    assert "move_list" not in names("household") and "delete_list" not in names("household")
    assert {"move_list", "delete_list", "list_items", "create_list"} <= set(names("bere"))
    tools = {t["name"]: t for t in hs.handle_rpc(svc, "bere", {"id": 1, "method": "tools/list"})["result"]["tools"]}
    assert "shared" in tools["add_items"]["inputSchema"]["properties"]
    assert "PERSONAL" in tools["create_list"]["description"]
    household = {t["name"]: t for t in hs.handle_rpc(svc, "household", {"id": 1, "method": "tools/list"})["result"]["tools"]}
    assert "shared" not in household["add_items"]["inputSchema"]["properties"]
    assert "shared" not in hs.TOOLS[5]["inputSchema"]["properties"]          # the shared table is not changed
    answer = hs.handle_rpc(svc, "household", {"id": 2, "method": "tools/call",
                                              "params": {"name": "move_list", "arguments": {"name": "x", "to": "shared"}}})
    assert answer["error"]["message"] == "unknown tool: move_list"


# ── one set of tools ─────────────────────────────────────────────────────────

def test_items_on_a_personal_list(tmp_path):
    svc, fake = make(tmp_path)
    svc.create_list("bere", "Video games")
    got = svc.add_items("bere", "video games", ["Zelda", {"item": "Celeste", "details": "the farewell DLC"}, "zelda"])
    assert got == {"list": "Video games", "added": ["Zelda", "Celeste"], "already_on_it": ["Zelda"]}
    assert svc.tick_items("bere", "games", ["zel"]) == {"list": "Video games", "ticked": ["Zelda"]}
    item = saved(tmp_path, "bere")["lists"][0]["items"][0]
    assert item["done"] is True and item["done_at"] and item["added_at"]
    assert svc.add_items("bere", "video games", ["Zelda"])["back_on_the_list"] == ["Zelda"]
    assert saved(tmp_path, "bere")["lists"][0]["items"][0]["done_at"] is None
    assert svc.change_item("bere", "video games", "celeste", new_title="Celeste (again)", due="2026-12-24") == {
        "list": "Video games", "changed": "Celeste (again)"}
    with pytest.raises(hs.Refused, match="already on the list"):
        svc.change_item("bere", "video games", "celeste", new_title="zelda")
    svc.change_item("bere", "video games", "celeste", details="")
    assert svc.list_items("bere", "video games") == {"lists": [{"list": "Video games", "kind": "personal",
                                                                "open": ["Zelda", {"item": "Celeste (again)", "due": "2026-12-24"}]}]}
    assert svc.remove_items("bere", "video games", ["zelda"]) == {"list": "Video games", "removed": ["Zelda"]}
    assert fake.services() == []                                    # Home Assistant was never changed


def test_list_items_shows_both_kinds_and_finds_names_across_them(tmp_path):
    svc, fake = make(tmp_path)
    svc.create_list("bere", "Video games")
    lists = svc.list_items("bere", done=True)["lists"]
    assert [(l["list"], l["kind"]) for l in lists] == [("Shopping list", "shared"), ("Video games", "personal")]
    assert "id" in lists[0] and "id" not in lists[1]
    assert svc.add_items("bere", "shopping", ["Milk"])["list"] == "Shopping list"
    assert ("todo/add_item", {"entity_id": "todo.shopping_list", "item": "Milk"}) in fake.services()
    with pytest.raises(hs.Refused, match=r"Say which list: Shopping list \(shared\), Video games \(personal\)"):
        svc.add_items("bere", "", ["x"])


def test_the_same_name_on_both_kinds(tmp_path):
    svc, fake = make(tmp_path, {"todo.to_do": ("To do", [])})
    svc.create_list("bere", "To do")
    with pytest.raises(hs.Refused, match="could be several lists.*shared: true"):
        svc.add_items("bere", "to-do", ["x"])
    svc.add_items("bere", "to do", ["Mine"], shared=False)
    svc.add_items("bere", "to do", ["Ours"], shared=True)
    svc.add_items("bere", "my to do", ["Also mine"])
    svc.add_items("bere", "our to-do list", ["Also ours"])
    assert [i["text"] for i in saved(tmp_path, "bere")["lists"][0]["items"]] == ["Mine", "Also mine"]
    assert [i["summary"] for i in fake.lists["todo.to_do"]] == ["Ours", "Also ours"]
    # Over MCP, "shared" may come as text.
    answer = hs.handle_rpc(svc, "bere", {"id": 1, "method": "tools/call", "params": {
        "name": "list_items", "arguments": {"list": "to do", "shared": "false"}}})
    assert json.loads(answer["result"]["content"][0]["text"])["lists"][0]["kind"] == "personal"


def test_personal_lists_work_while_home_assistant_is_down(tmp_path):
    svc, fake = make(tmp_path)
    svc.create_list("bere", "Video games")
    fake.status = 0
    out = svc.list_items("bere")
    assert [l["list"] for l in out["lists"]] == ["Video games"] and "could not be read" in out["note"]
    assert svc.add_items("bere", "video games", ["Zelda"])["added"] == ["Zelda"]
    with pytest.raises(hs.Refused, match="shared lists could not be read"):
        svc.add_items("bere", "shopping", ["Milk"])


def test_delete_list(tmp_path):
    svc, _ = make(tmp_path)
    svc.create_list("bere", "Video games")
    svc.add_items("bere", "video games", ["Zelda"])
    with pytest.raises(hs.Refused, match="is a shared list"):
        svc.delete_list("bere", "shopping")
    with pytest.raises(hs.Refused, match="Say which list"):
        svc.delete_list("bere", "")
    assert svc.delete_list("bere", "video games") == {"deleted": "Video games", "kind": "personal", "items": 1}
    assert saved(tmp_path, "bere")["lists"] == []


# ── moving a list ────────────────────────────────────────────────────────────

def test_move_a_personal_list_to_shared(tmp_path):
    svc, fake = make(tmp_path)
    svc.create_list("bere", "Wedding")
    svc.add_items("bere", "wedding", ["Rings", {"item": "Music", "details": "a string quartet"}, "Cake"])
    svc.tick_items("bere", "wedding", ["cake"])
    got = svc.move_list("bere", "wedding", "shared")
    assert got == {"list": "Wedding", "kind": "shared", "id": "todo.wedding", "moved_items": 2, "ticked_items_left_out": 1}
    assert [(i["summary"], i.get("description")) for i in fake.lists["todo.wedding"]] == [
        ("Rings", None), ("Music", "a string quartet")]
    assert saved(tmp_path, "bere")["lists"] == []
    assert [l["kind"] for l in svc.list_items("greg")["lists"] if l["list"] == "Wedding"] == ["shared"]


def test_moving_to_shared_without_the_admin_login_changes_nothing(tmp_path):
    svc, fake = make(tmp_path, admin=False)
    svc.create_list("bere", "Wedding")
    svc.add_items("bere", "wedding", ["Rings"])
    with pytest.raises(hs.Refused, match="needs Home Assistant admin"):
        svc.move_list("bere", "wedding", "shared")
    assert [i["text"] for i in saved(tmp_path, "bere")["lists"][0]["items"]] == ["Rings"]


def test_move_a_shared_list_to_personal(tmp_path):
    lists = {"todo.shopping_list": ("Shopping list", []),
             "todo.video_games": ("Video games", [{"summary": "Zelda", "status": "needs_action", "description": "TOTK"},
                                                  {"summary": "Doom", "status": "completed"}])}
    svc, fake = make(tmp_path, lists, entries=[{"entry_id": "e9", "domain": "local_todo", "title": "Video games"},
                                               {"entry_id": "e2", "domain": "local_todo", "title": "Other"}])
    got = svc.move_list("bere", "video games", "personal")
    assert got == {"list": "Video games", "kind": "personal", "moved_items": 2, "shared_list_removed": True}
    assert fake.deleted == ["e9"]
    items = saved(tmp_path, "bere")["lists"][0]["items"]
    assert [(i["text"], i["done"], i.get("details")) for i in items] == [("Zelda", False, "TOTK"), ("Doom", True, None)]
    assert [l["list"] for l in svc.list_items("greg")["lists"]] == ["Shopping list"]   # Greg no longer sees it


def test_a_shared_list_that_cannot_be_removed_stays_and_says_so(tmp_path):
    # The Shopping List integration is not a Local To-do entry; nothing is removed.
    svc, fake = make(tmp_path, entries=[{"entry_id": "e2", "domain": "local_todo", "title": "Other"}])
    got = svc.move_list("bere", "shopping", "personal")
    assert got["shared_list_removed"] is False and "still in Home Assistant" in got["note"]
    assert fake.deleted == [] and "todo.shopping_list" in fake.lists
    assert [i["text"] for i in saved(tmp_path, "bere")["lists"][0]["items"]] == ["Peas"]
    # Without the admin login: copied, and the shared list stays.
    svc, fake = make(tmp_path / "b", admin=False)
    got = svc.move_list("bere", "shopping", "personal")
    assert got["shared_list_removed"] is False and "admin login" in got["note"]
    # Two entries with the same name: never guess.
    svc, fake = make(tmp_path / "c", {"todo.x": ("X", [])}, entries=[
        {"entry_id": "e1", "domain": "local_todo", "title": "X"}, {"entry_id": "e2", "domain": "local_todo", "title": "x"}])
    assert svc.move_list("bere", "x", "personal")["shared_list_removed"] is False and fake.deleted == []


def test_move_list_checks_its_arguments(tmp_path):
    svc, _ = make(tmp_path)
    with pytest.raises(hs.Refused, match="Say to"):
        svc.move_list("bere", "shopping", "elsewhere")
    with pytest.raises(hs.Refused, match="Say which list"):
        svc.move_list("bere", "", "shared")


# ── the host ─────────────────────────────────────────────────────────────────

def test_list_owner():
    assert hh.list_owner({"owners": ["bere"]}) == "bere"
    assert hh.list_owner({"owners": ["greg", "bere"]}) == ""
    assert hh.list_owner({"owners": ["greg"], "role": "household"}) == ""
    assert hh.list_owner({"owners": []}) == "" and hh.list_owner({}) == ""
    assert hh.list_owner({"owners": ["../x"]}) == ""


def test_up_gives_the_service_a_private_lists_volume_and_each_agent_its_person(tmp_path):
    env = write_settings(tmp_path, "HA_URL=http://192.0.2.10\nHA_TOKEN=ha-secret\n")
    agents = {"household": {"name": "household", "role": "household", "owners": ["greg", "bere"],
                            "house": {"enabled": True, "domains": ["todo"]}},
              "bere": {"name": "bere", "owners": ["bere"], "house": {"enabled": True, "domains": ["todo"]}}}
    rt = FakeRuntime(env, agents, {"logos-home-household": "LOGOS_HOUSE_TOKEN=tok\n",
                                   "logos-home-bere": "LOGOS_HOUSE_TOKEN=btok\n"})
    assert hh.HouseHost(rt).up() is True
    cfg = json.loads([i for i in rt.inputs if i][0])
    assert cfg["agents"]["bere"]["person"] == "bere" and "person" not in cfg["agents"]["household"]
    assert ["docker", "volume", "create", "logos-lists"] in rt.calls
    (start,) = [c for c in rt.calls if c[:3] == ["docker", "run", "-d"]]
    assert "logos-lists:/lists" in start and "HOUSE_LISTS=/lists" in start and "--read-only" in start
    assert any("chmod 0700 /l" in " ".join(c) for c in rt.calls)
