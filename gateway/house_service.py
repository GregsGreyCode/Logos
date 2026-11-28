"""The Logos house service: how the agents work the lights (story H1) and the shared lists.

The agent never holds a Home Assistant login. It calls this service (MCP over HTTP, one bearer
token per agent), and the service calls Home Assistant with its own non-admin account. What the
agent may touch is decided here, not left to the agent or to Home Assistant (whose users cannot
be limited to some devices):

  * Only the domains in the agent's definition (``light`` and ``scene`` to start with), minus
    anything in its ``exclude`` list.
  * Only these Home Assistant services can ever be called: ``light.turn_on``,
    ``light.turn_off``, ``scene.turn_on``, and for the shared lists ``todo.get_items``,
    ``todo.add_item``, ``todo.update_item`` and ``todo.remove_item``. Nothing the agent sends is
    passed through as a service name or as raw service data.
  * Locks, alarms, heating, covers and cameras are refused whatever the definition says: they
    need a confirmation step this service does not have yet.

The shared lists (shopping, to-do) are Home Assistant to-do lists (``todo.*``), so Greg and Béré
see the same list in the Home Assistant app and every agent with the ``todo`` domain works on it.

Creating a list (``create_list``) is the one thing that needs a Home Assistant ADMIN: a new list
is a new "Local To-do" config entry. When the optional admin login is configured
(``ha_admin_url``/``ha_admin_token``), :class:`ListMaker` holds it and can do two things with it:
run the ``local_todo`` config flow with a checked name, and (for ``move_list`` to personal, after
the items are copied) remove the one Local To-do entry with that name. It never reads states or calls
services; everything else, including finding the new list afterwards, uses the non-admin login.
The admin token is never logged or returned. At most ``CREATE_PER_DAY`` lists per agent a day.

Personal lists (Greg, 28 Nov) are lists only their person sees, e.g. a list of video games to
play: this service keeps them, not Home Assistant, one file per person (``<person>.json`` in
``$HOUSE_LISTS``, the ``logos-lists`` volume; written whole and atomically, mode 0600). An agent
with ONE owner (``person`` in its config) works on that person's personal lists with the same
tools as the shared ones; the household agent has no person, so it gets none. The person comes
from the config, never from anything the agent sends, so no agent can reach another person's
lists. Every list in an answer says which kind it is: "personal" or "shared".

``move_list`` turns one kind into the other. To shared: a new Home Assistant list (as
create_list), the open items copied, then the personal list removed. To personal: every item
copied, then the Home Assistant list removed with the admin login, but only a Local To-do list
whose config entry has exactly that name; anything else (the Shopping List integration, a list
renamed in Home Assistant) stays where it is and the answer says so.

Rooms come from Home Assistant itself: a light whose state lists member lights (a Hue room or
zone, or a Home Assistant light group) is a room, and Hue scenes carry their room's name.

It runs in a plain Python image with no DNS (``ha_url`` is an address, not a name).
Run:  python house_service.py  (config from $HOUSE_CONFIG, personal lists in $HOUSE_LISTS)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "2025-06-18"
#: Domains an agent may be given. Anything else is refused even if a definition names it.
SUPPORTED_DOMAINS = ("light", "scene", "todo")
SERVICES = {("light", "turn_on"), ("light", "turn_off"), ("scene", "turn_on"),
            ("todo", "add_item"), ("todo", "update_item"), ("todo", "remove_item")}
MAX_ITEMS = 30                  # items in one call
TITLE_MAX = 60                  # the Home Assistant app cuts longer titles in the list; the rest goes in details
SET_DUE_DATE, SET_DESCRIPTION = 16, 64   # Home Assistant TodoListEntityFeature bits
#: Never, whatever the definition says: these need a confirmation step first.
NEVER = ("lock", "alarm_control_panel", "climate", "cover", "camera", "siren", "valve", "water_heater")
MAX_TARGETS = 40
SETTLE_SECONDS = 1.5            # Hue reports the new state about a second after the command
EVERYWHERE = {"all", "everything", "everywhere", "whole house", "house", "home", "all lights"}
LIST_NAME_MAX = 40
LIST_NAME_RE = re.compile(r"(?:[^\W_]|[ '\u2019&-])+")      # letters (any language), digits, spaces, ' & -
CREATE_PER_DAY = 5              # new lists per agent in 24 hours
CREATE_WAIT = 5.0               # seconds to wait for the new list to show up in Home Assistant's states
PERSON_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")    # a dashboard username: the name of its file
PERSONAL_LISTS_MAX = 50         # personal lists one person may have
PERSONAL_ITEMS_MAX = 500        # items on one personal list
NOT_PERSONAL = ("Personal lists belong to one person and their own agent; you work on the shared lists only "
                "(leave shared out).")
NO_ADMIN = ("Creating lists needs Home Assistant admin access: Greg can turn it on (docs/house.md, "
            "'Creating lists'). Until then, Greg can add one in Home Assistant: Settings > Devices & "
            "services > Add integration > Local To-do.")


class Refused(Exception):
    """Something the service will not do. ``str()`` is written for the agent to read."""


def norm(text: Any) -> str:
    """'The Kitchen lights' -> 'kitchen'; how names are compared."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    s = re.sub(r"\b(the|lights?|lamps?)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── Home Assistant ───────────────────────────────────────────────────────────

class HomeAssistant:
    def __init__(self, url: str, token: str, http: Optional[Callable] = None):
        self.url = url.rstrip("/")
        self.token = token
        self.http = http or self._urllib

    @staticmethod
    def _urllib(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError:
            return 0, b""

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        body = json.dumps(payload).encode() if payload is not None else None
        status, raw = self.http(method, self.url + path, headers, body)
        if status == 0:
            raise Refused("Home Assistant is not answering, so nothing was changed. Say so; do not retry in a loop.")
        if status in (401, 403):
            raise Refused("Home Assistant refused the house service's login. Tell your people; it needs fixing by hand.")
        if status >= 400:
            raise Refused(f"Home Assistant refused the request ({status}).")
        return json.loads(raw or b"null")

    def states(self) -> List[Dict[str, Any]]:
        return self._call("GET", "/api/states") or []

    def service(self, domain: str, service: str, data: Dict[str, Any]) -> None:
        if (domain, service) not in SERVICES:
            raise Refused(f"{domain}.{service} is not something this service does.")
        self._call("POST", f"/api/services/{domain}/{service}", data)

    def todo_items(self, entity_id: str) -> List[Dict[str, Any]]:
        """Every item on one to-do list, open and done (``todo.get_items`` returns a response)."""
        if not entity_id.startswith("todo."):
            raise Refused("That is not a list.")
        got = self._call("POST", "/api/services/todo/get_items?return_response", {"entity_id": entity_id}) or {}
        return list((((got.get("service_response") or {}).get(entity_id)) or {}).get("items") or [])


class ListMaker:
    """The admin login, good for two things: a new Local To-do list (Home Assistant's
    ``local_todo`` config flow) and removing one by its name. It has no way to read states or call
    services."""

    def __init__(self, url: str, token: str, http: Optional[Callable] = None):
        self._ha = HomeAssistant(url, token, http)

    def _call(self, method: str, path: str, payload: Any = None) -> Any:
        try:
            return self._ha._call(method, path, payload)
        except Refused as e:
            if "login" in str(e):
                raise Refused("Home Assistant refused the admin login used for creating lists. "
                              "Tell your people; it needs fixing by hand (docs/house.md).")
            raise

    def create(self, name: str) -> None:
        """Run the flow for a list called ``name`` (already checked). Raises Refused if Home
        Assistant does not make it."""
        flow = self._call("POST", "/api/config/config_entries/flow",
                          {"handler": "local_todo", "show_advanced_options": False}) or {}
        flow_id = str(flow.get("flow_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", flow_id) or flow.get("type") != "form":
            raise Refused("Home Assistant would not start a new list "
                          f"({flow.get('reason') or flow.get('type') or 'no answer'}).")
        try:
            done = self._call("POST", f"/api/config/config_entries/flow/{flow_id}", {"todo_list_name": name}) or {}
        except Refused:
            self._abandon(flow_id)
            raise
        if done.get("type") != "create_entry":
            self._abandon(flow_id)
            why = done.get("reason") or ", ".join(str(v) for v in (done.get("errors") or {}).values())
            raise Refused(f"Home Assistant did not make the list ({why or done.get('type') or 'no answer'}).")

    def remove(self, name: str) -> bool:
        """Remove the Local To-do list whose config entry is called ``name`` (case and spacing
        ignored; exactly one such entry). False, with nothing removed, when there is none: another
        kind of list (the Shopping List integration) or one renamed in Home Assistant."""
        entries = self._call("GET", "/api/config/config_entries/entry?domain=local_todo") or []
        want = re.sub(r"\s+", " ", name).strip().lower()
        hits = [e for e in entries if isinstance(e, dict) and e.get("domain") == "local_todo"
                and re.sub(r"\s+", " ", str(e.get("title") or "")).strip().lower() == want]
        entry_id = str(hits[0].get("entry_id") or "") if len(hits) == 1 else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", entry_id):
            return False
        self._call("DELETE", f"/api/config/config_entries/entry/{entry_id}")
        return True

    def _abandon(self, flow_id: str) -> None:
        try:
            self._ha._call("DELETE", f"/api/config/config_entries/flow/{flow_id}")
        except Exception:
            pass


# ── personal lists (Greg, 28 Nov) ────────────────────────────────────────────

class PersonalLists:
    """Each person's own lists, one JSON file each: ``{"version", "owner", "lists": [{"name", "owner",
    "created_at", "items": [{"text", "done", "added_at", "done_at", "details"?, "due"?}]}]}``.
    Times are UTC, ``2026-11-28T09:30:00Z``. Callers hold :attr:`lock` from load to save."""

    def __init__(self, root: str, clock: Callable[[], float] = time.time):
        self.root, self.clock = root, clock
        self.lock = threading.RLock()

    def now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))

    def _path(self, person: str) -> str:
        if not PERSON_RE.fullmatch(person or ""):
            raise Refused("Personal lists need a person.")
        return os.path.join(self.root, person + ".json")

    def load(self, person: str) -> List[Dict[str, Any]]:
        try:
            with open(self._path(person), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            raise Refused("The personal lists could not be read just now, so nothing was changed. Say so.")
        return [l for l in (data.get("lists") or []) if isinstance(l, dict) and l.get("name")]

    def save(self, person: str, lists: List[Dict[str, Any]]) -> None:
        """Write the whole file at once: a temporary file beside it, flushed, then renamed over it."""
        path = self._path(person)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{person}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "owner": person, "lists": lists}, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:                                          # so the rename itself survives a power cut
            dfd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


# ── what the house looks like ────────────────────────────────────────────────

class House:
    """The allowed part of Home Assistant's states, with rooms worked out."""

    def __init__(self, states: List[Dict[str, Any]], domains: List[str], exclude: List[str]):
        allowed = {d for d in domains if d in SUPPORTED_DOMAINS}
        skip = set(exclude or [])
        self.by_id: Dict[str, Dict[str, Any]] = {}
        for s in states:
            eid = str(s.get("entity_id") or "")
            domain = eid.split(".", 1)[0]
            if domain in allowed and domain not in NEVER and eid not in skip:
                self.by_id[eid] = s
        self.rooms: Dict[str, Dict[str, Any]] = {}       # group entity id -> state
        self.lights: Dict[str, Dict[str, Any]] = {}
        for eid, s in self.by_id.items():
            if not eid.startswith("light."):
                continue
            members = (s.get("attributes") or {}).get("entity_id")
            if isinstance(members, list) and members:
                self.rooms[eid] = s
            else:
                self.lights[eid] = s
        # A room, zone or group switches its members itself: one with a member that is excluded,
        # never allowed or unknown is never sent as a whole, only as its allowed lights (split).
        everything = {str(s.get("entity_id") or ""): s for s in states}
        self.split: Dict[str, List[str]] = {}
        for rid in self.rooms:
            leaves = self._leaves(rid, everything)
            if any(l not in self.lights for l in leaves):
                self.split[rid] = sorted(l for l in leaves if l in self.lights)
        # A scene may set anything: Home Assistant's own scenes list what they set (only allowed
        # lights pass), and a Hue scene sets its room (only a room sent whole passes).
        rooms_by_name = {norm(self.name(s)): rid for rid, s in self.rooms.items()}
        self.scenes = {}
        for eid, s in self.by_id.items():
            if not eid.startswith("scene."):
                continue
            a = s.get("attributes") or {}
            sets = a.get("entity_id")
            if isinstance(sets, list) and sets:
                if all(m in self.lights or (m in self.rooms and m not in self.split) for m in sets):
                    self.scenes[eid] = s
            elif a.get("group_name"):
                rid = rooms_by_name.get(norm(a.get("group_name")))
                if rid is not None and rid not in self.split:
                    self.scenes[eid] = s
        self.lists = {eid: s for eid, s in self.by_id.items() if eid.startswith("todo.")}

    @staticmethod
    def _leaves(eid: str, everything: Dict[str, Dict[str, Any]], seen: frozenset = frozenset()) -> set:
        """The single entities a group switches, through groups inside groups."""
        members = ((everything.get(eid) or {}).get("attributes") or {}).get("entity_id")
        if eid in seen or not (isinstance(members, list) and members):
            return {eid}
        out: set = set()
        for m in members:
            out |= House._leaves(str(m), everything, seen | {eid})
        return out

    @staticmethod
    def name(s: Dict[str, Any]) -> str:
        return str((s.get("attributes") or {}).get("friendly_name") or s.get("entity_id"))

    def members(self, room_id: str) -> List[str]:
        return [m for m in (self.rooms[room_id].get("attributes") or {}).get("entity_id") or [] if m in self.lights]

    def room_kind(self, room_id: str) -> str:
        return str((self.rooms[room_id].get("attributes") or {}).get("hue_type") or "group")

    def room_of(self, light_id: str) -> str:
        """The room (not zone) a light is in, by name, or ''."""
        for rid in sorted(self.rooms, key=lambda r: (self.room_kind(r) != "room", r)):
            if light_id in self.members(rid):
                return self.name(self.rooms[rid])
        return ""

    def resolve_lights(self, targets: List[str]) -> List[str]:
        """Names (a room, a zone, a light, or 'everything') -> entity ids to send the command to.
        A whole room is sent as the room itself, so Hue changes it in one go."""
        out: List[str] = []
        for raw in targets:
            want = norm(raw)
            if not want:
                continue
            if want in EVERYWHERE:
                rooms = [r for r in self.rooms if self.room_kind(r) == "room"] or list(self.rooms)
                covered = {m for r in rooms for m in self.members(r)}
                out += rooms + [l for l in self.lights if l not in covered]
                continue
            every = {**self.rooms, **self.lights}
            exact = str(raw).strip().lower()
            hits = [eid for eid, s in every.items() if exact in (self.name(s).lower(), eid.lower())]
            if not hits:
                hits = [eid for eid, s in every.items()
                        if want in (norm(self.name(s)), norm(eid.split(".", 1)[1].replace("_", " ")))]
                rooms = [h for h in hits if h in self.rooms]
                if len(rooms) == 1 and all(h in rooms or h in self.members(rooms[0]) for h in hits):
                    hits = rooms                 # "the bathroom" = the room, not also its one lamp
            if not hits:
                hits = [eid for eid, s in {**self.rooms, **self.lights}.items() if want in norm(self.name(s))]
                if len(hits) > 1:
                    names = sorted(self.name(self.by_id[h]) for h in hits)
                    raise Refused(f"'{raw}' could mean several lights: {', '.join(names)}. Say which.")
            if not hits:
                raise Refused(f"There is no light or room called '{raw}'. "
                              f"Rooms: {', '.join(sorted(self.name(s) for s in self.rooms.values())) or 'none'}. "
                              "Use list_lights to see every light.")
            out += hits
        seen, unique = set(), []
        for eid in [l for e in out for l in self.split.get(e, [e])]:
            if eid not in seen:
                seen.add(eid)
                unique.append(eid)
        if not unique:
            raise Refused("Say which lights: a room, a light, or 'everything'.")
        if len(unique) > MAX_TARGETS:
            raise Refused(f"That is more than {MAX_TARGETS} lights at once; name rooms instead.")
        return unique

    def resolve_scene(self, scene: str, room: str = "") -> str:
        want, where = norm(scene), norm(room)
        found = []
        for eid, s in self.scenes.items():
            a = s.get("attributes") or {}
            scene_name = norm(a.get("name") or self.name(s))
            group = norm(a.get("group_name") or "")
            if where and group != where and not norm(self.name(s)).startswith(where):
                continue
            if want in (scene_name, norm(self.name(s))):
                found.append(eid)
        if len(found) == 1:
            return found[0]
        if not found:
            raise Refused(f"There is no scene '{scene}'{' in ' + room if room else ''}. Use list_scenes.")
        rooms = sorted({str((self.scenes[e].get("attributes") or {}).get("group_name") or "?") for e in found})
        raise Refused(f"'{scene}' is a scene in several rooms ({', '.join(rooms)}). Say which room.")


    def resolve_list(self, name: str) -> str:
        """'shopping' -> todo.shopping_list. With one list, an empty name means that one."""
        if not self.lists:
            raise Refused("There are no shared lists in Home Assistant yet. Greg can add one "
                          "(Settings > Devices & services > Add integration > Local To-do).")
        want = list_norm(name)
        if not want:
            if len(self.lists) == 1:
                return next(iter(self.lists))
            raise Refused(f"Say which list: {', '.join(sorted(self.name(s) for s in self.lists.values()))}.")
        hits = [eid for eid, s in self.lists.items()
                if want in (list_norm(self.name(s)), list_norm(eid.split(".", 1)[1].replace("_", " ")))]
        if not hits:
            hits = [eid for eid, s in self.lists.items() if want in list_norm(self.name(s))]
        if len(hits) == 1:
            return hits[0]
        names = ", ".join(sorted(self.name(self.lists[h]) for h in hits) if hits
                          else sorted(self.name(s) for s in self.lists.values()))
        raise Refused(f"'{name}' could be several lists: {names}. Say which." if hits
                      else f"There is no list called '{name}'. Lists: {names}.")


def list_norm(text: Any) -> str:
    """'The shopping list' -> 'shopping'; how list names are compared."""
    s = re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())
    s = re.sub(r"\btodos?\b", "to do", s)                 # "todo", "to-do" and "to do" are one name
    s = re.sub(r"\b(the|our|my|lists?)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_list_name(text: Any) -> str:
    """A new list's name: one line, 1-40 letters, digits, spaces and ' & -."""
    name = re.sub(r"\s+", " ", str(text or "")).strip()
    if not name:
        raise Refused("Say what the new list is called.")
    if len(name) > LIST_NAME_MAX:
        raise Refused(f"A list name is at most {LIST_NAME_MAX} characters; keep it short (e.g. 'Wedding').")
    if not LIST_NAME_RE.fullmatch(name):
        raise Refused("A list name is letters, numbers, spaces and ' & - only.")
    return name


def clean_item(text: Any) -> str:
    """One item as it goes on the list: one line, trimmed, at most 200 characters."""
    item = re.sub(r"\s+", " ", str(text or "")).strip()
    if not item:
        raise Refused("An item cannot be empty.")
    if len(item) > 200:
        raise Refused("An item is at most 200 characters; keep it short.")
    return item


def clean_due(value: Any) -> str:
    """A due date as YYYY-MM-DD, or ''."""
    due = str(value or "").strip()
    if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        raise Refused("A due date is written YYYY-MM-DD (run date first to work it out).")
    return due


def entry(raw: Any) -> Dict[str, str]:
    """One item to add: a plain string, or {"item", "details", "due"}."""
    if isinstance(raw, dict):
        out = {"item": clean_item(raw.get("item")), "details": str(raw.get("details") or "").strip(),
               "due": clean_due(raw.get("due"))}
    else:
        out = {"item": clean_item(raw), "details": "", "due": ""}
    if len(out["details"]) > 1000:
        raise Refused("Details are at most 1000 characters.")
    if len(out["item"]) > TITLE_MAX:
        raise Refused(f"'{out['item'][:40]}...' is too long for a title ({len(out['item'])} characters; the app "
                      f"cuts them after about {TITLE_MAX}). Keep the title short in their words and put "
                      "the rest in details.")
    return out


def light_view(s: Dict[str, Any]) -> Dict[str, Any]:
    a = s.get("attributes") or {}
    out: Dict[str, Any] = {"name": House.name(s), "state": s.get("state")}
    if s.get("state") == "on":
        if a.get("brightness") is not None:
            out["brightness"] = round(int(a["brightness"]) * 100 / 255)
        if a.get("color_mode") == "color_temp" and a.get("color_temp_kelvin"):
            out["kelvin"] = int(a["color_temp_kelvin"])
        elif a.get("rgb_color"):
            out["rgb"] = list(a["rgb_color"])
    return out


# ── the service ──────────────────────────────────────────────────────────────

class Service:
    """``agents``: name -> {"token", "domains": [...], "exclude": [entity ids]}."""

    def __init__(self, ha: HomeAssistant, agents: Dict[str, Dict[str, Any]], log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, lists_admin: Optional[ListMaker] = None,
                 clock: Callable[[], float] = time.time, personal: Optional[PersonalLists] = None):
        self.ha, self.agents, self.log, self.sleep = ha, agents, log, sleep
        self.lists_admin, self.clock = lists_admin, clock
        self.personal = personal                       # None: no personal lists (no volume for them)
        self._lists_lock = personal.lock if personal is not None else threading.RLock()
        self._created: Dict[str, List[float]] = {}     # agent -> when it created lists (rate limit)
        self._create_lock = threading.Lock()

    def agent_for_token(self, token: str) -> Optional[str]:
        for name, a in self.agents.items():
            if token and a.get("token") and hmac.compare_digest(token.encode(), a["token"].encode()):
                return name
        return None

    def house(self, agent: str) -> House:
        a = self.agents[agent]
        return House(self.ha.states(), list(a.get("domains") or []), list(a.get("exclude") or []))

    def _need(self, agent: str, domain: str) -> None:
        if domain not in (self.agents[agent].get("domains") or []):
            raise Refused(f"You are not set up to control {domain}s.")

    # tools

    def list_lights(self, agent: str, room: str = "") -> Dict[str, Any]:
        self._need(agent, "light")
        h = self.house(agent)
        rooms = []
        placed = set()
        for rid in sorted(h.rooms, key=lambda r: (h.room_kind(r) != "room", h.name(h.rooms[r]))):
            if room and norm(room) != norm(h.name(h.rooms[rid])):
                continue
            members = h.members(rid)
            if h.room_kind(rid) == "room":
                placed.update(members)
            on = sum(1 for m in members if h.lights[m].get("state") == "on")
            rooms.append({"room": h.name(h.rooms[rid]), "kind": h.room_kind(rid),
                          "on": f"{on} of {len(members)}", "lights": [light_view(h.lights[m]) for m in members]})
        if room and not rooms:
            raise Refused(f"There is no room called '{room}'. Rooms: "
                          f"{', '.join(sorted(h.name(s) for s in h.rooms.values())) or 'none'}.")
        out: Dict[str, Any] = {"rooms": rooms}
        loose = [light_view(s) for eid, s in sorted(h.lights.items()) if eid not in placed]
        if loose and not room:
            out["not_in_a_room"] = loose
        self.log(f"{agent} list_lights{' ' + room if room else ''}: {len(rooms)} rooms")
        return out

    def set_lights(self, agent: str, targets: List[str], on: Any, brightness: Any = None, kelvin: Any = None,
                   color: str = "", transition: Any = None) -> Dict[str, Any]:
        self._need(agent, "light")
        if not isinstance(on, bool):
            raise Refused("Say on: true or on: false.")
        h = self.house(agent)
        ids = h.resolve_lights(targets if isinstance(targets, list) else [targets])
        data: Dict[str, Any] = {"entity_id": ids}
        try:
            if transition not in (None, ""):
                data["transition"] = max(0.0, min(float(transition), 60.0))
            if on:
                if brightness not in (None, ""):
                    b = int(brightness)
                    if not 1 <= b <= 100:
                        raise Refused("Brightness is a percentage from 1 to 100 (use on: false for off).")
                    data["brightness_pct"] = b
                if kelvin not in (None, "") and color:
                    raise Refused("Give a colour or a white temperature (kelvin), not both.")
                if kelvin not in (None, ""):
                    data["color_temp_kelvin"] = max(2000, min(int(kelvin), 6500))
                if color:
                    if not re.fullmatch(r"[a-z]{3,20}", str(color).strip().lower().replace(" ", "")):
                        raise Refused("A colour is one plain name, like 'red', 'orange' or 'warmwhite'.")
                    data["color_name"] = str(color).strip().lower().replace(" ", "")
        except (TypeError, ValueError):
            raise Refused("Brightness, kelvin and transition are numbers.")
        self.ha.service("light", "turn_on" if on else "turn_off", data)
        self.log(f"{agent} set_lights {'on' if on else 'off'} {','.join(ids)}"
                 + "".join(f" {k}={v}" for k, v in data.items() if k != "entity_id"))
        self.sleep(SETTLE_SECONDS)
        after = self.house(agent)
        now = []
        for eid in ids:
            s = after.by_id.get(eid)
            if s is not None:
                now.append(light_view(s))
        unavailable = [v["name"] for v in now if v["state"] == "unavailable"]
        out: Dict[str, Any] = {"done": "on" if on else "off", "now": now}
        if unavailable:
            out["not_reachable"] = unavailable
        return out

    def list_scenes(self, agent: str, room: str = "") -> Dict[str, Any]:
        self._need(agent, "scene")
        h = self.house(agent)
        by_room: Dict[str, List[str]] = {}
        for s in h.scenes.values():
            a = s.get("attributes") or {}
            where = str(a.get("group_name") or "")
            if room and norm(where) != norm(room):
                continue
            by_room.setdefault(where or "Other", []).append(str(a.get("name") or House.name(s)))
        if room and not by_room:
            raise Refused(f"No scenes for '{room}'.")
        self.log(f"{agent} list_scenes{' ' + room if room else ''}")
        return {"scenes": [{"room": r, "scenes": sorted(n)} for r, n in sorted(by_room.items())]}

    def activate_scene(self, agent: str, scene: str, room: str = "") -> Dict[str, Any]:
        self._need(agent, "scene")
        h = self.house(agent)
        eid = h.resolve_scene(scene, room)
        self.ha.service("scene", "turn_on", {"entity_id": eid})
        a = h.scenes[eid].get("attributes") or {}
        self.log(f"{agent} activate_scene {eid}")
        return {"activated": {"scene": str(a.get("name") or h.name(h.scenes[eid])),
                              "room": str(a.get("group_name") or "")}}


    # lists: the shared ones in Home Assistant, and each person's own (Greg, 28 Nov)

    def _person(self, agent: str) -> str:
        """Whose personal lists this agent works on: its one owner, or '' (the household agent, or
        personal lists not turned on). Only ever from the config, never from the agent."""
        p = str(self.agents[agent].get("person") or "")
        return p if self.personal is not None and PERSON_RE.fullmatch(p) else ""

    def _items(self, eid: str) -> List[Dict[str, Any]]:
        return [{"item": str(i.get("summary") or ""), "done": i.get("status") == "completed",
                 "details": str(i.get("description") or ""), "due": str(i.get("due") or "")}
                for i in self.ha.todo_items(eid)]

    @staticmethod
    def _personal_items(l: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [{"item": str(i.get("text") or ""), "done": bool(i.get("done")),
                 "details": str(i.get("details") or ""), "due": str(i.get("due") or "")}
                for i in l.get("items") or []]

    @staticmethod
    def _show(i: Dict[str, Any]) -> Any:
        """An item for the agent: its title, or the title with its details and due date."""
        extra = {k: i[k] for k in ("details", "due") if i.get(k)}
        return {"item": i["item"], **extra} if extra else i["item"]

    @staticmethod
    def _features(h: "House", eid: str) -> int:
        try:
            return int((h.lists[eid].get("attributes") or {}).get("supported_features") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _pick(on_list: List[str], items: Any) -> List[str]:
        """The items on a list as written there, for names the agent gave (case and spacing
        ignored; a unique partial match counts)."""
        wanted = items if isinstance(items, list) else [items]
        if not wanted or len(wanted) > MAX_ITEMS:
            raise Refused(f"Name between 1 and {MAX_ITEMS} items.")
        out = []
        for raw in wanted:
            want = clean_item(raw).lower()
            hits = [s for s in on_list if s.strip().lower() == want] or [s for s in on_list if want in s.lower()]
            if not hits:
                raise Refused(f"'{raw}' is not on the list. Use list_items to see what is.")
            if len(set(hits)) > 1:
                raise Refused(f"'{raw}' could be {', '.join(sorted(set(hits)))}. Say which.")
            out.append(hits[0])
        return out

    def _match(self, eid: str, items: Any) -> List[str]:
        return self._pick([str(i.get("summary") or "") for i in self.ha.todo_items(eid)], items)

    def _find(self, agent: str, name: str, shared: Optional[bool] = None) -> Tuple[str, Any, Any]:
        """The list called ``name``: ``("shared", House, entity id)`` or ``("personal", every personal
        list, that list)``. An agent with no person looks only in Home Assistant, as before personal
        lists. ``shared`` True or False looks in one kind only. Hold ``_lists_lock``."""
        person = self._person(agent)
        if shared is False and not person:
            raise Refused(NOT_PERSONAL)
        if shared is True or not person:
            h = self.house(agent)
            return "shared", h, h.resolve_list(name)
        mine = self.personal.load(person)
        h, unread = None, ""
        if shared is None:
            try:
                h = self.house(agent)
            except Refused as e:                  # Home Assistant down: the personal lists still work
                unread = f" (the shared lists could not be read just now: {e})"
        cands = [("personal", l["name"], l["name"]) for l in mine]
        if h is not None:
            cands += [("shared", eid, h.name(s)) for eid, s in sorted(h.lists.items())]
        label = lambda c: f"{c[2]} ({c[0]})"
        if not cands:
            raise Refused(("You have no personal lists yet" if shared is False else "There are no lists yet")
                          + f"; make one with create_list.{unread}")
        want = list_norm(name)
        if not want:
            if len(cands) != 1:
                raise Refused(f"Say which list: {', '.join(sorted(label(c) for c in cands))}.")
            hit = cands[0]
        else:
            def keys(c):
                out = {list_norm(c[2])}
                if c[0] == "shared":
                    out.add(list_norm(c[1].split(".", 1)[1].replace("_", " ")))
                return out
            hits = [c for c in cands if want in keys(c)] or [c for c in cands if want in list_norm(c[2])]
            if len(hits) > 1:                      # "my games" is the personal one, "our/shared games" the other
                said = re.sub(r"\s+", " ", str(name)).strip().lower()
                prefer = ("personal" if re.match(r"(my|personal)\b", said) else
                          "shared" if re.match(r"(our|shared|the shared|household)\b", said) else "")
                hits = [c for c in hits if c[0] == prefer] if sum(c[0] == prefer for c in hits) == 1 else hits
            if not hits:
                raise Refused(f"There is no list called '{name}'. Lists: "
                              f"{', '.join(sorted(label(c) for c in cands))}.{unread}")
            if len(hits) > 1:
                raise Refused(f"'{name}' could be several lists: {', '.join(sorted(label(c) for c in hits))}. "
                              "Say which: shared: true for the shared one, shared: false for the personal one.")
            hit = hits[0]
        if hit[0] == "shared":
            return "shared", h, hit[1]
        return "personal", mine, next(l for l in mine if l["name"] == hit[1])

    def _view(self, kind: str, where: Any, which: Any, done: Any) -> Dict[str, Any]:
        if kind == "shared":
            items, name = self._items(which), where.name(where.lists[which])
            # "id": its Home Assistant entity, so a link can open this very list in the app (Greg, 21 Nov).
            extra: Dict[str, Any] = {"id": which}
        else:
            items, name, extra = self._personal_items(which), which["name"], {}
        return {"list": name, "kind": kind, **extra,
                "open": [self._show(i) for i in items if not i["done"]],
                **({"done": [i["item"] for i in items if i["done"]]} if done is True else {})}

    def list_items(self, agent: str, list_name: str = "", done: Any = False, shared: Optional[bool] = None) -> Dict[str, Any]:
        self._need(agent, "todo")
        person = self._person(agent)
        with self._lists_lock:
            if not person:
                if shared is False:
                    raise Refused(NOT_PERSONAL)
                h = self.house(agent)
                eids = [h.resolve_list(list_name)] if list_name or len(h.lists) == 1 else sorted(h.lists)
                if not eids:
                    h.resolve_list("")                       # says there are no lists
                lists = [self._view("shared", h, eid, done) for eid in eids]
                self.log(f"{agent} list_items {','.join(eids)}")
                return {"lists": lists}
            if list_name:
                kind, where, which = self._find(agent, list_name, shared)
                self.log(f"{agent} list_items {kind}")
                return {"lists": [self._view(kind, where, which, done)]}
            lists, out = [], {}
            if shared is not False:
                try:
                    h = self.house(agent)
                    lists += [self._view("shared", h, eid, done) for eid in sorted(h.lists)]
                except Refused as e:
                    out["note"] = f"The shared lists could not be read just now: {e}"
            if shared is not True:
                mine = self.personal.load(person)
                lists += [self._view("personal", mine, l, done) for l in mine]
            self.log(f"{agent} list_items: {len(lists)} lists")
            return {"lists": lists, **out}

    def create_list(self, agent: str, name: Any, shared: Optional[bool] = None) -> Dict[str, Any]:
        """A new list. A person's own agent makes a personal one unless ``shared`` is true; the
        household agent makes shared ones only (a Home Assistant Local To-do list). A name that is
        already a list of that kind gives that list back instead. Lists are read fresh on every
        call (there is no cache to refresh), so add_items can use the new one at once."""
        self._need(agent, "todo")
        person = self._person(agent)
        if shared is None:
            shared = not person
        name = clean_list_name(name)
        if not shared:
            if not person:
                raise Refused(NOT_PERSONAL)
            return self._create_personal(agent, person, name)
        with self._create_lock:
            h = self.house(agent)
            same = self._same_list(h, name)
            if same:
                self.log(f"{agent} create_list: '{name}' is already {same}")
                return {"list": h.name(h.lists[same]), "kind": "shared", "id": same, "already_existed": True}
            if self.lists_admin is None:
                raise Refused(NO_ADMIN)
            now = self.clock()
            recent = [t for t in self._created.get(agent, []) if now - t < 86400]
            if len(recent) >= CREATE_PER_DAY:
                raise Refused(f"You have made {CREATE_PER_DAY} lists today, the most in a day. "
                              "Use an existing list, or ask again tomorrow.")
            self._created[agent] = recent + [now]
            before = set(h.lists)
            self.lists_admin.create(name)
            self.log(f"house: {agent} created list {name}")
            waited, found = 0.0, None
            while True:
                h = self.house(agent)                   # the non-admin login: can it see the new list?
                new = [e for e in h.lists if e not in before]
                found = self._same_list(h, name, among=new) or (new[0] if len(new) == 1 else None)
                if found or waited >= CREATE_WAIT:
                    break
                self.sleep(0.5)
                waited += 0.5
        if not found:
            self.log(f"house: {agent} created list {name} but the house service cannot see it")
            return {"list": name, "kind": "shared", "created": True,
                    "note": ("Home Assistant made the list, but it is not showing up for the house service "
                             "yet. Say so; it may need a moment, or Greg may need to check it.")}
        self.log(f"house: {agent} list {name} is {found}")
        return {"list": h.name(h.lists[found]), "kind": "shared", "id": found, "created": True}

    @staticmethod
    def _same_name(name: str, have: str) -> bool:
        want = list_norm(name)
        return bool(want and want == list_norm(have)) or (
            re.sub(r"\s+", " ", name).strip().lower() == re.sub(r"\s+", " ", have).strip().lower())

    def _create_personal(self, agent: str, person: str, name: str) -> Dict[str, Any]:
        with self._lists_lock:
            mine = self.personal.load(person)
            same = next((l for l in mine if self._same_name(name, l["name"])), None)
            if same:
                return {"list": same["name"], "kind": "personal", "already_existed": True}
            if len(mine) >= PERSONAL_LISTS_MAX:
                raise Refused(f"You have {PERSONAL_LISTS_MAX} personal lists, the most there can be. "
                              "Use one of them, or delete one you no longer need.")
            mine.append({"name": name, "owner": person, "created_at": self.personal.now(), "items": []})
            self.personal.save(person, mine)
        self.log(f"{agent} created personal list for {person}")
        return {"list": name, "kind": "personal", "created": True}

    @staticmethod
    def _same_list(h: "House", name: str, among: Optional[List[str]] = None) -> Optional[str]:
        """The list already called ``name`` (case, spacing and words like 'list' ignored)."""
        for eid in (among if among is not None else sorted(h.lists)):
            if Service._same_name(name, h.name(h.lists[eid])):
                return eid
        return None

    def add_items(self, agent: str, list_name: str, items: Any, shared: Optional[bool] = None) -> Dict[str, Any]:
        self._need(agent, "todo")
        wanted = items if isinstance(items, list) else [items]
        if not wanted or len(wanted) > MAX_ITEMS:
            raise Refused(f"Add between 1 and {MAX_ITEMS} items.")
        entries = [entry(raw) for raw in wanted]          # check them all before changing anything
        with self._lists_lock:
            kind, where, which = self._find(agent, list_name, shared)
            if kind == "shared":
                return self._add_shared(agent, where, which, entries)
            return self._add_personal(agent, where, which, entries)

    def _add_shared(self, agent: str, h: "House", eid: str, entries: List[Dict[str, str]]) -> Dict[str, Any]:
        features = self._features(h, eid)
        current = {i["item"].strip().lower(): i for i in self._items(eid)}
        added, already, reopened, dropped = [], [], [], []
        for e in entries:
            item = e["item"]
            extra: Dict[str, Any] = {}
            if e["details"]:
                if features & SET_DESCRIPTION:
                    extra["description"] = e["details"]
                else:
                    dropped.append(item)
            if e["due"]:
                if features & SET_DUE_DATE:
                    extra["due_date"] = e["due"]
                else:
                    dropped.append(item)
            have = current.get(item.lower())
            if have and not have["done"]:
                already.append(have["item"])
            elif have:
                self.ha.service("todo", "update_item", {"entity_id": eid, "item": have["item"],
                                                        "status": "needs_action", **extra})
                reopened.append(have["item"])
            else:
                self.ha.service("todo", "add_item", {"entity_id": eid, "item": item, **extra})
                current[item.lower()] = {"item": item, "done": False}
                added.append(item)
        self.log(f"{agent} add_items {eid}: {len(added)} added, {len(reopened)} back, {len(already)} already")
        out: Dict[str, Any] = {"list": h.name(h.lists[eid]), "added": added}
        if reopened:
            out["back_on_the_list"] = reopened
        if already:
            out["already_on_it"] = already
        if dropped:
            out["details_not_kept"] = sorted(set(dropped))
            out["note"] = "This list cannot hold details or due dates; only the titles were saved."
        return out

    @staticmethod
    def _set_extra(i: Dict[str, Any], details: str, due: str) -> None:
        for key, value in (("details", details), ("due", due)):
            if value:
                i[key] = value

    def _add_personal(self, agent: str, mine: List[Dict[str, Any]], l: Dict[str, Any],
                      entries: List[Dict[str, str]], done: Optional[List[bool]] = None) -> Dict[str, Any]:
        """``done``: for each entry, whether it goes on already ticked (a list moved from Home Assistant)."""
        person, now = self._person(agent), self.personal.now()
        items = l.setdefault("items", [])
        current = {str(i.get("text") or "").strip().lower(): i for i in items}
        new = {e["item"].lower() for e in entries} - set(current)
        if len(items) + len(new) > PERSONAL_ITEMS_MAX:
            raise Refused(f"A personal list holds at most {PERSONAL_ITEMS_MAX} items; tidy it up first.")
        added, already, reopened = [], [], []
        for n, e in enumerate(entries):
            ticked = bool(done and done[n])
            have = current.get(e["item"].lower())
            if have and (not have.get("done") or ticked):
                already.append(have["text"])
            elif have:
                have.update(done=False, done_at=None)
                self._set_extra(have, e["details"], e["due"])
                reopened.append(have["text"])
            else:
                i = {"text": e["item"], "done": ticked, "added_at": now, "done_at": now if ticked else None}
                self._set_extra(i, e["details"], e["due"])
                items.append(i)
                current[e["item"].lower()] = i
                added.append(e["item"])
        self.personal.save(person, mine)
        self.log(f"{agent} add_items personal: {len(added)} added, {len(reopened)} back, {len(already)} already")
        out: Dict[str, Any] = {"list": l["name"], "added": added}
        if reopened:
            out["back_on_the_list"] = reopened
        if already:
            out["already_on_it"] = already
        return out

    def change_item(self, agent: str, list_name: str, item: Any, new_title: Any = None,
                    details: Any = None, due: Any = None, shared: Optional[bool] = None) -> Dict[str, Any]:
        """Rename an item, or set its details or due date ("" clears them)."""
        self._need(agent, "todo")
        with self._lists_lock:
            kind, where, which = self._find(agent, list_name, shared)
            if kind == "personal":
                return self._change_personal(agent, where, which, item, new_title, details, due)
            h, eid = where, which
            name = self._match(eid, [item])[0]
            data: Dict[str, Any] = {"entity_id": eid, "item": name}
            if new_title not in (None, ""):
                data["rename"] = entry(new_title)["item"]
            features = self._features(h, eid)
            if details is not None:
                if not features & SET_DESCRIPTION:
                    raise Refused("This list cannot hold details.")
                if len(str(details)) > 1000:
                    raise Refused("Details are at most 1000 characters.")
                data["description"] = str(details).strip()
            if due is not None:
                if not features & SET_DUE_DATE:
                    raise Refused("This list cannot hold due dates.")
                data["due_date"] = clean_due(due) or None
            if len(data) == 2:
                raise Refused("Say what to change: new_title, details or due.")
            self.ha.service("todo", "update_item", data)
            self.log(f"{agent} change_item {eid}")
            return {"list": h.name(h.lists[eid]), "changed": data.get("rename", name)}

    def _change_personal(self, agent: str, mine: List[Dict[str, Any]], l: Dict[str, Any], item: Any,
                         new_title: Any, details: Any, due: Any) -> Dict[str, Any]:
        items = l.setdefault("items", [])
        name = self._pick([str(i.get("text") or "") for i in items], [item])[0]
        it = next(i for i in items if i.get("text") == name)
        changes: Dict[str, Any] = {}
        if new_title not in (None, ""):
            title = entry(new_title)["item"]
            if title.lower() != name.lower() and any(str(i.get("text") or "").lower() == title.lower() for i in items):
                raise Refused(f"'{title}' is already on the list.")
            changes["text"] = title
        if details is not None:
            if len(str(details)) > 1000:
                raise Refused("Details are at most 1000 characters.")
            changes["details"] = str(details).strip()
        if due is not None:
            changes["due"] = clean_due(due)
        if not changes:
            raise Refused("Say what to change: new_title, details or due.")
        for key, value in changes.items():
            if value or key == "text":
                it[key] = value
            else:
                it.pop(key, None)                 # "" clears details or the due date
        self.personal.save(self._person(agent), mine)
        self.log(f"{agent} change_item personal")
        return {"list": l["name"], "changed": it["text"]}

    def tick_items(self, agent: str, list_name: str, items: Any, shared: Optional[bool] = None) -> Dict[str, Any]:
        self._need(agent, "todo")
        with self._lists_lock:
            kind, where, which = self._find(agent, list_name, shared)
            if kind == "personal":
                names = self._pick([str(i.get("text") or "") for i in which.get("items") or []], items)
                now = self.personal.now()
                for i in which.get("items") or []:
                    if i.get("text") in names and not i.get("done"):
                        i.update(done=True, done_at=now)
                self.personal.save(self._person(agent), where)
                self.log(f"{agent} tick_items personal: {len(names)}")
                return {"list": which["name"], "ticked": names}
            h, eid = where, which
            names = self._match(eid, items)
            for name in names:
                self.ha.service("todo", "update_item", {"entity_id": eid, "item": name, "status": "completed"})
            self.log(f"{agent} tick_items {eid}: {len(names)}")
            return {"list": h.name(h.lists[eid]), "ticked": names}

    def remove_items(self, agent: str, list_name: str, items: Any, shared: Optional[bool] = None) -> Dict[str, Any]:
        self._need(agent, "todo")
        with self._lists_lock:
            kind, where, which = self._find(agent, list_name, shared)
            if kind == "personal":
                names = self._pick([str(i.get("text") or "") for i in which.get("items") or []], items)
                which["items"] = [i for i in which.get("items") or [] if i.get("text") not in names]
                self.personal.save(self._person(agent), where)
                self.log(f"{agent} remove_items personal: {len(names)}")
                return {"list": which["name"], "removed": names}
            h, eid = where, which
            names = self._match(eid, items)
            self.ha.service("todo", "remove_item", {"entity_id": eid, "item": names})
            self.log(f"{agent} remove_items {eid}: {len(names)}")
            return {"list": h.name(h.lists[eid]), "removed": names}

    def move_list(self, agent: str, name: Any, to: Any) -> Dict[str, Any]:
        """A personal list becomes shared (a new Home Assistant list with its open items), or a
        shared one becomes personal (every item copied, then the Home Assistant list removed when
        that is possible: see :meth:`ListMaker.remove`)."""
        self._need(agent, "todo")
        person = self._person(agent)
        if not person:
            raise Refused(NOT_PERSONAL)
        if not str(name or "").strip():
            raise Refused("Say which list to move.")
        to = str(to or "").strip().lower()
        if to not in ("shared", "personal"):
            raise Refused("Say to: 'shared' or 'personal'.")
        with self._lists_lock:
            if to == "shared":
                _, mine, l = self._find(agent, str(name or ""), shared=False)
                made = self.create_list(agent, l["name"], shared=True)      # refused: nothing has changed
                if not made.get("id"):
                    return {**made, "note": (made.get("note", "") + " The personal list is kept as it is; "
                                             "try the move again in a moment.").strip()}
                open_items = [{"item": str(i.get("text") or ""), "details": str(i.get("details") or ""),
                               "due": str(i.get("due") or "")} for i in l.get("items") or [] if not i.get("done")]
                copied = self._add_shared(agent, self.house(agent), made["id"], open_items) if open_items else {}
                mine.remove(l)
                self.personal.save(person, mine)
                self.log(f"house: {agent} moved a personal list of {person} to shared {made['id']}")
                out: Dict[str, Any] = {"list": made["list"], "kind": "shared", "id": made["id"],
                                       "moved_items": len(open_items)}
                if made.get("already_existed"):
                    out["joined_the_shared_list"] = True
                ticked = sum(1 for i in l.get("items") or [] if i.get("done"))
                if ticked:
                    out["ticked_items_left_out"] = ticked
                if copied.get("details_not_kept"):
                    out["details_not_kept"], out["note"] = copied["details_not_kept"], copied["note"]
                return out
            _, h, eid = self._find(agent, str(name or ""), shared=True)
            title = h.name(h.lists[eid])
            items = self._items(eid)
            mine = self.personal.load(person)
            l = next((x for x in mine if self._same_name(title, x["name"])), None)
            if l is None:
                if len(mine) >= PERSONAL_LISTS_MAX:
                    raise Refused(f"You have {PERSONAL_LISTS_MAX} personal lists, the most there can be.")
                try:
                    clean = clean_list_name(title)
                except Refused:
                    clean = clean_list_name(re.sub(r"[^\w '’&-]+", " ", title)[:LIST_NAME_MAX] or "List")
                l = {"name": clean, "owner": person, "created_at": self.personal.now(), "items": []}
                mine.append(l)
            entries = [{"item": clean_item(i["item"][:200]), "details": i["details"][:1000], "due": i["due"]}
                       for i in items if i["item"].strip()]
            self._add_personal(agent, mine, l, entries, done=[i["done"] for i in items if i["item"].strip()])
            removed, note = False, ""
            if self.lists_admin is None:
                note = ("The shared list is still in Home Assistant too: removing it needs the admin login "
                        "(docs/house.md). Say so; it can be removed in the Home Assistant app.")
            else:
                try:
                    removed = self.lists_admin.remove(title)
                except Refused as e:
                    note = f"The shared list is still in Home Assistant too ({e})"
                if not removed and not note:
                    note = ("The shared list is still in Home Assistant too: it is not a Local To-do list made "
                            "under this name, so it cannot be removed from here. Say so; it can be removed in "
                            "the Home Assistant app.")
            self.log(f"house: {agent} moved shared {eid} to {person}'s personal lists"
                     f"{'' if removed else ' (the shared list stays)'}")
            out = {"list": l["name"], "kind": "personal", "moved_items": len(entries), "shared_list_removed": removed}
            return {**out, "note": note} if note else out

    def delete_list(self, agent: str, name: Any) -> Dict[str, Any]:
        """Delete one of the person's own lists, with everything on it. Shared lists are not deleted here."""
        self._need(agent, "todo")
        person = self._person(agent)
        if not person:
            raise Refused(NOT_PERSONAL)
        if not str(name or "").strip():
            raise Refused("Say which list to delete.")
        with self._lists_lock:
            kind, where, which = self._find(agent, str(name or ""))
            if kind == "shared":
                raise Refused(f"'{where.name(where.lists[which])}' is a shared list. Shared lists are removed in "
                              "the Home Assistant app, not here.")
            where.remove(which)
            self.personal.save(person, where)
        self.log(f"{agent} deleted a personal list of {person}")
        return {"deleted": which["name"], "kind": "personal", "items": len(which.get("items") or [])}


# ── MCP over HTTP ────────────────────────────────────────────────────────────

TOOLS = [
    {"name": "list_lights",
     "description": ("Every light you can control, room by room, with whether it is on, its brightness (%) and "
                     "colour. 'unavailable' means the light has no power (switched off at the wall)."),
     "inputSchema": {"type": "object", "properties": {
         "room": {"type": "string", "description": "Only this room. Default: every room."}}}},
    {"name": "set_lights",
     "description": ("Turn lights on or off, or change their brightness or colour. Name rooms ('Kitchen'), "
                     "zones ('TV Wall'), single lights ('Bed') or 'everything'."),
     "inputSchema": {"type": "object", "properties": {
         "targets": {"type": "array", "items": {"type": "string"},
                     "description": "Rooms, zones or lights, e.g. ['Kitchen'] or ['Lounge Left', 'Lounge Right']."},
         "on": {"type": "boolean", "description": "true to turn on (or change), false to turn off."},
         "brightness": {"type": "integer", "description": "1-100 (%), with on: true."},
         "kelvin": {"type": "integer", "description": "White temperature 2000 (warm) to 6500 (cool), with on: true."},
         "color": {"type": "string", "description": "A colour name such as 'red' or 'blue', with on: true."},
         "transition": {"type": "number", "description": "Seconds to fade over, 0-60."}},
         "required": ["targets", "on"]}},
    {"name": "list_scenes",
     "description": "The saved scenes (Relax, Bright, Nightlight...), by room.",
     "inputSchema": {"type": "object", "properties": {
         "room": {"type": "string", "description": "Only this room."}}}},
    {"name": "activate_scene",
     "description": "Switch a room to one of its saved scenes, by the names list_scenes gives.",
     "inputSchema": {"type": "object", "properties": {
         "scene": {"type": "string", "description": "e.g. 'Relax'."},
         "room": {"type": "string", "description": "e.g. 'Living Room'. Needed when several rooms have the scene."}},
         "required": ["scene"]}},
    {"name": "list_items",
     "description": ("What is on the shared lists in Home Assistant (shopping, to-do), the ones Greg and Béré see "
                     "in the Home Assistant app. Without a list name: every list."),
     "inputSchema": {"type": "object", "properties": {
         "list": {"type": "string", "description": "e.g. 'shopping'. Default: every list."},
         "done": {"type": "boolean", "description": "Also show ticked-off items."}}}},
    {"name": "add_items",
     "description": ("Put items on a shared list. Items already on it are not added twice; a ticked-off item "
                     "goes back on."),
     "inputSchema": {"type": "object", "properties": {
         "list": {"type": "string", "description": "e.g. 'shopping'. Can be left out when there is one list."},
         "items": {"type": "array", "items": {"anyOf": [
             {"type": "string"},
             {"type": "object", "properties": {
                 "item": {"type": "string", "description": "A short title in their words (at most 60 characters)."},
                 "details": {"type": "string", "description": "The rest of what they said, shown under the title."},
                 "due": {"type": "string", "description": "YYYY-MM-DD, when they gave a date."}},
              "required": ["item"]}]},
                   "description": ("One entry per item: a plain title ('2 lemons'), or {item, details, due} when "
                                   "there is more to say than fits a short title.")}},
         "required": ["items"]}},
    {"name": "create_list",
     "description": ("Make a new shared list in Home Assistant (e.g. 'Wedding'), when a person asks for one. "
                     "If a list with that name exists, you get that one back instead. Then add the items "
                     "with add_items."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": ("The new list's name, at most 40 characters: letters, "
                                                    "numbers, spaces and ' & -.")}},
         "required": ["name"]}},
    {"name": "change_item",
     "description": "Rename an item on a shared list, or set its details or due date ('' clears them).",
     "inputSchema": {"type": "object", "properties": {
         "list": {"type": "string"},
         "item": {"type": "string", "description": "The item as it is on the list now."},
         "new_title": {"type": "string"},
         "details": {"type": "string"},
         "due": {"type": "string", "description": "YYYY-MM-DD, or '' to clear."}},
         "required": ["item"]}},
    {"name": "tick_items",
     "description": "Tick items off a shared list (bought, or done). They stay on it as done until removed.",
     "inputSchema": {"type": "object", "properties": {
         "list": {"type": "string"},
         "items": {"type": "array", "items": {"type": "string"}}},
         "required": ["items"]}},
    {"name": "remove_items",
     "description": "Take items off a shared list altogether (added by mistake, no longer wanted).",
     "inputSchema": {"type": "object", "properties": {
         "list": {"type": "string"},
         "items": {"type": "array", "items": {"type": "string"}}},
         "required": ["items"]}},
]
LIST_TOOLS = {"list_items", "add_items", "change_item", "tick_items", "remove_items", "create_list"}

# A person's own agent sees the same list tools, told about their personal lists too (Greg, 28 Nov),
# with a `shared` choice for when a name is both kinds, and two more tools.
PERSONAL_DESCRIPTIONS = {
    "list_items": ("What is on the lists: the shared ones in Home Assistant (shopping, to-do), which Greg and "
                   "Béré both see, and your person's personal lists, which only they see. Each list says "
                   "kind: 'shared' or 'personal'. Without a list name: every list."),
    "add_items": ("Put items on a list, shared or personal, found by its name. Items already on it are not "
                  "added twice; a ticked-off item goes back on."),
    "create_list": ("Make a new list when your person asks for one. It is PERSONAL (only they see it) unless "
                    "you pass shared: true, which makes a shared list in Home Assistant that both see. If a "
                    "list with that name exists, you get that one back instead. Then add the items with add_items."),
    "change_item": "Rename an item on a list, or set its details or due date ('' clears them).",
    "tick_items": "Tick items off a list (bought, or done). They stay on it as done until removed.",
    "remove_items": "Take items off a list altogether (added by mistake, no longer wanted).",
}
SHARED_FLAG = {"shared": {"type": "boolean", "description": (
    "Only when a shared list and a personal list have the same name: true for the shared one, false for "
    "the personal one. Leave it out otherwise.")}}
CREATE_FLAG = {"shared": {"type": "boolean", "description": (
    "true: a shared list both of them see. Leave it out for a personal list, only for your person.")}}
PERSONAL_TOOLS = [
    {"name": "move_list",
     "description": ("Make a personal list shared (both see it; its open items go to a new Home Assistant list) "
                     "or a shared list personal (only your person sees it; the other person no longer will). "
                     "Only when your person asks, after saying what changes."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "The list, e.g. 'Video games'."},
         "to": {"type": "string", "enum": ["shared", "personal"]}},
         "required": ["name", "to"]}},
    {"name": "delete_list",
     "description": ("Delete one of your person's personal lists with everything on it. Only when they ask, "
                     "after they say yes. Shared lists cannot be deleted here."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "The personal list, e.g. 'Video games'."}},
         "required": ["name"]}},
]


def _personal_view(t: Dict[str, Any]) -> Dict[str, Any]:
    schema = dict(t["inputSchema"])
    flag = CREATE_FLAG if t["name"] == "create_list" else SHARED_FLAG
    schema["properties"] = {**schema.get("properties", {}), **flag}
    return {**t, "description": PERSONAL_DESCRIPTIONS.get(t["name"], t["description"]), "inputSchema": schema}


def tools_for(svc: Service, agent: str) -> List[Dict[str, Any]]:
    domains = svc.agents[agent].get("domains") or []
    light_tools, scene_tools = {"list_lights", "set_lights"}, {"list_scenes", "activate_scene"}
    out = [t for t in TOOLS if (t["name"] in light_tools and "light" in domains)
           or (t["name"] in scene_tools and "scene" in domains)
           or (t["name"] in LIST_TOOLS and "todo" in domains)]
    if "todo" in domains and svc._person(agent):
        out = [_personal_view(t) if t["name"] in LIST_TOOLS else t for t in out] + PERSONAL_TOOLS
    return out


def handle_rpc(svc: Service, agent: str, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        return None
    if method == "initialize":
        version = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
        result: Any = {"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "logos-house", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools_for(svc, agent)}
    elif method == "tools/call":
        params = req.get("params") or {}
        args = params.get("arguments") or {}
        name = params.get("name")
        # Which kind of list (Greg, 28 Nov): only a person's own agent is offered it; a model may send "true" as text.
        flag = str(args.get("shared")).strip().lower()
        shared = True if flag == "true" else False if flag == "false" else None
        calls = {
            "list_lights": lambda: svc.list_lights(agent, args.get("room") or ""),
            "set_lights": lambda: svc.set_lights(agent, args.get("targets") or [], args.get("on"),
                                                 args.get("brightness"), args.get("kelvin"),
                                                 args.get("color") or "", args.get("transition")),
            "list_scenes": lambda: svc.list_scenes(agent, args.get("room") or ""),
            "activate_scene": lambda: svc.activate_scene(agent, args.get("scene") or "", args.get("room") or ""),
            "list_items": lambda: svc.list_items(agent, args.get("list") or "", args.get("done"), shared),
            "add_items": lambda: svc.add_items(agent, args.get("list") or "", args.get("items"), shared),
            "create_list": lambda: svc.create_list(agent, args.get("name"), shared),
            "change_item": lambda: svc.change_item(agent, args.get("list") or "", args.get("item"),
                                                   args.get("new_title"), args.get("details"), args.get("due"), shared),
            "tick_items": lambda: svc.tick_items(agent, args.get("list") or "", args.get("items"), shared),
            "remove_items": lambda: svc.remove_items(agent, args.get("list") or "", args.get("items"), shared),
            "move_list": lambda: svc.move_list(agent, args.get("name") or args.get("list"), args.get("to")),
            "delete_list": lambda: svc.delete_list(agent, args.get("name") or args.get("list")),
        }
        if name not in calls or name not in {t["name"] for t in tools_for(svc, agent)}:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            payload = calls[name]()
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}
        except Refused as e:
            svc.log(f"{agent} {name} refused: {e}")
            result = {"content": [{"type": "text", "text": f"Not done: {e}"}], "isError": True}
        except Exception as e:                       # never leak a traceback to the agent
            svc.log(f"{agent} {name} FAILED: {type(e).__name__}: {e}")
            result = {"content": [{"type": "text", "text": "Not done: the house service hit an error."}],
                      "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: Any = None, headers: Tuple[Tuple[str, str], ...] = ()):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"status": "ok", "agents": sorted(svc.agents)})
            self._send(405, {"error": "use POST"}, (("Allow", "POST"),))

        def do_DELETE(self):
            self._send(200, {})

        def do_POST(self):
            if self.path.rstrip("/") != "/mcp":
                return self._send(404, {"error": "not found"})
            auth = self.headers.get("Authorization", "")
            agent = svc.agent_for_token(auth[7:].strip() if auth.lower().startswith("bearer ") else "")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""
            if agent is None:
                return self._send(401, {"error": "unknown or missing token"})
            try:
                req = json.loads(raw)
            except ValueError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            if isinstance(req, list):
                answers = [x for x in (handle_rpc(svc, agent, r) for r in req if isinstance(r, dict)) if x]
                return self._send(200, answers) if answers else self._send(202)
            answer = handle_rpc(svc, agent, req) if isinstance(req, dict) else None
            return self._send(200, answer) if answer else self._send(202)

    return Handler


def main() -> None:
    config = json.load(open(os.environ.get("HOUSE_CONFIG", "/data/config.json")))
    ha = HomeAssistant(config["ha_url"], config["ha_token"])
    admin = (ListMaker(config["ha_admin_url"], config["ha_admin_token"])
             if config.get("ha_admin_url") and config.get("ha_admin_token") else None)
    lists_dir = os.environ.get("HOUSE_LISTS", "")      # the logos-lists volume; without it, no personal lists
    svc = Service(ha, config["agents"], log=lambda m: print(m, flush=True), lists_admin=admin,
                  personal=PersonalLists(lists_dir) if lists_dir else None)
    port = int(os.environ.get("HOUSE_PORT", "8098"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(svc))
    print(f"house service: {len(config['agents'])} agents, MCP on :{port}, "
          f"creating lists {'on' if admin else 'off'}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
