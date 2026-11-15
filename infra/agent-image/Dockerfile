# Logos v2 agent image: upstream hermes-agent, unmodified, adapted to run as an
# OpenShell 0.1.8 sandbox (story S2.3).
#
# OpenShell replaces the image ENTRYPOINT/CMD with its own supervisor, needs a
# non-root OCI USER, and treats a non-/ WORKDIR as the workspace. The upstream
# image runs as root and drops to `hermes` inside its s6 entrypoint, with
# WORKDIR /opt/hermes, so this layer only fixes those three things.
#
# Start command (passed by Logos at sandbox create):
#   sh -c '/opt/hermes/docker/stage2-hook.sh; exec hermes gateway run'
#
# Persistent volumes (see docs/adr/0001):
#   logos-home-<agent>   -> /opt/data        hermes home: config, memories, sessions, cron
#   logos-brain-<agent>  -> /sandbox/brain   the agent's markdown vault

# hermes-agent v0.23.1 (Docker Hub, published 2026-11-10). Bump deliberately.
FROM nousresearch/hermes-agent:v0.23.1@sha256:276d44685c65c0cb2a25f190b63c65c8906f9dc76486fb1c05ae71b5826bda08

USER root
RUN install -d -o hermes -g hermes -m 0750 /sandbox /sandbox/brain \
 && install -d -o hermes -g hermes -m 0750 /opt/data

# Upstream limits hermes's write_file tool to HERMES_WRITE_SAFE_ROOT=/opt/data. The brain lives
# at /sandbox/brain, so it must be on that list or every note needs a code-execution workaround.
ENV PATH="/opt/hermes/.venv/bin:${PATH}" \
    HERMES_HOME=/opt/data \
    HERMES_WRITE_SAFE_ROOT=/opt/data:/sandbox/brain

LABEL org.opencontainers.image.title="logos-agent" \
      org.opencontainers.image.description="hermes-agent v0.23.1 for Logos v2 OpenShell sandboxes" \
      org.opencontainers.image.source="https://github.com/GregsGreyCode/Logos"

WORKDIR /sandbox
USER hermes
