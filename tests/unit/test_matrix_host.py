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
