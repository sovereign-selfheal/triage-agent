FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and the static knowledge base (edit it here, then release
# a new tag: the gitops repo only pins the resulting image digest).
COPY app.py agent.py mcp_tools.py knowledge.md ./

# Non-root user for OpenShift compatibility (arbitrary UID at runtime keeps group 0).
RUN useradd -u 1001 -r -g 0 -d /app -s /sbin/nologin appuser && \
    chown -R 1001:0 /app && \
    chmod -R g=u /app
USER 1001

EXPOSE 7860

ENV GRADIO_PORT=7860 \
    GRADIO_ANALYTICS_ENABLED=False \
    PYTHONUNBUFFERED=1

# Single container: no OGX sidecar to wait for.
CMD ["python", "app.py"]
