"""gateway/calendar_host.py: the household's own calendar server (story C1)."""
import configparser
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("logos_calendar_host_test", REPO / "gateway" / "calendar_host.py")
ch = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ch
spec.loader.exec_module(ch)


def parse(text):
    cp = configparser.RawConfigParser()
    cp.read_string(text)
    return cp


def test_server_settings_are_private_and_hashed():
    cp = parse(ch.radicale_config())
    assert cp["auth"]["type"] == "htpasswd"
    assert cp["auth"]["htpasswd_encryption"] == "bcrypt"
    assert cp["rights"]["type"] == "from_file"
    assert cp["web"]["type"] == "none"                     # no web UI to attack
    assert cp["logging"]["mask_passwords"] == "True"
    assert "git" in cp["storage"]["hook"] and "%(user)s" in cp["storage"]["hook"]


def test_rights_only_ever_name_the_signed_in_user():
    cp = parse(ch.RIGHTS)
    assert list(cp.sections()) == ["root", "principal", "calendars"]
    assert cp["root"]["permissions"] == "R"
    # Every rule that grants anything below the top level is scoped to {user}, so no one can
    # reach another person's calendars or the shared one by its real path.
    for name in ("principal", "calendars"):
        assert cp[name]["collection"].startswith("{user}")


def test_calendar_props():
    props = json.loads(ch.calendar_props("Béré", "#1565c0"))
    assert props["tag"] == "VCALENDAR"
    assert props["D:displayname"] == "Béré"
    assert "VEVENT" in props["C:supported-calendar-component-set"]


class FakeRuntime:
    env = {}

    def __init__(self):
        self.runner = self

    def run(self, cmd, *, input=None, check=True):
        return ""


@pytest.mark.parametrize("bad", ["Greg", "g", "household", "../x", "a b", "x" * 40])
def test_add_person_refuses_unusable_names(bad):
    with pytest.raises(ch.CalendarError):
        ch.CalendarHost(FakeRuntime()).add_person(bad)


def test_password_file_is_under_config(tmp_path):
    assert ch.password_file({"XDG_CONFIG_HOME": str(tmp_path)}, "greg") == tmp_path / "logos" / "calendar-greg-password"
