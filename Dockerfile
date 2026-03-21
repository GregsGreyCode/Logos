FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
 && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Install dependencies first — this layer only rebuilds when pyproject.toml changes
COPY pyproject.toml ./
RUN uv venv venv --python 3.11 --clear
ENV VIRTUAL_ENV="/app/venv"
ENV PATH="/app/venv/bin:$PATH"
ENV PYTHONPATH="/app"
RUN uv pip install ".[all]" || true

# Copy the rest of the source — only this layer rebuilds on code changes
COPY . /app/

RUN git submodule update --init --recursive || true

# Re-install in editable mode now that source is present
RUN uv pip install -e ".[all]"
RUN if [ -f "./mini-swe-agent/pyproject.toml" ]; then uv pip install -e "./mini-swe-agent"; fi
RUN if [ -f "./tinker-atropos/pyproject.toml" ]; then uv pip install -e "./tinker-atropos"; fi

RUN useradd -m -d /home/hermes -u 10001 hermes \
 && mkdir -p /home/hermes/.hermes/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache,whatsapp/session} \
 && chown -R 10001:10001 /home/hermes

ENV HOME=/home/hermes

# Build-time SHA baked in — passed via: docker buildx build --build-arg BUILD_SHA=$(git rev-parse --short HEAD)
ARG BUILD_SHA=unknown
ENV BUILD_SHA=${BUILD_SHA}

EXPOSE 8080

CMD ["hermes", "gateway", "run"]
