"""LOGOS_HOME paths, config.yaml loading and .env helpers for the gateway.

This is the small set of host-side config helpers the gateway needs. It used
to live in the vendored ``logos_cli.config``; the gateway must not import
``logos_cli`` (the CLI imports the gateway, not the other way round).
"""

from __future__ import annotations

import copy
import os
import platform
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_IS_WINDOWS = platform.system() == "Windows"
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# =============================================================================
# Paths
# =============================================================================

def get_logos_home() -> Path:
    """Return the Logos home directory.

    ``LOGOS_HOME`` wins, then ``~/.logos``.
    """
    return Path(
        os.getenv("LOGOS_HOME")
        or str(Path.home() / ".logos")
    )


# Legacy name still used throughout the gateway.
get_hermes_home = get_logos_home


def get_config_path() -> Path:
    """Path of the runtime config file."""
    return get_logos_home() / "config.yaml"


def get_env_path() -> Path:
    """Path of the .env file holding API keys."""
    return get_logos_home() / ".env"


def _secure_dir(path) -> None:
    """Owner-only access (0700). Best effort."""
    try:
        os.chmod(path, 0o700)
    except (OSError, NotImplementedError):
        pass


def _secure_file(path) -> None:
    """Owner-only read/write (0600). Best effort."""
    try:
        if os.path.exists(str(path)):
            os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


def ensure_logos_home() -> Path:
    """Create the Logos home directory structure with owner-only permissions."""
    home = get_logos_home()
    home.mkdir(parents=True, exist_ok=True)
    _secure_dir(home)
    for subdir in ("cron", "sessions", "logs", "memories"):
        d = home / subdir
        d.mkdir(parents=True, exist_ok=True)
        _secure_dir(d)
    return home


ensure_hermes_home = ensure_logos_home


# =============================================================================
# config.yaml
# =============================================================================

# Defaults for the sections the gateway itself reads through load_config().
DEFAULT_CONFIG: Dict[str, Any] = {
    "stt": {
        "provider": "local",  # "local" (faster-whisper) | "openai" (Whisper API)
        "local": {"model": "base"},
        "openai": {"model": "whisper-1"},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base*; *override* wins on conflict."""
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> Dict[str, Any]:
    """Load config-base.yaml (infra-managed) overlaid with config.yaml (runtime).

    Runtime wins on conflict. Missing or unreadable files are skipped.
    """
    ensure_logos_home()
    config = copy.deepcopy(DEFAULT_CONFIG)
    for path in (get_logos_home() / "config-base.yaml", get_config_path()):
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                config = _deep_merge(config, loaded)
        except Exception as e:
            print(f"Warning: Failed to load {path.name}: {e}")
    return config


# =============================================================================
# .env
# =============================================================================

def load_env() -> Dict[str, str]:
    """Parse KEY=VALUE lines from the .env file."""
    env_path = get_env_path()
    env_vars: Dict[str, str] = {}
    if env_path.exists():
        open_kw = {"encoding": "utf-8", "errors": "replace"} if _IS_WINDOWS else {}
        with open(env_path, **open_kw) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    env_vars[key.strip()] = value.strip().strip("\"'")
    return env_vars


def save_env_value(key: str, value: str) -> None:
    """Save or update a value in the .env file (atomic, owner-only)."""
    if not _ENV_VAR_NAME_RE.match(key):
        raise ValueError(f"Invalid environment variable name: {key!r}")
    value = value.replace("\n", "").replace("\r", "")
    ensure_logos_home()
    env_path = get_env_path()

    read_kw = {"encoding": "utf-8", "errors": "replace"} if _IS_WINDOWS else {}
    write_kw = {"encoding": "utf-8"} if _IS_WINDOWS else {}

    lines = []
    if env_path.exists():
        with open(env_path, **read_kw) as f:
            lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")

    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), suffix=".tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", **write_kw) as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _secure_file(env_path)

    os.environ[key] = value

    if not _IS_WINDOWS:
        try:
            os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


def get_env_value(key: str) -> Optional[str]:
    """Return a value from the process environment, else from the .env file."""
    if key in os.environ:
        return os.environ[key]
    return load_env().get(key)
