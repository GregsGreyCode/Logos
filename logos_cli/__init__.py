"""
Logos CLI — control-plane command-line interface for the Logos platform.
"""

# Read the canonical version from the installed package metadata so the
# CLI always reports what pyproject.toml declares. Previously this file
# hardcoded ``__version__ = "1.0.1"`` which drifted ~30 patch releases
# behind reality the moment we stopped updating two places in lockstep.
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("logos")
except PackageNotFoundError:
    # Fall back to reading pyproject.toml directly (editable installs
    # occasionally miss metadata depending on the installer version).
    __version__ = "1.0.1"
    try:
        import tomllib  # Python 3.11+
        from pathlib import Path

        _pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if _pyproject.exists():
            with _pyproject.open("rb") as _fh:
                __version__ = tomllib.load(_fh).get("project", {}).get("version", "unknown")
    except Exception:
        pass

# Release date was never kept in sync with the version anyway. Drop it —
# the version number is the source of truth; the git tag has the date
# for anyone who needs it.
__release_date__ = ""
