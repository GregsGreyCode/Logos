"""Phones behind on a brain (Greg, 21 Nov): from the server's Syncthing, read-only."""
import json
import types

from gateway import sync_status as s

NOW = 1_800_000_000.0
ME, PHONE, WORK, BERE = "AAAAAAA-1", "PHONE00-2", "WORK000-3", "BERE000-4"


class Runner:
    def __init__(self, data, key="k"):
        self.data, self.key, self.asked = data, key, []

    def run(self, argv, **k):
        return types.SimpleNamespace(returncode=0 if self.key else 1, stdout=self.key + "\n")

    def http(self, url, headers=None, timeout=10, **k):
        assert headers == {"X-API-Key": self.key}
        path = url.replace(s.GUI, "")
        self.asked.append(path)
        return json.dumps(self.data[path]).encode()


def stamp(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def data(phone_seen, phone_need, bere_connected=True):
    return {
        "/rest/system/status": {"myID": ME},
        "/rest/config/devices": [{"deviceID": ME, "name": "server"}, {"deviceID": PHONE, "name": "Greg's phone"},
                                 {"deviceID": WORK, "name": "dev-workstation"}, {"deviceID": BERE, "name": "Béré's phone"}],
        "/rest/system/connections": {"connections": {PHONE: {"connected": False}, WORK: {"connected": False},
                                                     BERE: {"connected": bere_connected}}},
        "/rest/stats/device": {PHONE: {"lastSeen": stamp(phone_seen)}, WORK: {"lastSeen": "1970-01-01T00:00:00Z"},
                               BERE: {"lastSeen": stamp(NOW)}},
        "/rest/config/folders": [{"id": "brain-greg", "devices": [{"deviceID": ME}, {"deviceID": PHONE}, {"deviceID": WORK}]},
                                 {"id": "brain-household", "devices": [{"deviceID": PHONE}, {"deviceID": BERE}]},
                                 {"id": "other", "devices": [{"deviceID": PHONE}]}],
        f"/rest/db/completion?folder=brain-greg&device={PHONE}": {"needItems": phone_need, "needDeletes": 0},
        f"/rest/db/completion?folder=brain-greg&device={WORK}": {"needItems": 0, "needDeletes": 0},
        f"/rest/db/completion?folder=brain-household&device={PHONE}": {"needItems": 0, "needDeletes": 0},
    }


def test_a_quiet_phone_missing_changes_is_behind():
    out = s.behind(Runner(data(NOW - 3 * 86400, 6)), NOW)
    assert out == [{"agent": "greg", "device": "Greg's phone", "last_seen": NOW - 3 * 86400, "missing": 6}]


def test_recently_seen_up_to_date_or_connected_is_not():
    assert s.behind(Runner(data(NOW - 3600, 6)), NOW) == []            # seen an hour ago: between syncs
    assert s.behind(Runner(data(NOW - 3 * 86400, 0)), NOW) == []       # quiet but has everything


def test_no_syncthing_means_nothing_to_say():
    assert s.behind(Runner({}, key=""), NOW) == []
    assert s.behind(Runner({}), NOW) == []                              # the API does not answer
