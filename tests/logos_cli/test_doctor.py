"""Tests for the reduced ``logos doctor``."""

import sqlite3

import pytest

import logos_cli.doctor as doctor


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGOS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return tmp_path / "home"


def _fake_tools(monkeypatch, which, outputs):
    """which: {name: path|None}; outputs: {argv0: (code, text)}."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: which.get(name))
    monkeypatch.setattr(doctor, "_run", lambda cmd, timeout=10.0: outputs[cmd[0]])


def test_python_check_ok():
    assert doctor.check_python()[0] == doctor.OK


def test_python_check_fails_on_old_interpreter(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 9, 1, "final", 0))
    status, _, detail = doctor.check_python()
    assert status == doctor.FAIL and "3.11" in detail


def test_docker_missing(monkeypatch):
    _fake_tools(monkeypatch, {}, {})
    assert doctor.check_docker()[0] == doctor.FAIL


def test_docker_daemon_unreachable(monkeypatch):
    _fake_tools(monkeypatch, {"docker": "/usr/bin/docker"},
                {"docker": (1, "Cannot connect to the Docker daemon")})
    status, _, detail = doctor.check_docker()
    assert status == doctor.FAIL and "not reachable" in detail


def test_docker_reachable(monkeypatch):
    _fake_tools(monkeypatch, {"docker": "/usr/bin/docker"}, {"docker": (0, "28.1.1")})
    status, _, detail = doctor.check_docker()
    assert status == doctor.OK and "28.1.1" in detail


def test_openshell_missing(monkeypatch):
    _fake_tools(monkeypatch, {}, {})
    assert doctor.check_openshell()[0] == doctor.FAIL


def test_openshell_expected_version(monkeypatch):
    _fake_tools(monkeypatch, {"openshell": "/usr/bin/openshell"},
                {"/usr/bin/openshell": (0, f"openshell {doctor.EXPECTED_OPENSHELL}")})
    status, _, detail = doctor.check_openshell()
    assert status == doctor.OK and doctor.EXPECTED_OPENSHELL in detail


def test_openshell_other_version_warns(monkeypatch):
    _fake_tools(monkeypatch, {"openshell": "/usr/bin/openshell"},
                {"/usr/bin/openshell": (0, "openshell 0.0.26")})
    status, _, detail = doctor.check_openshell()
    assert status == doctor.WARN and "0.0.26" in detail


def test_logos_home_writable_creates_it(_home):
    assert not _home.exists()
    assert doctor.check_logos_home()[0] == doctor.OK
    assert _home.is_dir()
    assert list(_home.iterdir()) == []


def test_logos_home_not_writable(_home):
    _home.parent.mkdir(parents=True, exist_ok=True)
    _home.write_text("a file, not a directory")
    assert doctor.check_logos_home()[0] == doctor.FAIL


def test_auth_db_absent_is_ok(_home):
    status, _, detail = doctor.check_auth_db()
    assert status == doctor.OK and "not created yet" in detail


def test_auth_db_opens(_home):
    _home.mkdir(parents=True)
    conn = sqlite3.connect(_home / "auth.db")
    conn.execute("CREATE TABLE users (id TEXT)")
    conn.commit()
    conn.close()
    status, _, detail = doctor.check_auth_db()
    assert status == doctor.OK and "1 tables" in detail


def test_auth_db_corrupt_fails(_home):
    _home.mkdir(parents=True)
    (_home / "auth.db").write_bytes(b"this is not a sqlite database at all" * 10)
    assert doctor.check_auth_db()[0] == doctor.FAIL


def test_run_doctor_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "CHECKS", [lambda: (doctor.OK, "A", "fine")])
    assert doctor.run_doctor() == 0
    monkeypatch.setattr(doctor, "CHECKS", [lambda: (doctor.WARN, "A", "meh")])
    assert doctor.run_doctor() == 0
    monkeypatch.setattr(doctor, "CHECKS", [lambda: (doctor.OK, "A", "fine"),
                                           lambda: (doctor.FAIL, "B", "broken")])
    assert doctor.run_doctor() == 1
    out = capsys.readouterr().out
    assert "B: broken" in out and "1 check(s) failed" in out
