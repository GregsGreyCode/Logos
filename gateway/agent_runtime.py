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
MANAGED_JOBS = ("brain-commit", "brain-index", "consolidate", "noticeboard-check", "noticeboard-urgent",
                "weekly-digest")
HOME_TARGET = "/opt/data"
BRAIN_TARGET = "/opt/data/brain"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
MISSING = "Missing"
# Sessions opened with `openshell sandbox exec` do not inherit the image ENV, so hermes CLI calls
# must be told where the agent's home is.
HERMES = "export HERMES_HOME=/opt/data; hermes"
# _config_version marks the file as current for the pinned hermes (v0.23.1 = 53); without it
# hermes treats the file as pre-v12 and skips its config migrations. Bump with the image.
CONFIG_VERSION = 53

Data = Union[str, bytes]


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
    spec.loader.exec_module(mod)
    return mod


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

    def call(self, argv: Sequence[str]) -> int:
        """Run ``argv`` attached to this process's stdio; return its exit code."""
        ...

    def http(self, url: str, *, data: Optional[bytes] = None,
             headers: Optional[Mapping[str, str]] = None, timeout: float = 10) -> bytes:
        """GET ``url`` (POST when ``data`` is given) and return the body."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SubprocessRunner:
    """Real :class:`Runner`: subprocess, urllib and time.sleep."""

    def run(self, argv, *, input=None, text=True):
        p = subprocess.run(list(argv), input=input, text=text, capture_output=True)
        return Completed(p.returncode, p.stdout, p.stderr)

    def call(self, argv):
        return subprocess.call(list(argv))

    def http(self, url, *, data=None, headers=None, timeout=10):
        req = urllib.request.Request(url, data=data, headers=dict(headers or {}))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def sleep(self, seconds):
        time.sleep(seconds)


def merge(base: dict, extra: Mapping[str, Any]) -> None:
    """Deep-merge ``extra`` into ``base``: nested mappings merge, anything else replaces."""
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v


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

    # ── definitions ──────────────────────────────────────────────────────

    def definition_path(self, name: str) -> Path:
        return self.repo / "agents" / f"{name}.yaml"

    def defined(self) -> list[str]:
        """Names of every agent with a definition in ``agents/``."""
        return sorted(p.stem for p in (self.repo / "agents").glob("*.yaml")
                      if not p.stem.endswith(".local"))

    def load(self, name: str) -> dict:
        path = self.definition_path(name)
        if not path.exists():
            raise AgentRuntimeError(f"no agent definition at {path}")
        agent = yaml.safe_load(path.read_text())
        # agents/<name>.local.yaml is untracked: personal values (Telegram user IDs) stay out of git.
        local = path.with_suffix(".local.yaml")
        if local.exists():
            merge(agent, yaml.safe_load(local.read_text()) or {})
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
            if lm.ensure(self.runner.http, lm.server(model), model["default"], model["context_length"]):
                print(f"loaded {model['default']} in LM Studio (context {model['context_length']})")
        except Exception as e:
            print(f"warning: could not check or load the model in LM Studio: {e}", file=sys.stderr)

    def config_text(self, name: str, agent: Mapping[str, Any], noticeboard_token: str = "",
                    voice_token: str = "") -> str:
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
        cfg = {"_config_version": CONFIG_VERSION,
               "timezone": agent.get("timezone", "UTC"),
               "model": model}
        if (agent.get("telegram") or {}).get("enabled"):
            # No "gateway shutting down / back online" messages to the person on every rebuild.
            cfg["platforms"] = {"telegram": {"gateway_restart_notification": False}}
        if (agent.get("telegram") or {}).get("group_chats"):
            # One shared conversation per group (hermes otherwise keeps one per participant).
            cfg["group_sessions_per_user"] = False
        if agent.get("brain_index", True):
            cfg["memory"] = {"memory_char_limit": MEMORY_CHAR_LIMIT}
        reset = agent.get("session_reset")
        if reset and str(reset.get("mode", "none")) != "none":
            # hermes no longer resets conversations on a timer; this catalog plugin (baked into the
            # image) starts a fresh session on the first message after the daily boundary.
            cfg["plugins"] = {"enabled": [RESET_PLUGIN]}
            cfg["session_reset"] = {k: reset[k] for k in ("mode", "at_hour", "idle_minutes") if k in reset}
        nb = _sibling("noticeboard_host")
        if nb.uses_noticeboard(agent) and noticeboard_token:
            # The only channel to other agents: two tools, send_message and read_messages.
            cfg["mcp_servers"] = {"noticeboard": {
                "url": nb.NoticeboardHost(self).url(),
                "headers": {"Authorization": f"Bearer {noticeboard_token}"}}}
        vh = _sibling("voice_host")
        if vh.uses_voice(agent) and voice_token:
            # Voice notes are turned into text by the shared voice service (docs/voice.md).
            # hermes calls it as an OpenAI-style transcription endpoint.
            cfg["stt"] = {"enabled": True, "provider": "openai",
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
        text = ((self.repo / agent["soul"]).read_text().rstrip() + "\n\n"
                + (self.repo / "souls/_shared/brain.md").read_text())
        if _sibling("noticeboard_host").uses_noticeboard(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/noticeboard.md").read_text()
        if _sibling("voice_host").uses_voice(agent):
            text = text.rstrip() + "\n\n" + (self.repo / "souls/_shared/voice.md").read_text()
        return text

    def env_file(self, agent: Mapping[str, Any], home: str) -> str:
        """Merge managed keys into the agent's .env, keeping secrets already stored there."""
        cur = dict(l.split("=", 1) for l in self.get(home, ".env").splitlines()
                   if "=" in l and not l.startswith("#"))
        tg = agent.get("telegram") or {}
        if tg.get("enabled"):
            token = self.env.get("LOGOS_TELEGRAM_BOT_TOKEN") or cur.get("TELEGRAM_BOT_TOKEN")
            if not token:
                raise AgentRuntimeError(
                    "telegram.enabled is true but no token: set LOGOS_TELEGRAM_BOT_TOKEN for the first `up`")
            cur["TELEGRAM_BOT_TOKEN"] = token
            cur["TELEGRAM_ALLOWED_USERS"] = ",".join(str(u) for u in tg.get("allowed_users") or [])
            # Group chats: which groups the bot may answer in, and whether it must be @mentioned there.
            groups = [str(g) for g in tg.get("group_chats") or []]
            if groups:
                cur["TELEGRAM_GROUP_ALLOWED_CHATS"] = ",".join(groups)
            else:
                cur.pop("TELEGRAM_GROUP_ALLOWED_CHATS", None)
            cur["TELEGRAM_REQUIRE_MENTION"] = "true" if tg.get("require_mention", True) else "false"
            # Home channel: where scheduled jobs and failure notices are delivered. A person's DM chat
            # id is their user id, so default to the first allowed user unless the definition says otherwise.
            home_chat = (tg.get("home_channel") or next(iter(tg.get("group_chats") or []), None)
                         or next(iter(tg.get("allowed_users") or []), None))
            if home_chat:
                cur["TELEGRAM_HOME_CHANNEL"] = str(home_chat)
                cur.setdefault("TELEGRAM_HOME_CHANNEL_NAME", agent.get("display_name", agent["name"]))
        else:
            cur.pop("TELEGRAM_BOT_TOKEN", None)
            cur.pop("TELEGRAM_ALLOWED_USERS", None)
        cur["TZ"] = cur["HERMES_TIMEZONE"] = agent.get("timezone", "UTC")
        cur.setdefault("API_SERVER_KEY", secrets.token_urlsafe(32))
        mg = _sibling("model_gateway")
        if mg.uses_gateway(agent):
            cur.setdefault(mg.KEY_ENV, "sk-bf-" + secrets.token_urlsafe(32))
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

    def ensure_jobs(self, name: str, agent: Mapping[str, Any], home: str) -> None:
        """Install the brain jobs: an hourly git commit (no model) and the nightly consolidation."""
        for script in ("brain_commit.sh", "brain_inbox_state.sh", "brain_index.py", "noticeboard_state.py",
                       "noticeboard_urgent.py"):
            self.put(home, f"scripts/{script}", (self.jobs_dir / script).read_text(), mode="0750")
        if not self.wait_api(name):
            print("warning: hermes API did not come up; brain jobs not installed", file=sys.stderr)
            return
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
        if con.get("enabled", True):
            model = ""
            if con.get("model"):  # omitted: the job follows the agent's main model
                model = f'--model {con["model"]} --provider {con.get("provider", agent["model"]["provider"])} '
            self.sbx(name, f'{HERMES} cron create --name consolidate --monitor-script brain_inbox_state.sh '
                           f'--workdir /opt/data/brain --timezone {tz} --continuity --deliver local {model}'
                           f'"{con.get("schedule", "0 3 * * *")}" "$(cat)"',
                     input=(self.jobs_dir / "consolidate.md").read_text())
        digest = agent.get("digest") or {}
        if digest.get("enabled", True) and (agent.get("telegram") or {}).get("enabled"):
            # Sunday evening: what the brain learned this week, sent to the person.
            self.sbx(name, f'{HERMES} cron create --name weekly-digest --workdir /opt/data/brain '
                           f'--timezone {tz} --deliver telegram --failure-deliver local '
                           f'"{digest.get("schedule", "0 18 * * 0")}" "$(cat)"',
                     input=(self.jobs_dir / "weekly_digest.md").read_text())
        if _sibling("noticeboard_host").uses_noticeboard(agent):
            # Every ten minutes a script asks the noticeboard whether anything new is waiting. The
            # model runs only when it is, and what it reports goes to the person (or stays silent).
            deliver = "telegram" if (agent.get("telegram") or {}).get("enabled") else "local"
            self.sbx(name, f'{HERMES} cron create --name noticeboard-check '
                           f'--monitor-script noticeboard_state.py --timezone {tz} '
                           f'--deliver {deliver} --failure-deliver local "*/10 * * * *" "$(cat)"',
                     input=(self.jobs_dir / "noticeboard_check.md").read_text())
            # Every minute, no model: an urgent message is shown to the person at once.
            self.sbx(name, f'{HERMES} cron create --name noticeboard-urgent --no-agent '
                           f'--script noticeboard_urgent.py --timezone {tz} --deliver {deliver} '
                           f'--failure-deliver local "* * * * *"')
        have = self.job_ids(name)
        print("jobs: " + ", ".join(sorted(have)) if have else "warning: no cron jobs registered")

    # ── sandbox lifecycle ────────────────────────────────────────────────

    def phases(self) -> dict[str, str]:
        """Sandbox name -> phase for everything OpenShell knows about."""
        return parse_phases(self.run(["openshell", "sandbox", "list"], check=False))

    def phase(self, name: str) -> str:
        return self.phases().get(name, MISSING)

    def policy_path(self, name: str, agent: Mapping[str, Any]) -> Path:
        """The policy file to create the sandbox with.

        The tracked policy carries documentation addresses. Two rules are filled in per agent,
        in an untracked copy under ``.rendered/``, so real addresses never need committing:

        * ``lm_studio``: the agent's model endpoint. That is the model gateway when the agent
          uses it, otherwise ``model.base_url`` (usually set in the untracked ``.local.yaml``).
        * ``noticeboard``: the noticeboard's address, or removed when the agent is not on it.
        * ``voice``: the voice service's address, or removed when the agent has no voice.
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
        elif model.get("provider") == "lmstudio":
            point("lm_studio", model["base_url"])
        nb = _sibling("noticeboard_host")
        if nb.uses_noticeboard(agent):
            point("noticeboard", nb.NoticeboardHost(self).url())
            point("noticeboard_poll", nb.NoticeboardHost(self).poll_url())
        else:
            rules.pop("noticeboard", None)
            rules.pop("noticeboard_poll", None)
        vh = _sibling("voice_host")
        if vh.uses_voice(agent):
            point("voice", vh.VoiceHost(self).base_url(), rest_rules=[("POST", "/v1/audio/transcriptions")])
        else:
            rules.pop("voice", None)
        if json.dumps(rules, sort_keys=True) == before:
            return src
        out = self.repo / ".rendered" / f"{name}.policy.yaml"
        out.parent.mkdir(exist_ok=True)
        out.write_text(f"# Rendered from {agent['policy']} for agent {name}. Do not edit or commit.\n"
                       + yaml.safe_dump(policy, sort_keys=False))
        return out

    def create_argv(self, name: str, agent: Mapping[str, Any]) -> list[str]:
        """The full ``openshell sandbox create`` command for ``agent``."""
        cmd = ["openshell", "sandbox", "create", "--name", name, "--no-tty", "--detach",
               "--restart-policy", "always", "--from", agent["image"],
               "--policy", str(self.policy_path(name, agent)),
               "--driver-config-json", json.dumps(mounts_config(name))]
        for p in agent.get("providers") or []:
            cmd += ["--provider", p]
        return cmd + ["--", "sh", "-c", START]

    def down(self, name: str) -> None:
        """Delete the sandbox (volumes are kept) and wait for it to go."""
        if self.phase(name) != MISSING:
            self.run(["openshell", "sandbox", "delete", name])
            for _ in range(30):
                if self.phase(name) == MISSING:
                    break
                self.runner.sleep(1)

    def up(self, name: str) -> int:
        """Create or recreate the sandbox from its definition. 0 when it reaches Ready."""
        agent = self.load(name)
        home, brain = volumes(name)
        for v in (home, brain):
            self.ensure_volume(v)
        env_text = self.env_file(agent, home)
        self.put(home, ".env", env_text, mode="0600")
        nb = _sibling("noticeboard_host")
        token = next((l.split("=", 1)[1] for l in env_text.splitlines() if l.startswith(nb.TOKEN_ENV + "=")), "")
        vh = _sibling("voice_host")
        voice_token = next((l.split("=", 1)[1] for l in env_text.splitlines()
                            if l.startswith(vh.TOKEN_ENV + "=")), "")
        self.put(home, "config.yaml", self.config_text(name, agent, token, voice_token))
        self.ensure_vault(brain)
        self.ensure_model(agent["model"])
        self.put(home, "SOUL.md", self.soul_text(agent))
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
        mg = _sibling("model_gateway")
        if mg.uses_gateway(agent):
            try:
                if mg.ModelGateway(self).up():
                    print("model gateway: started with the current agents and budgets")
            except (ValueError, TimeoutError) as e:
                raise AgentRuntimeError(f"model gateway: {e}")
        self.down(name)
        self.run(self.create_argv(name, agent))
        for _ in range(60):
            ph = self.phase(name)
            if ph in ("Ready", "Error"):
                break
            self.runner.sleep(2)
        print(f"{name}: {ph}")
        if ph == "Ready":
            self.ensure_jobs(name, agent, home)
        return 0 if ph == "Ready" else 1


def _text(data: Data) -> str:
    return data.decode(errors="replace") if isinstance(data, bytes) else (data or "")
