FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
 && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY . /app/

RUN git submodule update --init --recursive || true

RUN uv venv venv --python 3.11 --clear
ENV VIRTUAL_ENV="/app/venv"
ENV PATH="/app/venv/bin:$PATH"
ENV PYTHONPATH="/app"

RUN uv pip install -e ".[all]"
RUN if [ -f "./mini-swe-agent/pyproject.toml" ]; then uv pip install -e "./mini-swe-agent"; fi
RUN if [ -f "./tinker-atropos/pyproject.toml" ]; then uv pip install -e "./tinker-atropos"; fi

RUN useradd -m -d /home/hermes -u 10001 hermes \
 && mkdir -p /home/hermes/.hermes/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache,whatsapp/session} \
 && chown -R 10001:10001 /home/hermes

ENV HOME=/home/hermes

EXPOSE 8080

CMD ["hermes", "gateway", "run"]