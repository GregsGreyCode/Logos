"""Homelab widgets on Home (F42, designed 23 Nov: https://claude.ai/artifact/DJ7eLVTkisqY2p7rWggQKs).

A widget kind is a small definition: what setup asks for, who sees it, how often it refreshes, and
how its numbers are fetched. Adding one is adding a ``Kind`` to ``KINDS`` and its drawing to
``gateway/ui/widgets.js``; setup, the five shared states, caching and Notifications come with it.

- **Keys stay here.** Setup is saved in ``~/.config/logos/widgets.json`` (owner-only). A browser
  sees a secret only as "set"; agents never see any of it. The Logos server asks the service and
  hands Home only the numbers.
- **Who sees what.** ``home`` kinds are for everyone at home; ``admin`` kinds (servers, Proxmox,
  backups…) only for whoever manages Logos. Only they set widgets up.
- **The five states** (the design's "States" board): not set up (``setup``), fine (``ok``), needs
  you (``needs``: the problem first, and a line in Notifications), can't reach the source (the last
  copy with ``stale`` and its age), and loading, which the page draws itself.
- Each kind is fetched at most once per ``refresh`` seconds, whoever asks; the copy is kept in
  memory, so a restart fetches again.

Kinds so far: ``services`` (is each address answering, and how fast) and ``servers`` (CPU, memory,
disk and uptime of each Proxmox node, with a read-only API token).
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import aiohttp
from aiohttp import web

CONFIG_FILE = "widgets.json"
TIMEOUT = aiohttp.ClientTimeout(total=8)
SLOW_MS = 1500


class Problem(Exception):
    """A failure in plain words, for the setup sheet and the widget."""


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"              # text | url | secret | lines | check
    help: str = ""
    placeholder: str = ""
    required: bool = True


@dataclass
class Kind:
    id: str
    name: str
    who: str                        # "home" (everyone) or "admin" (whoever manages Logos)
    refresh: int                    # seconds between fetches
    fields: List[Field]
    fetch: Callable[[Mapping[str, Any], aiohttp.ClientSession, dict], Awaitable[dict]]
    about: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ── services: is each address answering ──────────────────────────────────────────────────────

def parse_services(text: str) -> List[Dict[str, str]]:
    """One per line: "Name, https://address" or just the address (named after its host)."""
    out = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, url = line.rpartition(",") if "," in line else ("", "", line)
        url = url.strip()
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise Problem(f'"{line}" is not an address: start it with http:// or https://')
        out.append({"name": name.strip() or u.hostname, "url": url})
    if not out:
        raise Problem("Add at least one service: a name, a comma, then its address.")
    if len(out) > 40:
        raise Problem("Up to 40 services.")
    return out


async def _probe(session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        # Any answer counts as up (a login page's 401 too); only the server failing (5xx) or no
        # answer is down. Services on a home network often have their own certificates: not checked.
        async with session.get(url, ssl=False, allow_redirects=False) as r:
            ms = int((time.monotonic() - started) * 1000)
            if r.status >= 500:
                return {"up": False, "ms": ms, "detail": f"error {r.status}"}
            return {"up": True, "ms": ms, "slow": ms > SLOW_MS}
    except asyncio.TimeoutError:
        return {"up": False, "detail": "not answering"}
    except aiohttp.ClientError:
        return {"up": False, "detail": "not answering"}


async def fetch_services(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    services = parse_services(conf.get("services", ""))
    results = await asyncio.gather(*(_probe(session, s["url"]) for s in services))
    now = time.time()
    since = memo.setdefault("down_since", {})
    items = []
    for s, res in zip(services, results):
        if res["up"]:
            since.pop(s["url"], None)
        else:
            since.setdefault(s["url"], now)
            res["since"] = since[s["url"]]
        items.append({"name": s["name"], "url": s["url"], **res})
    # Problems first, then slow, then the rest in the order given.
    items.sort(key=lambda i: (i["up"], not i.get("slow")))
    down = sum(1 for i in items if not i["up"])
    return {"state": "needs" if down else "ok", "items": items, "down": down,
            "slow": sum(1 for i in items if i.get("slow")),
            "summary": f"{down} down" if down else f"all {len(items)} up"}


# ── servers: Proxmox nodes ───────────────────────────────────────────────────────────────────

def _proxmox_base(url: str) -> str:
    u = urllib.parse.urlsplit(str(url or "").strip().rstrip("/"))
    if u.scheme != "https" or not u.hostname:
        raise Problem("Proxmox's address starts with https://, for example https://192.168.1.10:8006")
    return f"https://{u.netloc}"


async def proxmox_get(conf: Mapping[str, Any], session: aiohttp.ClientSession, path: str) -> Any:
    base = _proxmox_base(conf.get("url", ""))
    token, secret = str(conf.get("token_id") or "").strip(), str(conf.get("secret") or "").strip()
    if "!" not in token or not secret:
        raise Problem("The token ID looks like name@pam!logos, and needs its secret.")
    tls: Any = False if conf.get("own_certificate") else ssl.create_default_context()
    try:
        async with session.get(f"{base}/api2/json{path}", ssl=tls,
                               headers={"Authorization": f"PVEAPIToken={token}={secret}"}) as r:
            if r.status in (401, 403):
                raise Problem("Proxmox refused the token: check the token ID and secret, and that it may read (PVEAuditor).")
            if r.status != 200:
                raise Problem(f"Proxmox answered with an error ({r.status}).")
            return (await r.json()).get("data")
    except aiohttp.ClientConnectorCertificateError:
        raise Problem("Proxmox's certificate is its own, not a public one: tick \"Proxmox uses its own certificate\".")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise Problem("Nothing answered at that address.")


def _pct(used: Any, total: Any) -> Optional[int]:
    try:
        return round(100 * float(used) / float(total)) if float(total) else None
    except (TypeError, ValueError):
        return None


async def fetch_servers(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    nodes = await proxmox_get(conf, session, "/nodes") or []
    items = []
    for n in sorted(nodes, key=lambda n: str(n.get("node"))):
        online = n.get("status") == "online"
        items.append({"name": str(n.get("node") or "?"), "online": online,
                      "cpu": round(100 * float(n.get("cpu") or 0)) if online else None,
                      "mem": _pct(n.get("mem"), n.get("maxmem")), "mem_gb": round(float(n.get("mem") or 0) / 2**30, 1),
                      "mem_total_gb": round(float(n.get("maxmem") or 0) / 2**30),
                      "disk": _pct(n.get("disk"), n.get("maxdisk")), "uptime": int(n.get("uptime") or 0)})
    full = [i for i in items if (i["disk"] or 0) >= 95 or (i["mem"] or 0) >= 97]
    off = [i for i in items if not i["online"]]
    state = "needs" if (full or off) else "ok"
    summary = (f"{off[0]['name']} is offline" if off else f"{full[0]['name']}: disk {full[0]['disk']}%" if full
               else f"{len(items)} node{'s' if len(items) != 1 else ''}")
    return {"state": state, "items": items, "summary": summary}


KINDS: Dict[str, Kind] = {k.id: k for k in [
    Kind("services", "Services", "home", 60, [
        Field("services", "Services", "lines", placeholder="Home Assistant, http://192.168.1.20:8123\nJellyfin, http://192.168.1.30:8096",
              help="One per line: a name, a comma, then its address. Any answer counts as up; Logos only asks, it never signs in."),
    ], fetch_services, about="Whether each of your services answers, and how fast. A service that stops answering also goes to Notifications."),
    Kind("servers", "Servers", "admin", 30, [
        Field("url", "Proxmox address", "url", placeholder="https://192.168.1.10:8006"),
        Field("token_id", "API token ID", "text", placeholder="logos@pve!home",
              help="In Proxmox: Datacenter › Permissions › API Tokens › Add. Give its user the PVEAuditor role (read only)."),
        Field("secret", "Token secret", "secret"),
        Field("own_certificate", "Proxmox uses its own certificate", "check", required=False,
              help="Usual at home: Proxmox makes its own. Tick it so Logos does not ask for a public one."),
    ], fetch_servers, about="CPU, memory, disk and uptime of each Proxmox node. Read only: starting and stopping stays in Proxmox."),
]}


# ── setup, kept owner-only ───────────────────────────────────────────────────────────────────

def config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base) / "logos" / CONFIG_FILE


def load_all(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads((path or config_path()).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_all(data: Mapping[str, Any], path: Optional[Path] = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def clean(kind: Kind, given: Mapping[str, Any], before: Mapping[str, Any]) -> Dict[str, Any]:
    """The fields as saved: a secret left blank keeps the one already saved."""
    out: Dict[str, Any] = {}
    for f in kind.fields:
        v = given.get(f.key)
        if f.kind == "check":
            out[f.key] = bool(v)
            continue
        v = str(v or "").strip()
        if f.kind == "secret" and not v:
            v = str(before.get(f.key) or "")
        if f.required and not v:
            raise Problem(f"{f.label} is needed.")
        out[f.key] = v[:4000]
    return out


def public_fields(kind: Kind, conf: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The fields as a browser may see them: a secret only as whether it is set."""
    out = []
    for f in kind.fields:
        item = {"key": f.key, "label": f.label, "kind": f.kind, "help": f.help, "placeholder": f.placeholder,
                "required": f.required}
        if f.kind == "secret":
            item["set"] = bool(conf.get(f.key))
        else:
            item["value"] = conf.get(f.key, False if f.kind == "check" else "")
        out.append(item)
    return out


# ── fetching, at most once per refresh ───────────────────────────────────────────────────────

class Widgets:
    def __init__(self, path: Optional[Path] = None, clock=time.time):
        self.path = path
        self.clock = clock
        self.cache: Dict[str, dict] = {}          # kind -> {"data", "at", "error", "tried"}
        self.memo: Dict[str, dict] = {}           # kind -> what a fetch keeps between runs (down since…)
        self.locks: Dict[str, asyncio.Lock] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    def conf(self, kind_id: str) -> Optional[Dict[str, Any]]:
        return load_all(self.path).get(kind_id)

    async def _session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=TIMEOUT)
        return self.session

    async def test(self, kind: Kind, conf: Mapping[str, Any]) -> dict:
        """One fetch with these settings, for the setup sheet (raises Problem)."""
        return await kind.fetch(conf, await self._session(), {})

    async def get(self, kind: Kind, force: bool = False) -> dict:
        conf = self.conf(kind.id)
        if not conf:
            return {"state": "setup"}
        lock = self.locks.setdefault(kind.id, asyncio.Lock())
        async with lock:
            c = self.cache.get(kind.id) or {}
            now = self.clock()
            if force or not c or now - c.get("tried", 0) >= kind.refresh:
                c = {**c, "tried": now}
                try:
                    c.update(data=await kind.fetch(conf, await self._session(), self.memo.setdefault(kind.id, {})),
                             at=now, error=None)
                except Problem as e:
                    c["error"] = str(e)
                except Exception:                            # never a crash on Home: the widget says so
                    c["error"] = "It could not be asked just now."
                self.cache[kind.id] = c
        if c.get("data") is None:
            return {"state": "unreachable", "error": c.get("error"), "tried": c.get("tried")}
        out = {**c["data"], "at": c["at"]}
        if c.get("error"):
            out.update(stale=True, error=c["error"])         # the last copy, dimmed, with its age
        return out

    def needs(self, is_admin: bool) -> List[dict]:
        """Lines for Notifications: widgets whose last copy needs someone (no fetching here)."""
        out = []
        for kid, c in self.cache.items():
            kind, data = KINDS.get(kid), (c or {}).get("data") or {}
            if not kind or (kind.who == "admin" and not is_admin) or data.get("state") != "needs":
                continue
            out.append({"id": f"widget-{kid}", "type": "needs", "kind": "widget", "ts": c.get("at") or 0,
                        "title": f"{kind.name}: {data.get('summary', 'needs a look')}",
                        "text": ", ".join(i["name"] for i in data.get("items", []) if i.get("up") is False or i.get("online") is False)[:200],
                        "link": "#/home"})
        return out


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _who(request: web.Request) -> Dict[str, str]:
    from gateway.dashboard_api import _user
    return _user(request)


def _kind_for(request: web.Request, admin_only: bool = False) -> Kind:
    kind = KINDS.get(request.match_info["kind"])
    user = _who(request)
    is_admin = user["role"] == "admin"
    if kind is None or (kind.who == "admin" and not is_admin):
        raise web.HTTPNotFound()
    if admin_only and not is_admin:
        raise web.HTTPForbidden()
    return kind


async def handle_list(request: web.Request) -> web.Response:
    """GET /api/v2/widgets — the widget kinds you may put on Home, and whether each is set up."""
    is_admin = _who(request)["role"] == "admin"
    saved = load_all(request.app["widgets"].path)
    out = []
    for k in KINDS.values():
        if k.who == "admin" and not is_admin:
            continue
        item = {"id": k.id, "name": k.name, "who": k.who, "refresh": k.refresh, "about": k.about,
                "set_up": bool(saved.get(k.id)), "can_set_up": is_admin}
        if is_admin:
            item["fields"] = public_fields(k, saved.get(k.id) or {})
        out.append(item)
    return web.json_response({"widgets": out})


async def handle_get(request: web.Request) -> web.Response:
    """GET /api/v2/widgets/{kind} — its numbers (fetched at most once per refresh)."""
    kind = _kind_for(request)
    return web.json_response(await request.app["widgets"].get(kind, force=bool(request.query.get("fresh"))))


async def _fields(request: web.Request) -> Mapping[str, Any]:
    try:
        body = await request.json()
        return body.get("fields") or {}
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest()


async def handle_test(request: web.Request) -> web.Response:
    """POST /api/v2/widgets/{kind}/test {fields} — try these settings without saving them."""
    kind = _kind_for(request, admin_only=True)
    w = request.app["widgets"]
    try:
        conf = clean(kind, await _fields(request), w.conf(kind.id) or {})
        data = await w.test(kind, conf)
    except Problem as e:
        return web.json_response({"ok": False, "error": str(e)})
    return web.json_response({"ok": True, "summary": data.get("summary", "")})


async def handle_put(request: web.Request) -> web.Response:
    """PUT /api/v2/widgets/{kind} {fields} — tried first, then saved (whoever manages Logos)."""
    kind = _kind_for(request, admin_only=True)
    w = request.app["widgets"]
    try:
        conf = clean(kind, await _fields(request), w.conf(kind.id) or {})
        data = await w.test(kind, conf)
    except Problem as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    saved = load_all(w.path)
    saved[kind.id] = conf
    save_all(saved, w.path)
    w.cache.pop(kind.id, None)
    w.memo.pop(kind.id, None)
    return web.json_response({"ok": True, "summary": data.get("summary", "")})


async def handle_delete(request: web.Request) -> web.Response:
    """DELETE /api/v2/widgets/{kind} — forget its setup (and its key)."""
    kind = _kind_for(request, admin_only=True)
    w = request.app["widgets"]
    saved = load_all(w.path)
    saved.pop(kind.id, None)
    save_all(saved, w.path)
    w.cache.pop(kind.id, None)
    return web.json_response({"ok": True})


async def _close(app: web.Application):
    yield
    s = app["widgets"].session
    if s is not None and not s.closed:
        await s.close()


def register(app: web.Application, path: Optional[Path] = None) -> None:
    from gateway.auth.middleware import require_csrf
    app["widgets"] = Widgets(path)
    app.cleanup_ctx.append(_close)
    app.router.add_get("/api/v2/widgets", handle_list)
    app.router.add_get("/api/v2/widgets/{kind}", handle_get)
    app.router.add_post("/api/v2/widgets/{kind}/test", require_csrf(handle_test))
    app.router.add_put("/api/v2/widgets/{kind}", require_csrf(handle_put))
    app.router.add_delete("/api/v2/widgets/{kind}", require_csrf(handle_delete))
