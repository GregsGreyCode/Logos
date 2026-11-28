"""F38: adding a person safely (gateway/people.py, gateway/people_api.py, docs/people.md).

The invitation (single use, expiring, only its hash kept, never logged), the agent made from the
template outside the repo, the household agent learning about them, who-can-see-what, removing
someone into the graveyard and back, and the 30-day deletion. Docker, OpenShell, Matrix and the
calendar are fakes."""
import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.auth.db as auth_db
from gateway import agent_runtime as ar
from gateway import calendar_host as ch
from gateway import people as pp
from gateway import people_api
from gateway.agent_runtime import AgentRuntime, Completed

REPO = Path(__file__).resolve().parents[2]


# ── fakes ────────────────────────────────────────────────────────────────────────────────────

class Volumes:
    """Docker volumes as {volume: {path: bytes}}; answers the commands AgentRuntime and People send."""

    def __init__(self):
        self.vols: dict = {}
        self.calls: list = []

    def run(self, argv, *, input=None, text=True):
        argv = list(argv)
        self.calls.append(argv)
        ok = lambda out=b"" if not text else "": Completed(0, out, "")          # noqa: E731
        if argv[:3] == ["docker", "volume", "inspect"]:
            return Completed(0 if argv[3] in self.vols else 1, "", "")
        if argv[:3] == ["docker", "volume", "create"]:
            self.vols.setdefault(argv[-1], {})
            return ok()
        if argv[:3] == ["docker", "volume", "rm"]:
            for v in argv[3:]:
                self.vols.pop(v, None)
            return ok()
        if argv[:2] == ["docker", "run"] and "-v" in argv:
            vol = argv[argv.index("-v") + 1].split(":")[0]
            files = self.vols.setdefault(vol, {})
            if "tar" in argv and "-czf" in argv:
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w:gz") as t:
                    for rel, data in files.items():
                        info = tarfile.TarInfo(rel)
                        info.size = len(data)
                        t.addfile(info, io.BytesIO(data))
                return Completed(0, buf.getvalue(), b"")
            if "tar" in argv and "-xzf" in argv:
                with tarfile.open(fileobj=io.BytesIO(input), mode="r:gz") as t:
                    for m in t.getmembers():
                        files[m.name] = t.extractfile(m).read()
                return Completed(0, b"", b"")
            if argv[-2:-1] == ["-c"] and "cat > /v/" in argv[-1]:       # put()
                rel = argv[-1].split("cat > /v/", 1)[1].split(" ", 1)[0]
                files[rel] = input.encode() if isinstance(input, str) else input
                return ok()
            if "cat" in argv and argv[-1].startswith("/v/"):              # get()
                rel = argv[-1][3:]
                return Completed(0, files[rel].decode(), "") if rel in files else Completed(1, "", "missing")
        return ok()

    def call(self, argv):
        return 0

    def http(self, *a, **k):
        raise OSError("no network in tests")

    def sleep(self, s):
        pass


class Runtime(AgentRuntime):
    """The real definitions code (private folder, overlays, household members); `up`, `down` and the
    phase are pretend."""

    def __init__(self, repo, env):
        super().__init__(repo, Volumes(), env=env)
        self.ups, self.downs, self.phases_ = [], [], {}

    def up(self, name, load_model=True):
        assert load_model is False                       # the People flow never touches the model server
        self.load(name)
        for vol in ar.volumes(name):                     # as the real `up` does first
            self.ensure_volume(vol)
        self.ups.append(name)
        self.phases_[name] = "Ready"
        return 0

    def down(self, name):
        self.downs.append(name)
        self.phases_.pop(name, None)

    def phase(self, name):
        return self.phases_.get(name, "Missing")


class Matrix:
    server = "home.test"

    def __init__(self):
        self.people, self.rooms, self.closed, self.agents = {}, [], [], []
        self.taken = {"greg"}

    def settings(self):
        return {"MATRIX_SERVER_NAME": self.server}

    def user_id(self, local):
        return f"@{local}:{self.server}"

    def available(self, local):
        return local not in self.taken and local not in self.people

    def register_person(self, local, password, display_name=""):
        self.people[local] = password
        return self.user_id(local)

    def agent_login(self, name, current):
        self.agents.append(name)
        return {"MATRIX_ACCESS_TOKEN": f"tok-{name}", "MATRIX_USER_ID": self.user_id(f"{name}-agent"),
                "MATRIX_DEVICE_ID": "D", "MATRIX_PASSWORD": "pw"}

    def direct_room(self, token, creator, invitee):
        self.rooms.append((token, creator, invitee))
        return "!dm:home.test"

    def deactivate(self, local, sleep=None):
        self.closed.append(local)
        return "done"


class Calendar:
    def __init__(self):
        self.people, self.removed, self.restored, self.purged = {}, [], [], []

    def running(self):
        return True

    def add_person(self, name, display_name="", password=""):
        assert password                                  # the person's own, never a file
        self.people[name] = password

    def remove_person(self, name):
        self.removed.append(name)
        return f"{name}:$2b$12$hash"

    def restore_person(self, line):
        self.restored.append(line)

    def purge_person(self, name):
        self.purged.append(name)


HOUSEHOLD = {"name": "household", "display_name": "Household agent", "role": "household", "owners": ["greg"],
             "image": "logos-agent:0.23.1", "soul": "souls/household/soul.md", "policy": "infra/agent-image/policy.yaml",
             "timezone": "Europe/London",
             "calendar": {"enabled": True, "free_busy": {"greg": "Greg"}},
             "matrix": {"enabled": True, "allowed_users": []},
             "model": {"provider": "lmstudio", "base_url": "http://192.0.2.117:1234/v1", "default": "qwen/q",
                       "context_length": 65536, "gateway": True}}
GREG = {"name": "greg", "display_name": "Greg's agent", "owners": ["greg"], "image": "logos-agent:0.23.1",
        "calendar": {"enabled": True, "person": "greg"}, "model": {"provider": "lmstudio", "default": "qwen/q"}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "agents").mkdir(parents=True)
    (repo / "agents" / "household.yaml").write_text(yaml.safe_dump(HOUSEHOLD))
    (repo / "agents" / "household.local.yaml").write_text(yaml.safe_dump({"matrix": {"allowed_users": ["@greg:home.test"]}}))
    (repo / "agents" / "greg.yaml").write_text(yaml.safe_dump(GREG))
    env = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    rt = Runtime(repo, env)
    monkeypatch.setattr(auth_db, "_DB_PATH", None)
    auth_db.init_db(tmp_path / "db")
    auth_db.create_user("g@example.org", "greg", "x", role="admin", display_name="Greg")
    mx, cal = Matrix(), Calendar()
    clock = {"now": 1_800_000_000.0}
    mgr = pp.People(rt, auth_db, matrix=lambda r: mx, calendar=lambda r: cal, now=lambda: clock["now"])
    monkeypatch.setattr(pp.People, "refresh_services", lambda self: [])
    return {"rt": rt, "mx": mx, "cal": cal, "mgr": mgr, "repo": repo, "env": env, "clock": clock, "tmp": tmp_path}


OPTS = {"chat": True, "calendar": True, "household": True, "email": True}


def add_ann(world, opts=OPTS):
    user = auth_db.create_user("ann@example.org", "ann", "x", display_name="Ann", status="pending")
    auth_db.update_user(user["id"], status="active")
    world["mgr"].accept_accounts("ann", "Ann", "a long password", opts)
    world["rt"].phases_["household"] = "Ready"
    return world["mgr"].set_up_agent("ann", "Ann", opts)


# ── names ────────────────────────────────────────────────────────────────────────────────────

def test_names_that_are_something_else_are_refused(world):
    rt = world["rt"]
    for bad in ("Ann", "a", "household", "greg-agent", "logos-admin", "logosx", "dev", "1ann", "an n", "x" * 32):
        assert pp.check_new_person(bad, "Ann", "", rt, auth_db), bad
    assert "already signs in" in pp.check_new_person("greg", "Greg", "", rt, auth_db)
    assert pp.check_new_person("ann", "Ann", "not-an-email", rt, auth_db)
    assert pp.check_new_person("ann", "", "", rt, auth_db)
    rt.runner.vols["logos-brain-ann"] = {}                  # an old agent's storage is never taken over
    assert "still has its storage" in pp.check_new_person("ann", "Ann", "", rt, auth_db)
    del rt.runner.vols["logos-brain-ann"]
    assert pp.check_new_person("ann", "Ann", "ann@example.org", rt, auth_db) is None
    assert pp.slug("Anne-Marie Dupré") == "anne-marie" and pp.slug("Élodie") == "elodie"


def test_tokens_are_long_and_only_hashes_are_kept():
    t = pp.new_token()
    assert len(t) >= 43 and pp.hash_token(t) != t and len(pp.hash_token(t)) == 64
    assert pp.password_problem("short") and pp.password_problem(" padded password ") and not pp.password_problem("a long password")


# ── the definition: outside the repo, read by the runtime ───────────────────────────────────────

def test_the_new_agent_is_defined_outside_the_repo_and_joins_the_household(world):
    rt, repo = world["rt"], world["repo"]
    steps = add_ann(world)
    assert [s["state"] for s in steps] == ["ok"] * 7, steps
    path = ar.private_agents_dir(world["env"]) / "ann.yaml"
    assert path.exists() and not (repo / "agents" / "ann.yaml").exists()        # never in the public repo
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert "ann" in rt.defined() and rt.is_private("ann") and not rt.is_private("greg")
    ann = rt.load("ann")
    assert ann["owners"] == ["ann"] and ann["display_name"] == "Ann's agent"
    assert ann["matrix"]["allowed_users"] == ["@ann:home.test"] and ann["matrix"]["home_room"] == "!dm:home.test"
    assert ann["calendar"] == {"enabled": True, "person": "ann"} and ann["mail"] == {"enabled": True, "person": "ann"}
    assert ann["model"]["base_url"] == HOUSEHOLD["model"]["base_url"]           # copied from the household agent
    assert "house" not in ann and "advisor" not in ann and "speaker_voice" not in ann
    house = rt.load("household")
    assert house["owners"] == ["greg", "ann"]
    assert house["matrix"]["allowed_users"] == ["@greg:home.test", "@ann:home.test"]
    assert house["calendar"]["free_busy"] == {"greg": "Greg", "ann": "Ann"}
    assert rt.ups == ["ann", "household"]                                        # the household agent restarted
    # Accounts made with the password they chose; the agent's chat account made before its first start.
    assert world["mx"].people == {"ann": "a long password"} and world["cal"].people == {"ann": "a long password"}
    assert world["mx"].rooms == [("tok-ann", "@ann-agent:home.test", "@ann:home.test")]
    env = rt.runner.vols["logos-home-ann"][".env"].decode()
    # E7: the agent's chat login secrets are kept on the host, never in its home volume.
    assert "LOGOS_MATRIX_ACCESS_TOKEN" not in env and "LOGOS_MATRIX_USER_ID=@ann-agent:home.test" in env
    from gateway import agent_secrets
    assert agent_secrets.load(world["env"], "ann")["LOGOS_MATRIX_ACCESS_TOKEN"] == "tok-ann"
    # Running it again keeps what is done.
    again = world["mgr"].set_up_agent("ann", "Ann", OPTS)
    assert all(s["state"] == "ok" for s in again) and world["mx"].agents == ["ann"]


def test_household_left_alone_when_not_asked_or_not_running(world):
    steps = add_ann(world, {**OPTS, "household": False})
    assert world["rt"].load("household")["owners"] == ["greg"]
    assert next(s for s in steps if s["id"] == "household")["state"] == "skipped"
    world["rt"].phases_.pop("household")
    user = auth_db.create_user("bo@example.org", "bo", "x", display_name="Bo")
    world["mgr"].accept_accounts("bo", "Bo", "a long password", OPTS)
    steps = world["mgr"].set_up_agent("bo", "Bo", OPTS)
    assert "next start" in next(s for s in steps if s["id"] == "household")["detail"]
    assert world["rt"].ups == ["ann", "bo"]                                      # not started where it was not running
    assert user


def test_a_failed_chat_account_still_gives_them_an_agent(world):
    world["mx"].register_person = lambda *a: (_ for _ in ()).throw(RuntimeError("server down"))
    steps = add_ann(world)
    by = {s["id"]: s for s in steps}
    assert by["chat"]["state"] == "failed" and "server down" in by["chat"]["detail"]
    assert by["room"]["state"] == "skipped" and by["agent"]["state"] == "ok"
    assert "matrix" not in world["rt"].load("ann")


def test_up_can_leave_the_model_server_alone():
    src = Path(ar.__file__).read_text()
    assert src.count("self.ensure_model(") == 1                              # one call, behind the flag
    assert "if load_model:\n            self.ensure_model(" in src


# ── who can see what ───────────────────────────────────────────────────────────────────────────

def test_visibility(world):
    add_ann(world)
    rt = world["rt"]
    agents = {n: rt.load(n) for n in rt.defined()}
    v = pp.visibility("ann", agents, {"ann": "Ann", "greg": "Greg"})
    assert v["own_agent"] == "ann"
    assert [u["name"] for u in v["uses"]] == ["ann", "household"]
    assert next(u for u in v["uses"] if u["name"] == "household")["shared_with"] == ["Greg"]
    seen = {s["what"]: s["who"] for s in v["seen_by"]}
    assert seen["Ann's agent: its chats and its brain"] == ["Ann"]
    cal = seen["Their own calendar"]
    assert "Greg's agent (read only)" in cal and "Household agent (busy times only)" in cal
    assert "Greg, on their phone (read only)" in cal
    assert any("email" in r.lower() for r in v["reach"])
    g = pp.visibility("greg", agents, {"ann": "Ann", "greg": "Greg"})
    assert [u["name"] for u in g["uses"]] == ["greg", "household"]


# ── removing, restoring, deleting for good ─────────────────────────────────────────────────────

def test_remove_keeps_a_backup_then_restore_brings_everything_back(world):
    rt, mgr = world["rt"], world["mgr"]
    add_ann(world)
    rt.runner.vols["logos-brain-ann"]["people/greg.md"] = b"# Greg\n"
    home_before = dict(rt.runner.vols["logos-home-ann"])
    assert mgr.removable("greg")                                                 # set up by hand: not from here
    meta = mgr.remove("ann", "usr_admin")
    user = auth_db.get_user_by_username("ann")
    assert user["status"] == "suspended"
    assert "logos-home-ann" not in rt.runner.vols and "logos-brain-ann" not in rt.runner.vols
    assert "ann" not in rt.defined() and rt.downs == ["ann"]
    assert rt.load("household")["owners"] == ["greg"]                            # off the household agent
    assert world["cal"].removed == ["ann"]
    grave = mgr.graveyard.root / "ann"
    assert grave.stat().st_mode & 0o777 == 0o700
    for f in ("meta.json", "home.tar.gz", "brain.tar.gz", "definition.yaml", "calendar-account"):
        assert (grave / f).stat().st_mode & 0o777 == 0o600, f
    assert meta["delete_after"] - meta["removed_at"] == 30 * 86400
    with pytest.raises(pp.PeopleError):
        mgr.remove("ann", "usr_admin")
    # Restore within the 30 days.
    out = mgr.restore("ann")
    assert out["notes"] == []
    assert rt.runner.vols["logos-brain-ann"]["people/greg.md"] == b"# Greg\n"
    assert rt.runner.vols["logos-home-ann"] == home_before
    assert auth_db.get_user_by_username("ann")["status"] == "active"
    assert "ann" in rt.defined() and rt.load("household")["owners"] == ["greg", "ann"]
    assert world["cal"].restored == ["ann:$2b$12$hash"] and not grave.exists()


def test_after_30_days_everything_is_deleted(world):
    rt, mgr, clock = world["rt"], world["mgr"], world["clock"]
    add_ann(world)
    mgr.remove("ann", "usr_admin")
    clock["now"] += 29 * 86400
    assert mgr.purge_due() == [] and mgr.graveyard.get("ann")
    clock["now"] += 2 * 86400
    done = mgr.purge_due()
    assert [d["username"] for d in done] == ["ann"]
    assert not (mgr.graveyard.root / "ann").exists()
    assert auth_db.get_user_by_username("ann") is None
    assert world["cal"].purged == ["ann"] and world["mx"].closed == ["ann", "ann-agent"]


def test_a_backup_that_does_not_read_back_deletes_nothing(world):
    rt, mgr = world["rt"], world["mgr"]
    add_ann(world)
    real = rt.runner.run

    def broken(argv, *, input=None, text=True):
        if "-czf" in argv:
            return Completed(0, b"not a tarball", b"")
        return real(argv, input=input, text=text)
    rt.runner.run = broken
    with pytest.raises(pp.PeopleError, match="does not read back"):
        mgr.remove("ann", "usr_admin")
    assert "logos-home-ann" in rt.runner.vols and "ann" in rt.defined()


# ── the calendar's own part (runs the real setup script on a folder) ───────────────────────────

def test_calendar_remove_and_purge(tmp_path):
    root = tmp_path / "radicale"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "users").write_text("ann:$2b$a\ngreg:$2b$g\nlogos-service:$2b$04$s\n")
    (root / "etc" / "service-password").write_text("pw\n")
    top = root / "collections" / "collection-root"
    for p in ("ann", "greg"):
        (top / p / "personal").mkdir(parents=True)
    (top / "greg" / "view-ann").symlink_to("../ann/personal")

    def run(**req):
        base = {"config": "", "rights": "", "gitignore": "", "shared_props": "{}", "add": None, "service": "logos-service"}
        out = subprocess.run([sys.executable, "-c", ch.SETUP, str(root)], input=json.dumps({**base, **req}),
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out.strip().splitlines()[-1])
    found = run(remove="ann")
    assert found["people"] == ["greg"] and found["removed"] == "ann:$2b$a"
    assert not os.path.lexists(top / "greg" / "view-ann") and (top / "ann" / "personal").is_dir()   # kept
    found = run(add={"name": "ann", "hash": "$2b$a", "props": "{}"})                           # restored
    assert found["people"] == ["ann", "greg"] and os.path.lexists(top / "greg" / "view-ann")
    run(purge="ann")
    assert not (top / "ann").exists() and not os.path.lexists(top / "greg" / "view-ann")


# ── the endpoints ───────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(world, monkeypatch):
    from gateway.auth.middleware import auth_middleware
    from gateway.auth.tokens import issue_access_token
    monkeypatch.setenv("LOGOS_JWT_SECRET", "test-secret-" + "x" * 32)
    people_api.check_rate_limit.__globals__["_rate_store"].clear()
    admin = auth_db.get_user_by_username("greg")
    member = auth_db.create_user("b@example.org", "bere", "x", display_name="Béré")

    class Backend:
        runtime = world["rt"]

    def call(method, path, body=None, who=admin, csrf=True, raw=None):
        async def flow():
            app = web.Application(middlewares=[auth_middleware])
            app["sandbox_backend"] = Backend()
            app["people_factory"] = lambda: world["mgr"]
            people_api.register(app)
            async with TestClient(TestServer(app)) as client:
                cookies = {"csrf_token": "c"}
                if who:
                    cookies["access_token"] = issue_access_token(who["id"], who["email"], who["role"])
                client.session.cookie_jar.update_cookies(cookies)
                headers = {"X-CSRF-Token": "c"} if csrf else {}
                kw = {"json": body} if body is not None else ({"data": raw, "headers": {**headers, "Content-Type": "text/plain"}} if raw else {})
                r = await client.request(method, path, headers=headers, **kw) if "headers" not in kw else \
                    await client.request(method, path, **kw)
                data = await r.json(content_type=None) if r.content_type == "application/json" else await r.text()
                jobs = app.get("people_jobs") or {}
                for _kind, task in jobs.values():
                    await task
                return r.status, data, r.cookies
        return asyncio.run(flow())
    return {"call": call, "admin": admin, "member": member, "world": world}


def invite(api, **over):
    body = {"display_name": "Ann", "username": "ann", "email": "ann@example.org", "days": 7,
            "chat": True, "calendar": True, "household": True, "email_read": True, **over}
    return api["call"]("POST", "/api/v2/people", body)


def test_only_an_admin_invites_and_csrf_is_checked(api):
    assert invite(api)[0] == 201
    call = api["call"]
    body = {"display_name": "Bo", "username": "bo"}
    assert call("POST", "/api/v2/people", body, who=api["member"])[0] == 403
    assert call("POST", "/api/v2/people", body, csrf=False)[0] == 403
    assert call("POST", "/api/v2/people", body, who=None)[0] == 401
    assert call("GET", "/api/v2/people/audit", who=api["member"])[0] == 403
    assert call("GET", "/api/v2/people/ann", who=api["member"])[0] == 404          # not yours, not an admin
    assert call("POST", "/api/v2/people/ann/remove", {"confirm": "ann"}, who=api["member"])[0] == 403


def test_the_invitation_works_once_and_the_secret_is_never_kept_or_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    code, out, _ = invite(api)
    token = out["token"]
    stored = auth_db.latest_invite(auth_db.get_user_by_username("ann")["id"])
    assert stored["token_hash"] == pp.hash_token(token) and token not in json.dumps(stored)
    assert auth_db.get_user_by_username("ann")["status"] == "pending"
    call = api["call"]
    code, who, _ = call("POST", "/auth/invite/check", {"token": token}, who=None, csrf=False)
    assert code == 200 and who["display_name"] == "Ann" and who["invited_by"] == "Greg"
    assert who["chat_id"] == "@ann:home.test" and who["calendar"] is True
    assert call("POST", "/auth/invite/check", {"token": token + "x"}, who=None, csrf=False)[0] == 410
    assert call("POST", "/auth/invite/accept", {"token": token, "password": "short"}, who=None, csrf=False)[0] == 400
    assert call("POST", "/auth/invite/accept", raw=json.dumps({"token": token, "password": "a long password"}),
                who=None, csrf=False)[0] == 415                                    # JSON only
    code, done, cookies = call("POST", "/auth/invite/accept", {"token": token, "password": "a long password"},
                               who=None, csrf=False)
    assert code == 200 and "access_token" in cookies
    assert auth_db.get_user_by_username("ann")["status"] == "active"
    assert call("POST", "/auth/invite/accept", {"token": token, "password": "another long one"},
                who=None, csrf=False)[0] == 410                                    # once
    steps = {s["id"]: s["state"] for s in auth_db.get_people_setup("ann")}
    assert steps == {k: "ok" for k in ("account", "chat", "calendar", "definition", "room", "agent", "household")}
    logged = caplog.text + json.dumps(auth_db.list_audit(limit=50))
    assert token not in logged and "a long password" not in logged


def test_an_expired_or_renewed_link_cannot_be_used(api):
    code, out, _ = invite(api)
    call = api["call"]
    code, new, _ = call("POST", "/api/v2/people/ann/invite", {"days": 3})
    assert code == 200 and new["token"] != out["token"]
    assert call("POST", "/auth/invite/check", {"token": out["token"]}, who=None, csrf=False)[0] == 410
    assert call("POST", "/auth/invite/check", {"token": new["token"]}, who=None, csrf=False)[0] == 200
    with auth_db._conn() as conn:
        conn.execute("UPDATE invites SET expires_at = ?", (int(time.time()) - 1,))
    assert call("POST", "/auth/invite/accept", {"token": new["token"], "password": "a long password"},
                who=None, csrf=False)[0] == 410
    code, page, _ = call("GET", "/api/v2/people/ann")
    assert page["state"] == "invite_expired"
    assert call("DELETE", "/api/v2/people/ann/invite")[0] == 200
    assert auth_db.get_user_by_username("ann") is None


def test_a_taken_chat_name_is_refused_before_anything_is_made(api):
    api["world"]["mx"].taken.add("ann")
    code, out, _ = invite(api)
    assert code == 400 and "chat server" in out["message"] and auth_db.get_user_by_username("ann") is None
    assert invite(api, chat=False)[0] == 201


def test_a_removed_person_is_signed_out_at_once_and_can_be_restored(api):
    call = api["call"]
    token = invite(api)[1]["token"]
    call("POST", "/auth/invite/accept", {"token": token, "password": "a long password"}, who=None, csrf=False)
    ann = auth_db.get_user_by_username("ann")
    assert call("GET", "/api/v2/people/ann", who=ann)[0] == 200                       # their own page
    code, page, _ = call("GET", "/api/v2/people/ann")
    assert page["removable"] is True and page["visibility"]["own_agent"] == "ann"
    assert call("GET", "/api/v2/people/greg")[1]["removable"] is False              # yourself
    assert call("POST", "/api/v2/people/ann/remove", {"confirm": "nope"})[0] == 400
    assert call("POST", "/api/v2/people/ann/remove", {"confirm": "ann"})[0] == 202
    assert call("GET", "/api/v2/people/ann", who=ann)[0] == 401                       # their session stops now
    code, listing, _ = call("GET", "/api/v2/people")
    assert [p["username"] for p in listing["removed"]] == ["ann"]
    assert "ann" not in [p["username"] for p in listing["people"]]
    assert call("GET", "/api/v2/people/ann")[1]["removed"]["by"] == "Greg"
    assert call("POST", "/api/v2/people/ann/restore", {})[0] == 202
    assert call("GET", "/api/v2/people/ann", who=ann)[0] == 200
    actions = [a["action"] for a in call("GET", "/api/v2/people/audit")[1]["actions"]]
    assert actions[:4] == ["person_restored", "person_removed", "invite_accepted", "person_invited"]


def test_the_invite_page_keeps_the_secret_out_of_addresses(api):
    code, html, _ = api["call"]("GET", "/invite", who=None, csrf=False)
    assert code == 200 and "history.replaceState" in html and "location.hash" in html
    assert "/auth/invite/accept" in html and "no-referrer" in html


def test_deleting_a_calendar_for_good_also_drops_its_read_only_key(tmp_path, monkeypatch):
    class Rt:
        env = {"XDG_CONFIG_HOME": str(tmp_path)}
    host = ch.CalendarHost(Rt())
    monkeypatch.setattr(host, "_setup", lambda add=None, **extra: [])
    path = ch.readers_file(Rt.env)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ann": "a", "greg": "g"}))
    host.purge_person("ann")
    assert json.loads(path.read_text()) == {"greg": "g"} and path.stat().st_mode & 0o777 == 0o600
