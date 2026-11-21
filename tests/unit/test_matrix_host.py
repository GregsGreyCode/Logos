"""gateway/matrix_host.py: the private Matrix server and agent accounts."""
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("logos_matrix_host_test", REPO / "gateway" / "matrix_host.py")
mh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mh
spec.loader.exec_module(mh)

SETTINGS = {"MATRIX_SERVER_NAME": "logos.example.ts.net", "MATRIX_REGISTRATION_TOKEN": "tok"}


def test_the_server_is_private():
    env = mh.server_env(SETTINGS)
    assert env["CONDUWUIT_ALLOW_FEDERATION"] == "false"
    assert env["CONDUWUIT_TRUSTED_SERVERS"] == "[]"
    assert env["CONDUWUIT_REGISTRATION_TOKEN"] == "tok"          # sign-up only with the token
    assert env["CONDUWUIT_URL_PREVIEW_DOMAIN_CONTAINS_ALLOWLIST"] == "[]"
    assert env["CONDUWUIT_WELL_KNOWN__CLIENT"] == "https://logos.example.ts.net"


class Runner:
    def __init__(self):
        self.posts = []

    def http(self, url, *, data=None, headers=None, timeout=10):
        body = json.loads(data) if data else None
        self.posts.append((url, body))
        if body and "auth" not in body:          # first step: the server asks for the token
            raise urllib.error.HTTPError(url, 401, "", {}, io.BytesIO(json.dumps(
                {"session": "s1", "flows": [{"stages": ["m.login.registration_token"]}]}).encode()))
        assert body["auth"] == {"type": "m.login.registration_token", "token": "tok", "session": "s1"}
        return json.dumps({"access_token": "at", "user_id": "@dev-agent:logos.example.ts.net",
                           "device_id": "DEV1"}).encode()


class Runtime:
    def __init__(self, tmp_path):
        f = tmp_path / "matrix.env"
        f.write_text("".join(f"{k}={v}\n" for k, v in SETTINGS.items()))
        self.env = {"LOGOS_MATRIX_ENV": str(f)}
        self.runner = Runner()


def test_an_agent_gets_its_own_account_once(tmp_path):
    host = mh.MatrixHost(Runtime(tmp_path))
    login = host.agent_login("dev", {})
    assert login["MATRIX_USER_ID"] == "@dev-agent:logos.example.ts.net"
    assert login["MATRIX_ACCESS_TOKEN"] == "at" and login["MATRIX_DEVICE_ID"] == "DEV1"
    assert len(login["MATRIX_PASSWORD"]) >= 32
    assert host.rt.runner.posts[0][1]["username"] == "dev-agent"
    again = host.agent_login("dev", login)             # stored: no new account
    assert again == login and len(host.rt.runner.posts) == 2


def test_missing_settings_say_where(tmp_path):
    rt = Runtime(tmp_path)
    Path(rt.env["LOGOS_MATRIX_ENV"]).write_text("MATRIX_SERVER_NAME=x\n")
    with pytest.raises(mh.MatrixError, match="MATRIX_REGISTRATION_TOKEN"):
        mh.MatrixHost(rt).settings()


def test_the_sandbox_policy_lets_matrix_ids_through():
    # Secret-storage key ids (and other Matrix ids in paths) contain an encoded '/'; the
    # sandbox proxy drops those unless the rule allows them, which broke cross-signing.
    import yaml
    policy = yaml.safe_load((REPO / "infra/agent-image/policy.yaml").read_text())
    for e in policy["network_policies"]["matrix"]["endpoints"]:
        assert e.get("allow_encoded_slash") is True


def test_a_written_recovery_key_is_handed_back_on_start(monkeypatch):
    from gateway import agent_runtime as art
    host = type("Host", (), {"up": lambda s: False, "url": lambda s: "http://172.17.0.1:6167",
                             "agent_login": lambda s, n, cur: {"MATRIX_ACCESS_TOKEN": "at",
                                                               "MATRIX_USER_ID": "@dev-agent:x"},
                             "set_display_name": lambda s, uid, tok, name: renamed.append((uid, name)) or True})
    renamed = []
    fake = type("M", (), {"MatrixHost": lambda rt: host(), "MatrixError": mh.MatrixError,
                          "agent_display_name": staticmethod(mh.agent_display_name)})
    monkeypatch.setattr(art, "_sibling", lambda name: fake)

    class RT:
        MATRIX_EXPORTED = art.AgentRuntime.MATRIX_EXPORTED
        MATRIX_RECOVERY_FILE = art.AgentRuntime.MATRIX_RECOVERY_FILE

        def __init__(self, files):
            self.files = files

        def get(self, vol, rel):
            return self.files.get((vol, rel), "")

    agent = {"name": "dev", "matrix": {"enabled": True}}
    cur = {}
    art.AgentRuntime._matrix_env(RT({}), agent, cur, "logos-home-dev")
    assert "MATRIX_RECOVERY_KEY" not in cur                 # first start: bootstrap writes one
    assert cur["MATRIX_RECOVERY_KEY_OUTPUT_FILE"] == "/opt/data/matrix-recovery-key"
    assert cur["MATRIX_REQUIRE_MENTION"] == "true"              # rooms: only when @mentioned
    assert cur["MATRIX_SESSION_SCOPE"] == "room"                # one conversation per room, no thread per message
    assert renamed == [("@dev-agent:x", "dev \U0001F916")]        # named in Element on every up
    cur = {"MATRIX_RECOVERY_KEY": "stale"}
    art.AgentRuntime._matrix_env(RT({("logos-home-dev", "matrix-recovery-key"): "EsT abc\n"}),
                                 agent, cur, "logos-home-dev")
    assert cur["MATRIX_RECOVERY_KEY"] == "EsT abc"
    art.AgentRuntime._matrix_env(RT({}), {"name": "dev"}, cur, "logos-home-dev")
    assert "MATRIX_RECOVERY_KEY" not in cur                 # Matrix off: nothing exported
    assert "MATRIX_REQUIRE_MENTION" not in cur
    assert "MATRIX_SESSION_SCOPE" not in cur
    art.AgentRuntime._matrix_env(RT({}), {"name": "dev", "matrix": {"enabled": True, "require_mention": False}},
                                 cur, "logos-home-dev")
    assert cur["MATRIX_REQUIRE_MENTION"] == "false"


def test_the_client_url_can_differ_from_the_server_name():
    env = mh.server_env({**SETTINGS, "MATRIX_SERVER_NAME": "gregandbere.com",
                         "MATRIX_CLIENT_URL": "https://logos.example.ts.net"})
    assert env["CONDUWUIT_SERVER_NAME"] == "gregandbere.com"
    assert env["CONDUWUIT_WELL_KNOWN__CLIENT"] == "https://logos.example.ts.net"
    assert "CONDUWUIT_WELL_KNOWN__SERVER" not in env          # no federation: nothing to announce


def test_the_first_user_token_is_read_from_the_server_log():
    log = ("\x1b[2m2026\x1b[0m INFO In order to use your new homeserver, you need to create its first "
           "user account.\nOpen your Matrix client of choice and register an account on gregandbere.com "
           "using the registration token \x1b[1mAbC123xyz\x1b[0m . Pick your own username.\n")
    assert mh.first_user_token(log) == "AbC123xyz"
    assert mh.first_user_token("nothing here") == ""


def test_no_name_suffix_and_no_announcements_call():
    env = mh.server_env(SETTINGS)
    assert env["CONDUWUIT_NEW_USER_DISPLAYNAME_SUFFIX"] == ""
    assert env["CONDUWUIT_ALLOW_ANNOUNCEMENTS_CHECK"] == "false"


def test_agent_display_name():
    assert mh.agent_display_name({"name": "greg", "display_name": "Greg's agent"}) == "Greg's agent \U0001F916"
    assert mh.agent_display_name({"name": "household", "display_name": "Household agent",
                                  "matrix": {"display_name": "Household \U0001F916"}}) == "Household \U0001F916"
    assert mh.agent_display_name({"name": "dev"}) == "dev \U0001F916"


# ── reset_password: a forgotten Matrix password ─────────────────────────────────────────────

class ResetServer:
    """A pretend Matrix server: the admin room answers the reset command, and records redactions."""
    def __init__(self, works=True):
        self.works, self.sent, self.redacted, self.logins, self.password = works, [], [], [], None

    def call(self, path, body=None, token="", method=""):
        if path.endswith("/login"):
            user = body["identifier"]["user"]
            self.logins.append(user)
            if user == "logos-admin":
                return {"access_token": "adm", "user_id": "@logos-admin:home"}
            return {"access_token": "u"} if self.works and body["password"] == self.password else {"errcode": "M_FORBIDDEN"}
        if "/directory/room/" in path:
            return {"room_id": "!admins:home"}
        if "/send/m.room.message/" in path:
            self.sent.append(body["body"])
            self.password = body["body"].split()[-1]
            return {"event_id": "$cmd"}
        if "/messages" in path:
            return {"chunk": [{"event_id": "$ans", "sender": "@conduit:home", "type": "m.room.message",
                               "content": {"body": "Successfully reset the password" if self.works else "No such user"}},
                              {"event_id": "$cmd", "sender": "@logos-admin:home", "type": "m.room.message"},
                              {"event_id": "$old", "sender": "@conduit:home", "type": "m.room.message",
                               "content": {"body": "an earlier answer"}}]}       # newest first
        if "/redact/" in path:
            self.redacted.append(path.split("/redact/")[1].split("/")[0])
            return {"event_id": "$r"}
        return {}


def reset_host(monkeypatch, server):
    from gateway import matrix_host as mh
    host = mh.MatrixHost.__new__(mh.MatrixHost)
    monkeypatch.setattr(host, "settings", lambda: {"MATRIX_SERVER_NAME": "home", "MATRIX_REGISTRATION_TOKEN": "t",
                                                    "MATRIX_ADMIN_PASSWORD": "admin-pw"}, raising=False)
    monkeypatch.setattr(host, "_call", server.call, raising=False)
    return host


def test_a_reset_gives_a_working_password_and_leaves_no_trace_in_the_admin_room(monkeypatch):
    server = ResetServer()
    host = reset_host(monkeypatch, server)
    password = host.reset_password("bere", sleep=lambda s: None)
    assert server.sent == [f"!admin users reset-password bere {password}"]       # no --logout: her phone stays in
    assert len(password) >= 16 and server.logins == ["logos-admin", "bere"]          # proven by signing in once
    assert server.redacted == ["%24cmd", "%24ans"]                                   # command and answer redacted


def test_a_failed_reset_says_so_and_bad_names_are_refused(monkeypatch):
    from gateway.matrix_host import MatrixError
    host = reset_host(monkeypatch, ResetServer(works=False))
    with pytest.raises(MatrixError, match="did not take: No such user"):
        host.reset_password("nobody", sleep=lambda s: None)
    for bad in ("", "Bere Smith", "logos-admin", "../x"):
        with pytest.raises(MatrixError):
            host.reset_password(bad, sleep=lambda s: None)
