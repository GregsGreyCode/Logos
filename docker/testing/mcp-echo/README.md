# Echo MCP server — verification image

**This is a test-only image.** It exists to prove the Logos
Docker-container MCP deploy pipeline works end-to-end. Do not ship
this or bind it into production agents.

## What it does

A ~100-line aiohttp server that speaks the MCP streamable-HTTP
transport and exposes one tool:

- **`echo(text: str) -> str`** — returns its input verbatim.

No external dependencies at runtime (no APIs, no databases, no keys).

## Build

```bash
docker build -f docker/testing/mcp-echo/Dockerfile \
             -t logos-mcp-echo-test:local docker/testing/mcp-echo
```

The catalogue entry (`echo-test` in `gateway/mcp_catalogue.py`) points
at `logos-mcp-echo-test:local` so the Deploy-as-container flow uses
the locally-built image without needing a registry push.

## Verify end-to-end

1. Open **Config → Tools** in the dashboard
2. Click **+ Add server** → the catalogue opens
3. Pick **"Echo (test server)"**
4. Click **Deploy as container →** (no config fields to fill)
5. Wait a few seconds; the row's status flips to **running**
6. Click the row → drawer shows the `echo` tool
7. Chat with any agent and ask it to call `echo` with some text
8. Agent response should contain the text verbatim

That round-trip exercises every piece of the MCP Docker deploy path:
image pull, container start, port binding, gateway auto-wire,
gateway-proxy → container forwarding, and the agent-side MCP client.

## Clean up

From the dashboard: click the **✕** on the server row → the container
is stopped and removed, the DB row is deleted.

From the shell, if the gateway failed to undeploy:

```bash
docker rm -f mcp-echo-test
```
