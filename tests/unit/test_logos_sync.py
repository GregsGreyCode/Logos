"""scripts/logos-sync: which brains the shared Syncthing container serves."""
import importlib.machinery, importlib.util, pathlib

_path = str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "logos-sync")
_loader = importlib.machinery.SourceFileLoader("logos_sync", _path)
sync = importlib.util.module_from_spec(importlib.util.spec_from_loader("logos_sync", _loader))
_loader.exec_module(sync)


def test_agents_come_from_brain_mounts_only():
    mounts = [
        {"Destination": "/var/syncthing/config", "Name": "logos-sync"},
        {"Destination": "/var/syncthing/brains/greg", "Name": "logos-brain-greg"},
        {"Destination": "/var/syncthing/brains/bere", "Name": "logos-brain-bere"},
        {"Destination": "/var/syncthing", "Name": "anonymous"},
    ]
    assert sync.agents_from_mounts(mounts) == ["bere", "greg"]


def test_no_mounts_means_no_agents():
    assert sync.agents_from_mounts([]) == []
