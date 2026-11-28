"""Network approvals (story E6): a site an agent's sandbox refused becomes a question for its person.

How OpenShell 0.1.8 surfaces a blocked connection (docs/network-approvals.md has the evidence):

* The sandbox proxy denies it and logs an OCSF ``NET:OPEN DENIED`` line.
* The sandbox's denial aggregator (its "policy advisor", every 10 s) turns each denied
  ``(host, port, binary)`` into a *draft rule chunk* on the gateway, listed by ``openshell rule get``
  (pending / approved / rejected, with a hit count once it is denied again).
* A live sandbox's policy can be amended without recreating it: ``openshell policy update
  --add-endpoint`` / ``--remove-rule`` (``--wait`` until the sandbox has loaded it).

Logos reads the chunks, never approves them through OpenShell (``rule approve`` merges the proposed
rule as it stands, private-address overrides included), and writes its own rule instead, after its
own checks. Every chunk Logos has dealt with is marked ``rejected`` in OpenShell with the reason, so
OpenShell's inbox stays empty; a new attempt on it bumps its hit count, which is how a site asked
again later is noticed.

The decisions live in ``~/.config/logos/network-approvals.json`` (0600, never in git):

* ``always``: in the agent's policy from now on; ``logos-agent up`` merges it in (:func:`rules_for`).
* ``once``: in the live sandbox only, taken out again after :data:`ONCE_S`.
* ``deny``: no question about it for :data:`DENY_S`.

Only names are let through, never addresses; never anything that resolves to a private, LAN,
tailnet or loopback address, never the model server, never the household's own domains.

Standard library only: ``scripts/logos-agent`` loads this file by path (gateway/agent_runtime.py).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CONFIG_FILE = "network-approvals.json"        # in ~/.config/logos
NEVER_FILE = "network-never.txt"              # more names never to allow, one per line (the household's domains)
ONCE_S = 3600                                 # "Allow once": an hour in the live sandbox
DENY_S = 7 * 86400                            # "Deny": not asked again for a week
REQUEST_KEEP_S = 7 * 86400                    # a question nobody answers goes away after a week
LOG_KEEP_S = 30 * 86400
POLL_S = 15
RULE_PREFIX = "logos_net_"
HERMES_PYTHON = "/opt/hermes/tools/python-*/bin/python3*"     # as in infra/agent-image/policy.yaml
DECISIONS = ("once", "always", "deny")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# Names that only mean something inside a home or a tailnet.
PRIVATE_SUFFIXES = ("localhost", "local", "lan", "home", "internal", "intranet", "corp", "localdomain",
                    "home.arpa", "arpa", "ts.net", "beta.tailscale.net", "test", "invalid", "example")
_LOCK = threading.Lock()


# ── where it is kept ─────────────────────────────────────────────────────────────────────────

def config_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos"


def store_path(env: Mapping[str, str]) -> Path:
    return config_dir(env) / CONFIG_FILE


def _empty() -> Dict[str, Any]:
    return {"agents": {}, "requests": {}, "seen": {}, "log": []}


def load(env: Mapping[str, str]) -> Dict[str, Any]:
    try:
        data = json.loads(store_path(env).read_text())
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    out = _empty()
    for k in out:
        if isinstance(data.get(k), type(out[k])):
            out[k] = data[k]
    return out


def save(env: Mapping[str, str], data: Mapping[str, Any]) -> None:
    path = store_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _agent(data: Dict[str, Any], name: str) -> Dict[str, list]:
    a = data["agents"].setdefault(name, {})
    for k in ("always", "once", "deny"):
        if not isinstance(a.get(k), list):
            a[k] = []
    return a


# ── what may be let through ──────────────────────────────────────────────────────────────────

def normalise(host: Any) -> str:
    return str(host or "").strip().lower().rstrip(".")


def rule_name(host: str, port: int) -> str:
    return RULE_PREFIX + re.sub(r"[^a-z0-9]+", "_", normalise(host)).strip("_") + f"_{int(port)}"


def _blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global is False for RFC 1918, loopback, link-local, ULA and 100.64/10 (CGNAT: Tailscale's range).
    return not ip.is_global or ip.is_multicast


def check_host(host: Any, port: Any, never: Iterable[str] = (),
               resolve: Optional[Callable[..., Any]] = socket.getaddrinfo) -> Optional[str]:
    """Why ``host:port`` may not be let through this way, or ``None`` when it may.

    ``resolve`` (``socket.getaddrinfo``) checks what the name points at now; ``None`` checks the
    name alone (``logos-agent up``, which may run with no network). The sandbox's own proxy refuses
    private addresses for a rule without ``allowed_ips`` too, so a name that changes later is still
    held back there."""
    h = normalise(host)
    try:
        p = int(port)
    except (TypeError, ValueError):
        return "not a port number"
    if not 0 < p < 65536:
        return "not a port number"
    if not h or len(h) > 253:
        return "not a site name"
    if "*" in h:
        return "wildcards are not offered"
    try:
        ipaddress.ip_address(h.strip("[]"))
        return "an address, not a site name: only names can be allowed"
    except ValueError:
        pass
    if any(h == s or h.endswith("." + s) for s in PRIVATE_SUFFIXES):
        return "a name inside a home network"
    labels = h.split(".")
    if len(labels) < 2 or not all(_LABEL.match(l) for l in labels) or labels[-1].isdigit():
        return "not a site name"
    for n in never:
        n = normalise(n)
        if n and (h == n or h.endswith("." + n)):
            return "one of this household's own addresses"
    if resolve is None:
        return None
    try:
        infos = resolve(h, p, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return "the name could not be found"
    addrs = set()
    for info in infos or []:
        try:
            addrs.add(ipaddress.ip_address(info[4][0].split("%")[0]))
        except (ValueError, IndexError, TypeError):
            continue
    if not addrs:
        return "the name could not be found"
    if any(_blocked_ip(a) for a in addrs):
        return "it points at a private address (home network, tailnet or this machine)"
    return None


def never_hosts(agent: Mapping[str, Any], env: Mapping[str, str]) -> set:
    """Names never let through for this agent: its model server, the household's Matrix server and
    its people's domains (from ``matrix.allowed_users``), and ``~/.config/logos/network-never.txt``."""
    out = set()
    model = agent.get("model") or {}
    for url in (model.get("base_url"), (agent.get("matrix") or {}).get("homeserver")):
        if url:
            out.add(normalise(urllib.parse.urlsplit(str(url)).hostname))
    for u in (agent.get("matrix") or {}).get("allowed_users") or []:
        if ":" in str(u):
            out.add(normalise(str(u).split(":", 1)[1]))
    try:
        for line in (config_dir(env) / NEVER_FILE).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(normalise(line))
    except OSError:
        pass
    return {n for n in out if n}


def approvers(agent: Mapping[str, Any]) -> List[str]:
    """Who decides: the agent's own people (``owners``). The household agent's are both of them.
    An agent with no named owners has nobody to ask, so nothing is offered for it."""
    owners = agent.get("owners")
    if isinstance(owners, str):
        owners = [owners]
    return [str(o).strip().lower() for o in owners or [] if str(o).strip()]


def enabled(agent: Mapping[str, Any]) -> bool:
    return agent.get("network_approvals", True) is not False and agent.get("sandbox") is not False


# ── OpenShell's draft rules ──────────────────────────────────────────────────────────────────

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ENDPOINT = re.compile(r"([A-Za-z0-9.\-\[\]:]+?):(\d+)(?:\s*\[[^\]]*\])?")


def parse_rules(text: str) -> List[Dict[str, Any]]:
    """The chunks in ``openshell rule get <name>`` (0.1.8 prints text only)::

        Chunk: 2bee005c-…
        Status: pending
        Rule: allow_example_org_443
        Binary: /usr/bin/curl
        Endpoints: example.org:443 [L4]
        Hits: 2 (first seen 2026-11-27 23:50:38, last seen 2026-11-27 23:54:21)

    ``Hits`` is printed only once a chunk has been denied more than once."""
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for raw in _ANSI.sub("", text or "").splitlines():
        line = raw.strip()
        key, _, val = line.partition(":")
        val = val.strip()
        if key == "Chunk":
            cur = {"id": val, "status": "", "rule": "", "binary": "", "endpoints": [], "hits": 1, "last_seen": ""}
            out.append(cur)
        elif cur is None:
            continue
        elif key == "Status":
            cur["status"] = val.lower()
        elif key == "Rule":
            cur["rule"] = val
        elif key == "Binary":
            cur["binary"] = val
        elif key == "Endpoints":
            cur["endpoints"] = [(normalise(m.group(1)), int(m.group(2))) for m in _ENDPOINT.finditer(val)]
        elif key == "Hits":
            m = re.match(r"(\d+)", val)
            cur["hits"] = int(m.group(1)) if m else 1
            m = re.search(r"last seen ([0-9:\- ]+)", val)
            cur["last_seen"] = m.group(1).strip() if m else ""
    return [c for c in out if c["id"] and c["endpoints"]]


def _binary_ok(path: str) -> bool:
    return bool(re.fullmatch(r"/[A-Za-z0-9._/+-]{1,200}", path or "")) and ".." not in path


def binaries_for(found: Iterable[str]) -> List[str]:
    """The hermes interpreter (whose children match through their ancestors), plus the exact
    program that was refused (``/usr/bin/curl``), as OpenShell's own proposal would."""
    out = [HERMES_PYTHON]
    for b in found:
        if _binary_ok(b) and b not in out:
            out.append(b)
    return out


def rules_for(env: Mapping[str, str], name: str) -> Dict[str, Any]:
    """The policy rules for the sites always allowed for ``name`` (merged in by ``logos-agent up``).
    Each is checked again by name; one that no longer passes is left out."""
    a = (load(env).get("agents") or {}).get(name) or {}
    rules: Dict[str, Any] = {}
    for e in a.get("always") or []:
        host, port = normalise(e.get("host")), e.get("port")
        if check_host(host, port, resolve=None):
            continue
        rules[rule_name(host, int(port))] = {
            "endpoints": [{"host": host, "port": int(port)}],
            "binaries": [{"path": b} for b in binaries_for(e.get("binaries") or [])],
        }
    return rules


# ── the approvals ────────────────────────────────────────────────────────────────────────────

def _active(entries: list, host: str, port: int, now: float) -> Optional[dict]:
    for e in entries:
        if e.get("host") == host and int(e.get("port") or 0) == port and float(e.get("until") or now + 1) > now:
            return e
    return None


class Approvals:
    """Polls each running agent's draft rules, asks, and carries out the answer.

    ``runtime`` is a :class:`gateway.agent_runtime.AgentRuntime` (``run``, ``load``, ``defined``,
    ``phases``, ``env``). ``audit(user_id, action, target, metadata)`` writes the audit log; it is
    given the site, the agent, the decision and who made it, nothing else."""

    def __init__(self, runtime: Any, audit: Optional[Callable[..., None]] = None,
                 resolve: Optional[Callable[..., Any]] = socket.getaddrinfo,
                 clock: Callable[[], float] = time.time):
        self.runtime = runtime
        self.env = runtime.env
        self.audit = audit or (lambda *a, **k: None)
        self.resolve = resolve
        self.clock = clock

    # OpenShell calls (through the runtime's runner, so tests fake them)
    def _run(self, argv: Sequence[str]) -> Tuple[bool, str]:
        p = self.runtime.runner.run(list(argv))
        return p.returncode == 0, _ANSI.sub("", (p.stdout or "") + (p.stderr or ""))

    def chunks(self, name: str) -> List[Dict[str, Any]]:
        ok, text = self._run(["openshell", "rule", "get", name])
        return parse_rules(text) if ok else []

    def _reject(self, name: str, chunk_id: str, reason: str) -> None:
        ok, text = self._run(["openshell", "rule", "reject", name, "--chunk-id", chunk_id, "--reason", reason])
        if not ok:
            logger.info("network: could not mark chunk %s of %s: %s", chunk_id[:8], name, text.strip()[-200:])

    def apply_live(self, name: str, host: str, port: int, binaries: Sequence[str]) -> Tuple[bool, str]:
        argv = ["openshell", "policy", "update", name, "--rule-name", rule_name(host, port),
                "--add-endpoint", f"{host}:{port}"]
        for b in binaries_for(binaries):
            argv += ["--binary", b]
        return self._run(argv + ["--wait", "--timeout", "60"])

    def remove_live(self, name: str, host: str, port: int) -> Tuple[bool, str]:
        return self._run(["openshell", "policy", "update", name, "--remove-rule", rule_name(host, port),
                          "--wait", "--timeout", "60"])

    def _definition(self, name: str) -> Dict[str, Any]:
        try:
            return dict(self.runtime.load(name))
        except Exception:
            return {}

    # ── polling ──

    def poll(self, names: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """One look at every running agent. Returns the questions that are new (to alert about)."""
        now = self.clock()
        if names is None:
            try:
                phases = self.runtime.phases()
                names = [n for n in self.runtime.defined() if phases.get(n) == "Ready"]
            except Exception:
                return []
        fetched = {}
        for name in names:
            agent = self._definition(name)
            if agent and enabled(agent):
                fetched[name] = (agent, self.chunks(name))
        expired: List[Tuple[str, str, int]] = []
        new: List[Dict[str, Any]] = []
        refused: List[Tuple[str, str, List[str], str, int]] = []
        to_reject: List[Tuple[str, str, str]] = []
        with _LOCK:
            data = load(self.env)
            for name, a in data["agents"].items():
                keep = []
                for e in a.get("once") or []:
                    if float(e.get("until") or 0) <= now:
                        if not _active(a.get("always") or [], e["host"], int(e["port"]), now):
                            expired.append((name, e["host"], int(e["port"])))
                        data["log"].append({"ts": now, "agent": name, "host": e["host"], "port": e["port"],
                                            "decision": "expired", "by": ""})
                    else:
                        keep.append(e)
                a["once"] = keep
                a["deny"] = [e for e in a.get("deny") or [] if float(e.get("until") or 0) > now]
            data["requests"] = {k: r for k, r in data["requests"].items()
                                if now - float(r.get("ts") or 0) < REQUEST_KEEP_S}
            for name, (agent, chunks) in fetched.items():
                a = _agent(data, name)
                groups: Dict[Tuple[str, int], List[dict]] = {}
                for c in chunks:
                    for host, port in c["endpoints"][:1]:
                        groups.setdefault((host, port), []).append(c)
                for (host, port), cs in groups.items():
                    fresh = False
                    for c in cs:
                        key = f"{name}|{c['id']}"
                        before = data["seen"].get(key)
                        if before is None:
                            if c["status"] == "pending":
                                fresh = True
                            data["seen"][key] = c["hits"]
                        elif c["hits"] > int(before):
                            fresh = True
                            data["seen"][key] = c["hits"]
                    pending = [c for c in cs if c["status"] == "pending"]
                    if (_active(a["always"], host, port, now) or _active(a["once"], host, port, now)
                            or _active(a["deny"], host, port, now)):
                        to_reject += [(name, c["id"], "decided in Logos") for c in pending]
                        continue
                    open_ = next((r for r in data["requests"].values()
                                  if r["agent"] == name and r["host"] == host and r["port"] == port), None)
                    if open_ is not None:
                        open_["binaries"] = sorted(set(open_.get("binaries") or []) | {c["binary"] for c in cs if c["binary"]})
                        open_["chunks"] = sorted(set(open_.get("chunks") or []) | {c["id"] for c in cs})
                        continue
                    if not fresh:
                        continue
                    reason = check_host(host, port, never_hosts(agent, self.env), self.resolve)
                    if reason:
                        refused.append((name, host, [c["id"] for c in cs], reason, port))
                        data["log"].append({"ts": now, "agent": name, "host": host, "port": port,
                                            "decision": "refused", "by": "", "reason": reason})
                        continue
                    rid = hashlib.sha256(f"{name}|{host}|{port}|{now}".encode()).hexdigest()[:12]
                    req = {"id": rid, "agent": name, "host": host, "port": port, "ts": now,
                           "binaries": sorted({c["binary"] for c in cs if c["binary"]}),
                           "chunks": sorted(c["id"] for c in cs), "for": "",
                           "approvers": approvers(agent)}
                    data["requests"][rid] = req
                    new.append(dict(req))
            data["log"] = [l for l in data["log"] if now - float(l.get("ts") or 0) < LOG_KEEP_S]
            save(self.env, data)
        for name, host, port in expired:
            ok, text = self.remove_live(name, host, port)
            logger.info("network: %s's one-time access to %s:%s ended%s", name, host, port, "" if ok else f" (not removed: {text[-200:]})")
            self.audit(None, "network_expired", "agent", name, {"host": host, "port": port})
        for name, host, ids, reason, port in refused:
            for cid in ids:
                self._reject(name, cid, f"refused by Logos: {reason}")
            logger.info("network: %s tried %s:%s; not offered (%s)", name, host, port, reason)
            self.audit(None, "network_refused", "agent", name, {"host": host, "port": port, "reason": reason})
        for name, cid, reason in to_reject:
            self._reject(name, cid, reason)
        for r in new:
            logger.info("network: %s wants to reach %s:%s; asking %s", r["agent"], r["host"], r["port"],
                        ", ".join(r["approvers"]) or "nobody (no owners)")
        return new

    def set_for(self, rid: str, text: str) -> None:
        """What the agent was doing when it was refused (dashboard: the conversation's title)."""
        with _LOCK:
            data = load(self.env)
            if rid in data["requests"]:
                data["requests"][rid]["for"] = str(text or "")[:160]
                save(self.env, data)

    # ── deciding ──

    def decide(self, rid: str, decision: str, username: str, user_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """``decision``: once | always | deny. Only one of the agent's own people may decide."""
        if decision not in DECISIONS:
            return 404, {"error": "not_found"}
        now = self.clock()
        with _LOCK:
            req = load(self.env)["requests"].get(rid)
        if not req:
            return 404, {"error": "not_found", "message": "That question has already been answered."}
        agent = self._definition(req["agent"])
        if (username or "").lower() not in approvers(agent):
            return 403, {"error": "forbidden", "message": "Only the agent's own people can decide this."}
        host, port = req["host"], int(req["port"])
        if decision != "deny":
            reason = check_host(host, port, never_hosts(agent, self.env), self.resolve)
            if reason:
                return 400, {"error": "refused", "message": f"{host} cannot be allowed: {reason}."}
            ok, text = self.apply_live(req["agent"], host, port, req.get("binaries") or [])
            if not ok:
                logger.warning("network: could not allow %s:%s for %s: %s", host, port, req["agent"], text[-300:])
                return 502, {"error": "openshell", "message": "The sandbox did not take the change. Try again in a minute."}
        with _LOCK:
            data = load(self.env)
            if rid not in data["requests"]:
                return 404, {"error": "not_found", "message": "That question has already been answered."}
            data["requests"].pop(rid)
            a = _agent(data, req["agent"])
            entry = {"host": host, "port": port, "by": username, "at": now, "binaries": req.get("binaries") or []}
            for k in ("always", "once", "deny"):
                a[k] = [e for e in a[k] if not (e.get("host") == host and int(e.get("port") or 0) == port)
                        or (k == "always" and decision == "once")]
            if decision == "always":
                a["always"].append(entry)
            elif decision == "once":
                a["once"].append({**entry, "until": now + ONCE_S})
            else:
                a["deny"].append({**entry, "until": now + DENY_S})
            data["log"].append({"ts": now, "agent": req["agent"], "host": host, "port": port,
                                "decision": decision, "by": username})
            save(self.env, data)
        for cid in req.get("chunks") or []:
            self._reject(req["agent"], cid, f"decided in Logos: {decision}")
        logger.info("network: %s chose %s for %s reaching %s:%s", username, decision, req["agent"], host, port)
        self.audit(user_id, f"network_{decision}", "agent", req["agent"], {"host": host, "port": port})
        return 200, {"ok": True, "decision": decision, "agent": req["agent"], "host": host, "port": port}

    def revoke(self, name: str, host: str, port: int, username: str, user_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """Take a site away again (its people, or an admin). The live sandbox loses it at once."""
        host, port = normalise(host), int(port)
        with _LOCK:
            data = load(self.env)
            a = _agent(data, name)
            had = [e for k in ("always", "once") for e in a[k] if e.get("host") == host and int(e.get("port") or 0) == port]
            if not had:
                return 404, {"error": "not_found"}
            for k in ("always", "once"):
                a[k] = [e for e in a[k] if not (e.get("host") == host and int(e.get("port") or 0) == port)]
            data["log"].append({"ts": self.clock(), "agent": name, "host": host, "port": port,
                                "decision": "removed", "by": username})
            save(self.env, data)
        ok, text = self.remove_live(name, host, port)
        if not ok:
            logger.info("network: %s:%s removed for %s; the live sandbox said: %s", host, port, name, text[-200:])
        self.audit(user_id, "network_removed", "agent", name, {"host": host, "port": port})
        return 200, {"ok": True}

    # ── reading ──

    def pending(self) -> List[Dict[str, Any]]:
        with _LOCK:
            return sorted(load(self.env)["requests"].values(), key=lambda r: -float(r.get("ts") or 0))

    def allowed(self, name: Optional[str] = None) -> Dict[str, Dict[str, list]]:
        """``{agent: {"always": [...], "once": [...], "deny": [...]}}`` as they stand now."""
        now = self.clock()
        with _LOCK:
            agents = load(self.env)["agents"]
        out = {}
        for n, a in agents.items():
            if name is not None and n != name:
                continue
            out[n] = {k: [{"host": e.get("host"), "port": e.get("port"), "by": e.get("by"), "at": e.get("at"),
                           **({"until": e.get("until")} if k != "always" else {})}
                          for e in a.get(k) or [] if k == "always" or float(e.get("until") or 0) > now]
                      for k in ("always", "once", "deny")}
        return out

    def recent(self, since: float) -> List[Dict[str, Any]]:
        with _LOCK:
            return [l for l in load(self.env)["log"] if float(l.get("ts") or 0) >= since]


# ── what the person sees ─────────────────────────────────────────────────────────────────────

def site(host: str, port: Any) -> str:
    return host if int(port or 0) == 443 else f"{host}:{port}"


def prog(binaries: Iterable[str]) -> str:
    names = [os.path.basename(b) for b in binaries if b]
    names = ["Python" if n.startswith("python") else n for n in names]
    return ", ".join(dict.fromkeys(names))


WORDS = {"once": "allowed it for an hour", "always": "allowed it from now on",
         "deny": "said no; it will not ask again for a week", "removed": "took it away again"}


def notification_items(approvals: Approvals, username: str, visible: Mapping[str, str], now: float,
                       since: float, names: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Items for GET /api/v2/notifications. ``visible``: agent name -> display name, for the agents
    this person may use. A question shows only to the agent's own people (its approvers)."""
    items: List[Dict[str, Any]] = []
    me = (username or "").lower()
    for r in approvals.pending():
        if r["agent"] not in visible or me not in (r.get("approvers") or []):
            continue
        who = visible[r["agent"]]
        bits = [f"For: {r['for']}" if r.get("for") else "", f"Using {prog(r.get('binaries') or [])}" if r.get("binaries") else ""]
        items.append({"id": f"net:{r['id']}", "kind": "network", "type": "needs", "ts": float(r["ts"]),
                      "agent": r["agent"], "who": who, "rid": r["id"], "host": r["host"], "port": r["port"],
                      "title": f"{who} wants to reach {site(r['host'], r['port'])}",
                      "text": " · ".join(b for b in bits if b) or "Its sandbox stopped the connection.",
                      "link": "#/noticeboard"})
    for l in approvals.recent(since):
        if l["agent"] not in visible or l.get("decision") not in ("once", "always", "deny", "removed"):
            continue
        who = visible[l["agent"]]
        by = "You" if l.get("by") == me else (names or {}).get(l.get("by")) or str(l.get("by") or "Someone").capitalize()
        items.append({"id": f"netlog:{l['agent']}:{l['host']}:{int(float(l['ts']))}", "kind": "network_done", "type": "update",
                      "ts": float(l["ts"]), "agent": l["agent"], "who": who,
                      "title": f"{who} · {site(l['host'], l['port'])}",
                      "text": f"{by} {WORDS[l['decision']]}"})
    return items


def push_message(req: Mapping[str, Any], shown: str, private: bool) -> Dict[str, Any]:
    if private:
        return {"title": "Logos", "body": "Something needs you", "link": "#/noticeboard", "room": f"net-{req['id']}"}
    return {"title": f"{shown} wants to reach {site(req['host'], req['port'])}",
            "body": "Allow once, always allow or deny, in Logos Notifications.",
            "link": "#/noticeboard", "room": f"net-{req['id']}"}
