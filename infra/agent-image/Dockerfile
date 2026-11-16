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
#   logos-brain-<agent>  -> /opt/data/brain   the agent's markdown vault

# hermes-agent v0.23.1 (Docker Hub, published 2026-11-10). Bump deliberately.
FROM nousresearch/hermes-agent:v0.23.1@sha256:276d44685c65c0cb2a25f190b63c65c8906f9dc76486fb1c05ae71b5826bda08

USER root
RUN install -d -o hermes -g hermes -m 0750 /sandbox /opt/data

# hermes plugins Logos relies on, pinned by commit. A sandbox cannot reach GitHub, so they are
# fetched at build time; the start command copies them into the agent's home.
#   hermes-session-reset-policy 0.3.0 (hermes curated catalog, approved by Greg 2026-11-16):
#   starts a fresh conversation on the first message after a daily boundary.
RUN git clone -q https://github.com/fastfinge/hermes-session-reset-policy.git \
        /opt/logos/plugins/hermes-session-reset-policy \
 && git -C /opt/logos/plugins/hermes-session-reset-policy checkout -q 0f2ea5ad995f1e4ba7c92c2dbd21afa868b8ae21 \
 && rm -rf /opt/logos/plugins/hermes-session-reset-policy/.git /opt/logos/plugins/hermes-session-reset-policy/tests \
 && chmod -R a+rX /opt/logos

# Upstream limits hermes's write_file tool to HERMES_WRITE_SAFE_ROOT=/opt/data (set by the base
# image). The brain volume is mounted inside the home, at /opt/data/brain, so it is covered.
ENV PATH="/opt/hermes/.venv/bin:${PATH}" \
    HERMES_HOME=/opt/data

LABEL org.opencontainers.image.title="logos-agent" \
      org.opencontainers.image.description="hermes-agent v0.23.1 for Logos v2 OpenShell sandboxes" \
      org.opencontainers.image.source="https://github.com/GregsGreyCode/Logos"

WORKDIR /sandbox
USER hermes
