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
    kind: str = "text"              # text | url | secret | lines | check | choice
    help: str = ""
    placeholder: str = ""
    required: bool = True
    options: List[List[str]] = field(default_factory=list)    # kind "choice": [[value, label], …]


@dataclass
class Kind:
    id: str
    name: str
    who: str                        # "home" (everyone) or "admin" (whoever manages Logos)
    refresh: int                    # seconds between fetches
    fields: List[Field]
    fetch: Callable[[Mapping[str, Any], aiohttp.ClientSession, dict], Awaitable[dict]]
    about: str = ""
    uses: str = ""                  # another kind whose setup this one shares (Proxmox uses Servers' token)
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def conf_id(self) -> str:
        return self.uses or self.id


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


# ── Proxmox: VMs and containers (Servers' token) ──────────────────────────────────────────────

async def fetch_guests(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    guests = await proxmox_get(conf, session, "/cluster/resources?type=vm") or []
    items = []
    for g in guests:
        if g.get("template"):
            continue
        running = g.get("status") == "running"
        items.append({"id": int(g.get("vmid") or 0), "name": str(g.get("name") or g.get("vmid")), "running": running,
                      "kind": "container" if g.get("type") == "lxc" else "vm", "node": str(g.get("node") or ""),
                      "cpu": round(100 * float(g.get("cpu") or 0)) if running else None,
                      "mem_gb": round(float(g.get("mem") or 0) / 2**30, 1) if running else None})
    items.sort(key=lambda g: g["id"])
    up = sum(1 for g in items if g["running"])
    return {"state": "ok", "items": items, "running": up, "stopped": len(items) - up,
            "summary": f"{up} running" + (f" · {len(items) - up} stopped" if len(items) > up else "")}


# ── backups: Logos's own export, and Proxmox's backup runs ───────────────────────────────────

BACKUP_LATE = {"logos": 26 * 3600, "proxmox": 8 * 86400}     # older than this is late


def logos_export_status() -> Optional[Dict[str, Any]]:
    """The nightly export (infra/systemd/logos-backup-export.service), from systemd; None if not set up."""
    import subprocess
    try:
        out = subprocess.run(["systemctl", "--user", "show", "logos-backup-export.service", "--timestamp=unix",
                              "-p", "Result", "-p", "ExecMainStartTimestamp", "-p", "ExecMainExitTimestamp", "-p", "LoadState"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    v = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if v.get("LoadState") != "loaded":
        return None
    ran = v.get("ExecMainExitTimestamp", "").lstrip("@")
    return {"ran": int(ran) if ran.isdigit() else 0, "ok": v.get("Result") == "success"}


async def fetch_backups(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    now = time.time()
    items = []
    own = await asyncio.to_thread(logos_export_status)
    if own is not None:
        items.append({"name": "Logos export", "ran": own["ran"] or None,
                      "ok": own["ok"] and bool(own["ran"]) and now - own["ran"] < BACKUP_LATE["logos"],
                      "detail": "never ran" if not own["ran"] else ("failed" if not own["ok"] else "")})
    px = memo.get("with_conf") or {}                # Servers' setup, when there is one
    if px:
        for node in await proxmox_get(px, session, "/nodes") or []:
            name = str(node.get("node"))
            tasks = await proxmox_get(px, session, f"/nodes/{urllib.parse.quote(name)}/tasks?typefilter=vzdump&limit=10") or []
            done = [t for t in tasks if t.get("endtime")]
            if not done:
                continue
            last = max(done, key=lambda t: t.get("endtime") or 0)
            ok = str(last.get("status")) == "OK"
            items.append({"name": f"Proxmox · {name}", "ran": int(last.get("endtime") or 0),
                          "ok": ok and now - int(last.get("endtime") or 0) < BACKUP_LATE["proxmox"],
                          "detail": "" if ok else "failed"})
    if not items:
        raise Problem("No backups found: neither Logos's nightly export nor Proxmox backup runs.")
    for i in items:
        if not i["ok"] and not i["detail"]:
            i["detail"] = "late"
    bad = [i for i in items if not i["ok"]]
    return {"state": "needs" if bad else "ok", "items": sorted(items, key=lambda i: i["ok"]),
            "summary": f"{bad[0]['name']}: {bad[0]['detail']}" if bad else f"all {len(items)} OK"}


# ── ad blocking: Pi-hole or AdGuard Home ─────────────────────────────────────────────────────

async def fetch_adblock(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    u = urllib.parse.urlsplit(str(conf.get("url") or "").strip().rstrip("/"))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Problem("The address starts with http:// or https://")
    base, pw = f"{u.scheme}://{u.netloc}", str(conf.get("password") or "")
    try:
        if conf.get("product") == "adguard":
            async with session.get(f"{base}/control/stats", ssl=False, auth=aiohttp.BasicAuth(str(conf.get("username") or ""), pw)) as r:
                if r.status in (401, 403):
                    raise Problem("AdGuard refused the username or password.")
                d = await r.json(content_type=None)
            total, blocked = int(d.get("num_dns_queries") or 0), int(d.get("num_blocked_filtering") or 0)
            top = [next(iter(x)) for x in (d.get("top_blocked_domains") or [])[:2] if isinstance(x, dict) and x]
        else:                                                    # Pi-hole 6: a session, used once, then closed
            async with session.post(f"{base}/api/auth", json={"password": pw}, ssl=False) as r:
                sid = ((await r.json(content_type=None)).get("session") or {}).get("sid") if r.status == 200 else None
            if not sid:
                raise Problem("Pi-hole refused the password (its app password, from Settings › Web interface / API).")
            try:
                async with session.get(f"{base}/api/stats/summary", ssl=False, headers={"X-FTL-SID": sid}) as r:
                    q = (await r.json(content_type=None)).get("queries") or {}
                async with session.get(f"{base}/api/stats/top_domains?blocked=true&count=2", ssl=False, headers={"X-FTL-SID": sid}) as r:
                    top = [x.get("domain") for x in ((await r.json(content_type=None)).get("domains") or []) if x.get("domain")]
            finally:
                async with session.delete(f"{base}/api/auth", ssl=False, headers={"X-FTL-SID": sid}):
                    pass
            total, blocked = int(q.get("total") or 0), int(q.get("blocked") or 0)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise Problem("Nothing answered at that address.")
    pct = round(100 * blocked / total) if total else 0
    return {"state": "ok", "percent": pct, "blocked": blocked, "total": total, "top": top[:2],
            "summary": "Pi-hole" if conf.get("product") != "adguard" else "AdGuard"}


# ── now playing: Jellyfin ────────────────────────────────────────────────────────────────────

async def fetch_playing(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    u = urllib.parse.urlsplit(str(conf.get("url") or "").strip().rstrip("/"))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Problem("Jellyfin's address starts with http:// or https://")
    try:
        async with session.get(f"{u.scheme}://{u.netloc}/Sessions?activeWithinSeconds=600", ssl=False,
                               headers={"X-Emby-Token": str(conf.get("api_key") or "")}) as r:
            if r.status in (401, 403):
                raise Problem("Jellyfin refused the API key (Dashboard › API Keys).")
            sessions = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise Problem("Nothing answered at that address.")
    items = []
    for s in sessions or []:
        it = s.get("NowPlayingItem")
        if not it:
            continue
        play = s.get("PlayState") or {}
        title = str(it.get("Name") or "")
        if it.get("SeriesName"):
            title = f"{it['SeriesName']} · S{it.get('ParentIndexNumber') or 0} E{it.get('IndexNumber') or 0}"
        length, pos = int(it.get("RunTimeTicks") or 0) / 1e7, int(play.get("PositionTicks") or 0) / 1e7
        items.append({"title": title, "where": str(s.get("DeviceName") or ""), "who": str(s.get("UserName") or ""),
                      "paused": bool(play.get("IsPaused")), "position": int(pos), "length": int(length),
                      "percent": round(100 * pos / length) if length else None})
    return {"state": "ok", "items": items, "summary": f"{len(items)} playing" if items else "nothing playing"}


# ── downloads: Sonarr and Radarr ─────────────────────────────────────────────────────────────

async def _arr(session, url: str, key: str, path: str):
    u = urllib.parse.urlsplit(url.strip().rstrip("/"))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Problem(f'"{url}" is not an address: start it with http:// or https://')
    async with session.get(f"{u.scheme}://{u.netloc}{u.path}{path}", ssl=False, headers={"X-Api-Key": key}) as r:
        if r.status in (401, 403):
            raise Problem("Sonarr or Radarr refused the API key (Settings › General › API Key).")
        return await r.json(content_type=None)


async def fetch_downloads(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    items, upcoming = [], 0
    apps = [(n, conf.get(f"{n}_url"), conf.get(f"{n}_key")) for n in ("sonarr", "radarr")]
    apps = [(n, u, k) for n, u, k in apps if u]
    if not apps:
        raise Problem("Fill in Sonarr, Radarr, or both.")
    try:
        for name, url, key in apps:
            q = await _arr(session, url, str(key or ""), "/api/v3/queue?pageSize=20&includeEpisode=true&includeMovie=true")
            for r in (q or {}).get("records", []):
                size, left = float(r.get("size") or 0), float(r.get("sizeleft") or 0)
                stalled = str(r.get("trackedDownloadState") or "") in ("importBlocked", "failedPending") or \
                    str(r.get("status") or "") in ("warning", "failed")
                items.append({"title": str(r.get("title") or "?")[:120], "app": name,
                              "percent": round(100 * (size - left) / size) if size else 0,
                              "left": str(r.get("timeleft") or ""), "waiting": str(r.get("status")) in ("queued", "delay", "paused"),
                              "stalled": stalled})
            if name == "sonarr":
                start = time.strftime("%Y-%m-%d")
                end = time.strftime("%Y-%m-%d", time.localtime(time.time() + 7 * 86400))
                upcoming = len(await _arr(session, url, str(key or ""), f"/api/v3/calendar?start={start}&end={end}") or [])
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise Problem("Nothing answered at one of the addresses.")
    stalled = sum(1 for i in items if i["stalled"])
    return {"state": "needs" if stalled else "ok", "items": items, "upcoming": upcoming,
            "summary": f"{stalled} stalled" if stalled else (f"{len(items)} in the queue" if items else "queue empty")}


# ── bookmarks: the household's services, a tap away ──────────────────────────────────────────

async def fetch_bookmarks(conf: Mapping[str, Any], session: aiohttp.ClientSession, memo: dict) -> dict:
    items = parse_services(conf.get("links", ""))
    return {"state": "ok", "items": items, "summary": ""}


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
    Kind("proxmox", "Proxmox", "admin", 60, [], fetch_guests, uses="servers",
         about="The VMs and containers, running or stopped. It uses the Servers setup (the same read-only token)."),
    Kind("backups", "Backups", "admin", 300, [], fetch_backups, extra={"with": "servers"},
         about="Logos's own nightly export and, with Servers set up, Proxmox's backup runs. A failed or late one goes to Notifications."),
    Kind("adblock", "Ad blocking", "home", 300, [
        Field("product", "Which", "choice", options=[["pihole", "Pi-hole"], ["adguard", "AdGuard Home"]]),
        Field("url", "Address", "url", placeholder="http://192.168.1.2"),
        Field("username", "Username (AdGuard only)", "text", required=False),
        Field("password", "Password", "secret",
              help="Pi-hole: an app password (Settings › Web interface / API). AdGuard: the admin login."),
    ], fetch_adblock, about="How much of the household's traffic was blocked today, and what most."),
    Kind("playing", "Now playing", "home", 30, [
        Field("url", "Jellyfin address", "url", placeholder="http://192.168.1.30:8096"),
        Field("api_key", "API key", "secret", help="Jellyfin › Dashboard › API Keys › +, named Logos."),
    ], fetch_playing, about="What is playing on Jellyfin now, where and how far in."),
    Kind("downloads", "Downloads", "home", 60, [
        Field("sonarr_url", "Sonarr address", "url", required=False, placeholder="http://192.168.1.40:8989"),
        Field("sonarr_key", "Sonarr API key", "secret", required=False),
        Field("radarr_url", "Radarr address", "url", required=False, placeholder="http://192.168.1.40:7878"),
        Field("radarr_key", "Radarr API key", "secret", required=False, help="Each one's Settings › General › API Key."),
    ], fetch_downloads, about="The download queue from Sonarr and Radarr, and what is coming this week. A stalled one needs you."),
    Kind("bookmarks", "Bookmarks", "home", 3600, [
        Field("links", "Links", "lines", placeholder="Proxmox, https://192.168.1.10:8006\nHome Assistant, http://192.168.1.20:8123",
              help="One per line: a name, a comma, then its address."),
    ], fetch_bookmarks, about="Your services, a tap away."),
]}


# ── setup, kept owner-only ───────────────────────────────────────────────────────────────────

def needs_no_setup(kind: Kind) -> bool:
    """A kind with nothing to ask (Backups reads Logos itself) is set up from the start."""
    return not kind.fields and not kind.uses


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
        if f.kind == "choice":
            v = v if v in [o[0] for o in f.options] else (f.options[0][0] if f.options else "")
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
                "required": f.required, "options": f.options}
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

    def forget(self, conf_id: str) -> None:
        """A setup changed: every kind that uses it (or reads it alongside) fetches afresh."""
        for k in KINDS.values():
            if conf_id in (k.conf_id, k.extra.get("with")):
                self.cache.pop(k.id, None)
                self.memo.pop(k.id, None)

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
        conf = self.conf(kind.conf_id)
        if not conf and not needs_no_setup(kind):
            return {"state": "setup"}
        conf = conf or {}
        lock = self.locks.setdefault(kind.id, asyncio.Lock())
        async with lock:
            c = self.cache.get(kind.id) or {}
            now = self.clock()
            if force or not c or now - c.get("tried", 0) >= kind.refresh:
                c = {**c, "tried": now}
                try:
                    memo = self.memo.setdefault(kind.id, {})
                    if kind.extra.get("with"):
                        memo["with_conf"] = self.conf(kind.extra["with"]) or {}
                    c.update(data=await kind.fetch(conf, await self._session(), memo),
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
            bad = [i for i in data.get("items", []) if i.get("up") is False or i.get("online") is False
                   or i.get("ok") is False or i.get("stalled")]
            out.append({"id": f"widget-{kid}", "type": "needs", "kind": "widget", "ts": c.get("at") or 0,
                        "title": f"{kind.name}: {data.get('summary', 'needs a look')}",
                        "text": ", ".join(str(i.get("name") or i.get("title") or "") for i in bad)[:200],
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
                "set_up": bool(saved.get(k.conf_id)) or needs_no_setup(k), "can_set_up": is_admin,
                "uses": k.uses}
        if is_admin:
            item["fields"] = public_fields(KINDS[k.conf_id], saved.get(k.conf_id) or {})
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
        conf = clean(KINDS[kind.conf_id], await _fields(request), w.conf(kind.conf_id) or {})
        data = await w.test(kind, conf)
    except Problem as e:
        return web.json_response({"ok": False, "error": str(e)})
    return web.json_response({"ok": True, "summary": data.get("summary", "")})


async def handle_put(request: web.Request) -> web.Response:
    """PUT /api/v2/widgets/{kind} {fields} — tried first, then saved (whoever manages Logos)."""
    kind = _kind_for(request, admin_only=True)
    w = request.app["widgets"]
    try:
        conf = clean(KINDS[kind.conf_id], await _fields(request), w.conf(kind.conf_id) or {})
        data = await w.test(kind, conf)
    except Problem as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    saved = load_all(w.path)
    saved[kind.conf_id] = conf
    save_all(saved, w.path)
    w.forget(kind.conf_id)
    return web.json_response({"ok": True, "summary": data.get("summary", "")})


async def handle_delete(request: web.Request) -> web.Response:
    """DELETE /api/v2/widgets/{kind} — forget its setup (and its key)."""
    kind = _kind_for(request, admin_only=True)
    w = request.app["widgets"]
    saved = load_all(w.path)
    saved.pop(kind.conf_id, None)
    save_all(saved, w.path)
    w.forget(kind.conf_id)
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
