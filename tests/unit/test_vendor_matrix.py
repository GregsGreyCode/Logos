"""M0: the Matrix files the dashboard serves are exactly the pinned, checksummed build."""
import hashlib
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[2] / "gateway" / "ui" / "vendor" / "matrix"


def test_every_vendored_file_matches_its_manifest_checksum():
    lines = [l for l in (VENDOR / "MANIFEST").read_text().splitlines() if l and not l.startswith("#")]
    listed = {}
    for line in lines:
        digest, name = line.split(None, 1)
        listed[name.lstrip("*").removeprefix("./")] = digest
    on_disk = {str(p.relative_to(VENDOR)) for p in VENDOR.rglob("*") if p.is_file() and p.name != "MANIFEST"}
    assert set(listed) == on_disk, "files added or missing: run scripts/logos-vendor-matrix"
    for name, digest in listed.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_versions_are_the_pinned_ones():
    head = (VENDOR / "MANIFEST").read_text().splitlines()[0]
    assert "matrix-js-sdk 45.1.0" in head and "matrix-sdk-crypto-wasm 18.9.2" in head
