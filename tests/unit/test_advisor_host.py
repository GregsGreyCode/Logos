"""gateway/advisor_host.py: the @advisor login survives a failure later in setting it up."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("logos_advisor_host_test", REPO / "gateway" / "advisor_host.py")
ah = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ah
spec.loader.exec_module(ah)


class FakeHost:
    registered = 0
    fail_rooms = True

    def __init__(self, rt):
        pass

    def running(self):
        return True

    def settings(self):
        return {}

    def url(self):
        return "http://matrix"

    def register(self, localpart, password):
        FakeHost.registered += 1
        if FakeHost.registered > 1:
            raise RuntimeError("M_USER_IN_USE")
        return {"user_id": "@advisor:home", "access_token": "tok"}

    def set_display_name(self, user_id, token, name):
        pass

    def _call(self, path, body, token=None, method="POST"):
        return {} if FakeHost.fail_rooms else {"room_id": "!r"}


def test_a_failed_room_does_not_lose_the_login(tmp_path, monkeypatch):
    monkeypatch.setattr(ah, "_sibling", lambda m: types.SimpleNamespace(MatrixHost=FakeHost))
    host = ah.AdvisorHost(types.SimpleNamespace(env={"XDG_CONFIG_HOME": str(tmp_path)}))
    agents = {"bere": {"advisor": {"enabled": True, "person": "bere"}, "matrix": {"allowed_users": ["@bere:home"]}}}
    with pytest.raises(ah.AdvisorError):
        host.matrix(agents)                                   # the account is made, then the room fails
    saved = json.loads((tmp_path / "logos" / ah.MATRIX_FILE).read_text())
    assert saved["token"] == "tok" and saved["password"]
    FakeHost.fail_rooms = False
    out = host.matrix(agents)                                 # no second registration
    assert FakeHost.registered == 1 and out["rooms"]["bere"]["room"] == "!r"
