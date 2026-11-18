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
    assert list(cp.sections()) == ["service", "root", "principal", "calendars"]
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


def test_what_each_agent_sees():
    people = ["bere", "greg"]
    greg = ch.calendars_for({"name": "greg", "calendar": {"enabled": True, "person": "greg"}}, people)
    assert greg["full"]["personal"]["path"] == "/greg/personal/"
    assert greg["full"]["household"]["path"] == "/household/calendar/"
    assert greg["busy"] == {}
    house = ch.calendars_for({"name": "household", "calendar": {"enabled": True,
                                                                "free_busy": {"greg": "Greg", "bere": "Béré"}}}, people)
    assert list(house["full"]) == ["household"]                     # no one's personal calendar in full
    assert house["busy"]["bere"] == {"paths": ["/bere/personal/"], "label": "Béré"}
    with pytest.raises(ch.CalendarError):
        ch.calendars_for({"name": "x", "calendar": {"enabled": True, "person": "nobody"}}, people)


def test_service_config_and_rights():
    cfg = ch.service_config({"greg": {"timezone": "Europe/London", "calendar": {"enabled": True, "person": "greg"}}},
                            {"greg": "tok"}, ["greg"], "pw")
    assert cfg["radicale"] == "http://logos-calendar:5232" and cfg["user"] == "logos-service"
    assert cfg["agents"]["greg"]["token"] == "tok"
    rights = parse(ch.RIGHTS)
    assert rights["service"]["user"] == "logos-service"
    assert "W" not in rights["service"]["permissions"]              # cannot create or delete calendars
    with pytest.raises(ch.CalendarError):
        ch.CalendarHost(FakeRuntime()).add_person("logos-service")
