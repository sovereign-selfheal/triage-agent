#!/usr/bin/env bash
# start.sh — Wait for the OGX sidecar and start the Gradio UI
set -e

OGX_URL="${OGX_BASE_URL:-http://localhost:8321}"
echo "Waiting for OGX server to be ready at ${OGX_URL}..."
for i in $(seq 1 30); do
    if curl -sf "${OGX_URL}/v1/models" -o /dev/null 2>/dev/null; then
        echo "OGX server is ready."
        break
    fi
    echo "  Attempt ${i}/30 — waiting 5s..."
    sleep 5
done

echo "Starting Gradio UI on port ${GRADIO_PORT:-7860}..."
exec python app.py
