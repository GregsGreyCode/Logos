"""Run the household's own calendar server (story C1).

Calendars live here instead of with Google: one Radicale CalDAV server in the ``logos-calendar``
container and volume. Phones reach it only over Tailscale (``tailscale serve`` gives it an HTTPS
address on the tailnet) and sync it into the Android calendar with DAVx5. The LAN and the
internet cannot reach it, and the container itself cannot reach anything (no DNS).

Layout of the volume (mounted at /var/lib/radicale)::

    etc/config                     Radicale's settings (written by `up`)
    etc/rights                     who may open which calendar (written by `up`)
    etc/users                      people and their bcrypt password hashes
    collections/                   a git repository: every change is committed as it happens
      collection-root/greg/personal     Greg's own calendar
      collection-root/greg/household -> ../household/calendar   the shared one, by symlink
      collection-root/household/calendar

Each person sees their own calendars plus the shared Household calendar, which appears under
their account through the symlink. No one can open another person's calendar. Agents get no
account here: in story C3 they go through a Logos calendar service that checks what each one
may see.

Agents use the calendars through the Logos calendar service (:mod:`gateway.calendar_service`,
story C3) in the ``logos-calendar-service`` container: MCP on the Docker bridge address, one token
per agent, and its own Radicale account (``logos-service``). It shares a private Docker network
with Radicale, so Radicale itself is never on the bridge. An agent definition says what it sees::

    calendar: {enabled: true, person: greg}                  # Greg's own calendar + Household
    calendar: {enabled: true, free_busy: {greg: Greg, bere: Béré}}   # Household + busy times only

Passwords for people are written to ``~/.config/logos/calendar-<name>-password`` (mode 0600)
so they can be typed into DAVx5; delete the file once the phone is set up.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

CONTAINER = "logos-calendar"
VOLUME = "logos-calendar"
SERVICE = "logos-calendar-service"      # container and config volume of the agents' calendar service
NETWORK = "logos-calendar"              # private network: Radicale + the service, nothing else
SERVICE_PORT = 8095
SERVICE_USER = "logos-service"          # the service's Radicale account
TOKEN_ENV = "LOGOS_CALENDAR_TOKEN"      # an agent's own token, kept in its home volume .env
SERVICE_FILE = Path(__file__).with_name("calendar_service.py")
#: Radicale 3.9.2 (the project's own image), pinned by digest.
IMAGE = "ghcr.io/kozea/radicale@sha256:c74545ef669a3166646ec13438a31b5d0bfff198d8ba7ecd214c0a5f40f03fb4"
PORT = 5232
ROOT = "/var/lib/radicale"
PYTHON = "/app/bin/python"          # the image's own Python (has bcrypt)
MEMORY = "256m"
SHARED = "household"                # the shared calendar's owner directory; not a login
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

GITIGNORE = ".Radicale.cache\n.Radicale.lock\n.Radicale.tmp-*\n"

# Commit every change as it happens: the history is the backup and the undo.
HOOK = ("git add -A && (git diff --cached --quiet || git -c user.name=Radicale "
        "-c user.email=radicale@logos.invalid commit -q -m \"Changes by \"%(user)s)")


def radicale_config() -> str:
    return f"""# Written by gateway/calendar_host.py; changes here are overwritten on the next `up`.
[server]
hosts = 0.0.0.0:{PORT}
max_connections = 8

[auth]
type = htpasswd
htpasswd_filename = {ROOT}/etc/users
htpasswd_encryption = bcrypt
delay = 2

[rights]
type = from_file
file = {ROOT}/etc/rights

[storage]
filesystem_folder = {ROOT}/collections
hook = {HOOK}

[web]
type = none

[logging]
level = info
mask_passwords = True
"""


# Sections are tried in order; the first match decides. Anything not matched is refused.
RIGHTS = """# Written by gateway/calendar_host.py.
# The Logos calendar service reads and writes events everywhere; it cannot create or delete
# calendars. What each agent may see is enforced by the service (gateway/calendar_service.py).
[service]
user: logos-service
collection: .*
permissions: Rrw

# Anyone signed in may list the top level (needed for discovery).
[root]
user: .+
collection:
permissions: R

# Your own account: list it, and create calendars in it from the phone.
[principal]
user: .+
collection: {user}
permissions: RW

# Your own calendars, including the shared Household calendar linked into your account.
[calendars]
user: .+
collection: {user}/[^/]+
permissions: rw
"""


def calendar_props(display_name: str, color: str) -> str:
    return json.dumps({"tag": "VCALENDAR", "D:displayname": display_name,
                       "ICAL:calendar-color": color,
                       "C:supported-calendar-component-set": "VEVENT,VTODO"}, ensure_ascii=False)


#: Runs inside the image (as the radicale user) to lay out the volume. Reads JSON on stdin:
#:   {"config": str, "rights": str, "gitignore": str, "shared_props": str,
#:    "add": {"name", "hash", "props"} | null}
#: Prints JSON: {"people": [names]}.
SETUP = r"""
import json, os, subprocess, sys
root = sys.argv[1]
req = json.load(sys.stdin)
etc, coll = f"{root}/etc", f"{root}/collections"
top = f"{coll}/collection-root"
os.makedirs(etc, exist_ok=True); os.makedirs(top, exist_ok=True)
def write(path, text, mode=0o600):
    with open(path + ".new", "w") as f: f.write(text)
    os.chmod(path + ".new", mode); os.replace(path + ".new", path)
write(f"{etc}/config", req["config"]); write(f"{etc}/rights", req["rights"])
users = f"{etc}/users"
lines = open(users).read().splitlines() if os.path.exists(users) else []
people = {l.split(":", 1)[0]: l for l in lines if ":" in l}
add = req.get("add")
if add:
    people[add["name"]] = f"{add['name']}:{add['hash']}"
secret = f"{etc}/service-password"
if not os.path.exists(secret) or req["service"] not in people:
    import bcrypt, secrets as _s
    pw = _s.token_urlsafe(32)
    write(secret, pw + "\n")
    people[req["service"]] = f"{req['service']}:" + bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()
write(users, "".join(people[n] + "\n" for n in sorted(people)))
def git(*a): subprocess.run(["git", *a], cwd=coll, check=True, capture_output=True)
if not os.path.isdir(f"{coll}/.git"):
    git("init", "-q", "-b", "main")
write(f"{coll}/.gitignore", req["gitignore"], 0o644)
shared = f"{top}/household/calendar"
if not os.path.isdir(shared):
    os.makedirs(shared)
    write(f"{shared}/.Radicale.props", req["shared_props"], 0o644)
for name in people:
    if name == req["service"]:
        continue
    home = f"{top}/{name}"
    os.makedirs(home, exist_ok=True)
    if add and add["name"] == name and not os.path.isdir(f"{home}/personal"):
        os.makedirs(f"{home}/personal")
        write(f"{home}/personal/.Radicale.props", add["props"], 0o644)
    link = f"{home}/household"
    if not os.path.lexists(link):
        os.symlink("../household/calendar", link)
git("add", "-A")
if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=coll).returncode:
    git("-c", "user.name=Logos", "-c", "user.email=logos@logos.invalid", "commit", "-q", "-m", "Logos: calendar setup")
print(json.dumps({"people": sorted(n for n in people if n != req["service"]),
                  "service_password": open(secret).read().strip()}))
"""

HASH = r"""
import bcrypt, sys
print(bcrypt.hashpw(sys.stdin.read().encode(), bcrypt.gensalt(rounds=12)).decode())
"""


class CalendarError(Exception):
    pass


def uses_calendar(agent: Mapping[str, Any]) -> bool:
    return bool((agent.get("calendar") or {}).get("enabled"))


def calendars_for(agent: Mapping[str, Any], people: List[str]) -> Dict[str, dict]:
    """What one agent may see: ``full`` calendars (details, changes) and ``busy`` ones (times only)."""
    cal = agent.get("calendar") or {}
    full = {"household": {"path": "/household/calendar/", "label": "Household (shared by everyone)"}}
    busy = {}
    person = cal.get("person")
    if person:
        if person not in people:
            raise CalendarError(f"agent {agent.get('name')}: calendar.person {person!r} has no calendar account")
        full = {"personal": {"path": f"/{person}/personal/", "label": f"{person.capitalize()}'s own calendar"},
                **full}
    for who, label in (cal.get("free_busy") or {}).items():
        if who not in people:
            raise CalendarError(f"agent {agent.get('name')}: free_busy {who!r} has no calendar account")
        if who != person:
            busy[who] = {"paths": [f"/{who}/personal/"], "label": str(label)}
    return {"full": full, "busy": busy}


def service_config(agents: Mapping[str, Mapping[str, Any]], tokens: Mapping[str, str], people: List[str],
                   password: str) -> dict:
    return {"radicale": f"http://{CONTAINER}:{PORT}", "user": SERVICE_USER, "password": password,
            "agents": {n: {"token": tokens[n], "timezone": str(a.get("timezone") or "UTC"),
                           **calendars_for(a, people)} for n, a in sorted(agents.items())}}


def password_file(env: Mapping[str, str], name: str) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".config")
    return Path(base) / "logos" / f"calendar-{name}-password"


class CalendarHost:
    """``runtime`` is an :class:`~gateway.agent_runtime.AgentRuntime`."""

    def __init__(self, runtime: Any):
        self.rt = runtime

    # ── facts ────────────────────────────────────────────────────────────

    def running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}"],
                          check=False)
        return CONTAINER in out.split()

    def _label(self) -> str:
        return self.rt.run(["docker", "inspect", CONTAINER, "-f",
                            '{{index .Config.Labels "logos.calendar.stamp"}}'], check=False).strip()

    def stamp(self) -> str:
        return hashlib.sha256((radicale_config() + RIGHTS + IMAGE + NETWORK).encode()).hexdigest()[:16]

    # ── volume ───────────────────────────────────────────────────────────

    def _in_image(self, script: str, payload: str, *args: str) -> str:
        return self.rt.run(["docker", "run", "--rm", "-i", "--network", "none",
                            "--entrypoint", PYTHON, "-v", f"{VOLUME}:{ROOT}", IMAGE,
                            "-c", script, *args], input=payload)

    def _setup(self, add: Optional[Dict[str, str]] = None) -> List[str]:
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            self.rt.run(["docker", "volume", "create", VOLUME])
        req = {"config": radicale_config(), "rights": RIGHTS, "gitignore": GITIGNORE,
               "shared_props": calendar_props("Household", "#2e7d32"), "add": add, "service": SERVICE_USER}
        out = self._in_image(SETUP, json.dumps(req), ROOT)
        found = json.loads(out.strip().splitlines()[-1])
        self._service_password = found["service_password"]
        return found["people"]

    # ── lifecycle ────────────────────────────────────────────────────────

    def up(self) -> bool:
        """Lay out the volume, (re)start the server if its settings changed, and the agents'
        service if their tokens or its code changed. True when either was (re)started."""
        people = self._setup()
        if self.rt.runner.run(["docker", "network", "inspect", NETWORK]).returncode != 0:
            self.rt.run(["docker", "network", "create", NETWORK])
        started = self._up_server()
        return self._up_service(people) or started

    def _up_server(self) -> bool:
        stamp = self.stamp()
        if self.running() and self._label() == stamp:
            return False
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)
        self.rt.run(["docker", "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
                     "--network", NETWORK,
                     "--label", f"logos.calendar.stamp={stamp}",
                     "--memory", MEMORY, "--read-only", "--tmpfs", "/tmp",
                     # It never needs to call out; with no resolver it cannot look a name up.
                     "--dns", "127.0.0.1",
                     "-p", f"127.0.0.1:{PORT}:{PORT}",
                     "-v", f"{VOLUME}:{ROOT}", IMAGE, "--config", f"{ROOT}/etc/config"])
        self.wait()
        return True

    def wait(self, tries: int = 30) -> None:
        import urllib.error
        for _ in range(tries):
            try:
                self.rt.runner.http(f"http://127.0.0.1:{PORT}/.well-known/caldav", timeout=3)
                return
            except urllib.error.HTTPError:
                return                      # it answered (401 or a redirect): it is up
            except OSError:
                self.rt.runner.sleep(1)
        raise TimeoutError("the calendar server did not start; see: docker logs " + CONTAINER)

    # ── the agents' calendar service ────────────────────────────────────

    def bind_address(self) -> str:
        out = self.rt.run(["docker", "network", "inspect", "bridge", "-f",
                           "{{(index .IPAM.Config 0).Gateway}}"], check=False).strip()
        return out or "172.17.0.1"

    def service_url(self) -> str:
        return f"http://{self.bind_address()}:{SERVICE_PORT}/mcp"

    def agents(self) -> Dict[str, dict]:
        return {n: a for n in self.rt.defined() for a in [self.rt.load(n)] if uses_calendar(a)}

    def token_for(self, name: str) -> str:
        for line in self.rt.get(f"logos-home-{name}", ".env").splitlines():
            if line.startswith(TOKEN_ENV + "="):
                return line.split("=", 1)[1].strip()
        return ""

    def _service_label(self) -> str:
        return self.rt.run(["docker", "inspect", SERVICE, "-f",
                            '{{index .Config.Labels "logos.calendar.stamp"}}'], check=False).strip()

    def _service_running(self) -> bool:
        out = self.rt.run(["docker", "ps", "--filter", f"name=^{SERVICE}$", "--format", "{{.Names}}"], check=False)
        return SERVICE in out.split()

    def _up_service(self, people: List[str]) -> bool:
        agents = {n: a for n, a in self.agents().items()}
        tokens = {n: self.token_for(n) for n in agents}
        agents = {n: a for n, a in agents.items() if tokens[n]}  # joins on its first `up`
        if not agents:
            return False
        config = json.dumps(service_config(agents, tokens, people, self._service_password), indent=1,
                            sort_keys=True, ensure_ascii=False)
        stamp = hashlib.sha256((config + SERVICE_FILE.read_text() + IMAGE).encode()).hexdigest()[:16]
        if self._service_running() and self._service_label() == stamp:
            return False
        if self.rt.runner.run(["docker", "volume", "inspect", SERVICE]).returncode != 0:
            self.rt.run(["docker", "volume", "create", SERVICE])
        self.rt.run(["docker", "run", "--rm", "-i", "-v", f"{SERVICE}:/v", "alpine:3", "sh", "-c",
                     "cat > /v/config.json && chown -R 1000:1000 /v && chmod 0600 /v/config.json"], input=config)
        self.rt.run(["docker", "rm", "-f", SERVICE], check=False)
        self.rt.run(["docker", "run", "-d", "--name", SERVICE, "--restart", "unless-stopped",
                     "--label", f"logos.calendar.stamp={stamp}", "--network", NETWORK,
                     "--memory", MEMORY, "--read-only", "--tmpfs", "/tmp", "--dns", "127.0.0.1",
                     "-p", f"{self.bind_address()}:{SERVICE_PORT}:{SERVICE_PORT}",
                     "-v", f"{SERVICE}:/data:ro", "-v", f"{SERVICE_FILE}:/app/calendar_service.py:ro",
                     "-e", "CALENDAR_CONFIG=/data/config.json", "-e", f"CALENDAR_PORT={SERVICE_PORT}",
                     "--entrypoint", PYTHON, IMAGE, "/app/calendar_service.py"])
        for _ in range(30):
            try:
                self.rt.runner.http(f"http://{self.bind_address()}:{SERVICE_PORT}/health", timeout=3)
                return True
            except Exception:
                self.rt.runner.sleep(1)
        raise TimeoutError("the calendar service did not start; see: docker logs " + SERVICE)

    def down(self) -> None:
        self.rt.run(["docker", "rm", "-f", SERVICE], check=False)
        self.rt.run(["docker", "rm", "-f", CONTAINER], check=False)

    # ── people ───────────────────────────────────────────────────────────

    def add_person(self, name: str, display_name: str = "", color: str = "#1565c0",
                   reset: bool = False) -> Path:
        """Create (or with ``reset``, re-password) a person. Writes their new password to a 0600
        file and returns its path. Their own calendar and the Household link are created too."""
        if not NAME_RE.match(name) or name in (SHARED, SERVICE_USER):
            raise CalendarError(f"{name!r} is not a usable name (lowercase letters, digits, dashes)")
        if name in self.people() and not reset:
            raise CalendarError(f"{name} already exists (use --reset for a new password)")
        password = secrets.token_urlsafe(18)
        hashed = self._in_image(HASH, password).strip()
        if not hashed.startswith("$2"):
            raise CalendarError("could not hash the password")
        self._setup({"name": name, "hash": hashed,
                     "props": calendar_props(display_name or name.capitalize(), color)})
        path = password_file(self.rt.env, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password + "\n")
        return path

    def people(self) -> List[str]:
        if self.rt.runner.run(["docker", "volume", "inspect", VOLUME]).returncode != 0:
            return []
        text = self._in_image("import sys,os;p=sys.argv[1];print(open(p).read() if os.path.exists(p) else '')",
                              "", f"{ROOT}/etc/users")
        return sorted(n for n in (l.split(":", 1)[0] for l in text.splitlines() if ":" in l) if n != SERVICE_USER)

    def status(self) -> str:
        if not self.running():
            return "calendar: not running"
        agents = [n for n in sorted(self.agents()) if self.token_for(n)]
        return (f"calendar: running on 127.0.0.1:{PORT} (reach it through tailscale serve)\n"
                f"  people: {', '.join(self.people()) or 'none yet'}\n"
                f"  agents' service: {'running at ' + self.service_url() if self._service_running() else 'not running'}"
                f" ({', '.join(agents) or 'no agents yet'})")
