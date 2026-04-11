"""
Debug commands for the logos CLI.

Provides a unified view over the structured JSON-lines log file written by
the gateway (``~/.logos/logs/unified.jsonl``). Designed as the fix for the
observability gap documented in docs/MISSING.md M6 — during a long debugging
session on 2026-04-11 we couldn't find where Python stdlib logger output was
going because the CLI spinner in ``logos gateway run`` masked stdout. This
module makes that output easy to read at ``logos debug tail``.

Current subcommands:
    logos debug tail    — pretty-print or follow the unified log file

Future candidates:
    logos debug grep    — structured filter across the file (like tail --filter
                          but one-shot, optimised for pipes)
    logos debug trace   — gather all events for a given task_id/session_id
                          and display as an ordered timeline
    logos debug events  — emit a live event stream over stdout for tooling
                          consumption (jq-friendly)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

# ── ANSI colors (no rich/colorama dep — keep this module import-cheap) ───────

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
    "bright_red": "\033[91m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_magenta": "\033[95m",
    "bright_cyan": "\033[96m",
}


def _c(color: str, text: str, enabled: bool = True) -> str:
    """Wrap text in ANSI color codes (or passthrough if colors disabled)."""
    if not enabled:
        return text
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"


# Level → color mapping. Mirrors the convention of most log viewers.
_LEVEL_COLORS = {
    "DEBUG": "gray",
    "INFO": "cyan",
    "WARNING": "yellow",
    "WARN": "yellow",
    "ERROR": "red",
    "CRITICAL": "bright_red",
    "FATAL": "bright_red",
}

# Level → numeric for comparison filtering. Mirrors logging module constants.
_LEVEL_NUM = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
    "FATAL": 50,
}


# ── Configuration ────────────────────────────────────────────────────────────


def _default_log_path() -> Path:
    """Resolve the unified log file path, respecting $LOGOS_HOME / $HERMES_HOME."""
    home = (
        os.environ.get("LOGOS_HOME")
        or os.environ.get("HERMES_HOME")
        or str(Path.home() / ".logos")
    )
    return Path(home) / "logs" / "unified.jsonl"


# ── Time parsing ─────────────────────────────────────────────────────────────

_SINCE_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|m|h|d)?$")


def _parse_since(since: str) -> float:
    """Parse a ``--since`` value into a UNIX timestamp cutoff.

    Accepts:
      - Relative offsets: ``5s``, ``30m``, ``2h``, ``1d`` (or bare integer = seconds)
      - Absolute timestamps: ``2026-04-11T14:00``, ``2026-04-11 14:00:00``
      - Epoch seconds: ``1775917149`` (anything that looks like a plain int >= 10^9)
    """
    since = since.strip()
    if not since:
        raise ValueError("empty --since value")

    # Plain integer — if huge, assume epoch seconds
    try:
        val = float(since)
        if val >= 1_000_000_000:  # > 2001-09-09, clearly an epoch timestamp
            return val
    except ValueError:
        pass

    # Relative offset like 5m, 2h, 30s, 1d
    m = _SINCE_RE.match(since)
    if m:
        amount = float(m.group(1))
        unit = m.group(2) or "s"
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return time.time() - amount * mult

    # ISO-ish timestamp
    import datetime
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.datetime.strptime(since, fmt)
            return dt.timestamp()
        except ValueError:
            continue

    raise ValueError(
        f"could not parse --since value {since!r}. "
        f"Use a relative offset (e.g. 5m, 2h, 1d), an epoch timestamp, "
        f"or an ISO datetime like 2026-04-11T14:00"
    )


# ── Filter matching ──────────────────────────────────────────────────────────


@dataclass
class _Filter:
    """A single ``--filter key=value`` constraint on log records."""
    key: str
    value: str
    negate: bool = False  # True for key!=value

    def matches(self, record: Dict[str, Any]) -> bool:
        actual = record.get(self.key)
        if actual is None:
            # Missing key never matches a positive filter, always matches a negative
            return self.negate
        actual_str = str(actual)
        # Glob-style wildcards for *.value / prefix* / *substr*
        if "*" in self.value:
            pattern = "^" + re.escape(self.value).replace(r"\*", ".*") + "$"
            match = re.match(pattern, actual_str) is not None
        else:
            match = actual_str == self.value
        return (not match) if self.negate else match


def _parse_filter(raw: str) -> _Filter:
    """Parse ``key=value`` or ``key!=value`` into a _Filter."""
    if "!=" in raw:
        key, value = raw.split("!=", 1)
        return _Filter(key=key.strip(), value=value.strip(), negate=True)
    if "=" in raw:
        key, value = raw.split("=", 1)
        return _Filter(key=key.strip(), value=value.strip())
    raise ValueError(
        f"invalid --filter {raw!r}. "
        f"Use key=value or key!=value (e.g. task_id=abc123, level!=DEBUG)"
    )


# ── Record pretty printer ────────────────────────────────────────────────────


_CORRELATION_KEYS = ("session_id", "task_id", "user_id", "worker_id", "chat_id")

# Standard fields already rendered in the main line; extras go into a dim suffix
_RENDERED = frozenset({
    "ts", "level", "logger", "msg", "source", "pid", "exc", *_CORRELATION_KEYS,
})


def _format_record(record: Dict[str, Any], *, colors: bool = True) -> str:
    """Render one log record as a colored single-line string."""
    import datetime

    ts = record.get("ts", 0.0)
    try:
        ts_str = datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S.%f")[:-3]
    except (TypeError, ValueError):
        ts_str = str(ts)

    level = str(record.get("level", "INFO")).upper()
    level_color = _LEVEL_COLORS.get(level, "cyan")
    source = str(record.get("source", "?"))
    logger_name = str(record.get("logger", "?"))
    msg = str(record.get("msg", ""))

    # Compact correlation IDs: only show the ones that are set (!= "-")
    corrs = []
    for k in _CORRELATION_KEYS:
        v = record.get(k, "-")
        if v and v != "-":
            # Short key form: task_id → t, user_id → u, etc.
            short = k[0]
            corrs.append(f"{short}={v}")
    corr_str = " ".join(corrs)

    # Extra fields beyond the standard ones (e.g. a caller used extra={...})
    extras = {
        k: v for k, v in record.items()
        if k not in _RENDERED and not k.startswith("_")
    }
    extras_str = ""
    if extras:
        try:
            extras_str = json.dumps(extras, default=str, ensure_ascii=False)
        except Exception:
            extras_str = str(extras)

    parts = [
        _c("gray", ts_str, colors),
        _c(level_color, f"{level:<7}", colors),
        _c("bright_blue", f"[{source}]", colors),
        _c("dim", logger_name, colors),
        msg,
    ]
    if corr_str:
        parts.append(_c("bright_magenta", f"({corr_str})", colors))
    if extras_str:
        parts.append(_c("dim", extras_str, colors))

    line = " ".join(p for p in parts if p)

    # Exception info on its own line(s) below, indented
    exc = record.get("exc")
    if exc:
        indented = "\n".join("    " + l for l in str(exc).splitlines())
        line = f"{line}\n{_c('red', indented, colors)}"

    return line


# ── File reader (tail + follow) ──────────────────────────────────────────────


def _iter_lines(path: Path, *, follow: bool, tail_lines: Optional[int]) -> Iterator[str]:
    """Yield lines from the log file, optionally following new appends.

    If ``tail_lines`` is set, emit only the last N lines first (like ``tail -n``).
    If ``follow`` is True, block and continue yielding new lines as they arrive
    (``tail -f``).
    """
    if not path.exists():
        print(
            f"{_c('yellow', 'warning:', True)} log file does not exist yet: {path}\n"
            f"  This file is created the first time the gateway runs with the\n"
            f"  M6 unified-log handler attached (gateway/run.py). If you haven't\n"
            f"  restarted the gateway since M6 landed, do so now.",
            file=sys.stderr,
        )
        if not follow:
            return
        # In follow mode, wait for the file to appear
        while not path.exists():
            time.sleep(0.5)

    # Read the last N lines (or all, if tail_lines is None)
    if tail_lines is not None:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # Simple last-N implementation: good enough for log files under ~100MB.
            # For pathologically large files we can switch to a seek-from-end reader
            # if it ever matters in practice.
            lines = f.readlines()
            for line in lines[-tail_lines:]:
                yield line.rstrip("\n")
            last_pos = f.tell()
    else:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line.rstrip("\n")
            last_pos = f.tell()

    if not follow:
        return

    # Follow mode: poll the file for new content.
    # Handles log rotation by re-opening if the inode changes.
    f = path.open("r", encoding="utf-8", errors="replace")
    f.seek(last_pos)
    last_inode = path.stat().st_ino
    try:
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
                continue
            time.sleep(0.25)
            # Check for rotation
            try:
                new_inode = path.stat().st_ino
            except FileNotFoundError:
                continue
            if new_inode != last_inode:
                f.close()
                f = path.open("r", encoding="utf-8", errors="replace")
                last_inode = new_inode
    except KeyboardInterrupt:
        pass
    finally:
        f.close()


# ── Main command entry point ─────────────────────────────────────────────────


def run_tail(args) -> int:
    """Execute ``logos debug tail``. Returns an exit code."""
    path = Path(args.file) if args.file else _default_log_path()

    # Build filter set
    filters: List[_Filter] = []
    for raw in (args.filter or []):
        try:
            filters.append(_parse_filter(raw))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    since_ts: Optional[float] = None
    if args.since:
        try:
            since_ts = _parse_since(args.since)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    min_level = _LEVEL_NUM.get(args.level.upper()) if args.level else None

    # Colors: auto-detect unless explicitly disabled
    colors = sys.stdout.isatty() if args.color == "auto" else (args.color == "always")

    def passes(rec: Dict[str, Any]) -> bool:
        if min_level is not None:
            level = _LEVEL_NUM.get(str(rec.get("level", "INFO")).upper(), 20)
            if level < min_level:
                return False
        if since_ts is not None:
            ts = rec.get("ts", 0.0)
            try:
                if float(ts) < since_ts:
                    return False
            except (TypeError, ValueError):
                pass
        for f in filters:
            if not f.matches(rec):
                return False
        return True

    count = 0
    malformed = 0
    try:
        for raw_line in _iter_lines(
            path,
            follow=args.follow,
            tail_lines=args.lines if not args.all else None,
        ):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                malformed += 1
                if args.raw:
                    print(raw_line)
                continue

            if not passes(record):
                continue

            if args.raw:
                print(raw_line)
            else:
                print(_format_record(record, colors=colors))
            count += 1
    except BrokenPipeError:
        # Expected when piping into `head` / `less` that exits first
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0

    if malformed and not args.raw:
        print(
            f"{_c('yellow', 'note:', True)} skipped {malformed} malformed line(s). "
            f"Re-run with --raw to see them verbatim.",
            file=sys.stderr,
        )

    return 0


def debug_command(args) -> int:
    """Dispatch entry point for ``logos debug <subcommand>``."""
    sub = getattr(args, "debug_command", None)
    if sub == "tail":
        return run_tail(args)
    # Default: print help
    print(
        "logos debug — observability commands\n"
        "\n"
        "Subcommands:\n"
        "  tail    Pretty-print the unified structured log\n"
        "\n"
        "Run `logos debug tail --help` for options.",
        file=sys.stderr,
    )
    return 2
