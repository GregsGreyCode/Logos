"""gateway/mail_host.py: the mail service's config comes from private files, never the command line."""
import json

from gateway import mail_host as mh


class FakeRunner:
    def __init__(self):
        self.ran = []

    def run(self, argv, input=None, text=True):
        self.ran.append(argv)
        return type("C", (), {"returncode": 1})()

    def http(self, url, timeout=3):
        return b"{}"

    def sleep(self, s):
        pass


class FakeRuntime:
    def __init__(self, env, agents, home_env):
        self.env, self._agents, self.home_env = env, agents, home_env
        self.runner, self.calls, self.inputs = FakeRunner(), [], []

    def defined(self):
        return list(self._agents)

    def load(self, name):
        return self._agents[name]

    def get(self, volume, rel):
        return self.home_env.get(volume, "")

    def run(self, cmd, *, input=None, check=True):
        self.calls.append(cmd)
        self.inputs.append(input)
        return "172.17.0.1\n" if cmd[:3] == ["docker", "network", "inspect"] else ""


AGENTS = {"greg": {"mail": {"enabled": True, "person": "greg"}}, "bere": {"mail": {"enabled": True, "person": "bere"}},
          "household": {"role": "household"}}
HOMES = {"logos-home-greg": "LOGOS_MAIL_TOKEN=tg\n", "logos-home-bere": "LOGOS_MAIL_TOKEN=tb\n"}


def write_box(tmp_path, person, box):
    d = tmp_path / "logos" / "mail"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{person}.json").write_text(json.dumps(box))
    return {"XDG_CONFIG_HOME": str(tmp_path)}


def test_nothing_starts_until_a_mailbox_is_connected(tmp_path):
    rt = FakeRuntime({"XDG_CONFIG_HOME": str(tmp_path)}, AGENTS, HOMES)
    assert mh.MailHost(rt).up() is False and rt.calls == []


def test_up_passes_logins_on_stdin_and_locks_the_container_down(tmp_path):
    env = write_box(tmp_path, "greg", {"kind": "outlook", "address": "g@hotmail.example", "client_id": "cid",
                                       "refresh_token": "SECRET-R"})
    write_box(tmp_path, "bere", {"kind": "gmail", "address": "b@gmail.example"})          # no password: ignored
    rt = FakeRuntime(env, AGENTS, HOMES)
    assert mh.MailHost(rt).up() is True
    (config,) = [i for i in rt.inputs if i]
    cfg = json.loads(config)
    assert cfg["agents"] == {"bere": {"token": "tb", "person": "bere"}, "greg": {"token": "tg", "person": "greg"}}
    assert list(cfg["mailboxes"]) == ["greg"] and cfg["mailboxes"]["greg"]["refresh_token"] == "SECRET-R"
    (start,) = [c for c in rt.calls if c[:3] == ["docker", "run", "-d"]]
    for flag in ("--read-only", "--cap-drop", "ALL", "--user", "1000:1000", "--network", "logos-mail",
                 "172.17.0.1:8099:8099", "logos-mail:/data:ro", "logos-mail-state:/state"):
        assert flag in start
    assert not any("SECRET-R" in part for c in rt.calls for part in c)                  # never on a command line


def test_who_uses_mail():
    assert mh.uses_mail({"mail": {"enabled": True, "person": "greg"}})
    assert not mh.uses_mail({"mail": {"enabled": True}}) and not mh.uses_mail({"role": "household"})
