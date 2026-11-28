"""Adding and removing people from the dashboard (backlog F38, docs/people.md).

What used to be done by hand for each new person (a dashboard account, ``agents/<name>.yaml`` and its
``.local.yaml``, ``scripts/logos-agent up``, a Matrix account, ``scripts/logos-calendar add-person``, the
household agent's owners) is one flow, People › Add a person:

1. An admin invites them (:func:`check_new_person`, :func:`new_token`): an account is made in the
   ``pending`` state (it cannot sign in) and a one-time link with a secret is shown to the admin once.
   Only the secret's SHA-256 is kept.
2. The person opens the link and chooses their password (:func:`accept_accounts`): their account
   becomes active, and the same password makes their chat (Matrix) and calendar accounts. The password
   is used there and then and never kept anywhere but as the usual hashes.
3. In the background (:meth:`People.set_up_agent`): their agent's definition is written from the
   template (:func:`template`) into a folder outside the repo (the repo is public; the definition names
   a person), its chat account and a direct chat with the person are made, it is started, and the
   household agent is restarted so it knows them.

Removing someone (:meth:`People.remove`) stops their agent, keeps a backup of its two volumes (brain and
home) in the graveyard for 30 days, restorable from their page (:meth:`People.restore`), after which it
is deleted for good (:meth:`People.purge_due`).

Everything that touches Docker, OpenShell, Matrix or the calendar goes through the injected runtime and
hosts, so tests use fakes.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import yaml

#: A person's username: also their agent's name, their sandbox, volumes, chat and calendar names.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
#: Names that are something else already (accounts of the servers, roles, test agents).
RESERVED = frozenset({"household", "admin", "root", "system", "logos", "logos-admin", "logos-service", "bench",
                      "bench-peer", "dev", "test", "everyone", "shared", "conduit", "continuwuity", "person"})
INVITE_DAYS = (1, 3, 7, 14)
DEFAULT_INVITE_DAYS = 7
GRAVE_DAYS = 30
MIN_PASSWORD = 10
MAX_PASSWORD = 256
HELPER = "alpine:3"
SANDBOX_UID = "10000:10000"
#: Actions shown in the People screen's list of admin actions.
AUDIT_ACTIONS = ("person_invited", "invite_renewed", "invite_cancelled", "invite_accepted", "person_setup",
                 "person_removed", "person_restored", "person_deleted", "create_user", "update_user",
                 "admin_set_password", "setup_wipe")

STEP_LABELS = {
    "account": "Account",
    "chat": "Chat account (Element X)",
    "calendar": "Calendar",
    "definition": "Their agent's definition",
    "room": "A chat with their agent",
    "agent": "Their agent",
    "household": "Household agent",
}


class PeopleError(Exception):
    """Something a person should be told, in words."""


# ── small pure helpers ──────────────────────────────────────────────────────────────────────────

def new_token() -> str:
    """The secret in an invitation link: 256 bits."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode()).hexdigest()


def slug(display_name: str) -> str:
    """"Anne-Marie Dupré" -> "anne-marie": a username suggestion (the admin can change it)."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", display_name or "").encode("ascii", "ignore").decode().lower()
    first = plain.strip().split(" ")[0] if plain.strip() else ""
    out = re.sub(r"[^a-z0-9-]+", "-", first).strip("-")
    return re.sub(r"^[^a-z]+", "", out)[:31]


def password_problem(password: str) -> Optional[str]:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"Choose a password of at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return "That password is too long."
    if password.strip() != password or not password.strip():
        return "A password cannot start or end with a space."
    return None


def check_new_person(username: str, display_name: str, email: str, runtime: Any, auth_db: Any,
                     graveyard: Optional["Graveyard"] = None) -> Optional[str]:
    """Why this person cannot be added under this name, or None."""
    if not display_name or len(display_name) > 60 or any(c in display_name for c in "\n\r\t<>"):
        return "Give their name (up to 60 characters)."
    if not NAME_RE.match(username or ""):
        return "A username is 2 to 31 lowercase letters, digits or dashes, starting with a letter."
    if username in RESERVED or username.endswith("-agent") or username.startswith("logos"):
        return f"“{username}” is kept for Logos itself. Choose another username."
    if email and (len(email) > 254 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email)):
        return "That email address does not look right."
    if auth_db.get_user_by_username(username):
        return f"Someone already signs in as “{username}”."
    if email and auth_db.get_user_by_email(email):
        return "Someone already has that email address."
    if runtime is not None:
        if username in runtime.defined():
            return f"There is already an agent called “{username}”."
        home, brain = f"logos-home-{username}", f"logos-brain-{username}"
        for vol in (home, brain):
            if runtime.runner.run(["docker", "volume", "inspect", vol]).returncode == 0:
                return f"An earlier agent called “{username}” still has its storage here ({vol}). Choose another username."
    if graveyard is not None and graveyard.get(username):
        return f"“{username}” was removed recently and can still be restored from their page."
    return None


def placeholder_email(username: str) -> str:
    """Accounts need an email; someone added without one signs in with their username."""
    return f"{username}@people.logos.invalid"


def base_agent(runtime: Any) -> Dict[str, Any]:
    """The agent a new person's agent copies its model, image and timezone from: the household agent,
    else the first agent that belongs to a person."""
    names = runtime.defined()
    order = (["household"] if "household" in names else []) + [n for n in names if n != "household"]
    for n in order:
        try:
            a = runtime.load(n)
        except Exception:
            continue
        if a.get("role") in ("test",) or a.get("sandbox") is False or not a.get("model"):
            continue
        return a
    raise PeopleError("There is no agent to copy the model settings from (agents/household.yaml).")


def template(runtime: Any, username: str, display_name: str, options: Mapping[str, Any],
             matrix_id: str = "", today: Optional[str] = None) -> Dict[str, Any]:
    """A new person's agent, like the first people's (agents/greg.yaml), without the parts that are
    one household's own (the house speaker, the lights, the Claude advisor)."""
    base = base_agent(runtime)
    model = {k: base["model"][k] for k in ("provider", "base_url", "default", "context_length",
                                           "load_context_length", "gateway", "vision") if k in base["model"]}
    calendar = bool(options.get("calendar"))
    agent: Dict[str, Any] = {
        "name": username,
        "display_name": f"{display_name}'s agent",
        "owners": [username],
        "person": {"username": username, "display_name": display_name, "household": bool(options.get("household")),
                   "matrix_id": matrix_id, "added": today or _dt.date.today().isoformat()},
        "image": base["image"],
        "soul": "souls/general/soul.md",
        "policy": base.get("policy") or "infra/agent-image/policy.yaml",
        "providers": [],
        "timezone": base.get("timezone") or "UTC",
        "noticeboard": True,
        "voice": True,
        "model": model,
        "budget": {"monthly_usd": float((base.get("budget") or {}).get("monthly_usd", 10))},
        "web": {"enabled": True},
        "consolidation": {"enabled": True, "schedule": "15 2 * * *"},
        "digest": {"enabled": True, "schedule": "0 18 * * 0"},
        "session_reset": {"mode": "daily", "at_hour": 3},
    }
    if matrix_id:
        agent["matrix"] = {"enabled": True, "e2ee": "required", "verified_only": True, "allowed_users": [matrix_id]}
    if calendar:
        agent["calendar"] = {"enabled": True, "person": username}
        if matrix_id:
            agent["briefing"] = {"enabled": True, "weekdays": "0 7 * * 1-5", "weekends": "0 9 * * 0,6"}
    if options.get("email"):
        agent["mail"] = {"enabled": True, "person": username}
    return agent


HEADER = ("# Written by Logos (People › Add a person) for {name}. It names a person, so it lives here,\n"
          "# outside the repo. Change it here; `scripts/logos-agent up {agent}` applies it.\n")


def write_definition(runtime: Any, agent: Mapping[str, Any]) -> Path:
    """Write a definition into the private folder: the folder 0700, the file 0600, atomically."""
    from gateway.agent_runtime import private_agents_dir
    folder = private_agents_dir(runtime.env)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    os.chmod(folder.parent, 0o700)
    path = folder / f"{agent['name']}.yaml"
    text = HEADER.format(name=agent.get("person", {}).get("display_name") or agent["name"], agent=agent["name"])
    text += yaml.safe_dump(dict(agent), sort_keys=False, allow_unicode=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def read_definition(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def parse_env(text: str) -> Dict[str, str]:
    return dict(l.split("=", 1) for l in (text or "").splitlines() if "=" in l and not l.startswith("#"))


# ── who can see what ───────────────────────────────────────────────────────────────────────────

def _owners(agent: Mapping[str, Any]) -> Optional[List[str]]:
    owners = agent.get("owners")
    if owners is None:
        return None
    if isinstance(owners, str):
        owners = [owners]
    return [str(o).strip().lower() for o in owners if str(o).strip()]


def visibility(username: str, agents: Mapping[str, Mapping[str, Any]], names: Mapping[str, str]) -> Dict[str, Any]:
    """For the person's page: which agents they use, what their own agent can reach, and who can see
    what is theirs. ``agents`` are the loaded definitions shown on the dashboard; ``names`` maps
    usernames to display names."""
    who = lambda u: names.get(u, u)                                     # noqa: E731
    uses, own, reach, seen = [], None, [], []
    cal_people = {(a.get("calendar") or {}).get("person") for a in agents.values()} - {None}
    for n, a in sorted(agents.items()):
        owners = _owners(a)
        if owners is not None and username not in owners:
            continue
        others = sorted(who(o) for o in (owners or []) if o != username)
        uses.append({"name": n, "display_name": str(a.get("display_name") or n),
                     "shared_with": others if owners is not None else ["everyone"],
                     "household": a.get("role") == "household"})
        if own is None and owners == [username] and a.get("role") not in ("household", "test"):
            own = n
    if own:
        a = agents[own]
        cal = a.get("calendar") or {}
        if (a.get("matrix") or {}).get("enabled"):
            reach.append("Chats in Element X, only with the people it is set to answer")
        if cal.get("enabled"):
            others = sorted(who(p) for p in cal_people if p != username)
            reach.append("Calendars: " + ", ".join(
                ["their own and Household (read and change)"] + ([f"{', '.join(others)} (read only)"] if others else [])))
        if (a.get("mail") or {}).get("enabled"):
            reach.append("Their own email, read only (once they connect it in Settings › Email)")
        if (a.get("house") or {}).get("enabled"):
            reach.append("The lights and the shared lists")
        if (a.get("web") or {}).get("enabled"):
            reach.append("Web search, through the web reader (private names are kept out)")
        if a.get("noticeboard"):
            reach.append("Messages to and from the other agents (the noticeboard)")
        reach.append("Not your home network, not the internet directly, not the other agents' brains")
        seen.append({"what": f"{a.get('display_name') or own}: its chats and its brain",
                     "who": [who(username)], "note": "Admins get no exception."})
        if cal.get("enabled") and cal.get("person") == username:
            readers = [f"{who(username)}'s agent (read and change)"]
            for n, b in sorted(agents.items()):
                bc = b.get("calendar") or {}
                if n == own or not bc.get("enabled"):
                    continue
                if bc.get("person") and bc.get("person") != username:
                    readers.append(f"{b.get('display_name') or n} (read only)")
                elif username in (bc.get("free_busy") or {}):
                    readers.append(f"{b.get('display_name') or n} (busy times only)")
            readers += [f"{who(p)}, on their phone (read only)" for p in sorted(cal_people) if p != username]
            seen.append({"what": "Their own calendar", "who": readers, "note": ""})
        if (a.get("mail") or {}).get("enabled"):
            seen.append({"what": "Their email", "who": [f"{a.get('display_name') or own} only"],
                         "note": "Nothing sends, moves or deletes mail."})
    for u in uses:
        if u["household"]:
            seen.append({"what": f"Their chats with {u['display_name']}",
                         "who": [who(username)], "note": "Everyone it belongs to sees its scheduled jobs and shared "
                                                         "room; each person sees only their own conversations."})
    return {"uses": uses, "own_agent": own, "reach": reach, "seen_by": seen}


# ── the graveyard: what is left of someone removed, for 30 days ─────────────────────────────────

class Graveyard:
    """``<people dir>/graveyard/<name>/``: ``meta.json``, ``home.tar.gz``, ``brain.tar.gz``,
    ``definition.yaml`` and, when they had a calendar, ``calendar-account`` (their password hash).
    Everything is 0600 in 0700 folders."""

    def __init__(self, env: Mapping[str, str]):
        from gateway.agent_runtime import people_dir
        self.root = people_dir(env) / "graveyard"

    def _dir(self, name: str) -> Path:
        if not NAME_RE.match(name or ""):
            raise PeopleError("not a username")
        return self.root / name

    def make(self, name: str) -> Path:
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        for p in (self.root.parent, self.root, d):
            os.chmod(p, 0o700)
        return d

    def write(self, name: str, file: str, data: bytes) -> Dict[str, Any]:
        path = self.make(name) / file
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def read(self, name: str, file: str) -> bytes:
        return (self._dir(name) / file).read_bytes()

    def has(self, name: str, file: str) -> bool:
        return (self._dir(name) / file).exists()

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads((self._dir(name) / "meta.json").read_text())
        except (OSError, ValueError, PeopleError):
            return None

    def list(self) -> List[Dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = [self.get(p.name) for p in sorted(self.root.iterdir()) if p.is_dir()]
        return [m for m in out if m]

    def forget(self, name: str) -> None:
        d = self._dir(name)
        if d.exists():
            shutil.rmtree(d)


def check_archive(data: bytes) -> int:
    """The number of entries in a .tar.gz, or PeopleError when it does not read back whole."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            return sum(1 for _ in t)
    except (tarfile.TarError, OSError, EOFError) as e:
        raise PeopleError(f"the backup does not read back: {e}")


# ── the work itself ───────────────────────────────────────────────────────────────────────────

Steps = List[Dict[str, str]]


def step(steps: Steps, sid: str, state: str, detail: str = "") -> Steps:
    """Set one step (ok, failed, skipped, running, waiting) in the list kept for the person's page."""
    for s in steps:
        if s["id"] == sid:
            s.update(state=state, detail=detail)
            return steps
    steps.append({"id": sid, "label": STEP_LABELS.get(sid, sid), "state": state, "detail": detail})
    return steps


class People:
    """The blocking work of the People flow. ``runtime`` is an AgentRuntime; ``matrix`` and ``calendar``
    are factories ``(runtime) -> host`` (MatrixHost, CalendarHost) so tests can pass fakes."""

    def __init__(self, runtime: Any, auth_db: Any, matrix: Optional[Callable] = None,
                 calendar: Optional[Callable] = None, now: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.rt = runtime
        self.db = auth_db
        self._matrix = matrix
        self._calendar = calendar
        self.now = now
        self.log = log
        self.graveyard = Graveyard(runtime.env)

    def matrix(self):
        if self._matrix:
            return self._matrix(self.rt)
        from gateway.matrix_host import MatrixHost
        return MatrixHost(self.rt)

    def calendar(self):
        if self._calendar:
            return self._calendar(self.rt)
        from gateway.calendar_host import CalendarHost
        return CalendarHost(self.rt)

    def save(self, username: str, steps: Steps) -> None:
        self.db.set_people_setup(username, steps)

    # What this Logos can set up at all, for the Add a person form.
    def offers(self) -> Dict[str, Any]:
        out = {"chat": False, "calendar": False, "household": False, "email": True, "matrix_server": ""}
        try:
            out["matrix_server"] = self.matrix().settings()["MATRIX_SERVER_NAME"]
            out["chat"] = True
        except Exception:
            pass
        try:
            out["calendar"] = bool(self.calendar().running())
        except Exception:
            pass
        out["household"] = "household" in self.rt.defined()
        return out

    # ── 2. the person accepts: accounts that need their password ─────────────────────────────

    def accept_accounts(self, username: str, display_name: str, password: str, options: Mapping[str, Any]) -> Steps:
        """Their chat and calendar accounts, with the password they just chose. Runs while they wait;
        the password is not kept. Returns the steps so far."""
        steps = self.db.get_people_setup(username) or []
        step(steps, "account", "ok", "Signed in for the first time")
        if options.get("chat"):
            try:
                mxid = self.matrix().register_person(username, password, display_name)
                step(steps, "chat", "ok", f"{mxid}, with the same password")
            except Exception as e:
                step(steps, "chat", "failed", _plain(e))
        else:
            step(steps, "chat", "skipped", "Not asked for")
        if options.get("calendar"):
            try:
                self.calendar().add_person(username, display_name, password=password)
                step(steps, "calendar", "ok", "Their own calendar and Household, with the same password")
            except Exception as e:
                step(steps, "calendar", "failed", _plain(e))
        else:
            step(steps, "calendar", "skipped", "Not asked for")
        for sid in ("definition", "room", "agent", "household"):
            if not any(s["id"] == sid for s in steps):
                step(steps, sid, "waiting")
        self.save(username, steps)
        return steps

    # ── 3. in the background: their agent ──────────────────────────────────────────────────────

    def set_up_agent(self, username: str, display_name: str, options: Mapping[str, Any]) -> Steps:
        """Write the definition (when there is none yet), make the agent's chat with the person, start
        the agent, and restart the household agent when it is running. Safe to run again."""
        from gateway.agent_runtime import private_agents_dir
        steps = self.db.get_people_setup(username) or []
        chat_ok = any(s["id"] == "chat" and s["state"] == "ok" for s in steps)
        cal_ok = any(s["id"] == "calendar" and s["state"] == "ok" for s in steps)
        path = private_agents_dir(self.rt.env) / f"{username}.yaml"
        try:
            if path.exists():
                agent = read_definition(path)
                step(steps, "definition", "ok", "Kept from before")
            else:
                mxid = self.matrix().user_id(username) if chat_ok else ""
                agent = template(self.rt, username, display_name,
                                 {**options, "calendar": bool(options.get("calendar")) and cal_ok}, mxid)
                write_definition(self.rt, agent)
                step(steps, "definition", "ok", "Written outside the repo")
        except Exception as e:
            step(steps, "definition", "failed", _plain(e))
            for sid in ("room", "agent", "household"):
                step(steps, sid, "skipped", "Needs the definition")
            self.save(username, steps)
            return steps
        self.save(username, steps)

        mxid = (agent.get("person") or {}).get("matrix_id")
        if not mxid:
            step(steps, "room", "skipped", "No chat account")
        elif (agent.get("matrix") or {}).get("home_room"):
            step(steps, "room", "ok", "Made before")
        else:
            step(steps, "room", "running")
            self.save(username, steps)
            try:
                room = self._agent_room(username, mxid)
                agent.setdefault("matrix", {})["home_room"] = room
                write_definition(self.rt, agent)
                step(steps, "room", "ok", "They are invited to it in Element X; reminders and briefings go there")
            except Exception as e:
                step(steps, "room", "failed", _plain(e))
        step(steps, "agent", "running", "Starting (a minute or two)")
        self.save(username, steps)
        try:
            code = self.rt.up(username, load_model=False)
            ph = self.rt.phase(username)
            step(steps, "agent", "ok" if code == 0 else "failed", "Running" if code == 0 else f"Did not start ({ph})")
        except Exception as e:
            step(steps, "agent", "failed", _plain(e))
        self.save(username, steps)
        self._household(steps, joining=bool(options.get("household")))
        self.save(username, steps)
        return steps

    def _agent_room(self, username: str, person_mxid: str) -> str:
        """Give the agent its chat account before its first start (so the room can be its home room from
        the first start), then make a direct chat with the person, from the agent's side."""
        from gateway.agent_runtime import volumes
        mx = self.matrix()
        home, _brain = volumes(username)
        self.rt.ensure_volume(home)
        cur = parse_env(self.rt.get(home, ".env"))
        if not (cur.get("LOGOS_MATRIX_ACCESS_TOKEN") and cur.get("LOGOS_MATRIX_USER_ID")):
            login = mx.agent_login(username, {})
            cur.update({f"LOGOS_{k}": v for k, v in login.items()})
            self.rt.put(home, ".env", "".join(f"{k}={v}\n" for k, v in sorted(cur.items())), mode="0600")
        return mx.direct_room(cur["LOGOS_MATRIX_ACCESS_TOKEN"], cur["LOGOS_MATRIX_USER_ID"], person_mxid)

    def _household(self, steps: Steps, joining: bool) -> None:
        if "household" not in self.rt.defined():
            step(steps, "household", "skipped", "There is no household agent")
            return
        if not joining:
            step(steps, "household", "skipped", "Not asked for")
            return
        if self.rt.phase("household") != "Ready":
            step(steps, "household", "ok", "Joined; it is not running on this machine, so it knows them from its next start")
            return
        step(steps, "household", "running", "Restarting it so it knows them (a minute)")
        try:
            code = self.rt.up("household", load_model=False)
            step(steps, "household", "ok" if code == 0 else "failed",
                 "Joined: it answers them and sees their busy times" if code == 0 else "It did not come back; see Agents")
        except Exception as e:
            step(steps, "household", "failed", _plain(e))

    # ── removing someone, and bringing them back ───────────────────────────────────────────────

    def removable(self, username: str) -> Optional[str]:
        """Why their agent cannot be removed from here, or None."""
        if username in self.rt.defined() and not self.rt.is_private(username):
            return (f"Their agent is defined in the repo (agents/{username}.yaml); "
                    "it was set up by hand and is removed by hand.")
        return None

    def remove(self, username: str, by: str) -> Dict[str, Any]:
        """Stop their agent, keep its brain and home in the graveyard for 30 days, delete the volumes, and
        take them off the household agent, the calendar and their mail. Their account is suspended."""
        from gateway.agent_runtime import private_agents_dir, volumes
        user = self.db.get_user_by_username(username)
        if not user:
            raise PeopleError("No such person.")
        why = self.removable(username)
        if why:
            raise PeopleError(why)
        if self.graveyard.get(username):
            raise PeopleError("They were already removed.")
        now = int(self.now())
        self.db.update_user(user["id"], status="suspended")
        self.db.revoke_all_user_tokens(user["id"])
        self.db.revoke_invites(user["id"])
        path = private_agents_dir(self.rt.env) / f"{username}.yaml"
        agent = read_definition(path) if path.exists() else {}
        person = agent.get("person") or {}
        meta: Dict[str, Any] = {"username": username, "display_name": user.get("display_name") or username,
                                "agent": username if agent else None, "removed_at": now, "removed_by": by,
                                "delete_after": now + GRAVE_DAYS * 86400, "household": bool(person.get("household")),
                                "matrix_id": person.get("matrix_id") or "", "files": {}, "notes": []}
        self.graveyard.make(username)
        if agent:
            self.rt.down(username)
            for kind, vol in zip(("home", "brain"), volumes(username)):
                if self.rt.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
                    meta["notes"].append(f"{vol} did not exist")
                    continue
                p = self.rt.runner.run(["docker", "run", "--rm", "-v", f"{vol}:/v:ro", HELPER,
                                        "tar", "-C", "/v", "-czf", "-", "."], text=False)
                if p.returncode != 0:
                    raise PeopleError(f"could not back up {vol}; nothing was deleted")
                check_archive(p.stdout)
                meta["files"][f"{kind}.tar.gz"] = self.graveyard.write(username, f"{kind}.tar.gz", p.stdout)
            meta["files"]["definition.yaml"] = self.graveyard.write(username, "definition.yaml", path.read_bytes())
            self._write_meta(username, meta)            # the backup is safe before anything is deleted
            path.unlink()
            for vol in volumes(username):
                if self.rt.runner.run(["docker", "volume", "inspect", vol]).returncode == 0:
                    self.rt.run(["docker", "volume", "rm", vol])
        cal = agent.get("calendar") or {}
        if cal.get("enabled") and cal.get("person") == username:
            try:
                line = self.calendar().remove_person(username)
                if line:
                    meta["files"]["calendar-account"] = self.graveyard.write(username, "calendar-account", line.encode())
            except Exception as e:
                meta["notes"].append(f"calendar: {_plain(e)}")
        try:
            from gateway import mail_connect
            if mail_connect.remove_box(self.rt.env, username):
                meta["notes"].append("their mailbox login was deleted; they connect it again after a restore")
        except Exception as e:
            meta["notes"].append(f"mail: {_plain(e)}")
        self._write_meta(username, meta)
        meta["notes"] += self.refresh_services()
        if meta["household"] and "household" in self.rt.defined() and self.rt.phase("household") == "Ready":
            try:
                self.rt.up("household", load_model=False)
            except Exception as e:
                meta["notes"].append(f"household agent: {_plain(e)}")
        self._write_meta(username, meta)
        return meta

    def _write_meta(self, username: str, meta: Mapping[str, Any]) -> None:
        self.graveyard.write(username, "meta.json", json.dumps(meta, indent=1, sort_keys=True).encode())

    def refresh_services(self) -> List[str]:
        """After an agent is gone, the shared services forget its token (each restarts only if its
        configuration changed). Returns what could not be done."""
        from gateway import agent_runtime as ar
        notes = []
        for module, cls in (("noticeboard_host", "NoticeboardHost"), ("voice_host", "VoiceHost"),
                            ("calendar_host", "CalendarHost"), ("mail_host", "MailHost"), ("web_host", "WebHost"),
                            ("model_gateway", "ModelGateway")):
            try:
                getattr(ar._sibling(module), cls)(self.rt).up()
            except Exception as e:
                notes.append(f"{module}: {_plain(e)}")
        return notes

    def restore(self, username: str) -> Dict[str, Any]:
        """Bring someone back within the 30 days: their volumes, definition and calendar, their account
        (with the password they had), their agent started and the household agent told."""
        from gateway.agent_runtime import private_agents_dir, volumes
        meta = self.graveyard.get(username)
        if not meta:
            raise PeopleError("There is nothing to restore for them.")
        user = self.db.get_user_by_username(username)
        if not user:
            raise PeopleError("Their account is gone.")
        if meta.get("agent"):
            if username in self.rt.defined():
                raise PeopleError(f"An agent called {username} exists again; restore by hand.")
            for kind, vol in zip(("home", "brain"), volumes(username)):
                if not self.graveyard.has(username, f"{kind}.tar.gz"):
                    continue
                if self.rt.runner.run(["docker", "volume", "inspect", vol]).returncode == 0:
                    raise PeopleError(f"{vol} exists again; restore by hand.")
                data = self.graveyard.read(username, f"{kind}.tar.gz")
                check_archive(data)
                self.rt.ensure_volume(vol)
                p = self.rt.runner.run(["docker", "run", "--rm", "-i", "-v", f"{vol}:/v", HELPER,
                                        "tar", "-C", "/v", "-xzf", "-"], input=data, text=False)
                if p.returncode != 0:
                    raise PeopleError(f"could not unpack {vol}")
            folder = private_agents_dir(self.rt.env)
            folder.mkdir(parents=True, exist_ok=True)
            os.chmod(folder, 0o700)
            target = folder / f"{username}.yaml"
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(self.graveyard.read(username, "definition.yaml"))
        notes = []
        if self.graveyard.has(username, "calendar-account"):
            try:
                self.calendar().restore_person(self.graveyard.read(username, "calendar-account").decode())
            except Exception as e:
                notes.append(f"calendar: {_plain(e)}")
        self.db.update_user(user["id"], status="active")
        if meta.get("agent"):
            try:
                if self.rt.up(username, load_model=False) != 0:
                    notes.append("their agent did not start; see Agents")
            except Exception as e:
                notes.append(f"agent: {_plain(e)}")
            if meta.get("household") and "household" in self.rt.defined() and self.rt.phase("household") == "Ready":
                try:
                    self.rt.up("household", load_model=False)
                except Exception as e:
                    notes.append(f"household agent: {_plain(e)}")
        self.graveyard.forget(username)
        return {"username": username, "notes": notes}

    def purge_due(self) -> List[Dict[str, Any]]:
        """Delete for good everyone removed more than 30 days ago: the backup, their calendar, their chat
        accounts (closed), and their dashboard account."""
        done = []
        now = self.now()
        for meta in self.graveyard.list():
            if meta.get("delete_after", now + 1) > now:
                continue
            name = meta["username"]
            notes = []
            if self.graveyard.has(name, "calendar-account"):
                try:
                    self.calendar().purge_person(name)
                except Exception as e:
                    notes.append(f"calendar: {_plain(e)}")
            if meta.get("matrix_id"):
                for local in ([name, f"{name}-agent"] if meta.get("agent") else [name]):
                    try:
                        self.matrix().deactivate(local)
                    except Exception as e:
                        notes.append(f"chat @{local}: {_plain(e)}")
            user = self.db.get_user_by_username(name)
            if user and user.get("status") != "active":
                self.db.delete_user(user["id"])
            self.db.set_people_setup(name, None)
            self.graveyard.forget(name)
            done.append({"username": name, "notes": notes})
        return done


def _plain(e: Exception) -> str:
    """An error for a person's page: one line, no long command output."""
    text = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return text[:300]
