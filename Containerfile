FROM python:3.11-slim

WORKDIR /app

# curl is needed by start.sh to health-check the OGX sidecar
RUN apt-get update -qq && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py agent.py start.sh ./

# Non-root user for OpenShift compatibility
RUN useradd -u 1001 -r -g 0 -d /app -s /sbin/nologin appuser && \
    chown -R 1001:0 /app && \
    chmod -R g=u /app && \
    chmod +x /app/start.sh
USER 1001

EXPOSE 7860

ENV GRADIO_PORT=7860 \
    GRADIO_ANALYTICS_ENABLED=False \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/start.sh"]
