# Trivial MCP server for smoke-testing the Logos Docker-container deploy
# pipeline. NOT a production image — lives under docker/testing/ so it's
# obvious at a glance this isn't part of the real agent/sandbox build.
#
# Build:
#   docker build -f docker/testing/mcp-echo/Dockerfile \
#                -t logos-mcp-echo-test:local docker/testing/mcp-echo
#
# Deploy via the dashboard:
#   Config → Tools → + Add server → "Echo (test server)" → Deploy as container
#
# See docker/testing/mcp-echo/README.md for the full verification flow.

FROM python:3.12-slim

RUN pip install --no-cache-dir aiohttp==3.*

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 8000

# The MCP server reads $PORT (default 8000) and binds 0.0.0.0. The
# Logos deploy flow publishes the container port to 127.0.0.1:<picked>
# on the host, so 0.0.0.0-in-container + 127.0.0.1-on-host = no LAN
# exposure.
CMD ["python", "/app/server.py"]
