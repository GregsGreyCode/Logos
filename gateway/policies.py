"""Per-agent network policy preset management.

Python port of the relevant half of NemoClaw's
``knowledge-repos/NemoClaw/src/lib/policies.ts``. This module owns the
life cycle of network policy presets for Logos agents — discovery,
loading, merging into the baseline, persisting per-agent selections
to the DB, and pushing the merged effective policy to a running
OpenShell sandbox via ``openshell policy set``.

The baseline policy lives at
``gateway/policies/openshell_default.yaml`` and contains only what
every Logos sandbox needs (inference.local + DNS). Presets live in
``gateway/policies/presets/*.yaml`` and are opt-in per-agent
additions that widen the network policy for specific integrations
(github, slack, telegram, discord, huggingface, pypi, etc.).

Usage
─────

    from gateway import policies as gp

    # Discover presets for the Tools editor UI
    for p in gp.list_presets():
        print(p.name, p.description)

    # Apply a preset to an agent — writes the DB AND pushes the
    # merged effective policy to the running sandbox so it takes
    # effect without waiting for a respawn.
    gp.apply_preset(agent_id, "github")

    # List what's currently applied to an agent
    applied = gp.get_applied_presets(agent_id)  # -> ["github", "slack"]

    # Compute the merged effective policy (baseline + applied
    # presets). Used by OpenShellExecutor.spawn() to write the
    # initial policy file, and by the policy-set path to re-push
    # at runtime after a preset change.
    effective = gp.compute_effective_policy(agent_id)  # dict

Design notes
────────────

* **DB is the source of truth for applied presets** — the list is
  stored as a JSON array in ``agents.applied_presets`` (added in
  db.py v10 migration). ``openshell policy set`` is a side effect;
  if pushing to the live sandbox fails (e.g. sandbox not yet
  spawned), the DB still reflects user intent and ``spawn()`` will
  pick it up at next sandbox (re-)creation.

* **Merge semantics**: preset entries override baseline entries on
  name collision. This matches NemoClaw's ``mergePresetIntoPolicy``
  at ``src/lib/policies.ts:166-221``. Presets SHOULD use unique
  network_policy names to avoid stomping the baseline — by
  convention the preset file's top-level key matches the preset
  name, so ``github.yaml`` contributes a ``network_policies.github``
  entry, not ``network_policies.inference_local``.

* **Failing to push is non-fatal**. If the sandbox doesn't exist
  yet, or openshell is unreachable, the DB change still happens and
  a warning is logged. The next spawn picks up the effective
  policy from the DB. Callers that need strict push semantics can
  inspect the log or call ``push_effective_policy(agent_id)``
  directly and wrap in their own error handling.

* **This module does NOT touch approval_requests or action_policies**.
  Those are the *application*-layer policy surface (per-tool-call
  gating) and live in ``gateway/auth/db.py``. Network policy
  presets are the *infrastructure*-layer surface (per-host egress).
  See MISSING.md M10's two-layer STAMP model for the division.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Canonical locations — computed once at import. ``gateway/policies/``
# is the directory (this file is ``gateway/policies.py`` and the
# baseline YAML is ``gateway/policies/openshell_default.yaml``).
_POLICIES_DIR = Path(__file__).parent / "policies"
_PRESETS_DIR = _POLICIES_DIR / "presets"
_BASELINE_PATH = _POLICIES_DIR / "openshell_default.yaml"


# ── Public data types ──────────────────────────────────────────────────────

@dataclass
class PresetInfo:
    """Metadata for a single preset file.

    Returned by :func:`list_presets` for use in the Tools editor UI
    and CLI listings. The ``path`` is informational — most callers
    should use :func:`load_preset` to get parsed contents rather
    than reading the file themselves.
    """

    name: str
    description: str
    path: Path


class PresetNotFound(Exception):
    """Raised when a preset name doesn't match any file in
    ``gateway/policies/presets/``."""


class PolicyMergeError(Exception):
    """Raised when the baseline policy can't be loaded or a preset
    can't be merged into it. Callers should treat this as a config
    bug — the baseline should always parse, and presets should always
    conform to the expected YAML shape."""


# ── Preset discovery ──────────────────────────────────────────────────────


def list_presets() -> List[PresetInfo]:
    """Return metadata for every preset in ``gateway/policies/presets/``.

    Sorted alphabetically by filename for UI stability. Files that
    fail to parse or lack the ``preset:`` metadata header are logged
    and skipped — a single broken preset should never hide all the
    others.
    """
    if not _PRESETS_DIR.exists():
        logger.debug("list_presets: %s does not exist", _PRESETS_DIR)
        return []
    out: List[PresetInfo] = []
    for path in sorted(_PRESETS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("list_presets: failed to parse %s: %s", path.name, exc)
            continue
        header = (data or {}).get("preset") or {}
        name = header.get("name")
        if not name:
            logger.warning(
                "list_presets: %s has no preset.name header — skipping", path.name,
            )
            continue
        out.append(PresetInfo(
            name=str(name),
            description=str(header.get("description", "")),
            path=path,
        ))
    return out


def load_preset(name: str) -> Dict[str, Any]:
    """Return the parsed YAML for preset ``name``.

    Raises :class:`PresetNotFound` if no matching ``.yaml`` file exists
    OR if the name is malformed (directory traversal guard).
    """
    # Directory traversal / injection guard. Preset names come from
    # the UI or from DB-stored applied lists, so treating them as
    # untrusted is the right default even though the attack surface
    # is narrow today.
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PresetNotFound(f"Invalid preset name: {name!r}")
    path = _PRESETS_DIR / f"{name}.yaml"
    if not path.exists():
        raise PresetNotFound(f"Preset not found: {name}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PresetNotFound(f"Preset {name} failed to parse: {exc}") from exc
    return data or {}


def extract_preset_network_policies(preset_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return just the ``network_policies`` dict from a parsed preset.

    Presets have two top-level sections: ``preset:`` (metadata) and
    ``network_policies:`` (the actual rules). For merging we only
    care about the latter — the header is used by the UI for
    labels/descriptions but is not part of the effective policy.
    """
    np = preset_data.get("network_policies")
    if not isinstance(np, dict):
        return {}
    return dict(np)


# ── Baseline + merge ──────────────────────────────────────────────────────


def load_baseline() -> Dict[str, Any]:
    """Return the parsed baseline ``openshell_default.yaml``.

    Raises :class:`PolicyMergeError` if the file is missing or
    malformed. This is a hard failure because every effective
    policy depends on the baseline; a broken baseline breaks all
    sandbox spawns.
    """
    try:
        text = _BASELINE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyMergeError(
            f"Baseline policy missing at {_BASELINE_PATH}: {exc}"
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyMergeError(
            f"Baseline policy {_BASELINE_PATH} failed to parse: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PolicyMergeError(
            f"Baseline policy {_BASELINE_PATH} is not a YAML mapping"
        )
    return data


def merge_presets_into_baseline(preset_names: List[str]) -> Dict[str, Any]:
    """Merge the named presets into the baseline and return the result.

    Preset ``network_policies`` entries are added to the baseline's
    ``network_policies``. On name collision the preset overrides the
    baseline entry — same semantics as NemoClaw's
    ``mergePresetIntoPolicy`` at ``src/lib/policies.ts:166-221``.
    Non-network sections (``filesystem_policy``, ``process``,
    ``landlock``) are preserved from the baseline as-is.

    Unknown preset names in the list are logged and silently skipped
    so a stale DB entry for a removed preset doesn't break the
    merge. :func:`set_applied_presets` validates at write time so
    this shouldn't normally happen.
    """
    merged = load_baseline()
    base_np = merged.setdefault("network_policies", {})
    if not isinstance(base_np, dict):
        raise PolicyMergeError(
            "Baseline policy network_policies is not a YAML mapping"
        )

    for preset_name in preset_names:
        try:
            preset_data = load_preset(preset_name)
        except PresetNotFound as exc:
            logger.warning(
                "merge_presets_into_baseline: skipping unknown preset %r: %s",
                preset_name, exc,
            )
            continue
        preset_np = extract_preset_network_policies(preset_data)
        for key, value in preset_np.items():
            if key in base_np:
                logger.info(
                    "merge_presets_into_baseline: preset %r overrides "
                    "baseline network_policies[%r]",
                    preset_name, key,
                )
            base_np[key] = value

    merged["network_policies"] = base_np
    return merged


def compute_effective_policy(agent_id: str) -> Dict[str, Any]:
    """Return the merged baseline + applied presets for an agent.

    Reads the applied preset list from the DB and merges everything
    into a single policy dict suitable for writing to a temp file
    and passing to ``openshell policy set --policy <file>``.
    """
    presets = get_applied_presets(agent_id)
    return merge_presets_into_baseline(presets)


# ── Applied presets (DB-backed, JSON-aware wrappers) ──────────────────────


def get_applied_presets(agent_id: str) -> List[str]:
    """Return the list of preset names currently applied to an agent.

    Thin wrapper over ``gateway.auth.db.get_agent_applied_presets``
    that lives here so UI code can import everything it needs from
    ``gateway.policies`` without also reaching into the auth module.
    """
    from gateway.auth import db as auth_db
    return auth_db.get_agent_applied_presets(agent_id)


def set_applied_presets(agent_id: str, presets: List[str]) -> List[str]:
    """Replace the applied preset list for an agent.

    Validates every entry exists in the presets directory before
    writing — unknown names are dropped with a warning. Returns the
    cleaned list actually written to the DB, so callers can tell
    which entries were accepted and which were silently dropped.
    """
    known = {p.name for p in list_presets()}
    clean: List[str] = []
    seen: set[str] = set()
    for name in presets or []:
        s = str(name)
        if s in seen:
            continue
        seen.add(s)
        if s not in known:
            logger.warning(
                "set_applied_presets(%s): dropping unknown preset %r "
                "(not in %s)", agent_id, s, _PRESETS_DIR,
            )
            continue
        clean.append(s)

    from gateway.auth import db as auth_db
    auth_db.set_agent_applied_presets(agent_id, clean)
    return clean


# ── Apply / remove (the main entry points for the UI) ────────────────────


def apply_preset(agent_id: str, preset_name: str) -> Dict[str, Any]:
    """Add ``preset_name`` to an agent's applied set and push the
    merged effective policy to the running sandbox.

    Idempotent — applying an already-applied preset is a no-op on
    the DB but still re-pushes to openshell (in case the running
    sandbox's policy drifted from the DB state).

    Returns the merged effective policy dict that was pushed.

    Raises:
        PresetNotFound: if ``preset_name`` is not a known preset
        PolicyMergeError: if the baseline policy can't be loaded
    """
    # Validate the preset exists before touching the DB
    load_preset(preset_name)

    current = get_applied_presets(agent_id)
    if preset_name not in current:
        current.append(preset_name)
        set_applied_presets(agent_id, current)
        logger.info(
            "apply_preset(%s, %s): added to applied list (now %s)",
            agent_id, preset_name, current,
        )
    else:
        logger.info(
            "apply_preset(%s, %s): already applied, re-pushing",
            agent_id, preset_name,
        )

    effective = compute_effective_policy(agent_id)
    push_effective_policy(agent_id, effective=effective)
    return effective


def remove_preset(agent_id: str, preset_name: str) -> Dict[str, Any]:
    """Remove ``preset_name`` from an agent's applied set and push
    the updated effective policy to the running sandbox.

    Idempotent — removing a preset that isn't applied is a no-op
    for the DB but still re-pushes the baseline to openshell.

    Returns the merged effective policy dict that was pushed.
    """
    current = get_applied_presets(agent_id)
    if preset_name in current:
        current.remove(preset_name)
        set_applied_presets(agent_id, current)
        logger.info(
            "remove_preset(%s, %s): removed (now %s)",
            agent_id, preset_name, current,
        )
    else:
        logger.info(
            "remove_preset(%s, %s): not applied, re-pushing baseline",
            agent_id, preset_name,
        )

    effective = compute_effective_policy(agent_id)
    push_effective_policy(agent_id, effective=effective)
    return effective


# ── Push to the running sandbox ───────────────────────────────────────────


def push_effective_policy(
    agent_id: str,
    effective: Optional[Dict[str, Any]] = None,
) -> bool:
    """Write the effective policy to a tempfile and call
    ``openshell policy set --policy <tempfile> --wait <sandbox>``.

    Best-effort: returns True on success, False (with a warning log)
    on any failure including "sandbox doesn't exist yet" and
    "openshell CLI unreachable". Callers should NOT treat a False
    return as a hard error — the DB is the source of truth for
    applied presets, and :func:`gateway.executors.openshell.
    OpenShellExecutor.spawn` re-reads the DB at next spawn so
    a missed runtime push is recovered automatically.

    Args:
        agent_id: the agent whose policy we're pushing
        effective: optional pre-computed effective policy (to avoid
            recomputing if the caller already has it). When None,
            computed from the DB state.

    Returns:
        True if the openshell CLI accepted the policy, False
        otherwise. False does NOT mean the DB state is wrong —
        only that the live sandbox didn't get the update this time.
    """
    # Import inside the function to avoid a module-level import cycle:
    # gateway.executors.openshell → gateway.auth.db → (would eventually
    # pull in gateway.policies for the spawn flow) → gateway.executors.
    from gateway.auth import db as auth_db
    from gateway.executors.openshell import (
        _openshell,
        _sanitize_sandbox_name,
        _load_state,
    )

    agent = auth_db.get_agent(agent_id)
    if not agent:
        logger.warning(
            "push_effective_policy(%s): agent not found in DB — skipping",
            agent_id,
        )
        return False

    sandbox_name = _sanitize_sandbox_name(f"hermes-{agent.get('name', '')}")
    if effective is None:
        effective = compute_effective_policy(agent_id)

    # Resolve which OpenShell gateway hosts this sandbox. Same lookup
    # spawn() uses at ``openshell.py:_resolve_sandbox_gateway``, but
    # inlined here to avoid pulling in the entire resolver chain for
    # a simple "which -g flag do I pass" question.
    target_gw: Optional[str] = None
    try:
        for inst in _load_state():
            if (inst.get("sandbox_name") == sandbox_name
                    or inst.get("worker_id") == sandbox_name):
                target_gw = inst.get("openshell_name")
                break
    except Exception as exc:
        logger.warning(
            "push_effective_policy(%s): state file lookup failed: %s",
            sandbox_name, exc,
        )

    if not target_gw:
        logger.info(
            "push_effective_policy(%s): no live sandbox in state file — "
            "policy will apply on next spawn",
            sandbox_name,
        )
        return False

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="logos-policy-", delete=False,
        encoding="utf-8",
    )
    try:
        # ``sort_keys=False`` preserves the baseline's section order
        # (filesystem_policy → landlock → process → network_policies)
        # for readable debug output. ``default_flow_style=False``
        # forces block style so the diff against the on-disk baseline
        # is visually comparable.
        yaml.safe_dump(
            effective, tmp,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        tmp.close()

        try:
            _openshell(
                "policy", "set",
                "--policy", tmp.name,
                "--wait", sandbox_name,
                gateway=target_gw,
                check=True,
                timeout=60.0,
            )
            logger.info(
                "push_effective_policy(%s): applied %d presets in gateway %s",
                sandbox_name, len(get_applied_presets(agent_id)), target_gw,
            )
            return True
        except Exception as exc:
            logger.warning(
                "push_effective_policy(%s): `openshell policy set` failed "
                "(non-fatal, will apply on next spawn): %s",
                sandbox_name, exc,
            )
            return False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ── Utility: preset-aware baseline path (for spawn-time merge) ────────────


def write_effective_policy_to_tempfile(agent_id: str) -> Path:
    """Compute the effective policy for an agent, serialize it, and
    return the tempfile path.

    Used by :class:`gateway.executors.openshell.OpenShellExecutor` at
    spawn time when applying the initial policy — the executor reads
    ``agents.applied_presets`` and writes a merged baseline+presets
    policy for ``openshell sandbox create --policy <file>``.

    The caller is responsible for unlinking the returned path after
    ``openshell sandbox create`` consumes it. Use a try/finally so
    the cleanup happens on error paths too.
    """
    effective = compute_effective_policy(agent_id)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="logos-effective-", delete=False,
        encoding="utf-8",
    )
    try:
        yaml.safe_dump(
            effective, tmp,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        tmp.close()
        return Path(tmp.name)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
