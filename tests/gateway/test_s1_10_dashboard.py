"""S1.10: the dashboard is trimmed to chat, agents and live activity.

Static checks on gateway/html/main_app.html: the removed v1 surfaces are
gone, every literal fetch() URL still has a route in the gateway, and
every method the markup calls is defined on the Alpine app() object.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO / "gateway"
MAIN = GATEWAY_DIR / "html" / "main_app.html"


def _page() -> str:
    return MAIN.read_text(encoding="utf-8")


def _routes() -> list[tuple[str, re.Pattern]]:
    src = "".join(
        (GATEWAY_DIR / f).read_text(encoding="utf-8")
        for f in ("http_api.py", "mcp_management.py")
    )
    out = []
    for m in re.finditer(
        r'add_(get|post|put|patch|delete)\(\s*"([^"]+)"', src
    ):
        rx = re.sub(r"\{[^}]+\}", "[^/]+", m.group(2))
        out.append((m.group(1).upper(), re.compile("^" + rx + "$")))
    return out


def test_page_is_substantially_smaller():
    # 13,385 lines before the trim.
    assert len(_page().splitlines()) < 6000


def test_removed_surfaces_are_gone():
    page = _page()
    for gone in (
        "phaser", "WorldManager", "initWorld", "agent-world", "characters.png",
        "Sortable", "tab==='compare'", "compareSend", "tab==='infra'", "infraTab",
        "loadModelRoutes", "modelRoutes", "loadClusters", "loadSandboxes(",
        "loadCloudProviders", "cloudProviders", "loadAdminMachines", "adminMachines",
        "loadSandboxPresets", "loadAdminSouls", "loadEvolution", "/evolution/",
        "canary", "data-testid=\"stamp-", "toolsCatalogue", "loadToolsTab",
        "/api/admin/sandboxes", "/api/admin/clusters", "/api/admin/model-routes",
        "/admin/machines", "/admin/cloud-providers", "/api/openshell/presets",
        "/proxy/", "/api/workers", "/spawn-templates", "setup_tools",
    ):
        assert gone not in page, gone
    assert not (GATEWAY_DIR / "world").exists()
    assert not (REPO / "assets" / "world").exists()


def test_kept_surfaces_are_present():
    page = _page()
    for kept in (
        'data-testid="nav-agents"', 'data-testid="nav-chats"',
        'data-testid="nav-activity"', 'data-testid="nav-admin"',
        'data-testid="agents-tab"', 'data-testid="agents-empty"',
        'data-testid="chat-input"', 'data-testid="chat-send"',
        "Live Executions", "No active sessions",
        "loadApprovals()", "loadDispatches()", "loadAdminUsers()",
        "submitChangePassword()", "logout()", "/api/setup/wipe",
    ):
        assert kept in page, kept
    # The 503 no_agents_running body from POST /chat renders as an error card.
    assert "isError: true" in page and "e.message || e.error" in page


def test_every_literal_fetch_url_is_routed():
    page = _page()
    routes = _routes()
    checked = 0
    # fetch('/literal/path' ...) and fetch('/prefix/' + expr + '/suffix' ...)
    for m in re.finditer(r"fetch\(\s*((?:'[^']*'|`[^`]*`)(?:\s*\+\s*(?:'[^']*'|[\w.()]+))*)", page):
        expr = m.group(1)
        parts = re.findall(r"'([^']*)'|`([^`]*)`|([\w.()]+)", expr)
        path = ""
        for sq, tq, other in parts:
            if sq:
                path += sq
            elif tq:
                path += re.sub(r"\$\{[^}]*\}", "X", tq)
            else:
                path += "X"
        path = path.split("?")[0]
        if not path.startswith("/"):
            continue  # instanceUrl + '/chat' style: prefix is '' at runtime
        tail = page[m.end(): m.end() + 300].split("fetch(")[0]
        mm = re.search(r"method:\s*['\"](\w+)['\"]", tail)
        method = mm.group(1).upper() if mm else "GET"
        # Two call sites end in a variable segment; expand to the values used.
        variants = [path]
        if path == "/admin/agents/X/X":
            variants = [f"/admin/agents/X/{e}" for e in
                        ("activity", "memories", "skills", "sessions", "files")]
        elif path == "/approvals/X/X":
            variants = ["/approvals/X/approve", "/approvals/X/reject"]
        for v in variants:
            assert any(meth == method and rx.match(v) for meth, rx in routes), (method, v)
            checked += 1
    assert checked >= 25


def test_methods_called_from_markup_are_defined():
    page = _page()
    start = page.index("function app() {")
    end = page.index("\n  };\n}", start)
    app_src = page[start:end]
    members = set(re.findall(
        r"^    (?:async\s+|get\s+)?([A-Za-z_$][\w$]*)\s*(?:\(|:)", app_src, re.M))
    markup = page[:start] + page[end:]
    markup = re.sub(r"<script\b.*?</script>", "", markup, flags=re.S)
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
    called = set()
    for m in re.finditer(r'\s(?:x-[a-z:.\-]+|@[a-z:.\-]+|:[a-z\-]+)="([^"]*)"', markup):
        expr = re.sub(r"'(?:[^'\\]|\\.)*'", "''", m.group(1))
        called.update(re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\(", expr))
    js_globals = {
        "app", "fetch", "setTimeout", "clearTimeout", "encodeURIComponent", "getCsrfToken",
        "Number", "String", "Date", "Math", "JSON", "Object", "Array",
        "parseInt", "parseFloat", "if", "for", "while", "return", "function",
        "$nextTick", "confirm",
    }
    missing = sorted(c for c in called - js_globals if c not in members)
    assert missing == [], missing
    assert len(called) > 50  # regex sanity
