# Logos advisor (story A1): sends a question its person approved to Claude, nothing else.
#
# Third-party part: the official Anthropic Python SDK, pinned (released 2026-11-04). The service
# code (gateway/advisor_service.py and the shared filter in gateway/web_service.py) is mounted
# read-only at start by gateway/advisor_host.py, so a code change needs no rebuild.
# 3.12-alpine, the same pinned base as the house service and web reader.
FROM python@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89

RUN pip install --no-cache-dir anthropic==2.5.0 && adduser -D -u 1000 advisor
USER 1000
