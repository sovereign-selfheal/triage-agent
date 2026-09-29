"""
AI Triage Agent — OGX edition.

Uses the OGX Responses API (server-side agentic loop) instead of a hand-rolled
tool-calling loop. The OGX server runs as a sidecar container in the same pod
and handles:
  - Tool calls to the Prometheus MCP server
  - Tool calls to the Ticketing MCP server
  - Tool calls to an optional Kubernetes-API MCP server (OCP_MCP_URL)
  - Inference via the platform's own LiteLLM router (model "auto"): the same
    gateway, policy hook and privacy gate that every other client goes
    through. There is no separate/external model backend for the agent
    (see gitops/AGENTS.md section 4).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator

import httpx
from ogx_client import AsyncOgxClient

logger = logging.getLogger(__name__)

OGX_BASE_URL = os.getenv("OGX_BASE_URL", "http://localhost:8321")
OCP_MCP_URL = os.getenv("OCP_MCP_URL", "")
PROMETHEUS_MCP_URL = os.getenv("PROMETHEUS_MCP_URL", "http://prometheus-mcp-server.agentic-triage.svc:8080/mcp")
TICKETING_MCP_URL = os.getenv("TICKETING_MCP_URL", "http://ticketing-mcp-server.agentic-triage.svc:8080/mcp")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "auto")
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "/app/knowledge.md")


def _load_knowledge() -> str:
    """Load the application knowledge base baked into the image."""
    try:
        with open(KNOWLEDGE_FILE) as f:
            content = f.read().strip()
        logger.info("Loaded knowledge base from %s (%d chars)", KNOWLEDGE_FILE, len(content))
        return content
    except FileNotFoundError:
        logger.warning("Knowledge file not found at %s — running without app context", KNOWLEDGE_FILE)
        return ""


def _mcp_tools() -> list[dict]:
    """Build the OGX `tools` list: one entry per MCP server that is configured."""
    tools = [
        {
            "type": "mcp",
            "server_label": "Prometheus MCP",
            "server_url": PROMETHEUS_MCP_URL,
            "require_approval": "never",
        },
        {
            "type": "mcp",
            "server_label": "Ticketing System MCP",
            "server_url": TICKETING_MCP_URL,
            "require_approval": "never",
        },
    ]
    if OCP_MCP_URL:
        tools.append(
            {
                "type": "mcp",
                "server_label": "OpenShift MCP",
                "server_url": OCP_MCP_URL,
                "require_approval": "never",
            }
        )
    else:
        logger.info("OCP_MCP_URL not set — running without the Kubernetes-API MCP tool")
    return tools


SYSTEM_PROMPT = """You are an OpenShift SRE troubleshooting assistant. Diagnose application problems using the tools below, then open an incident ticket and report your findings.

Tools available:

prometheus: `query_prometheus`, `query_prometheus_range`
ticketing: `create_incident`, `add_work_note`
openshift (only if available): `pods_list_in_namespace`, `pods_get`, `nodes_top`

Useful PromQL for the quarkus-buggy-app demo app (metrics are summary type — use _count and _sum, never _bucket):
- HTTP 5xx: rate(http_server_requests_seconds_count{namespace="agentic-triage",outcome="SERVER_ERROR"}[5m])
- HTTP 503: rate(http_server_requests_seconds_count{namespace="agentic-triage",status="503"}[5m])
- Avg latency: rate(http_server_requests_seconds_sum{namespace="agentic-triage"}[5m]) / rate(http_server_requests_seconds_count{namespace="agentic-triage"}[5m])
- Pod restarts: kube_pod_container_status_restarts_total{namespace="agentic-triage"}
- If a query returns no data after 2 attempts, stop retrying and report metrics as missing.

Workflow:
1. If the OpenShift MCP tool is available, list pods in the target namespace (default: agentic-triage). Note status and restarts.
2. For unhealthy pods, use pods_get to check events and conditions.
3. Query Prometheus for error rates and latency.
4. Synthesize a diagnosis from the data you collected.
5. Call create_incident with: short_description (concise summary), description (your full diagnosis), impact (1=High, 2=Medium, 3=Low), urgency (1/2/3), category ("Application" or "Infrastructure").
6. Output your diagnosis report.

Report format:
## Diagnosis Summary
**Application:** <name> | **Namespace:** <namespace>
**Symptoms:** <what you found>
**Root Cause:** <why it is happening>
**Affected Endpoints:** <endpoint, error rate, issue>
**Recommended Fix:** <actionable steps>
**Incident:** <ticket number from create_incident>
"""


AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "18"))


async def run_agent(user_message: str) -> AsyncIterator[str]:
    """
    Stream the agent's response for a given user message.

    The OGX server handles the full ReAct loop server-side: it calls the MCP
    tools, feeds results back to the model (via the platform's LiteLLM
    router), and streams the final answer.

    Yields:
        Text chunks (and tool-call status lines) from the agent.
    """
    client = AsyncOgxClient(
        base_url=OGX_BASE_URL,
        api_key="local",
        max_retries=5,
        timeout=httpx.Timeout(connect=30.0, read=AGENT_TIMEOUT_SECONDS, write=30.0, pool=30.0),
    )

    knowledge = _load_knowledge()
    instructions = (
        f"{SYSTEM_PROMPT}\n\n---\n## Application Knowledge Base\n\n{knowledge}"
        if knowledge
        else SYSTEM_PROMPT
    )

    logger.info("Sending request to OGX at %s", OGX_BASE_URL)

    try:
        stream = await asyncio.wait_for(
            client.responses.create(
                model=ROUTER_MODEL,
                input=user_message,
                instructions=instructions,
                tools=_mcp_tools(),
                stream=True,
                extra_body={"max_infer_iters": MAX_INFER_ITERS},
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Timed out waiting for OGX to start streaming")
        yield "\n\n⚠️ **Error:** Timed out connecting to the agent backend. Please try again.\n"
        return
    except Exception as exc:
        logger.exception("Failed to create OGX stream")
        yield f"\n\n⚠️ **Error:** Could not reach the agent backend: {exc}\n"
        return

    event_count = 0
    text_chunks = 0
    text_bytes = 0
    tool_calls = 0
    tool_failures = 0

    try:
        async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
            async for event in stream:
                event_count += 1
                event_type = getattr(event, "type", None)

                if event_type == "response.output_text.delta":
                    text_chunks += 1
                    text_bytes += len(event.delta)
                    yield event.delta

                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        tool_calls += 1
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        logger.info("Tool call started: %s → %s", server, tool)
                        yield f"\n\n> 🔧 Calling **{server}** → `{tool}`…\n\n"

                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", None) == "mcp_call":
                        server = getattr(item, "server_label", "unknown")
                        tool = getattr(item, "name", "unknown")
                        error = getattr(item, "error", None)
                        if error:
                            tool_failures += 1
                            logger.warning("Tool call failed: %s → %s: %s", server, tool, error)
                            yield f"\n\n> ⚠️ **{server}** → `{tool}` failed: {error}\n\n"
                        else:
                            logger.info("Tool call completed: %s → %s", server, tool)

                elif event_type == "response.completed":
                    resp = getattr(event, "response", None)
                    status = getattr(resp, "status", "unknown") if resp else "unknown"
                    logger.info("Stream response.completed — status=%s", status)

    except TimeoutError:
        logger.error("Agent exceeded %ds timeout", AGENT_TIMEOUT_SECONDS)
        yield (
            f"\n\n⚠️ **Error:** The agent took longer than {AGENT_TIMEOUT_SECONDS}s and was stopped. "
            "This usually means the model got stuck in a tool-calling loop. Please try again "
            "with a more specific question.\n"
        )
    except Exception as exc:
        logger.exception("Error during agent streaming")
        yield f"\n\n⚠️ **Error:** Agent encountered an error: {exc}\n"

    logger.info(
        "Stream finished — events=%d, text_chunks=%d, text_bytes=%d, tool_calls=%d, tool_failures=%d",
        event_count, text_chunks, text_bytes, tool_calls, tool_failures,
    )
    if text_chunks == 0:
        logger.warning("Stream produced no text output — the model may have ended on a tool call without generating a final answer")
