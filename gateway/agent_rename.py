"""Agents renaming themselves (Greg, 28 Nov): an agent picks a name in the chat; its person confirms it in Logos.

An agent calls its ``set_my_name`` tool (agent plugin ``logos-rename``) with the new name and, for an
agent on the house speaker, the name to wake it by. The plugin posts it here, on the one dashboard
path its sandbox may reach (``agent_rename`` in its network policy), signed with the agent's own
token (worked out from its name under the alert key, like gateway/speaker_say.py)::

    POST /api/v2/agent-rename/ask  {agent, token, name, wake_name?}
    -> {"asked": true, "tell": "..."}  or  {"asked": false, "reason": "...", "tell": "..."}

Nothing is renamed then. Logos checks the name with the same rules as Agents › Settings
(:func:`gateway.agent_settings_api.set_names`: 1-40 ordinary characters; a speaker name of one or
two words; never what another agent or a person is called, never another agent's wake word) and
asks the agent's people: a **Needs you** item in Notifications ("Finn wants to be called Nova",
Confirm / Not now) and a phone alert. Only a person's Confirm renames it::

    POST /api/v2/agent-rename/{rid}/{confirm|dismiss}       one of the agent's own people (owners)

Why the confirmation and not the person's "yes" in the chat: Logos never sees the chat (it is end
to end encrypted, and the agent's account of it is what a prompt injection would forge). The soul
rule (souls/_shared/name.md) says to call the tool only when the person asked or agreed; the
confirmation is what makes a forged request harmless: it shows up, nothing changes.

Confirm applies it everywhere at once (:func:`apply_live`, shared with Agents › Settings): the
private overlay (display_name, speaker_name, speaker_wake), the agent's Matrix display name, the
wake words (names.json, which logos-wake reloads; the old name stops waking it), the dashboard's
caches, the name section of the running SOUL.md (used from the agent's next conversation) and a
note the plugin adds to the agent's next turns (``/opt/data/logos-name.json``), so its next reply
already knows. Every ask, confirmation and refusal is in the audit log; never anything but names.

Standard library only at the top: scripts/logos-agent loads this file by path (agent_runtime._sibling).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

PATH = "/api/v2/agent-rename/ask"
TOKEN_ENV = "LOGOS_RENAME_TOKEN"
URL_ENV = "LOGOS_RENAME_URL"
NAME_KEYS = ("display_name", "speaker_name", "speaker_wake")
STORE_FILE = "agent-renames.json"          # in ~/.config/logos, 0600
NOTE_FILE = "logos-name.json"              # in the agent's home volume, read by the plugin each turn
KEEP_S = 3 * 86400                         # an unanswered question goes away after three days
LOG_KEEP_S = 30 * 86400
PER_DAY = 3                                # asks per agent a day: a looping agent cannot flood its person
MARK = ("<!-- logos:name -->", "<!-- /logos:name -->")
_LOCK = threading.Lock()


# ── who may, and the token ───────────────────────────────────────────────────────────────────

def approvers(agent: Mapping[str, Any]) -> List[str]:
    """The agent's own people (``owners``), who confirm. As gateway/network_approvals.py."""
    owners = (agent or {}).get("owners")
    if isinstance(owners, str):
        owners = [owners]
    return [str(o).strip().lower() for o in owners or [] if str(o).strip()]


def uses_rename(agent: Mapping[str, Any]) -> bool:
    """Any sandboxed agent with people to confirm (``rename: false`` takes the tool away)."""
    a = agent or {}
    return a.get("rename", True) is not False and a.get("sandbox") is not False and bool(approvers(a))


def token_for(agent: str, env: Optional[Mapping[str, str]] = None, key: Optional[bytes] = None) -> str:
    if key is None:
        from gateway import alert_text
        key = alert_text.key(env)
    return hmac.new(key, f"agent-rename:{agent}".encode(), hashlib.sha256).hexdigest()


def valid(agent: str, token: str, env: Optional[Mapping[str, str]] = None) -> bool:
    return bool(agent) and bool(token) and hmac.compare_digest(token_for(agent, env), token)


# ── the name in its instructions ─────────────────────────────────────────────────────────────

def name_section(agent: Mapping[str, Any]) -> str:
    """What SOUL.md says the agent is called, between markers so a rename can replace it live."""
    shown = str(agent.get("display_name") or agent.get("name") or "")
    spoken = " ".join(str(agent.get("speaker_name") or "").split())
    line = f"In Logos you are called **{shown}**: in the chat, on the dashboard, in phone alerts."
    if spoken:
        line += f' At the house speaker people wake you with "Hey {spoken}".'
    return f"{MARK[0]}\n## Your name\n\n{line}\n{MARK[1]}\n"


def replace_section(soul: str, agent: Mapping[str, Any]) -> Optional[str]:
    """``soul`` with its name section brought up to date, or None when it has none (made before this)."""
    start, end = soul.find(MARK[0]), soul.find(MARK[1])
    if start < 0 or end < start:
        return None
    return soul[:start] + name_section(agent) + soul[end + len(MARK[1]):].lstrip("\n")


# ── the questions, kept on this machine ──────────────────────────────────────────────────────

def store_path(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config")
    return Path(base) / "logos" / STORE_FILE


def _empty() -> Dict[str, Any]:
    return {"requests": {}, "log": []}


def load(env: Mapping[str, str]) -> Dict[str, Any]:
    try:
        data = json.loads(store_path(env).read_text())
    except (OSError, ValueError):
        return _empty()
    out = _empty()
    for k in out:
        if isinstance(data, dict) and isinstance(data.get(k), type(out[k])):
            out[k] = data[k]
    return out


def save(env: Mapping[str, str], data: Mapping[str, Any]) -> None:
    path = store_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def _tidy(data: Dict[str, Any], now: float) -> None:
    for rid, r in list(data["requests"].items()):
        if now - float(r.get("ts") or 0) >= KEEP_S:
            data["requests"].pop(rid)
            data["log"].append({"ts": now, "agent": r["agent"], "decision": "expired", "by": "",
                                "display_name": r.get("display_name")})
    data["log"] = [l for l in data["log"] if now - float(l.get("ts") or 0) < LOG_KEEP_S]


def pending(env: Mapping[str, str], now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    with _LOCK:
        rows = load(env)["requests"].values()
    return sorted((r for r in rows if now - float(r.get("ts") or 0) < KEEP_S), key=lambda r: -float(r["ts"]))


def recent(env: Mapping[str, str], since: float) -> List[Dict[str, Any]]:
    with _LOCK:
        return [l for l in load(env)["log"] if float(l.get("ts") or 0) >= since]


# ── asking ───────────────────────────────────────────────────────────────────────────────────

ASKED_TELL = ("Asked. Logos has asked {who} to confirm the name {new} in Logos (Notifications, with a phone "
              "alert). Nothing changes until they tap Confirm: until then you are still {old}. Tell them so in "
              "your reply.{wake}")
NOT_YOURS_TELL = "You cannot rename yourself here: you have nobody to confirm it. Your people can rename you in Agents › Settings."
SAME_TELL = "That is already your name. Nothing was asked."
LIMIT_TELL = ("Not asked: you asked {n} times today already. Your person can still rename you in Agents › Settings; "
              "do not try again today.")


def check(runtime: Any, name: str, new_name: Any, wake_name: Any = None,
          people: Optional[Mapping[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """The names to ask about ({display_name, speaker_name?}) and notes for the agent, checked with the
    same rules as Agents › Settings, without keeping anything. Raises agent_settings.Invalid. Blocking."""
    from gateway import agent_settings as S, agent_settings_api as api
    plain, agent = runtime.load(name, settings=False), runtime.load(name)
    lock = api.locked(runtime, name)
    values: Dict[str, Any] = {"display_name": new_name}
    notes: List[str] = []
    speaks = api._speaks(plain)
    if wake_name not in (None, "") and not speaks:
        notes.append("You do not answer on the house speaker, so the wake name was left out.")
    elif speaks and not lock.get("speaker_name"):
        if wake_name not in (None, ""):
            values["speaker_name"] = wake_name
        elif S.name_key(str(new_name or "")) != S.name_key(str(agent.get("display_name") or name)):
            try:               # the new name wakes it too, when it can be one
                S.check_speaker_name(new_name, api._taken_wake_words(runtime, name, people))
                values["speaker_name"] = new_name
            except S.Invalid as e:
                notes.append(f"Your wake name stays \"{agent.get('speaker_name')}\" ({e}) Pass wake_name to change it.")
    new = json.loads(json.dumps(S.overlay(runtime.env, name)))
    changed = api.set_names(runtime, name, plain, agent, lock, new, values, people)
    if not changed:
        raise S.Invalid(SAME_TELL)
    out = {"display_name": str(new.get("display_name") or plain.get("display_name") or name)}
    spoken = str(new.get("speaker_name") or plain.get("speaker_name") or "")
    if "speaker_name" in values and spoken != str(agent.get("speaker_name") or ""):
        out["speaker_name"] = spoken
    return out, notes


def ask(runtime: Any, name: str, new_name: Any, wake_name: Any = None, people: Optional[Mapping[str, str]] = None,
        now: Optional[float] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """The agent's request: (answer for the agent, the new question or None). Blocking, never raises
    for a name that cannot be used (the answer says why)."""
    from gateway import agent_settings as S
    now = time.time() if now is None else now
    agent = runtime.load(name)

    def no(reason: str, tell: str) -> Tuple[Dict[str, Any], None]:
        logger.info("rename: %s asked; not asked (%s)", name, reason)
        return {"asked": False, "reason": reason, "tell": tell}, None

    if not uses_rename(agent):
        return no("no_people", NOT_YOURS_TELL)
    try:
        names, notes = check(runtime, name, new_name, wake_name, people)
    except S.Invalid as e:
        if str(e) == SAME_TELL:
            return no("same", SAME_TELL)
        return no("invalid", f"Not asked: {e} Nothing changed.")
    with _LOCK:
        data = load(runtime.env)
        _tidy(data, now)
        today = [l for l in data["log"] if l.get("agent") == name and l.get("decision") == "asked" and now - float(l["ts"]) < 86400]
        if len(today) >= PER_DAY:
            save(runtime.env, data)
            return no("rate_limited", LIMIT_TELL.format(n=len(today)))
        for rid, r in list(data["requests"].items()):           # one question per agent: the newest
            if r["agent"] == name:
                data["requests"].pop(rid)
                data["log"].append({"ts": now, "agent": name, "decision": "replaced", "by": "", "display_name": r["display_name"]})
        rid = hashlib.sha256(f"{name}|{names['display_name']}|{now}|{os.urandom(4).hex()}".encode()).hexdigest()[:12]
        req = {"id": rid, "agent": name, "ts": now, "approvers": approvers(agent), **names,
               "old_display_name": str(agent.get("display_name") or name),
               "old_speaker_name": str(agent.get("speaker_name") or "")}
        data["requests"][rid] = req
        data["log"].append({"ts": now, "agent": name, "decision": "asked", "by": "", "display_name": names["display_name"]})
        save(runtime.env, data)
    from gateway import voice_id
    who = " or ".join(voice_id.display_name(p) for p in req["approvers"])
    wake = f' The house speaker would then wake to "Hey {names["speaker_name"]}".' if names.get("speaker_name") else ""
    tell = ASKED_TELL.format(who=who, new=names["display_name"], old=req["old_display_name"], wake=wake)
    if notes:
        tell += " " + " ".join(notes)
    logger.info("rename: %s asked to be called something else; asking %s", name, ", ".join(req["approvers"]))
    return {"asked": True, "tell": tell}, dict(req)


# ── the answer ───────────────────────────────────────────────────────────────────────────────

def done_text(req: Mapping[str, Any], applied: Mapping[str, Any]) -> str:
    """What the person is told once it is renamed: the new name and the wake word that changed."""
    text = f"{req['old_display_name']} is now called {applied.get('display_name') or req['display_name']}."
    old_sp, new_sp = req.get("old_speaker_name") or "", applied.get("speaker_name") or ""
    if new_sp and new_sp != old_sp:
        text += f' At the house speaker say "Hey {new_sp}"' + (f'; "Hey {old_sp}" no longer wakes it.' if old_sp else ".")
    return text


async def decide(app: Any, runtime: Any, rid: str, decision: str, user: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """``confirm`` renames (checked again: a name may have been taken meanwhile); ``dismiss`` does not.
    Only one of the agent's own people."""
    import asyncio
    from gateway import agent_settings as S, agent_settings_api as api
    if decision not in ("confirm", "dismiss"):
        return 404, {"error": "not_found"}
    env = runtime.env
    req = next((r for r in await asyncio.to_thread(pending, env) if r["id"] == rid), None)
    if req is None:
        return 404, {"error": "not_found", "message": "That has already been answered."}
    agent = await asyncio.to_thread(runtime.load, req["agent"])
    me = str(user.get("username") or "").lower()
    if me not in approvers(agent):
        return 403, {"error": "forbidden", "message": "Only the agent's own people can decide this."}
    now = time.time()
    applied: Dict[str, Any] = {}
    if decision == "confirm":
        people = await asyncio.to_thread(api.people_names, runtime.env)
        values = {"display_name": req["display_name"]}
        if req.get("speaker_name"):
            values["speaker_name"] = req["speaker_name"]

        def rename() -> List[str]:
            plain, current = runtime.load(req["agent"], settings=False), runtime.load(req["agent"])
            new = json.loads(json.dumps(S.overlay(env, req["agent"])))
            changed = api.set_names(runtime, req["agent"], plain, current, api.locked(runtime, req["agent"]),
                                    new, values, people)
            if changed:
                S.save_overlay(env, req["agent"], new)
            return changed
        was_pending = await asyncio.to_thread(S.pending, env, req["agent"])
        try:
            changed = await asyncio.to_thread(rename)
        except S.Invalid as e:
            return 400, {"error": "invalid", "message": f"{req['display_name']} cannot be used now: {e}"}
        applied = await apply_live(app, runtime, req["agent"], agent, by=me, mark_applied=not was_pending, changed=changed)
    with _LOCK:
        data = load(env)
        if data["requests"].pop(rid, None) is None and decision == "dismiss":
            return 404, {"error": "not_found", "message": "That has already been answered."}
        data["log"].append({"ts": now, "agent": req["agent"], "decision": "confirmed" if decision == "confirm" else "dismissed",
                            "by": me, "display_name": req["display_name"], "old_display_name": req["old_display_name"],
                            "speaker_name": applied.get("speaker_name") or "", "old_speaker_name": req.get("old_speaker_name") or ""})
        save(env, data)
    try:
        import gateway.auth.db as auth_db
        auth_db.write_audit_log(user.get("id") or None, "agent_rename_" + ("confirmed" if decision == "confirm" else "dismissed"),
                                target_type="agent", target_id=req["agent"],
                                metadata={"from": req["old_display_name"], "to": req["display_name"],
                                          **({"wake": req["speaker_name"]} if req.get("speaker_name") else {})})
    except Exception as e:
        logger.warning("rename: audit log failed: %s", type(e).__name__)
    if decision == "dismiss":
        await asyncio.to_thread(write_note, runtime, req["agent"], {"state": "declined", "ts": now,
                                                                    "asked": req["display_name"],
                                                                    "display_name": req["old_display_name"]})
        logger.info("rename: %s said not now to %s's new name", me, req["agent"])
        return 200, {"ok": True, "decision": "dismiss", "message": f"{req['old_display_name']} keeps its name."}
    logger.info("rename: %s confirmed %s's new name", me, req["agent"])
    return 200, {"ok": True, "decision": "confirm", "message": done_text(req, applied), **applied}


# ── applying a new name, whoever chose it ────────────────────────────────────────────────────

def write_note(runtime: Any, name: str, note: Mapping[str, Any]) -> bool:
    """The note the agent's plugin adds to its next turns (logos-rename). Blocking; never raises."""
    try:
        runtime.put(f"logos-home-{name}", NOTE_FILE, json.dumps(dict(note), ensure_ascii=False), mode="0644")
        return True
    except Exception as e:
        logger.info("rename: could not leave %s its note: %s", name, type(e).__name__)
        return False


def _matrix_name(runtime: Any, name: str, after: Mapping[str, Any]) -> Optional[bool]:
    """Set the agent's Matrix display name now, with its own login (from its home volume)."""
    from gateway import matrix_host as mh
    if not mh.uses_matrix(after):
        return None
    def parse(text: str) -> Dict[str, str]:
        return dict(l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))
    env = parse(runtime.get(f"logos-home-{name}", ".env"))
    # Where the agent's secrets are kept on the host instead (its volume then holds an OpenShell
    # placeholder, "openshell:resolve:env:…"), the login is read from there.
    conf = runtime.env.get("XDG_CONFIG_HOME") or os.path.join(runtime.env.get("HOME") or os.path.expanduser("~"), ".config")
    try:
        host = parse((Path(conf) / "logos" / "agent-secrets" / f"{name}.env").read_text())
    except OSError:
        host = {}
    real = lambda v: v if v and not v.startswith("openshell:resolve:") else ""  # noqa: E731
    uid = env.get("MATRIX_USER_ID") or env.get("LOGOS_MATRIX_USER_ID")
    token = (real(host.get("LOGOS_MATRIX_ACCESS_TOKEN")) or real(host.get("MATRIX_ACCESS_TOKEN"))
             or real(env.get("MATRIX_ACCESS_TOKEN")) or real(env.get("LOGOS_MATRIX_ACCESS_TOKEN")))
    if not uid or not token:
        return False
    try:
        mh.MatrixHost(runtime).set_display_name(uid, token, mh.agent_display_name(after))
        return True
    except Exception as e:                      # cosmetic: the next `up` sets it again
        logger.warning("rename: could not set %s's Matrix name: %s", name, type(e).__name__)
        return False


def apply_live_sync(runtime: Any, name: str, before: Mapping[str, Any], by: str = "",
                    mark_applied: bool = False) -> Dict[str, Any]:
    """Everything that shows the name, from the overlay already saved. Blocking."""
    from gateway import agent_settings as S, wake_host
    after = runtime.load(name)
    out: Dict[str, Any] = {"display_name": str(after.get("display_name") or name),
                           "speaker_name": str(after.get("speaker_name") or "")}
    out["matrix"] = _matrix_name(runtime, name, after)
    try:
        phrases = wake_host.write_names(runtime, runtime.env)
        out["wake"] = None if phrases is None else [p["phrase"] for p in phrases if p["agent"] == name]
    except Exception as e:
        logger.warning("rename: could not write the wake words: %s", e)
        out["wake"] = None
    home = f"logos-home-{name}"
    soul = runtime.get(home, "SOUL.md")
    patched = replace_section(soul, after) if soul else None
    out["soul"] = False
    if patched is not None and patched != soul:
        try:
            runtime.put(home, "SOUL.md", patched)
            out["soul"] = True
        except Exception as e:
            logger.info("rename: SOUL.md of %s not updated (%s); the note covers it", name, type(e).__name__)
    old_display, old_sp = str(before.get("display_name") or name), str(before.get("speaker_name") or "")
    out["note"] = write_note(runtime, name, {"state": "renamed", "ts": time.time(), "by": by,
                                             "display_name": out["display_name"], "old_display_name": old_display,
                                             "speaker_name": out["speaker_name"], "old_speaker_name": old_sp})
    if mark_applied:
        S.mark_applied(runtime.env, name)
    logger.info("rename: %s is now named in Matrix=%s, wake words=%s, SOUL.md=%s", name, out["matrix"],
                out["wake"] is not None, out["soul"])
    return out


async def apply_live(app: Any, runtime: Any, name: str, before: Mapping[str, Any], by: str = "",
                     mark_applied: bool = False, changed: Optional[List[str]] = None) -> Dict[str, Any]:
    """:func:`apply_live_sync`, then the dashboard's own caches. Never raises."""
    import asyncio
    try:
        out = await asyncio.to_thread(apply_live_sync, runtime, name, before, by, mark_applied)
    except Exception as e:
        logger.exception("rename: applying %s's name failed", name)
        out = {"error": type(e).__name__}
    for key in ("home_cache", "activity_cache", "models_overview"):
        try:
            app.pop(key, None)
        except Exception:
            pass
    try:
        import gateway.auth.db as auth_db
        from gateway import agent_chat
        await asyncio.to_thread(agent_chat.sync_definitions, runtime, auth_db)     # the agents table's description
    except Exception:
        pass
    return out


# ── what the person sees ─────────────────────────────────────────────────────────────────────

def notification_items(env: Mapping[str, str], username: str, visible: Mapping[str, str], now: float,
                       since: float, names: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Items for GET /api/v2/notifications: the question (only to the agent's own people), and the
    answers of the week."""
    me = (username or "").lower()
    items: List[Dict[str, Any]] = []
    for r in pending(env, now):
        if r["agent"] not in visible or me not in (r.get("approvers") or []):
            continue
        old = r.get("old_display_name") or visible[r["agent"]]
        text = (f'Asked in its chat. At the house speaker it would wake to "Hey {r["speaker_name"]}" instead of '
                f'"Hey {r["old_speaker_name"]}".' if r.get("speaker_name") and r.get("old_speaker_name") else
                f'Asked in its chat. At the house speaker it would wake to "Hey {r["speaker_name"]}".' if r.get("speaker_name")
                else "Asked in its chat. Nothing changes until you confirm.")
        items.append({"id": f"rename:{r['id']}", "kind": "rename", "type": "needs", "ts": float(r["ts"]),
                      "agent": r["agent"], "who": old, "rid": r["id"], "display_name": r["display_name"],
                      "speaker_name": r.get("speaker_name") or "",
                      "title": f"{old} wants to be called {r['display_name']}", "text": text})
    for l in recent(env, since):
        if l["agent"] not in visible or l.get("decision") not in ("confirmed", "dismissed"):
            continue
        by = "You" if l.get("by") == me else (names or {}).get(l.get("by")) or str(l.get("by") or "Someone").capitalize()
        if l["decision"] == "confirmed":
            text = f"{by} confirmed it"
            if l.get("speaker_name") and l.get("speaker_name") != l.get("old_speaker_name"):
                text += f'. Say "Hey {l["speaker_name"]}" at the house speaker' + (
                    f'; "Hey {l["old_speaker_name"]}" no longer wakes it' if l.get("old_speaker_name") else "")
            title = f"{l.get('old_display_name') or l['agent']} is now {l['display_name']}"
        else:
            text, title = f"{by} said not now", f"{visible[l['agent']]} keeps its name"
        items.append({"id": f"renamelog:{l['agent']}:{int(float(l['ts']))}", "kind": "rename_done", "type": "update",
                      "ts": float(l["ts"]), "agent": l["agent"], "who": visible[l["agent"]], "title": title, "text": text})
    return items


def push_message(req: Mapping[str, Any], private: bool) -> Dict[str, Any]:
    if private:
        return {"title": "Logos", "body": "Something needs you", "link": "#/noticeboard", "room": f"rename-{req['id']}"}
    return {"title": f"{req['old_display_name']} wants to be called {req['display_name']}",
            "body": "Confirm or Not now, in Logos Notifications.", "link": "#/noticeboard", "room": f"rename-{req['id']}"}


async def alert(app: Any, req: Mapping[str, Any]) -> int:
    """A phone alert to each of the agent's people. Returns how many were sent; never raises."""
    sent = 0
    try:
        from gateway import push
        store, session = app.get("push_store"), app.get("push_session")
        if store is None or session is None:
            return 0
        key = store.vapid_key()
        for username in req.get("approvers") or []:
            message = push_message(req, push.alerts_private(username))
            for d in store.devices(username):
                sub = store.get(d["pushkey"])
                if sub:
                    await push.send(session, sub, message, key)
                    sent += 1
    except Exception as e:
        logger.warning("rename: phone alert failed: %s", type(e).__name__)
    return sent


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _runtime(app: Any) -> Any:
    from gateway import agent_chat
    return agent_chat.runtime_of(app.get("sandbox_backend"))


async def handle_ask(request: "Any") -> "Any":
    """POST /api/v2/agent-rename/ask {agent, token, name, wake_name?} — from the agent's plugin."""
    import asyncio
    from aiohttp import web
    try:
        body = await request.json()
        agent, token = str(body["agent"]), str(body["token"])
        name, wake = body.get("name"), body.get("wake_name")
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "bad_request"}, status=400)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", agent) or not valid(agent, token):
        return web.json_response({"error": "forbidden"}, status=403)
    if not isinstance(name, str) or (wake is not None and not isinstance(wake, str)):
        return web.json_response({"asked": False, "reason": "invalid", "tell": "Give the new name as text."})
    runtime = _runtime(request.app)
    if runtime is None or agent not in runtime.defined():
        return web.json_response({"error": "forbidden"}, status=403)
    from gateway import agent_settings_api as api
    people = await asyncio.to_thread(api.people_names, runtime.env)
    out, req = await asyncio.to_thread(ask, runtime, agent, name[:200], (wake or "")[:200] or None, people)
    if req is not None:
        try:
            import gateway.auth.db as auth_db
            auth_db.write_audit_log(None, "agent_rename_asked", target_type="agent", target_id=agent,
                                    metadata={"from": req["old_display_name"], "to": req["display_name"],
                                              **({"wake": req["speaker_name"]} if req.get("speaker_name") else {})})
        except Exception:
            pass
        await alert(request.app, req)
    return web.json_response(out)


async def handle_decide(request: "Any") -> "Any":
    from aiohttp import web
    from gateway.dashboard_api import _user
    user = _user(request)
    runtime = _runtime(request.app)
    if runtime is None:
        return web.json_response({"error": "no_backend"}, status=503)
    status, body = await decide(request.app, runtime, request.match_info["rid"], request.match_info["decision"], user)
    return web.json_response(body, status=status)


def notifications_for(app: Any, username: str, visible: Mapping[str, str], now: float, since: float) -> List[Dict[str, Any]]:
    """For dashboard_api.handle_notifications. Blocking; [] on any trouble (the feed still shows)."""
    try:
        runtime = _runtime(app)
        env = runtime.env if runtime is not None else os.environ
        names = {}
        for l in recent(env, since):
            by = l.get("by")
            if by and by not in names:
                try:
                    import gateway.auth.db as auth_db
                    row = auth_db.get_user_by_username(by) or {}
                except Exception:
                    row = {}
                names[by] = str(row.get("display_name") or by)
        return notification_items(env, username, visible, now, since, names)
    except Exception as e:
        logger.warning("rename: notifications could not be read: %s", e)
        return []


def register(app: "Any") -> None:
    from gateway.auth.middleware import require_csrf
    app.router.add_post(PATH, handle_ask)
    app.router.add_post("/api/v2/agent-rename/{rid}/{decision}", require_csrf(handle_decide))
