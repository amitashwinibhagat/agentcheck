# AgentCheck server image.
#
#   docker build -t agentcheck:local .
#   docker run --rm -p 7373:7373 -v agentcheck-data:/data agentcheck:local
#
# Runs the web workspace + metered proxy on :7373 with the offline stub judge
# unless TYPESAFE_API_KEY is provided.

FROM python:3.12-slim

WORKDIR /app

# Dependencies first so source edits do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY agentcheck ./agentcheck

RUN pip install --no-cache-dir .

# Non-root; the store lives in the mounted volume.
RUN useradd -m -u 10001 agentcheck && mkdir -p /data && chown agentcheck /data
USER agentcheck

ENV AGENTCHECK_HOME=/data \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

EXPOSE 7373

# No key → the stub judge keeps the container useful offline.
CMD ["agentcheck", "serve", "--host", "0.0.0.0", "--port", "7373"]
