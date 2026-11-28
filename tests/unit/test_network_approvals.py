"""Story E6: a site an agent's sandbox refused becomes a question for its person (gateway/network_approvals.py).

OpenShell is faked: ``openshell rule get`` prints what 0.1.8 printed on the dev machine on 27 Nov."""
from __future__ import annotations

import json
import socket
import types

import pytest
import yaml

from gateway import network_approvals as na
from gateway.agent_runtime import AgentRuntime, Completed

PENDING = """Network Rules:  (version 1, 1 chunk)

  Chunk: 2bee005c-588a-43e3-8fd7-6ada97d2865f
  Status: pending
  Rule: allow_example_org_443
  Binary: /usr/bin/curl
  Confidence: 65%
  Rationale: Allow curl to connect to example.org:443 (HTTPS).
  Prover: prover: no new findings
  Candidate: 37674a201057
  Endpoints: example.org:443 [L4]
  Binaries: /usr/bin/curl
"""
REJECTED_AGAIN = """Network Rules:  (version 1, 1 chunk)

  Chunk: 2bee005c-588a-43e3-8fd7-6ada97d2865f
  Status: rejected
  Rule: allow_example_org_443_2
  Binary: /usr/bin/curl
  Endpoints: example.org:443 [L4]
  Binaries: /usr/bin/curl
  Hits: {hits} (first seen 2026-11-27 23:50:38, last seen 2026-11-27 23:54:21)
"""


def public(host, port, proto=None):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port))]


class Shell:
    """Fake runner: answers ``openshell rule get`` from ``self.rules[name]`` and records the rest."""

    def __init__(self):
        self.rules = {}
        self.calls = []
        self.fail_update = False

    def run(self, argv, *, input=None, text=True):
        self.calls.append(list(argv))
        if argv[:3] == ["openshell", "rule", "get"]:
            return Completed(0, self.rules.get(argv[3], "No network rules for sandbox"), "")
        if argv[:3] == ["openshell", "policy", "update"] and self.fail_update:
            return Completed(1, "", "error: boom")
        return Completed(0, "OK", "")

    def did(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


AGENTS = {
    "finn": {"name": "finn", "display_name": "Finn", "owners": ["greg"],
             "model": {"base_url": "http://192.0.2.117:1234/v1"},
             "matrix": {"allowed_users": ["@greg:home.example.net"]}},
    "household": {"name": "household", "display_name": "Household agent", "owners": ["greg", "bere"]},
    "nobody": {"name": "nobody", "owners": []},
}


@pytest.fixture
def setup(tmp_path):
    shell = Shell()
    clock = [1_000_000.0]
    audit = []
    runtime = types.SimpleNamespace(env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}, runner=shell,
                                    load=lambda n: AGENTS[n], defined=lambda: sorted(AGENTS),
                                    phases=lambda: {n: "Ready" for n in AGENTS})
    net = na.Approvals(runtime, audit=lambda *a: audit.append(a), resolve=public, clock=lambda: clock[0])
    return types.SimpleNamespace(net=net, shell=shell, clock=clock, audit=audit, env=runtime.env, tmp=tmp_path)


def test_rule_get_is_read_as_openshell_prints_it():
    [c] = na.parse_rules("\x1b[1m" + PENDING)
    assert (c["id"], c["status"], c["binary"], c["endpoints"], c["hits"]) == (
        "2bee005c-588a-43e3-8fd7-6ada97d2865f", "pending", "/usr/bin/curl", [("example.org", 443)], 1)
    [c] = na.parse_rules(REJECTED_AGAIN.format(hits=3))
    assert (c["status"], c["hits"], c["last_seen"]) == ("rejected", 3, "2026-11-27 23:54:21")
    assert na.parse_rules("No network rules for sandbox 'dev'") == []


@pytest.mark.parametrize("host,port,why", [
    ("192.168.1.10", 443, "an address"), ("10.0.0.1", 80, "an address"), ("100.101.102.103", 443, "an address"),
    ("[::1]", 443, "an address"), ("localhost", 80, "home network"), ("nas.local", 443, "home network"),
    ("box.tail1234.ts.net", 443, "home network"), ("router.lan", 80, "home network"), ("*.example.org", 443, "wildcards"),
    ("exa mple.org", 443, "not a site name"), ("intranet", 443, "home network"), ("printer", 443, "not a site name"), ("example.org", 0, "port"),
    ("example.org", 70000, "port"),
])
def test_only_public_site_names_are_offered(host, port, why):
    assert why in (na.check_host(host, port, resolve=public) or "")


def test_a_name_pointing_home_or_at_the_tailnet_is_refused():
    lan = lambda h, p, proto=None: [(2, 1, 6, "", ("192.168.1.226", p))]
    tailnet = lambda h, p, proto=None: [(2, 1, 6, "", ("100.88.1.2", p))]
    mapped = lambda h, p, proto=None: [(10, 1, 6, "", ("::ffff:10.1.2.3", p, 0, 0))]
    for resolve in (lan, tailnet, mapped):
        assert "private address" in na.check_host("sneaky.example.com", 443, resolve=resolve)

    def gone(*a, **k):
        raise socket.gaierror("no")
    assert "could not be found" in na.check_host("nothing.example.com", 443, resolve=gone)
    assert na.check_host("example.org", 443, resolve=public) is None


def test_the_model_server_and_the_households_own_domains_are_never_offered(setup):
    (setup.tmp / "cfg" / "logos").mkdir(parents=True)
    (setup.tmp / "cfg" / "logos" / "network-never.txt").write_text("# ours\nour-family.org\n")
    never = na.never_hosts(AGENTS["finn"], setup.env)
    assert {"192.0.2.117", "home.example.net", "our-family.org"} <= never
    assert "household's own" in na.check_host("matrix.home.example.net", 443, never, resolve=public)
    assert "household's own" in na.check_host("our-family.org", 443, never, resolve=public)


def test_a_refused_site_asks_the_agents_person_once(setup):
    setup.shell.rules["finn"] = PENDING
    new = setup.net.poll(["finn"])
    assert [(r["agent"], r["host"], r["port"], r["approvers"], r["binaries"]) for r in new] == [
        ("finn", "example.org", 443, ["greg"], ["/usr/bin/curl"])]
    assert setup.net.poll(["finn"]) == []                       # asked once, not on every look
    assert len(setup.net.pending()) == 1
    saved = setup.tmp / "cfg" / "logos" / "network-approvals.json"
    assert oct(saved.stat().st_mode & 0o777) == "0o600"


def test_the_household_agents_question_goes_to_both_and_an_ownerless_agent_asks_nobody(setup):
    setup.shell.rules["household"] = PENDING
    setup.shell.rules["nobody"] = PENDING
    new = {r["agent"]: r for r in setup.net.poll(["household", "nobody"])}
    assert new["household"]["approvers"] == ["greg", "bere"]
    assert new["nobody"]["approvers"] == []
    code, _ = setup.net.decide(new["nobody"]["id"], "always", "greg")
    assert code == 403


def test_only_the_agents_own_person_may_decide(setup):
    setup.shell.rules["finn"] = PENDING
    [req] = setup.net.poll(["finn"])
    code, body = setup.net.decide(req["id"], "always", "bere")
    assert code == 403 and not setup.shell.did("openshell", "policy", "update")
    assert setup.net.decide(req["id"], "sideways", "greg")[0] == 404


def test_always_allow_opens_it_live_keeps_it_and_logs_it(setup):
    setup.shell.rules["finn"] = PENDING
    [req] = setup.net.poll(["finn"])
    code, body = setup.net.decide(req["id"], "always", "greg", "u-greg")
    assert code == 200
    [update] = setup.shell.did("openshell", "policy", "update")
    assert update[:8] == ["openshell", "policy", "update", "finn", "--rule-name", "logos_net_example_org_443",
                          "--add-endpoint", "example.org:443"]
    assert ["--binary", na.HERMES_PYTHON] == update[8:10] and "/usr/bin/curl" in update and "--wait" in update
    assert "--allowed-ip" not in " ".join(update) and "allowed-ip" not in " ".join(update)
    assert setup.shell.did("openshell", "rule", "reject", "finn", "--chunk-id", "2bee005c-588a-43e3-8fd7-6ada97d2865f")
    assert setup.net.pending() == []
    assert setup.net.allowed("finn")["finn"]["always"][0]["host"] == "example.org"
    assert setup.audit == [("u-greg", "network_always", "agent", "finn", {"host": "example.org", "port": 443})]
    # Kept for the next `logos-agent up`, checked again by name.
    rules = na.rules_for(setup.env, "finn")
    assert rules == {"logos_net_example_org_443": {"endpoints": [{"host": "example.org", "port": 443}],
                     "binaries": [{"path": na.HERMES_PYTHON}, {"path": "/usr/bin/curl"}]}}
    # The chunk OpenShell keeps (now rejected) does not ask again while it is allowed.
    setup.shell.rules["finn"] = REJECTED_AGAIN.format(hits=5)
    assert setup.net.poll(["finn"]) == []


def test_a_sandbox_that_will_not_take_it_leaves_the_question_open(setup):
    setup.shell.rules["finn"] = PENDING
    [req] = setup.net.poll(["finn"])
    setup.shell.fail_update = True
    code, _ = setup.net.decide(req["id"], "once", "greg")
    assert code == 502 and len(setup.net.pending()) == 1


def test_allow_once_lasts_an_hour_then_a_new_attempt_asks_again(setup):
    setup.shell.rules["finn"] = PENDING
    [req] = setup.net.poll(["finn"])
    assert setup.net.decide(req["id"], "once", "greg")[0] == 200
    assert na.rules_for(setup.env, "finn") == {}                 # not for the next start
    setup.clock[0] += na.ONCE_S + 1
    setup.shell.rules["finn"] = REJECTED_AGAIN.format(hits=1)
    assert setup.net.poll(["finn"]) == []
    assert setup.shell.did("openshell", "policy", "update", "finn", "--remove-rule", "logos_net_example_org_443")
    setup.shell.rules["finn"] = REJECTED_AGAIN.format(hits=2)    # tried again after the hour
    [again] = setup.net.poll(["finn"])
    assert again["host"] == "example.org"


def test_deny_is_remembered_for_a_week(setup):
    setup.shell.rules["finn"] = PENDING
    [req] = setup.net.poll(["finn"])
    assert setup.net.decide(req["id"], "deny", "greg")[0] == 200
    assert not setup.shell.did("openshell", "policy", "update")
    setup.shell.rules["finn"] = REJECTED_AGAIN.format(hits=4)
    assert setup.net.poll(["finn"]) == []                          # retries in the week: no question
    setup.clock[0] += na.DENY_S + 1
    assert setup.net.poll(["finn"]) == []                          # nothing new tried
    setup.shell.rules["finn"] = REJECTED_AGAIN.format(hits=5)
    assert len(setup.net.poll(["finn"])) == 1                      # tried after the week: asked again


def test_a_private_destination_is_refused_without_asking(setup):
    setup.shell.rules["finn"] = PENDING.replace("example.org", "192.168.1.50")
    assert setup.net.poll(["finn"]) == []
    assert setup.shell.did("openshell", "rule", "reject", "finn")
    assert setup.audit[0][1] == "network_refused" and "address" in setup.audit[0][4]["reason"]


def test_a_chunk_decided_in_openshell_itself_is_left_alone(setup):
    setup.shell.rules["finn"] = REJECTED_AGAIN.format(hits=3)
    assert setup.net.poll(["finn"]) == []


def test_revoke_takes_it_out_of_the_sandbox_and_the_list(setup):
    setup.shell.rules["finn"] = PENDING
    [req] = setup.net.poll(["finn"])
    setup.net.decide(req["id"], "always", "greg")
    assert setup.net.revoke("finn", "example.org", 443, "greg")[0] == 200
    assert setup.shell.did("openshell", "policy", "update", "finn", "--remove-rule", "logos_net_example_org_443")
    assert na.rules_for(setup.env, "finn") == {}
    assert setup.net.revoke("finn", "example.org", 443, "greg")[0] == 404


def test_notifications_show_the_question_to_its_people_only(setup):
    setup.shell.rules["household"] = PENDING
    [req] = setup.net.poll(["household"])
    setup.net.set_for(req["id"], "Morning briefing")
    vis = {"household": "Household agent"}
    [item] = na.notification_items(setup.net, "bere", vis, setup.clock[0], 0)
    assert item["type"] == "needs" and item["kind"] == "network"
    assert item["title"] == "Household agent wants to reach example.org"
    assert item["text"] == "For: Morning briefing · Using curl"
    assert na.notification_items(setup.net, "alex", vis, setup.clock[0], 0) == []
    setup.net.decide(req["id"], "deny", "bere")
    [done] = na.notification_items(setup.net, "greg", vis, setup.clock[0], 0)
    assert done["type"] == "update" and done["title"] == "Household agent · example.org"
    [done] = na.notification_items(setup.net, "greg", vis, setup.clock[0], 0, names={"bere": "Béré"})
    assert done["text"] == "Béré said no; it will not ask again for a week"
    [mine] = na.notification_items(setup.net, "bere", vis, setup.clock[0], 0)
    assert mine["text"].startswith("You said no")


def test_the_phone_alert_says_only_what_and_where_to_answer():
    req = {"id": "abc", "host": "example.org", "port": 8443}
    m = na.push_message(req, "Finn", False)
    assert m["title"] == "Finn wants to reach example.org:8443" and m["link"] == "#/noticeboard"
    assert na.push_message(req, "Finn", True)["title"] == "Logos"


def test_logos_agent_up_merges_the_always_allowed_sites(tmp_path):
    from tests.unit.test_agent_runtime import AGENT_YAML, FakeRunner
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "alpha.yaml").write_text(AGENT_YAML)
    pol = tmp_path / "infra/agent-image/policy.yaml"
    pol.parent.mkdir(parents=True)
    pol.write_text("version: 1\nnetwork_policies:\n  anthropic:\n    endpoints:\n      - {host: api.anthropic.com, port: 443}\n"
                   "    binaries:\n      - path: /opt/hermes/tools/python-*/bin/python3*\n")
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
    r = AgentRuntime(tmp_path, FakeRunner(), env=env)
    agent = r.load("alpha")
    agent["model"] = {"provider": "anthropic"}
    assert r.policy_path("alpha", agent) == pol                     # nothing allowed: the tracked file as it is
    na.save(env, {"agents": {"alpha": {"always": [{"host": "example.org", "port": 443, "binaries": []},
                                                  {"host": "10.0.0.9", "port": 443}]}},
                  "requests": {}, "seen": {}, "log": []})
    rendered = yaml.safe_load(r.policy_path("alpha", agent).read_text())["network_policies"]
    assert rendered["logos_net_example_org_443"]["endpoints"] == [{"host": "example.org", "port": 443}]
    assert not any("10_0_0_9" in k for k in rendered)               # an address never gets in, even if written there
    agent["network_approvals"] = False
    assert r.policy_path("alpha", agent) == pol


def test_every_agent_is_told_not_to_retry_in_a_loop():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "souls/_shared/network.md").read_text()
    assert "loop" in text and "asks your person" in text
