"""Create, inspect and remove Logos v2 agents (OpenShell 0.1 sandboxes running hermes).

This is the logic behind ``scripts/logos-agent`` and
:class:`gateway.sandbox_backend.OpenShellBackend`. An agent is described by
``agents/<name>.yaml`` (plus an untracked ``agents/<name>.local.yaml``) and
has two labelled Docker volumes (docs/adr/0001):

  logos-home-<name>  -> /opt/data        hermes home (config, SOUL.md, memories, sessions)
  logos-brain-<name> -> /opt/data/brain   markdown vault

Every external effect (``docker``, ``openshell``, ``tar``, HTTP calls to
LM Studio, sleeping) goes through a :class:`Runner`, so tests drive
:class:`AgentRuntime` with a fake and never touch the real binaries.

This module deliberately imports nothing from the rest of ``gateway`` so the
CLI can load it by file path without importing the gateway package.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, Union

import yaml

REPO = Path(__file__).resolve().parent.parent
SANDBOX_UID = "10000:10000"          # `hermes` in the logos-agent image
HELPER = "alpine:3"
GIT_HELPER = "alpine/git:latest"
LABELS = ["--label", "openshell.ai/sandbox-attachable=true",
          "--label", "openshell.ai/sandbox-attachable-workspace=default"]
# Plugins baked into the image (/opt/logos/plugins) are copied into the home on every start, so the
# running copy always matches the image. hermes loads plugins only from its home.
START = ("/opt/hermes/docker/stage2-hook.sh; "
         "if [ -d /opt/logos/plugins ]; then mkdir -p /opt/data/plugins && "
         "for p in /opt/logos/plugins/*/; do n=$(basename \"$p\"); rm -rf \"/opt/data/plugins/$n\"; "
         "cp -R \"$p\" \"/opt/data/plugins/$n\"; done; fi; "
         "exec hermes gateway run")
MEMORY_CHAR_LIMIT = 6000             # hermes default is 2,200; raised so the brain index has room
RESET_PLUGIN = "hermes-session-reset-policy"
# Logos' own hermes plugins (infra/agent-plugins/<name>/), copied into the home by `up`.
#: The mail tools let through the sandbox rule (gateway/mail_service.py TOOL_NAMES).
MAIL_TOOLS = ("mail_new", "mail_needs_reply", "mail_search", "mail_read", "mail_thread")
#: The advisor tools let through the sandbox rule (gateway/advisor_service.py TOOL_NAMES).
ADVISOR_TOOLS = ("ask_advisor", "advisor_answer", "advisor_budget", "request_more_budget")
SOUL_MAX_CHARS = 40_000            # hermes context_file_max_chars; `up` refuses a SOUL.md longer than this
LOGOS_PLUGINS = ("logos-waitlist", "logos-voice", "logos-verified-only", "logos-matrix-flat", "logos-turn-time",
                 "logos-reactions", "logos-job-limits", "logos-media-batch", "logos-alert-text", "logos-vault", "logos-voice-tag",
                 "logos-speaker-chat", "logos-speaker-say", "logos-rename", "logos-household-words")
MANAGED_JOBS = ("brain-commit", "brain-index", "vault-digest", "consolidate", "noticeboard-check", "noticeboard-urgent",
                "weekly-digest", "morning-briefing", "morning-briefing-weekend", "morning-briefing-check",
                "morning-briefing-weekend-check", "rerun-after-outage", "model-watch", "changes-daily",
                "changes-week", "changes-check", "lists-mirror",
                # Retired (one post now covers both): still listed so `up` removes them from sandboxes.
                "changes-homelab", "changes-logos", "changes-homelab-week", "changes-logos-week")
# Story DC2: the daily changes. One script under two names (it reads its own name): the day, and
# on Sundays the week. Each post covers both the agents (Logos) and the home servers (homelab).
CHANGES_SCRIPTS = ("changes_daily.py", "changes_week.py")
# What the household agent may call on the web reader: never search_web or add_private_name.
HOUSEHOLD_READER_TOOLS = ["weather", "file_request", "recent_changes", "search_docs", "read_doc", "list_docs"]
HOME_TARGET = "/opt/data"
BRAIN_TARGET = "/opt/data/brain"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
MISSING = "Missing"
ALL_LOCAL = "all.local.yaml"            # untracked overrides for every agent
# Sessions opened with `openshell sandbox exec` do not inherit the image ENV, so hermes CLI calls
# must be told where the agent's home is.
HERMES = "export HERMES_HOME=/opt/data; hermes"
# hermes' own Python, for the one helper that uses its job store (carry_monitor_state.py).
HERMES_PY = "export HERMES_HOME=/opt/data; /opt/hermes/.venv/bin/python /opt/data/scripts/carry_monitor_state.py"
# _config_version marks the file as current for the pinned hermes (v0.23.1 = 53); without it
# hermes treats the file as pre-v12 and skips its config migrations. Bump with the image.
CONFIG_VERSION = 53

Data = Union[str, bytes]

# Each sandbox's ceiling (OpenShell --cpu / --memory). The VM has 4 cores and 8 GB; an agent
# normally uses well under 0.5 GB, so these only stop one runaway agent starving the others.
# Override per agent with `resources: {cpu: "1", memory: "1Gi"}` in its definition.
DEFAULT_LIMITS = {"cpu": "2", "memory": "2Gi"}


def delivery(agent: Mapping[str, Any]) -> str:
    """Where scheduled jobs send what they have to say: Matrix, else nowhere."""
    if (agent.get("matrix") or {}).get("enabled"):
        return "matrix"
    return "local"


def briefing(agent: Mapping[str, Any]) -> Mapping[str, Any]:
    """The morning briefing settings, or {} when this agent sends none (story C4).

    On by default for a person's own agent (one with ``calendar.person``) that can reach its
    person on Matrix; never for the household agent."""
    cfg = agent.get("briefing") or {}
    person = ((agent.get("calendar") or {}).get("person"))
    if not cfg.get("enabled", bool(person)) or delivery(agent) == "local" or agent.get("role") == "household":
        return {}
    return {"weekdays": cfg.get("weekdays", "0 7 * * 1-5"), "weekends": cfg.get("weekends", "0 9 * * 0,6"),
            "speaker": bool(cfg.get("speaker")) and _sibling("speaker_say").speaks(agent)}


#: Voice V3.1: ``briefing: {speaker: true}`` also says a short version on the house speaker.
BRIEFING_SPOKEN = """

Also, after writing the briefing, call `say_on_speaker` once with a spoken version for the house speaker:
at most two short sentences, starting with your person's name: the weather and their first event
("Good morning Greg. Rain until noon; the dentist is at half past four."). Set `private: true` and
`person: {person}`. If it is not said, carry on: the written briefing above is what matters.
"""


def briefing_prompt(jobs_dir: Path, agent: Mapping[str, Any]) -> str:
    """The briefing's instructions, with the spoken version when ``briefing.speaker`` is on."""
    text = logos_job(Path(jobs_dir) / "morning_briefing.md")
    if briefing(agent).get("speaker"):
        person = str((agent.get("calendar") or {}).get("person") or "")
        text = text.rstrip() + "\n" + BRIEFING_SPOKEN.replace("{person}", person)
    return text


def changes_jobs(agent: Mapping[str, Any]) -> list:
    """[(job, script, schedule)] for the daily changes, or [] (story DC2).

    Only for an agent with ``changes: {enabled: true}`` that can post to Matrix (the household
    agent, in the group chat). One post daily at 16:00 for both Logos and the homelab; the week's
    roll-up as one more post on Sundays at 16:10 (``weekly: false`` turns it off). ``changes-check``
    at 16:20 sends the plain list for any that did not go out."""
    cfg = agent.get("changes") or {}
    if not cfg.get("enabled") or delivery(agent) == "local":
        return []
    jobs = [("changes-daily", "changes_daily.py", cfg.get("daily", "0 16 * * *"))]
    if cfg.get("weekly", True):
        jobs += [("changes-week", "changes_week.py", cfg.get("week", "10 16 * * 0"))]
    return jobs + [("changes-check", "changes_check.py", cfg.get("check", "20 16 * * *"))]


def uses_lists(agent: Mapping[str, Any]) -> bool:
    """The agent works the shared lists in Home Assistant (house.domains includes todo)."""
    house = agent.get("house") or {}
    return bool(house.get("enabled")) and "todo" in (house.get("domains") or [])


def minutes_later(schedule: str, minutes: int = 15) -> Optional[str]:
    """``"0 7 * * 1-5"`` -> ``"15 7 * * 1-5"``; None unless minute and hour are plain numbers."""
    parts = schedule.split()
    if len(parts) != 5 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    total = int(parts[1]) * 60 + int(parts[0]) + minutes
    if total >= 24 * 60:
        return None                      # would move to the next day: leave it without a check
    return " ".join([str(total % 60), str(total // 60)] + parts[2:])


def _sibling(module: str):
    """Load a module that sits next to this file, whether or not ``gateway`` is importable
    (the CLI loads this file by path)."""
    import importlib.util
    name = f"logos_{module}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{module}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)      # not half-loaded: the next use shows the real error again
        raise
    return mod



# Logos's own model jobs carry this line. The logos-job-limits plugin refuses send_message in a job
# whose prompt has it (22 Nov: Béré's digest went to the household agent). Jobs a person set up in
# chat ("every Friday remind Béré's agent…") do not have it: that sending is the person's own ask.
LOGOS_JOB_MARK = "[Logos job: its reply goes to your person only; it sends nothing to anyone else.]"


def logos_job(path: Path) -> str:
    return f"{LOGOS_JOB_MARK}\n\n{Path(path).read_text()}"


def digest_prompt(jobs_dir: Path, agent: Mapping[str, Any]) -> str:
    """The weekly digest's instructions. Only the household agent's is about the household (22 Nov:
    a line for it in everyone's instructions read, to Béré's agent, like "send it to the household
    agent", and her week went there)."""
    text = logos_job(Path(jobs_dir) / "weekly_digest.md")
    if agent.get("role") == "household":
        # Greg, 22 Nov: the household's recap is about the household, never about one person.
        text += ('\nYou are the household agent and this goes to the household group chat: start with '
                 '"**The household\'s week, ...**" instead of "Your week". Include only what belongs to the '
                 'household: the house, the shared lists, shared plans and dates. Leave out anything about '
                 'one person alone (their memories, hobbies, health, work or plans), even if it reached you '
                 'from their agent.\n')
    return text

class AgentRuntimeError(Exception):
    """A step failed; the message is what the CLI prints before exiting 1."""


@dataclass(frozen=True)
class Completed:
    """Result of one external command."""

    returncode: int = 0
    stdout: Data = ""
    stderr: Data = ""


class Runner(Protocol):
    """The only door to the outside world."""

    def run(self, argv: Sequence[str], *, input: Optional[Data] = None, text: bool = True) -> Completed:
        """Run ``argv`` capturing output (``str`` when ``text`` else ``bytes``)."""
        ...

    # Optional: ``run_env(argv, env)`` runs ``argv`` with extra environment variables (how a secret
    # reaches ``openshell provider create/update --credential KEY`` without ever being in argv).

    def call(self, argv: Sequence[str]) -> int:
        """Run ``argv`` attached to this process's stdio; return its exit code."""
        ...

    def http(self, url: str, *, data: Optional[bytes] = None,
             headers: Optional[Mapping[str, str]] = None, timeout: float = 10,
             method: Optional[str] = None) -> bytes:
        """GET ``url`` (POST when ``data`` is given, or ``method``) and return the body."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SubprocessRunner:
    """Real :class:`Runner`: subprocess, urllib and time.sleep."""

    def run(self, argv, *, input=None, text=True):
        p = subprocess.run(list(argv), input=input, text=text, capture_output=True)
        return Completed(p.returncode, p.stdout, p.stderr)

    def run_env(self, argv, env):
        p = subprocess.run(list(argv), text=True, capture_output=True, env={**os.environ, **env})
        return Completed(p.returncode, p.stdout, p.stderr)

    def call(self, argv):
        return subprocess.call(list(argv))

    def http(self, url, *, data=None, headers=None, timeout=10, method=None):
        req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def sleep(self, seconds):
        time.sleep(seconds)


# Routines (Greg, 22 Nov): the built-in jobs people may change in the dashboard (Agents › Routines): the
# morning briefing, the weekly digest and the daily changes. Their choices are kept beside Logos's other
# state, outside git, and laid over each agent's definition whenever it is loaded, so a deploy keeps them.
ROUTINE_KEYS = ("briefing", "digest", "changes")
ROUTINE_JOBS = ("morning-briefing", "morning-briefing-check", "morning-briefing-weekend",
                "morning-briefing-weekend-check", "weekly-digest", "changes-daily", "changes-week", "changes-check")


def routines_file(env: Mapping[str, str]) -> Path:
    return Path(env.get("LOGOS_HOME") or Path(env.get("HOME") or Path.home()) / ".logos") / "routines.json"


def load_routines(env: Mapping[str, str]) -> dict:
    try:
        data = json.loads(routines_file(env).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_routines(env: Mapping[str, str], name: str, choice: Mapping[str, Any]) -> dict:
    """Keep one agent's routine choices ({briefing: {...}, digest: {...}, changes: {...}})."""
    data = load_routines(env)
    mine = data.setdefault(name, {})
    for key, value in choice.items():
        if key in ROUTINE_KEYS and isinstance(value, dict):
            mine.setdefault(key, {}).update(value)
    path = routines_file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return mine


# F38: agents added from the dashboard (People › Add a person) are defined outside the repo, because
# their definitions name a person and the repo is public. The runtime reads them beside agents/.
def people_dir(env: Mapping[str, str]) -> Path:
    """Where the dashboard keeps the people it added: ``agents/`` (their definitions) and ``graveyard/``."""
    if env.get("LOGOS_PEOPLE_DIR"):
        return Path(env["LOGOS_PEOPLE_DIR"])
    return Path(env.get("LOGOS_HOME") or Path(env.get("HOME") or Path.home()) / ".logos") / "people"


def private_agents_dir(env: Mapping[str, str]) -> Path:
    return people_dir(env) / "agents"


def household_members(env: Mapping[str, str]) -> list:
    """The people added from the dashboard who joined the household agent, from their agents' definitions:
    ``[{"username", "display_name", "matrix_id", "calendar"}]``. Read straight from the files (never
    through ``load``), so loading the household agent cannot recurse."""
    out = []
    folder = private_agents_dir(env)
    for path in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
        if path.stem.endswith(".local"):
            continue
        try:
            agent = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        person = agent.get("person") or {}
        if not isinstance(person, dict) or not person.get("household") or not person.get("username"):
            continue
        cal = agent.get("calendar") or {}
        out.append({"username": str(person["username"]).lower(),
                    "display_name": str(person.get("display_name") or person["username"]),
                    "matrix_id": str(person.get("matrix_id") or ""),
                    "calendar": bool(cal.get("enabled")) and cal.get("person") == person["username"]})
    return out


def join_household(agent: dict, members: Sequence[Mapping[str, Any]]) -> None:
    """Lay the dashboard-added members over the household agent's definition, as is done by hand for the
    first people: they become owners, may chat with it, and it sees their busy times."""
    for m in members:
        owners = agent.get("owners")
        if isinstance(owners, list) and m["username"] not in [str(o).lower() for o in owners]:
            agent["owners"] = [*owners, m["username"]]
        mx = agent.get("matrix")
        if isinstance(mx, dict) and m.get("matrix_id"):
            allowed = list(mx.get("allowed_users") or [])
            if m["matrix_id"] not in allowed:
                mx["allowed_users"] = [*allowed, m["matrix_id"]]
        cal = agent.get("calendar")
        if isinstance(cal, dict) and cal.get("enabled") and m.get("calendar"):
            busy = dict(cal.get("free_busy") or {})
            busy.setdefault(m["username"], m["display_name"])
            cal["free_busy"] = busy


def merge(base: dict, extra: Mapping[str, Any]) -> None:
    """Deep-merge ``extra`` into ``base``: nested mappings merge, anything else replaces."""
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v


BUSY_INPUT_MODES = ("queue", "steer")        # not hermes's "interrupt": it loses the running request


def busy_input_mode(agent: Mapping[str, Any]) -> str:
    """What a message sent while the agent is still answering does: wait its turn, or steer the run."""
    mode = str(agent.get("busy_input_mode") or "queue")
    if mode not in BUSY_INPUT_MODES:
        raise AgentRuntimeError(f"{agent.get('name')}: busy_input_mode must be one of {', '.join(BUSY_INPUT_MODES)}, not {mode!r}")
    return mode


def volumes(name: str) -> tuple[str, str]:
    """(home volume, brain volume) for agent ``name``."""
    return f"logos-home-{name}", f"logos-brain-{name}"


def parse_phases(listing: str) -> dict[str, str]:
    """Map sandbox name -> phase from ``openshell sandbox list`` output.

    The name is the first column and the phase the last; ANSI colour codes
    are stripped. The header row comes back too (``{"NAME": "PHASE"}``);
    callers look names up, so it is harmless.
    """
    phases: dict[str, str] = {}
    for line in ANSI.sub("", listing).splitlines():
        parts = line.split()
        if parts:
            phases.setdefault(parts[0], parts[-1])
    return phases


def mounts_config(name: str) -> dict:
    """The ``--driver-config-json`` payload mounting the agent's two volumes."""
    home, brain = volumes(name)
    return {"docker": {"mounts": [
        {"type": "volume", "source": home, "target": HOME_TARGET, "read_only": False},
        {"type": "volume", "source": brain, "target": BRAIN_TARGET, "read_only": False}]}}


class AgentRuntime:
    """Operations on agents, with the repo root and all side effects injected."""

    def __init__(self, repo: Union[str, Path, None] = None, runner: Optional[Runner] = None,
                 env: Optional[Mapping[str, str]] = None):
        self.repo = Path(repo) if repo is not None else REPO
        self.runner: Runner = runner or SubprocessRunner()
        self.env = os.environ if env is None else env
        self.jobs_dir = self.repo / "infra" / "agent-jobs"

    # ── plumbing ─────────────────────────────────────────────────────────

    def run(self, cmd, *, input=None, check=True) -> str:
        p = self.runner.run(cmd, input=input)
        if check and p.returncode != 0:
            raise AgentRuntimeError(
                f"command failed: {' '.join(cmd[:4])} ...\n{ANSI.sub('', p.stderr or p.stdout)[-800:]}")
        return ANSI.sub("", p.stdout)

    def secret(self, name: str, key: str) -> str:
        """The real value of one of agent ``name``'s secrets (E7): the host store, else its .env."""
        return _sibling("agent_secrets").lookup(self, name, key)

    # ── definitions ──────────────────────────────────────────────────────

    def definition_path(self, name: str) -> Path:
        """``agents/<name>.yaml`` in the repo, else the dashboard's own (F38: outside the repo)."""
        repo = self.repo / "agents" / f"{name}.yaml"
        if repo.exists():
            return repo
        private = private_agents_dir(self.env) / f"{name}.yaml"
        return private if private.exists() else repo

    def is_private(self, name: str) -> bool:
        """True when the agent was added from the dashboard (its definition is outside the repo)."""
        return self.definition_path(name).parent != self.repo / "agents"

    def defined(self) -> list[str]:
        """Names of every agent with a definition in ``agents/`` or added from the dashboard."""
        names = {p.stem for p in (self.repo / "agents").glob("*.yaml") if not p.stem.endswith(".local")}
        folder = private_agents_dir(self.env)
        if folder.is_dir():
            names |= {p.stem for p in folder.glob("*.yaml") if not p.stem.endswith(".local")}
        return sorted(names)

    def load(self, name: str, settings: bool = True) -> dict:
        path = self.definition_path(name)
        if not path.exists():
            raise AgentRuntimeError(f"no agent definition at {path}")
        agent = yaml.safe_load(path.read_text())
        # What its people chose in the dashboard (Agents › Settings, story E6), kept outside git
        # (gateway/agent_settings.py); under the .local.yaml files, so a hand edit there still wins.
        if settings:
            merge(agent, _sibling("agent_settings").overlay(self.env, name))
        # Untracked overrides, so personal values (Matrix users and rooms) stay out of git:
        # agents/all.local.yaml applies to every agent (for example the model in use right now,
        # see `scripts/logos-models local use`), then agents/<name>.local.yaml to this one
        # (for an agent added from the dashboard, also <name>.local.yaml beside its definition).
        agents = self.repo / "agents"
        overlays = [agents / ALL_LOCAL, agents / f"{name}.local.yaml"]
        if path.parent != agents:
            overlays.append(path.with_suffix(".local.yaml"))
        for local in overlays:
            if local.exists():
                merge(agent, yaml.safe_load(local.read_text()) or {})
        # People added from the dashboard who joined the household (F38).
        if agent.get("role") == "household":
            join_household(agent, household_members(self.env))
        # Last, what people chose for the routines in the dashboard (Agents › Routines).
        merge(agent, {k: v for k, v in (load_routines(self.env).get(name) or {}).items() if k in ROUTINE_KEYS})
        return agent

    # ── volumes ──────────────────────────────────────────────────────────

    def ensure_volume(self, vol: str) -> None:
        if self.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
            self.run(["docker", "volume", "create", *LABELS, vol])
            # A fresh volume is root-owned; the sandbox user must own it.
            self.run(["docker", "run", "--rm", "-v", f"{vol}:/v", HELPER, "chown", SANDBOX_UID, "/v"])

    def ensure_vault(self, vol: str) -> None:
        """Seed the brain volume from infra/brain-template (never overwriting) and make it a git repo."""
        t = self.runner.run(["tar", "-C", str(self.repo / "infra" / "brain-template"), "-c", "."], text=False)
        if t.returncode != 0:
            raise AgentRuntimeError("could not archive the brain template:\n" + _text(t.stderr)[-400:])
        # Unpack to a scratch dir, then copy without overwriting: files a person or the agent already
        # changed are never replaced, and files added to the template later still arrive.
        sh = ("mkdir -p /tmp/t && tar -x -C /tmp/t -f - && cd /tmp/t && find . -type f | while read f; do "
              '[ -e "/v/$f" ] || { mkdir -p "/v/$(dirname "$f")" && cp "$f" "/v/$f"; }; done; cd /v; '
              "git config --global --add safe.directory /v; "
              "[ -d .git ] || { git init -q -b main . && git add -A && "
              "git -c user.name=logos -c user.email=logos@localhost commit -q -m 'Start the brain'; }; "
              f"chown -R {SANDBOX_UID} /v")
        p = self.runner.run(["docker", "run", "--rm", "-i", "--entrypoint", "sh", "-v", f"{vol}:/v",
                             GIT_HELPER, "-c", sh], input=t.stdout, text=False)
        if p.returncode != 0:
            raise AgentRuntimeError("could not seed the brain volume:\n" + _text(p.stderr)[-400:])

    def put(self, vol: str, rel: str, content: str, mode: str = "0640") -> None:
        """Write a file into a volume through a helper container."""
        sh = (f'mkdir -p "$(dirname /v/{rel})" && cat > /v/{rel} && chown {SANDBOX_UID} /v/{rel} '
              f'&& chmod {mode} /v/{rel}')
        self.run(["docker", "run", "--rm", "-i", "-v", f"{vol}:/v", HELPER, "sh", "-c", sh], input=content)

    def get(self, vol: str, rel: str) -> str:
        # `docker run -v <name>` silently creates a missing volume (unlabelled and root-owned,
        # which OpenShell then refuses to mount), so never touch one that does not exist yet.
        if self.runner.run(["docker", "volume", "inspect", vol]).returncode != 0:
            return ""
        p = self.runner.run(["docker", "run", "--rm", "-v", f"{vol}:/v:ro", HELPER, "cat", f"/v/{rel}"])
        return p.stdout if p.returncode == 0 else ""

    # ── model, config, env ───────────────────────────────────────────────

    def ensure_model(self, model: Mapping[str, Any]) -> None:
        """LM Studio only: make sure the agent's model is loaded with the configured context length."""
        if model.get("provider") != "lmstudio":
            return
        lm = _sibling("local_models")
        try:
            window = lm.load_window(model)
            if lm.ensure(self.runner.http, lm.server(model), model["default"], window):
                print(f"loaded {model['default']} in LM Studio (context {window})")
        except Exception as e:
            print(f"warning: could not check or load the model in LM Studio: {e}", file=sys.stderr)

    def config_text(self, name: str, agent: Mapping[str, Any], noticeboard_token: str = "",
                    voice_token: str = "", calendar_token: str = "", web_token: str = "",
                    changes_token: str = "", house_token: str = "", mail_token: str = "",
                    advisor_token: str = "") -> str:
        """The hermes ``config.yaml`` written into the agent's home. ``noticeboard_token`` and
        ``voice_token`` are the agent's own tokens for those services (the file lives in its
        private home volume, next to ``.env``)."""
        m = agent["model"]
        model = {"default": m["default"], "provider": m["provider"], "base_url": m["base_url"],
                 "context_length": int(m["context_length"])}
        mg = _sibling("model_gateway")
        if mg.uses_gateway(agent):
            # Through the model gateway: an OpenAI-compatible endpoint, the agent's own capped key.
            model = {"default": mg.gateway_model(agent), "provider": "custom",
                     "base_url": mg.ModelGateway(self).base_url(), "key_env": mg.KEY_ENV,
                     "context_length": int(m["context_length"])}
        # A photo sent in a chat goes to the model as an image (hermes image_routing: model.supports_vision
        # is read first), for a model that can see; hermes's catalog does not know LM Studio's models.
        if m.get("vision"):
            model["supports_vision"] = True
        cfg = {"_config_version": CONFIG_VERSION,
               "timezone": agent.get("timezone", "UTC"),
               "model": model}
        # When the model cannot be reached, say so after the normal three attempts (about
        # twenty seconds). hermes would otherwise keep trying in the background for some
        # eight more minutes, during which the person hears nothing.
        # hermes's own web and browser tools are off: an agent has no network of its own, so they can
        # only fail, and its built-in web_search would hide the web reader's tool. Searching goes
        # through the web reader (search_web), for the agents that have it.
        cfg["agent"] = {"auto_recovery_cycles": 0, "disabled_toolsets": ["web", "browser"]}
        # Every tool in view: hermes otherwise hides MCP tools (calendar, noticeboard, web reader)
        # behind a tool_search step, which the local model often skips, so it never sees them.
        # Our agents have about a dozen MCP tools, so the extra context is small.
        cfg["tools"] = {"tool_search": {"enabled": "off"}}
        # The whole SOUL.md, never cut: hermes caps context files at 20,000 characters for a 64k window
        # and drops the middle of a longer one (keeping 70% head, 20% tail), so rules vanished
        # silently once the shared rules passed it (20 Nov, 21.8k-23.8k). ~24k chars is ~6k tokens.
        cfg["context_file_max_chars"] = SOUL_MAX_CHARS
        # Scheduled messages arrive as written: no "Cronjob Response: <name> (job_id: ...)" header and
        # "To stop or manage this job..." footer around them in the chat.
        # mirror_delivery: what a job sends to a chat is also written into that chat's session, so
        # the agent can see its own scheduled messages (a daily question, a reminder) when the
        # person answers them. hermes leaves this off by default.
        cfg["cron"] = {"wrap_response": False, "mirror_delivery": True}
        # Nothing in the sandbox may call home: no update checks, no model catalogue download.
        cfg["updates"] = {"check": False}
        cfg["model_catalog"] = {"enabled": False}
        # hermes's catalogue of AI models (names, context sizes, prices) comes from Logos's copy of
        # models.dev's (gateway/models_catalogue.py): its refresh every 4 h was refused and asked the
        # agent's people about models.dev (28 Nov). Same data; nothing leaves the sandbox.
        cfg["models_dev"] = {"url": self.models_catalogue_url()}
        # No "gateway shutting down / back online" messages to the person on every rebuild.
        if (agent.get("matrix") or {}).get("enabled"):
            # Scheduled messages (the daily question, the changes post, briefings) go straight into
            # the chat. hermes otherwise opens a "Hermes — <job>" thread for each once
            # cron.mirror_delivery is on (20 Nov, the changes post in the household group).
            cfg["platforms"] = {"matrix": {"gateway_restart_notification": False,
                                           "extra": {"cron_continuable_surface": "in_channel"}}}
            # One message per answer: which tools the agent is using goes to its log only, and
            # its "let me check..." text before a tool call is not sent as a message of its own.
            cfg["display"] = {"platforms": {"matrix": {
                "tool_progress": "log", "interim_assistant_messages": False}},
                # A message sent while the agent is still answering waits its turn ("queue"). hermes otherwise
                # interrupts the running answer for it, and that request is lost ("Add creme de
                # marron to the to-do list" on 20 Nov 13:25, cut off by the next item 30 s later).
                # An agent may choose "steer" (agents/<name>.yaml: busy_input_mode): the follow-up joins the run
                # in progress after its next step, without cancelling it (a correction reaches it; with no step
                # left, or a photo attached, it waits its turn as before). 23 Nov, Greg: "Quote*" waited 5 minutes.
                "busy_input_mode": busy_input_mode(agent)}
        if agent.get("brain_index", True):
            cfg["memory"] = {"memory_char_limit": MEMORY_CHAR_LIMIT}
        cfg.setdefault("plugins", {"enabled": []})["enabled"].extend(LOGOS_PLUGINS)
        reset = agent.get("session_reset")
        if reset and str(reset.get("mode", "none")) != "none":
            # hermes no longer resets conversations on a timer; this catalog plugin (baked into the
            # image) starts a fresh session on the first message after the daily boundary.
            cfg.setdefault("plugins", {"enabled": []})["enabled"].append(RESET_PLUGIN)
            cfg["session_reset"] = {k: reset[k] for k in ("mode", "at_hour", "idle_minutes") if k in reset}
        nb = _sibling("noticeboard_host")
        if nb.uses_noticeboard(agent) and noticeboard_token:
            # The only channel to other agents: two tools, send_message and read_messages.
            cfg.setdefault("mcp_servers", {})["noticeboard"] = {
                "url": nb.NoticeboardHost(self).url(),
                "headers": {"Authorization": f"Bearer {noticeboard_token}"}}
        ch = _sibling("calendar_host")
        if ch.uses_calendar(agent) and calendar_token:
            # The household's calendars, through the Logos calendar service (docs/calendar.md).
            cfg.setdefault("mcp_servers", {})["calendar"] = {
                "url": ch.CalendarHost(self).service_url(),
                "headers": {"Authorization": f"Bearer {calendar_token}"}}
        hh = _sibling("house_host")
        if hh.uses_house(agent) and house_token:
            # The lights, through the Logos house service (docs/house.md).
            cfg.setdefault("mcp_servers", {})["house"] = {
                "url": hh.HouseHost(self).url(),
                "headers": {"Authorization": f"Bearer {house_token}"}}
        ah = _sibling("advisor_host")
        if ah.uses_advisor(agent) and advisor_token:
            # Questions to Claude, each approved by the person first (docs/advisor.md).
            cfg.setdefault("mcp_servers", {})["advisor"] = {
                "url": ah.AdvisorHost(self).url(),
                "headers": {"Authorization": f"Bearer {advisor_token}"}}
        mh = _sibling("mail_host")
        if mh.uses_mail(agent) and mail_token:
            # The person's own email, through the Logos mail service (docs/mail.md).
            cfg.setdefault("mcp_servers", {})["mail"] = {
                "url": mh.MailHost(self).url(),
                "headers": {"Authorization": f"Bearer {mail_token}"}}
        wh = _sibling("web_host")
        if wh.uses_web(agent) and web_token:
            # Web search through the web reader, which filters what leaves the house (docs/web-reader.md).
            # Named logos_web: hermes groups an MCP server's tools under its name, and "web" is its own
            # (disabled) built-in web toolset.
            cfg.setdefault("mcp_servers", {})["logos_web"] = {
                "url": wh.WebHost(self).url(),
                "headers": {"Authorization": f"Bearer {web_token}"}}
        elif wh.uses_changes(agent) and changes_token:
            # Story FR1: the household agent reaches the reader only to file requests. Its changes
            # token gets file_request alone, from the reader and from the sandbox rule.
            cfg.setdefault("mcp_servers", {})["logos_web"] = {
                "url": wh.WebHost(self).url(),
                "headers": {"Authorization": f"Bearer {changes_token}"}}
        vh = _sibling("voice_host")
        if vh.uses_voice(agent) and voice_token:
            # Voice notes are turned into text by the shared voice service (docs/voice.md).
            # hermes calls it as an OpenAI-style transcription endpoint.
            # echo_transcripts: post what was heard before the answer, so the person can check it was
            # understood. Off unless the agent's yaml says `voice_echo: true` (Béré asked for it).
            cfg["stt"] = {"enabled": True, "provider": "openai",
                          "echo_transcripts": bool(agent.get("voice_echo", False)),
                          "openai": {"api_key": voice_token, "base_url": vh.VoiceHost(self).base_url(),
                                     "model": "whisper-1"}}
        else:
            # Without the service a voice note cannot be understood; never fall back to
            # downloading a model inside the sandbox.
            cfg["stt"] = {"enabled": False}
        return ("# Written by scripts/logos-agent; edit agents/%s.yaml instead.\n%s"
                % (name, yaml.safe_dump(cfg, sort_keys=False)))

    def soul_text(self, agent: Mapping[str, Any]) -> str:
        """The persona plus the shared rules every agent follows (brain filing, honesty)."""
        # The persona: the one its people wrote in the dashboard (E6), else the definition's soul: file.
        text = _sibling("agent_settings").own_soul(self.repo, self.env, agent).rstrip() + "\n\n"
        rn = _sibling("agent_rename")
        if (self.repo / "souls/_shared/name.md").exists():     # what it is called, and renaming itself
            text += rn.name_section(agent) + "\n"
            if rn.uses_rename(agent):
                text += (self.repo / "souls/_shared/name.md").read_text().rstrip() + "\n\n"
        text += (self.repo / "souls/_shared/brain.md").read_text()
        judgement = self.repo / "souls/_shared/judgement.md"   # checking facts, verifying, learning from mistakes
        if judgement.exists():
            text = text.rstrip() + "\n\n" + judgement.read_text()
        if _sibling("noticeboard_host").uses_noticeboard(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/noticeboard.md").read_text()
        if agent.get("vault", True) and (self.repo / "souls/_shared/vault.md").exists():
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/vault.md").read_text()       # files people send are kept (F47)
        if _sibling("network_approvals").enabled(agent) and (self.repo / "souls/_shared/network.md").exists():
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/network.md").read_text()   # E6: blocked sites ask
        if _sibling("voice_host").uses_voice(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/voice.md").read_text()
        if _sibling("speaker_say").speaks(agent) and (self.repo / "souls/_shared/speaker.md").exists():
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/speaker.md").read_text()   # V3.1
        if _sibling("calendar_host").uses_calendar(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/calendar.md").read_text()
        if _sibling("web_host").uses_web(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/web.md").read_text()
        hh = _sibling("house_host")
        if hh.uses_house(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/house.md").read_text()
            if uses_lists(agent) and (self.repo / "souls/_shared/lists.md").exists():
                text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/lists.md").read_text()
            notes = hh.config_dir(self.env) / hh.NOTES_FILE      # facts about this home, never in git
            if notes.exists() and notes.read_text().strip():
                text = text.rstrip() + "\n\n### About this home's lights\n\n" + notes.read_text().strip() + "\n"
        if _sibling("advisor_host").uses_advisor(agent) and (self.repo / "souls/_shared/advisor.md").exists():
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/advisor.md").read_text()
        if _sibling("mail_host").uses_mail(agent) and (self.repo / "souls/_shared/mail.md").exists():
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/mail.md").read_text()
        wh = _sibling("web_host")
        docs = bool(wh.WebHost(self).docmost().get("DOCMOST_READ_ROOTS"))
        # The web reader: the weather anywhere; requests; what changed; the homelab docs.
        for part in ("weather.md", "requests.md", "changes.md", "docs.md"):
            if part == "docs.md" and not docs:
                continue
            if (wh.uses_web(agent) or wh.uses_changes(agent)) and (self.repo / "souls/_shared" / part).exists():
                text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared" / part).read_text()
        return text

    MATRIX_EXPORTED = ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID", "MATRIX_DEVICE_ID",
                       "MATRIX_ALLOWED_USERS", "MATRIX_E2EE_MODE", "MATRIX_HOME_ROOM",
                       "MATRIX_RECOVERY_KEY_OUTPUT_FILE", "MATRIX_RECOVERY_KEY", "MATRIX_REQUIRE_MENTION",
                       "MATRIX_SESSION_SCOPE")
    MATRIX_RECOVERY_FILE = "matrix-recovery-key"      # in the home volume (/opt/data)

    def _matrix_env(self, agent: Mapping[str, Any], cur: dict, home: str = "") -> None:
        """The agent's Matrix account. Its login is kept under LOGOS_MATRIX_* so switching Matrix
        off (which removes what hermes reads) and on again reuses the same account and keys."""
        mx = agent.get("matrix") or {}
        for k in self.MATRIX_EXPORTED:
            cur.pop(k, None)
        if not mx.get("enabled"):
            return
        mh = _sibling("matrix_host")
        host = mh.MatrixHost(self)
        try:
            if host.up():
                print("matrix: server started")
            stored = {k[len("LOGOS_"):]: v for k, v in cur.items() if k.startswith("LOGOS_MATRIX_")}
            login = host.agent_login(agent["name"], stored)
        except (mh.MatrixError, TimeoutError, OSError) as e:
            raise AgentRuntimeError(f"matrix: {e}")
        for k, v in login.items():
            cur[f"LOGOS_{k}"] = v
        cur.update({k: v for k, v in login.items() if k != "MATRIX_PASSWORD"})
        cur["MATRIX_HOMESERVER"] = host.url()
        cur["MATRIX_ALLOWED_USERS"] = ",".join(str(u) for u in mx.get("allowed_users") or [])
        cur["MATRIX_E2EE_MODE"] = str(mx.get("e2ee", "required"))
        # In a room with more than two people, answer only when @mentioned (DMs always answer).
        cur["MATRIX_REQUIRE_MENTION"] = "true" if mx.get("require_mention", True) else "false"
        # One conversation per room. hermes otherwise starts a thread, and a fresh session, for every
        # top-level message in a group room, so the household agent forgot the message before
        # ("Try again" -> "I don't have the context"). DMs are one session already.
        cur["MATRIX_SESSION_SCOPE"] = str(mx.get("session_scope", "room"))
        # M5: answer only messages from devices the person has verified (logos-verified-only plugin).
        cur["LOGOS_ONLY_VERIFIED_DEVICES"] = "1" if mx.get("verified_only") else "0"
        # Cross-signing recovery key, written once (0600) into the home volume, so a rebuilt
        # sandbox can prove it is the same device and still read encrypted history.
        cur["MATRIX_RECOVERY_KEY_OUTPUT_FILE"] = f"/opt/data/{self.MATRIX_RECOVERY_FILE}"
        # Once written, hand it back on every start so the agent re-proves its identity.
        recovery = self.get(home, self.MATRIX_RECOVERY_FILE).strip() if home else ""
        if recovery:
            cur["MATRIX_RECOVERY_KEY"] = recovery
        if mx.get("home_room"):
            cur["MATRIX_HOME_ROOM"] = str(mx["home_room"])
        try:
            if host.set_display_name(login["MATRIX_USER_ID"], login["MATRIX_ACCESS_TOKEN"],
                                     mh.agent_display_name(agent)):
                print(f"matrix: named {mh.agent_display_name(agent)}")
        except (mh.MatrixError, OSError) as e:          # a name is cosmetic: report, carry on
            print(f"matrix: could not set the display name: {e}")

    def alert_url(self) -> str:
        """Where an agent hands Logos its reply's words for the phone alert: the dashboard, on the
        Docker bridge (as the Matrix server reaches it for pushes)."""
        host = _sibling("matrix_host").MatrixHost(self).bind_address()
        return f"http://{host}:{os.environ.get('LOGOS_PORT') or '8091'}{_sibling('alert_text').PATH}"

    def rename_url(self) -> str:
        """Where an agent asks Logos for a new name (gateway/agent_rename.py): the dashboard, like the alerts."""
        return self.alert_url().replace(_sibling("alert_text").PATH, _sibling("agent_rename").PATH)

    def speaker_say_url(self) -> str:
        """Where an agent asks Logos to say something on the house speaker: the dashboard, like the alerts."""
        return self.alert_url().replace(_sibling("alert_text").PATH, _sibling("speaker_say").PATH)

    def models_catalogue_url(self) -> str:
        """Where hermes gets its catalogue of AI models: Logos's copy of models.dev's, on the dashboard."""
        return self.alert_url().replace(_sibling("alert_text").PATH, _sibling("models_catalogue").PATH)

    def env_file(self, agent: Mapping[str, Any], home: str) -> str:
        """Merge managed keys into the agent's .env, keeping secrets already stored there."""
        cur = dict(l.split("=", 1) for l in self.get(home, ".env").splitlines()
                   if "=" in l and not l.startswith("#"))
        # E7: what the sandbox's .env holds as a placeholder, and what only the host keeps (the Matrix
        # login), come back from the host store, so every value below is the real one.
        sec = _sibling("agent_secrets")
        host = sec.load(self.env, agent["name"])
        for k, v in list(cur.items()):
            if sec.is_placeholder(v):
                if host.get(k):
                    cur[k] = host[k]
                else:
                    del cur[k]
        # Also a secret with no line at all (moved to the host, the sandbox not started since), so a
        # placeholder secret keeps its value across `up`s instead of being made anew.
        for k in (*sec.HOST_ONLY, *sec.SECRETS.values()):
            if host.get(k) and not cur.get(k):
                cur[k] = host[k]
        # Telegram is no longer used: drop anything an older `up` stored, the bot token included.
        for key in [k for k in cur if k.startswith("TELEGRAM_")]:
            del cur[key]
        self._matrix_env(agent, cur, home)
        at = _sibling("alert_text")
        if (agent.get("matrix") or {}).get("enabled"):
            # Phone alerts for its replies say what it wrote (gateway/alert_text.py). The token is
            # worked out from its name, so it is the same on every `up`.
            cur[at.TOKEN_ENV] = at.token_for(agent["name"], self.env)
            cur[at.URL_ENV] = self.alert_url()
            cur["LOGOS_AGENT_NAME"] = agent["name"]
        else:
            for k in (at.TOKEN_ENV, at.URL_ENV, "LOGOS_AGENT_NAME"):
                cur.pop(k, None)
        ss = _sibling("speaker_say")
        if ss.speaks(agent):
            # Voice V3.1: it may speak up on the house speaker (gateway/speaker_say.py). Worked out from
            # its name, like the alert token, so it is the same on every `up`.
            cur[ss.TOKEN_ENV] = ss.token_for(agent["name"], key=at.key(self.env))
            cur[ss.URL_ENV] = self.speaker_say_url()
            cur["LOGOS_AGENT_NAME"] = agent["name"]
        else:
            cur.pop(ss.TOKEN_ENV, None)
            cur.pop(ss.URL_ENV, None)
        rn = _sibling("agent_rename")
        if rn.uses_rename(agent):
            # It may ask for a new name; its person confirms in Logos (gateway/agent_rename.py).
            cur[rn.TOKEN_ENV] = rn.token_for(agent["name"], key=at.key(self.env))
            cur[rn.URL_ENV] = self.rename_url()
            cur["LOGOS_AGENT_NAME"] = agent["name"]
        else:
            cur.pop(rn.TOKEN_ENV, None)
            cur.pop(rn.URL_ENV, None)
        cur["TZ"] = cur["HERMES_TIMEZONE"] = agent.get("timezone", "UTC")
        cur.setdefault("API_SERVER_KEY", secrets.token_urlsafe(32))
        mg = _sibling("model_gateway")
        if mg.uses_gateway(agent):
            cur.setdefault(mg.KEY_ENV, "sk-bf-" + secrets.token_urlsafe(32))
            cur["LOGOS_MODEL_HEALTH_URL"] = mg.ModelGateway(self).health_url()   # D2: is LM Studio back?
        else:
            cur.pop("LOGOS_MODEL_HEALTH_URL", None)
        nb = _sibling("noticeboard_host")
        if nb.uses_noticeboard(agent):
            cur.setdefault(nb.TOKEN_ENV, secrets.token_urlsafe(32))
            cur["LOGOS_NOTICEBOARD_URL"] = nb.NoticeboardHost(self).url()
            cur["LOGOS_NOTICEBOARD_POLL_URL"] = nb.NoticeboardHost(self).poll_url()
        vh = _sibling("voice_host")
        if vh.uses_voice(agent):
            cur.setdefault(vh.TOKEN_ENV, secrets.token_urlsafe(32))
        else:
            cur.pop(vh.TOKEN_ENV, None)
        ch = _sibling("calendar_host")
        if ch.uses_calendar(agent):
            cur.setdefault(ch.TOKEN_ENV, secrets.token_urlsafe(32))
            cur["LOGOS_CALENDAR_URL"] = ch.CalendarHost(self).service_url()     # for the briefing script
        else:
            cur.pop(ch.TOKEN_ENV, None)
            cur.pop("LOGOS_CALENDAR_URL", None)
        hh = _sibling("house_host")
        if hh.uses_house(agent):
            cur.setdefault(hh.TOKEN_ENV, secrets.token_urlsafe(32))
            cur["LOGOS_HOUSE_URL"] = hh.HouseHost(self).url()          # for the lists copy (lists_mirror.py)
        else:
            cur.pop(hh.TOKEN_ENV, None)
            cur.pop("LOGOS_HOUSE_URL", None)
        ah = _sibling("advisor_host")
        if ah.uses_advisor(agent):
            cur.setdefault(ah.TOKEN_ENV, secrets.token_urlsafe(32))
        else:
            cur.pop(ah.TOKEN_ENV, None)
        mh = _sibling("mail_host")
        if mh.uses_mail(agent):
            cur.setdefault(mh.TOKEN_ENV, secrets.token_urlsafe(32))
            cur[mh.URL_ENV] = mh.MailHost(self).url()                  # for the morning briefing's line
        else:
            cur.pop(mh.TOKEN_ENV, None)
            cur.pop(mh.URL_ENV, None)
        wh = _sibling("web_host")
        if wh.uses_web(agent):
            cur.setdefault(wh.TOKEN_ENV, secrets.token_urlsafe(32))
            cur[wh.WEATHER_ENV] = wh.WebHost(self).weather_url()
        else:
            cur.pop(wh.TOKEN_ENV, None)
            cur.pop(wh.WEATHER_ENV, None)
        if wh.uses_changes(agent):         # story DC1: a token that opens only /changes on the web reader
            cur.setdefault(wh.CHANGES_TOKEN_ENV, secrets.token_urlsafe(32))
            cur[wh.CHANGES_ENV] = wh.WebHost(self).changes_url()
        else:
            cur.pop(wh.CHANGES_TOKEN_ENV, None)
            cur.pop(wh.CHANGES_ENV, None)
        cur["GATEWAY_ALLOW_ALL_USERS"] = "false"
        return "".join(f"{k}={v}\n" for k, v in sorted(cur.items()))

    # ── inside the sandbox ───────────────────────────────────────────────

    def exec_argv(self, name: str, rest: Sequence[str]) -> list[str]:
        return ["openshell", "sandbox", "exec", "--name", name, "--no-tty", "--", *rest]

    def sbx(self, name: str, script: str, input: Optional[str] = None) -> str:
        p = self.runner.run(self.exec_argv(name, ["sh", "-c", script]), input=input)
        return ANSI.sub("", p.stdout + p.stderr)

    def exec(self, name: str, rest: Sequence[str]) -> int:
        """Run a command inside the sandbox on this process's stdio."""
        return self.runner.call(self.exec_argv(name, rest))

    def wait_api(self, name: str, tries: int = 60) -> bool:
        for _ in range(tries):
            if self.sbx(name, "curl -s -o /dev/null -w '%{http_code}' -m 3 "
                              "http://127.0.0.1:8642/health").strip().endswith("200"):
                return True
            self.runner.sleep(3)
        return False

    def job_ids(self, name: str) -> dict[str, str]:
        """Map cron job name -> id from `hermes cron list`."""
        ids, last = {}, None
        for line in self.sbx(name, f"{HERMES} cron list").splitlines():
            m = re.match(r"\s+([0-9a-f]{8,})\s+\[", line)
            if m:
                last = m.group(1)
            m = re.match(r"\s+Name:\s+(\S+)", line)
            if m and last:
                ids[m.group(1)] = last
        return ids

    def install_digest(self, name: str, agent: Mapping[str, Any], tz: str) -> None:
        """Sunday evening: what the brain learned this week, sent to the person (a routine)."""
        digest = agent.get("digest") or {}
        if digest.get("enabled", True) and delivery(agent) != "local":
            # Sunday evening: what the brain learned this week, sent to the person.
            self.sbx(name, f'{HERMES} cron create --name weekly-digest --workdir /opt/data/brain '
                           f'--timezone {tz} --deliver {delivery(agent)} --failure-deliver local '
                           f'"{digest.get("schedule", "0 18 * * 0")}" "$(cat)"',
                     input=digest_prompt(self.jobs_dir, agent))

    def install_briefing(self, name: str, agent: Mapping[str, Any], tz: str) -> None:
        """The morning briefing and its fallback check (a routine)."""
        brief = briefing(agent)
        if brief:
            # Story C4: the day ahead, sent in the morning. The script puts today's weather and events
            # into the prompt; a morning missed while the machine was off is skipped, never sent late.
            prompt = briefing_prompt(self.jobs_dir, agent)
            for job, when in (("morning-briefing", brief["weekdays"]), ("morning-briefing-weekend", brief["weekends"])):
                self.sbx(name, f'{HERMES} cron create --name {job} --script briefing_today.py '
                               f'--workdir /opt/data/brain --timezone {tz} --catch-up off '
                               f'--deliver {delivery(agent)} --failure-deliver local "{when}" "$(cat)"',
                         input=prompt)
                # A quarter of an hour later, no model: if the briefing did not go out (the model
                # server busy or off), send the plain facts so a morning is never missed.
                check = minutes_later(when)
                if check:
                    self.sbx(name, f'{HERMES} cron create --name {job}-check --no-agent --script briefing_check.py '
                                   f'--timezone {tz} --catch-up off --deliver {delivery(agent)} '
                                   f'--failure-deliver local "{check}"')

    def install_changes(self, name: str, agent: Mapping[str, Any], tz: str) -> None:
        """What changed today and this week, in the group chat (a routine)."""
        for job, script, when in changes_jobs(agent):
            # Story DC2: what changed in the homelab and in Logos, in the group chat. The script puts
            # the list into the prompt, or skips the model when nothing changed; changes-check sends
            # the plain list for any that did not go out. Missed while the machine was off: skipped.
            if job == "changes-check":
                self.sbx(name, f'{HERMES} cron create --name {job} --no-agent --script {script} '
                               f'--timezone {tz} --catch-up off --deliver {delivery(agent)} '
                               f'--failure-deliver local "{when}"')
            else:
                self.sbx(name, f'{HERMES} cron create --name {job} --script {script} --workdir /opt/data/brain '
                               f'--timezone {tz} --catch-up off --deliver {delivery(agent)} '
                               f'--failure-deliver local "{when}" "$(cat)"',
                         input=logos_job(self.jobs_dir / "changes_summary.md"))

    def refresh_routines(self, name: str) -> list:
        """Put the routine jobs back as the agent's definition and people's choices now say, without
        touching anything else (the dashboard calls this after a change). Returns the job names."""
        agent = self.load(name)
        tz = agent.get("timezone", "UTC")
        for job, jid in self.job_ids(name).items():
            if job in ROUTINE_JOBS:
                self.sbx(name, f"{HERMES} cron remove {jid}")
        self.install_briefing(name, agent, tz)
        self.install_digest(name, agent, tz)
        self.install_changes(name, agent, tz)
        return sorted(j for j in self.job_ids(name) if j in ROUTINE_JOBS)

    def ensure_jobs(self, name: str, agent: Mapping[str, Any], home: str) -> None:
        """Install the brain jobs: an hourly git commit (no model) and the nightly consolidation."""
        for script in ("brain_commit.sh", "brain_inbox_state.sh", "brain_index.py", "noticeboard_state.py",
                       "noticeboard_urgent.py", "briefing_today.py", "briefing_check.py",
                       "carry_monitor_state.py", "rerun_after_outage.py", "model_watch.py",
                       "changes_facts.py", "changes_check.py", "lists_mirror.py", "vault_digest.py"):
            self.put(home, f"scripts/{script}", (self.jobs_dir / script).read_text(), mode="0750")
        for script in CHANGES_SCRIPTS:
            self.put(home, f"scripts/{script}", (self.jobs_dir / "changes_facts.py").read_text(), mode="0750")
        if not self.wait_api(name):
            print("warning: hermes API did not come up; brain jobs not installed", file=sys.stderr)
            return
        # Keep what the monitor jobs last saw, or each re-created job wakes the model once for nothing.
        self.sbx(name, f"{HERMES_PY} save")
        for jid in [i for n, i in self.job_ids(name).items() if n in MANAGED_JOBS]:
            self.sbx(name, f"{HERMES} cron remove {jid}")
        tz = agent.get("timezone", "UTC")
        con = agent.get("consolidation") or {}
        self.sbx(name, f'{HERMES} cron create --name brain-commit --no-agent --script brain_commit.sh '
                       f'--timezone {tz} --deliver local "0 * * * *"')
        if agent.get("brain_index", True):
            # Script only: refreshes the page list in built-in memory. New conversations pick it up.
            self.sbx(name, f'{HERMES} cron create --name brain-index --no-agent --script brain_index.py '
                           f'--timezone {tz} --deliver local "50 * * * *"')
            self.sbx(name, f"export HERMES_HOME=/opt/data LOGOS_MEMORY_CHAR_LIMIT={MEMORY_CHAR_LIMIT}; "
                           "python /opt/data/scripts/brain_index.py")
        if agent.get("vault", True):
            # Script only: a note in the brain for each file kept in the vault (F47 phase 1). Behind people's chats.
            self.sbx(name, f'{HERMES} cron create --name vault-digest --no-agent --script vault_digest.py '
                           f'--timezone {tz} --deliver local "*/10 * * * *"')
        if con.get("enabled", True):
            model = ""
            if con.get("model"):  # omitted: the job follows the agent's main model
                model = f'--model {con["model"]} --provider {con.get("provider", agent["model"]["provider"])} '
            self.sbx(name, f'{HERMES} cron create --name consolidate --monitor-script brain_inbox_state.sh '
                           f'--workdir /opt/data/brain --timezone {tz} --continuity --deliver local {model}'
                           f'"{con.get("schedule", "0 3 * * *")}" "$(cat)"',
                     input=logos_job(self.jobs_dir / "consolidate.md"))
        self.install_digest(name, agent, tz)
        if _sibling("noticeboard_host").uses_noticeboard(agent):
            # Every ten minutes a script asks the noticeboard whether anything new is waiting. The
            # model runs only when it is, and what it reports goes to the person (or stays silent).
            deliver = delivery(agent)
            self.sbx(name, f'{HERMES} cron create --name noticeboard-check '
                           f'--monitor-script noticeboard_state.py --timezone {tz} '
                           f'--deliver {deliver} --failure-deliver local "*/10 * * * *" "$(cat)"',
                     input=logos_job(self.jobs_dir / "noticeboard_check.md"))
            # Every minute, no model: an urgent message is shown to the person at once.
            self.sbx(name, f'{HERMES} cron create --name noticeboard-urgent --no-agent '
                           f'--script noticeboard_urgent.py --timezone {tz} --deliver {deliver} '
                           f'--failure-deliver local "* * * * *"')
        self.install_briefing(name, agent, tz)
        if _sibling("model_gateway").uses_gateway(agent):
            # Story D2: a digest or brain tidy that failed while LM Studio was off runs again when it is back.
            self.sbx(name, f'{HERMES} cron create --name rerun-after-outage --no-agent '
                           f'--script rerun_after_outage.py --timezone {tz} --deliver local "*/5 * * * *"')
            if agent.get("role") == "household" and delivery(agent) != "local":
                # The group chat hears when LM Studio is offline (over two minutes) and when it is back.
                self.sbx(name, f'{HERMES} cron create --name model-watch --no-agent --script model_watch.py '
                               f'--timezone {tz} --deliver {delivery(agent)} --failure-deliver local "* * * * *"')
        if agent.get("role") == "household" and uses_lists(agent):
            # The shared lists live in Home Assistant; a copy goes in the household brain for Obsidian.
            self.sbx(name, f'{HERMES} cron create --name lists-mirror --no-agent --script lists_mirror.py '
                           f'--timezone {tz} --deliver local "*/15 * * * *"')
        self.install_changes(name, agent, tz)
        self.sbx(name, f"{HERMES_PY} restore")
        have = self.job_ids(name)
        print("jobs: " + ", ".join(sorted(have)) if have else "warning: no cron jobs registered")

    # ── sandbox lifecycle ────────────────────────────────────────────────

    def phases(self) -> dict[str, str]:
        """Sandbox name -> phase for everything OpenShell knows about."""
        return parse_phases(self.run(["openshell", "sandbox", "list"], check=False))

    def phase(self, name: str) -> str:
        return self.phases().get(name, MISSING)

    def revive(self, wait_s: float = 600) -> list:
        """After this machine restarts, OpenShell leaves the sandboxes it had in Error, and they
        stayed down until someone ran `up` (the dev agent from 22 Nov's restart until 23 Nov).
        Waits for OpenShell to answer, then recreates each agent whose sandbox is in Error.
        An agent with no sandbox was taken down on purpose and stays down. Returns the names."""
        for _ in range(max(1, int(wait_s // 10))):
            out = self.run(["openshell", "sandbox", "list"], check=False)
            phases = parse_phases(out)
            if phases.get("NAME") == "PHASE" or "no sandboxes" in (out or "").lower():
                break                       # its list, not an error saying it is not up yet
            self.runner.sleep(10)
        else:
            print("revive: OpenShell did not answer; nothing done")
            return []
        revived = []
        for name in self.defined():
            if self.load(name).get("sandbox") is False or phases.get(name) != "Error":
                continue
            print(f"revive: {name} was in Error after a restart; recreating it", flush=True)
            try:
                if self.up(name) == 0:
                    revived.append(name)
            except AgentRuntimeError as e:
                print(f"revive: {name}: {e}", flush=True)
        return revived

    def policy_path(self, name: str, agent: Mapping[str, Any]) -> Path:
        """The policy file to create the sandbox with.

        The tracked policy carries documentation addresses. Two rules are filled in per agent,
        in an untracked copy under ``.rendered/``, so real addresses never need committing:

        * ``lm_studio``: the agent's model endpoint. That is the model gateway when the agent
          uses it, otherwise ``model.base_url`` (usually set in the untracked ``.local.yaml``).
        * ``noticeboard``: the noticeboard's address, or removed when the agent is not on it.
        * ``voice``: the voice service's address, or removed when the agent has no voice.
        * ``calendar``: the calendar service's address, or removed when the agent has no calendar.
        * ``house``: the house service's address, or removed unless the agent has ``house`` on.
          Only the tools for its domains are let through.
        * ``models_catalogue``: the dashboard's copy of the models.dev catalogue, GET on that path only.
        * ``logos_net_*``: sites the agent's people always allowed (story E6, docs/network-approvals.md),
          from ``~/.config/logos/network-approvals.json``.
        """
        src = self.repo / agent["policy"]
        if not src.exists():
            return src
        policy = yaml.safe_load(src.read_text()) or {}
        rules = policy.get("network_policies") or {}
        before = json.dumps(rules, sort_keys=True)

        def point(rule: str, target: str, rest_rules: Optional[list] = None) -> None:
            url = urllib.parse.urlsplit(target)
            host, port = url.hostname, url.port or (443 if url.scheme == "https" else 80)
            for e in (rules.get(rule) or {}).get("endpoints") or []:
                e["host"], e["port"] = host, port
                if rest_rules is not None:      # exact methods and paths instead of a preset
                    e.pop("access", None)
                    e["rules"] = [{"allow": {"method": m, "path": p}} for m, p in rest_rules]
                try:
                    e["allowed_ips"] = [f"{ipaddress.ip_address(host)}/32"]
                except ValueError:
                    e.pop("allowed_ips", None)      # a hostname: nothing to pin

        model = agent.get("model") or {}
        mg = _sibling("model_gateway")
        if mg.uses_gateway(agent):
            # Behind the model gateway the agent talks only to the gateway, never to LM Studio itself.
            # Only the inference API: the gateway's admin and UI paths stay out of reach.
            point("lm_studio", mg.ModelGateway(self).base_url(),
                  rest_rules=[("GET", "/v1/**"), ("POST", "/v1/**")])
            point("model_health", mg.ModelGateway(self).health_url(), rest_rules=[("GET", "/guard/health")])
        elif model.get("provider") == "lmstudio":
            point("lm_studio", model["base_url"])
        if not mg.uses_gateway(agent):
            rules.pop("model_health", None)
        nb = _sibling("noticeboard_host")
        if nb.uses_noticeboard(agent):
            point("noticeboard", nb.NoticeboardHost(self).url())
            point("noticeboard_poll", nb.NoticeboardHost(self).poll_url())
        else:
            rules.pop("noticeboard", None)
            rules.pop("noticeboard_poll", None)
        mh = _sibling("matrix_host")
        if mh.uses_matrix(agent):
            point("matrix", mh.MatrixHost(self).url())
            if "alerts" in rules:          # the reply's words for the phone alert: that one path only
                point("alerts", self.alert_url(), rest_rules=[("POST", _sibling("alert_text").PATH)])
        else:
            rules.pop("matrix", None)
            rules.pop("alerts", None)
        ss = _sibling("speaker_say")
        if ss.speaks(agent) and "speaker_say" in rules:   # speaking up on the house speaker: that one path only
            point("speaker_say", self.speaker_say_url(), rest_rules=[("POST", ss.PATH)])
        else:
            rules.pop("speaker_say", None)
        rn = _sibling("agent_rename")
        if rn.uses_rename(agent) and "agent_rename" in rules:   # asking for a new name: that one path only
            point("agent_rename", self.rename_url(), rest_rules=[("POST", rn.PATH)])
        else:
            rules.pop("agent_rename", None)
        if "models_catalogue" in rules:    # hermes's model catalogue, from Logos's copy: that one path only
            point("models_catalogue", self.models_catalogue_url(), rest_rules=[("GET", _sibling("models_catalogue").PATH)])
        vh = _sibling("voice_host")
        if vh.uses_voice(agent):
            point("voice", vh.VoiceHost(self).base_url(), rest_rules=[("POST", "/v1/audio/transcriptions")])
        else:
            rules.pop("voice", None)
        ch = _sibling("calendar_host")
        if ch.uses_calendar(agent):
            point("calendar", ch.CalendarHost(self).service_url())
        else:
            rules.pop("calendar", None)
        hh = _sibling("house_host")
        if hh.uses_house(agent) and "house" in rules:
            point("house", hh.HouseHost(self).url())
            domains = hh.agent_settings(agent)["domains"]
            tools = ([t for t in ("list_lights", "set_lights") if "light" in domains]
                     + [t for t in ("list_scenes", "activate_scene") if "scene" in domains]
                     + [t for t in ("list_items", "add_items", "change_item", "tick_items", "remove_items", "create_list") if "todo" in domains])
            for e in rules["house"].get("endpoints") or []:
                e["rules"] = [{"allow": {"method": m}} for m in
                              ("initialize", "notifications/initialized", "ping", "tools/list")] + [
                    {"allow": {"method": "tools/call", "tool": {"any": tools}}}]
        else:
            rules.pop("house", None)
        ah = _sibling("advisor_host")
        if ah.uses_advisor(agent) and "advisor" in rules:
            point("advisor", ah.AdvisorHost(self).url())
            for e in rules["advisor"].get("endpoints") or []:
                e["rules"] = [{"allow": {"method": m}} for m in
                              ("initialize", "notifications/initialized", "ping", "tools/list")] + [
                    {"allow": {"method": "tools/call", "tool": {"any": list(ADVISOR_TOOLS)}}}]
        else:
            rules.pop("advisor", None)
        mh = _sibling("mail_host")
        if mh.uses_mail(agent) and "mail" in rules:
            point("mail", mh.MailHost(self).url())
            for e in rules["mail"].get("endpoints") or []:
                e["rules"] = [{"allow": {"method": m}} for m in
                              ("initialize", "notifications/initialized", "ping", "tools/list")] + [
                    {"allow": {"method": "tools/call", "tool": {"any": list(MAIL_TOOLS)}}}]
        else:
            rules.pop("mail", None)
        wh = _sibling("web_host")
        if wh.uses_web(agent):
            point("web", wh.WebHost(self).url())
            point("weather", wh.WebHost(self).weather_url(), rest_rules=[("GET", "/weather")])
        elif wh.uses_changes(agent) and "web" in rules:
            # Stories FR1, DC1, DM1: the household agent may file requests, read the changes and read the
            # homelab docs on the reader, nothing else.
            # The whole rule list is written here, so nothing else on the reader is ever open to it.
            point("web", wh.WebHost(self).url())
            for e in rules["web"].get("endpoints") or []:
                e.pop("access", None)
                e["rules"] = [{"allow": {"method": m}} for m in
                              ("initialize", "notifications/initialized", "ping", "tools/list")] + [
                    {"allow": {"method": "tools/call", "tool": {"any": HOUSEHOLD_READER_TOOLS}}}]
            rules.pop("weather", None)
        else:
            rules.pop("web", None)
            rules.pop("weather", None)
        if wh.uses_changes(agent):
            point("changes", wh.WebHost(self).changes_url(), rest_rules=[("GET", "/changes/**")])
        else:
            rules.pop("changes", None)
        na = _sibling("network_approvals")
        if na.enabled(agent):
            # E6: the sites the agent's people chose "Always allow" for (~/.config/logos, never in git).
            for rule, body in na.rules_for(self.env, name).items():
                rules.setdefault(rule, body)
            policy["network_policies"] = rules
        if json.dumps(rules, sort_keys=True) == before:
            return src
        out = self.repo / ".rendered" / f"{name}.policy.yaml"
        out.parent.mkdir(exist_ok=True)
        out.write_text(f"# Rendered from {agent['policy']} for agent {name}. Do not edit or commit.\n"
                       + yaml.safe_dump(policy, sort_keys=False))
        return out

    def create_argv(self, name: str, agent: Mapping[str, Any], placeholders: Sequence[str] = ()) -> list[str]:
        """The full ``openshell sandbox create`` command for ``agent``. ``placeholders``: the secrets
        (gateway/agent_secrets.py) whose provider is attached, so the sandbox sees a placeholder."""
        sec = _sibling("agent_secrets")
        cmd = ["openshell", "sandbox", "create", "--name", name, "--no-tty", "--detach",
               "--restart-policy", "always", "--from", agent["image"],
               "--policy", str(self.policy_path(name, agent)),
               "--driver-config-json", json.dumps(mounts_config(name))]
        for p in agent.get("providers") or []:
            cmd += ["--provider", p]
        for s in placeholders:
            cmd += ["--provider", sec.provider_name(name, s)]
        limits = {**DEFAULT_LIMITS, **(agent.get("resources") or {})}
        cmd += ["--cpu", str(limits["cpu"]), "--memory", str(limits["memory"])]
        return cmd + ["--", "sh", "-c", sec.START_HOOK + START]

    # ── E7: secrets the sandbox only sees as placeholders (gateway/agent_secrets.py) ──

    #: Which sandbox rules each secret is sent to (the endpoints its provider is bound to).
    SECRET_RULES = {"matrix": ("matrix",), "model": ("lm_studio",), "noticeboard": ("noticeboard", "noticeboard_poll"),
                    "voice": ("voice",), "calendar": ("calendar",), "web": ("web", "weather"),
                    "changes": ("changes",), "house": ("house",), "mail": ("mail",), "advisor": ("advisor",),
                    "alert": ("alerts",), "speaker_say": ("speaker_say",)}

    def secret_endpoints(self, name: str, agent: Mapping[str, Any]) -> dict:
        """``{secret: (endpoints, binaries)}`` from the agent's rendered sandbox policy: where each of
        its secrets may be sent. A secret whose rules the policy does not have is left out."""
        path = Path(self.policy_path(name, agent))
        policy = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        rules = policy.get("network_policies") or {}
        wh = _sibling("web_host")
        out = {}
        for secret, names in self.SECRET_RULES.items():
            names = list(names)
            if secret == "changes" and not wh.uses_web(agent):
                names.append("web")          # FR1: the household agent files requests with its changes token
            if secret == "model" and not _sibling("model_gateway").uses_gateway(agent):
                continue                     # no gateway, no key
            eps, bins = [], []
            for n in names:
                rule = rules.get(n) or {}
                eps += [e for e in rule.get("endpoints") or [] if e.get("host")]
                bins += [b["path"] if isinstance(b, Mapping) else str(b) for b in rule.get("binaries") or []]
            if eps:
                out[secret] = (eps, sorted(set(bins)))
        return out

    def split_secrets(self, name: str, agent: Mapping[str, Any], env_text: str) -> tuple:
        """``(sandbox_env_text, host_values, placeholders)``: the agent's full .env split into what its
        home volume gets and what only the host keeps (``~/.config/logos/agent-secrets/<name>.env``).
        ``placeholders`` lists the secrets the sandbox gets as an OpenShell placeholder instead."""
        sec = _sibling("agent_secrets")
        full = sec.parse(env_text)
        host = {k: full.pop(k) for k in sec.HOST_ONLY if k in full}
        endpoints = self.secret_endpoints(name, agent)
        on = []
        for secret, key in sec.SECRETS.items():
            if full.get(key) and secret in endpoints and sec.enabled(agent, secret):
                host[key] = full.pop(key)
                on.append(secret)
        return sec.render(full), host, on

    def _openshell_secret(self, argv: Sequence[str], key: str, value: str):
        """Run an ``openshell provider`` command that reads ``key`` from its environment."""
        run_env = getattr(self.runner, "run_env", None)
        if run_env is None:
            raise AgentRuntimeError("this runner cannot pass a secret through the environment")
        p = run_env(list(argv), {key: value})
        if p.returncode != 0:
            # The CLI never prints credential values; show its message, never the value.
            raise AgentRuntimeError(f"command failed: {' '.join(argv[:4])} ...\n"
                                    + ANSI.sub("", _text(p.stderr) or _text(p.stdout))[-400:].replace(value, "<secret>"))
        return p

    def providers_for(self, name: str) -> list:
        """The names of the E7 providers OpenShell holds for agent ``name``."""
        prefix = _sibling("agent_secrets").provider_name(name, "")
        out = self.run(["openshell", "provider", "list"], check=False)
        names = [l.split()[0] for l in out.splitlines()[1:] if l.split()]
        return [n for n in names if n.startswith(prefix) and n[len(prefix):] in
                {s.replace("_", "-") for s in _sibling("agent_secrets").SECRETS}]

    def sync_providers(self, name: str, agent: Mapping[str, Any], host: Mapping[str, str],
                       placeholders: Sequence[str]) -> None:
        """Create or update the profile and provider of each placeholder secret (before the sandbox
        is created: OpenShell refuses an unknown provider)."""
        sec = _sibling("agent_secrets")
        endpoints = self.secret_endpoints(name, agent)
        existing = set(self.providers_for(name))
        folder = self.repo / ".rendered" / f"{name}.providers"
        folder.mkdir(parents=True, exist_ok=True)
        for secret in placeholders:
            eps, bins = endpoints[secret]
            prof = sec.profile(name, secret, eps, bins)
            pid, path = prof["id"], folder / f"{secret}.yaml"
            current = self.runner.run(["openshell", "profile", "export", pid])
            if current.returncode == 0:
                prof["resource_version"] = (yaml.safe_load(_text(current.stdout)) or {}).get("resource_version", 1)
            path.write_text(f"# Rendered for agent {name} (E7). No secrets; do not edit or commit.\n"
                            + yaml.safe_dump(prof, sort_keys=False))
            if current.returncode == 0:
                self.run(["openshell", "profile", "update", "-f", str(path), pid])
            else:
                self.run(["openshell", "profile", "import", "-f", str(path)])
            key = sec.SECRETS[secret]
            pname = sec.provider_name(name, secret)
            if pname in existing:
                self._openshell_secret(["openshell", "provider", "update", pname, "--credential", key], key, host[key])
            else:
                self._openshell_secret(["openshell", "provider", "create", "--name", pname, "--type", pid,
                                        "--credential", key], key, host[key])

    def drop_providers(self, name: str, keep: Sequence[str] = ()) -> list:
        """Delete the E7 providers (and their profiles) of agent ``name`` not in ``keep``. Call it
        with the sandbox gone: OpenShell will not delete a provider a sandbox still uses."""
        sec = _sibling("agent_secrets")
        keep_names = {sec.provider_name(name, s) for s in keep}
        dropped = []
        for pname in self.providers_for(name):
            if pname not in keep_names:
                self.run(["openshell", "provider", "delete", pname], check=False)
                self.run(["openshell", "profile", "delete", pname], check=False)
                dropped.append(pname)
        return dropped

    def down(self, name: str) -> None:
        """Delete the sandbox (volumes are kept) and wait for it to go."""
        if self.phase(name) != MISSING:
            self.run(["openshell", "sandbox", "delete", name])
            for _ in range(30):
                if self.phase(name) == MISSING:
                    break
                self.runner.sleep(1)

    def up(self, name: str, load_model: bool = True) -> int:
        """Create or recreate the sandbox from its definition. 0 when it reaches Ready.
        ``load_model=False`` (``--no-model-load``) leaves LM Studio alone: the model guard loads the
        model at its window when the agent next asks for it."""
        agent = self.load(name)
        try:
            _sibling("model_gateway").require_local_only(name, agent)
        except ValueError as e:
            raise AgentRuntimeError(str(e))
        if (agent.get("telegram") or {}).get("enabled"):
            raise AgentRuntimeError(f"{name}: Telegram is no longer supported; people talk to agents "
                                    "on Matrix. Remove the telegram section from the definition.")
        home, brain = volumes(name)
        for v in (home, brain):
            self.ensure_volume(v)
        env_text = self.env_file(agent, home)
        # E7: the secrets switched to placeholders, and the Matrix password, stay on the host; the
        # sandbox's .env gets the rest (and, at start, the placeholders: agent_secrets.START_HOOK).
        sec = _sibling("agent_secrets")
        sandbox_env, host_secrets, placeholders = self.split_secrets(name, agent, env_text)
        sec.save(self.env, name, host_secrets)
        self.put(home, ".env", sandbox_env, mode="0600")
        full = sec.parse(env_text)

        def tok(key: str) -> str:
            """What config.yaml says for a token: a ${VAR} reference to the placeholder, or the value."""
            if any(sec.SECRETS[s] == key for s in placeholders):
                return sec.ref(key)
            return full.get(key, "")

        nb = _sibling("noticeboard_host")
        vh = _sibling("voice_host")
        ch = _sibling("calendar_host")
        wh = _sibling("web_host")
        hh = _sibling("house_host")
        mh = _sibling("mail_host")
        ah = _sibling("advisor_host")
        self.put(home, "config.yaml", self.config_text(
            name, agent, tok(nb.TOKEN_ENV), tok(vh.TOKEN_ENV), tok(ch.TOKEN_ENV), tok(wh.TOKEN_ENV),
            tok(wh.CHANGES_TOKEN_ENV), tok(hh.TOKEN_ENV), tok(mh.TOKEN_ENV), tok(ah.TOKEN_ENV)), mode="0600")
        for plugin in LOGOS_PLUGINS:
            for path in sorted((self.repo / "infra" / "agent-plugins" / plugin).iterdir()):
                if path.is_file():
                    self.put(home, f"plugins/{plugin}/{path.name}", path.read_text())
        self.ensure_vault(brain)
        if load_model:
            self.ensure_model(agent["model"])
        else:
            print("model: not checked or loaded (--no-model-load)")
        soul = self.soul_text(agent)
        if len(soul) > SOUL_MAX_CHARS:
            raise AgentRuntimeError(f"SOUL.md is {len(soul)} characters, over {SOUL_MAX_CHARS}: hermes would cut "
                                    "the middle of it. Shorten the rules or raise SOUL_MAX_CHARS.")
        self.put(home, "SOUL.md", soul)
        if nb.uses_noticeboard(agent):
            try:
                if nb.NoticeboardHost(self).up():
                    print("noticeboard: started with the current agents")
            except (ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"noticeboard: {e}")
        if vh.uses_voice(agent):
            try:
                if vh.VoiceHost(self).up():
                    print("voice: started with the current agents")
            except (ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"voice: {e}")
        th = _sibling("tts_host")
        if th.uses_speaker(agent):
            # The house speaker reads this agent's answers in its own voice (docs/voice.md).
            try:
                started = th.TtsHost(self).up()
                if started is None:
                    print("speaker voices: off (no ~/.config/logos/speaker, see docs/voice.md)")
                elif started:
                    print("speaker voices: started")
            except (th.TtsError, TimeoutError) as e:
                raise AgentRuntimeError(f"speaker voices: {e}")
            # And hears it: speech to text for Home Assistant, with who said it (VO.1; off without STT_PORT).
            sh = _sibling("stt_host")
            try:
                if sh.SttHost(self).up():
                    print("speech to text: started")
            except (sh.SttError, th.TtsError, TimeoutError) as e:
                raise AgentRuntimeError(f"speech to text: {e}")
        wk = _sibling("wake_host")
        if th.uses_speaker(agent) or wk.uses_wake(agent):
            # The house speaker wakes to the agents' names (docs/voice.md); names.json follows the yaml.
            try:
                started = wk.WakeHost(self).up()
                if started:
                    print("wake words: started")
            except (wk.WakeError, TimeoutError) as e:
                raise AgentRuntimeError(f"wake words: {e}")
        if ch.uses_calendar(agent):
            try:
                if ch.CalendarHost(self).up():
                    print("calendar: started with the current agents")
            except (ch.CalendarError, ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"calendar: {e}")
        if hh.uses_house(agent):
            try:
                if hh.HouseHost(self).up():
                    print("house: started with the current agents")
            except (hh.HouseError, ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"house: {e}")
        if ah.uses_advisor(agent):
            try:
                if ah.AdvisorHost(self).up():
                    print("advisor: started with the current agents")
            except (ah.AdvisorError, ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"advisor: {e}")
        if mh.uses_mail(agent):
            try:
                if mh.MailHost(self).up():
                    print("mail: started with the connected mailboxes")
            except (mh.MailError, ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"mail: {e}")
        if wh.uses_web(agent) or wh.uses_changes(agent):     # DC1: the household's /changes token too
            try:
                if wh.WebHost(self).up():
                    print("web reader: started with the current agents")
            except (wh.WebError, ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"web reader: {e}")
        mg = _sibling("model_gateway")
        if mg.uses_gateway(agent):
            try:
                if mg.ModelGateway(self).up():
                    print("model gateway: started with the current agents and budgets")
            except (ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"model gateway: {e}")
        if placeholders:
            self.sync_providers(name, agent, host_secrets, placeholders)
            print("secrets: placeholders for " + ", ".join(placeholders))
        self.down(name)
        self.drop_providers(name, keep=placeholders)
        self.run(self.create_argv(name, agent, placeholders))
        for _ in range(60):
            ph = self.phase(name)
            if ph in ("Ready", "Error"):
                break
            self.runner.sleep(2)
        print(f"{name}: {ph}")
        if ph == "Ready":
            self.ensure_jobs(name, agent, home)
            _sibling("agent_settings").note_applied(self.env, name)     # E6: "saved, not in use yet" ends
        return 0 if ph == "Ready" else 1


def _text(data: Data) -> str:
    return data.decode(errors="replace") if isinstance(data, bytes) else (data or "")
