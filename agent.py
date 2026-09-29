"""
Triage agent — calls the platform's own LiteLLM router directly.

No more OGX sidecar and no external Nemotron/MaaS endpoint (see AGENTS.md section 3b
of the gitops repo for the rationale): this agent is an OpenAI-compatible client that
runs its own tool-calling loop against ``ROUTER_BASE_URL`` (the same public gateway
external MaaS clients use, `https://router.<appsDomain>/v1`), with model ``auto`` so the
policy hook of components/litellm-router picks the real target (local model or SOTA) per
request, exactly like any other client of the platform.

Tools come from one or more MCP servers (prometheus-mcp-server, ticketing-mcp-server,
and optionally an external Kubernetes-API MCP server) via `mcp_tools.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncIterator

from openai import AsyncOpenAI

from mcp_tools import McpServer, call_tool, list_openai_tools

logger = logging.getLogger(__name__)

ROUTER_BASE_URL = os.getenv("ROUTER_BASE_URL", "http://localhost:4000/v1")
ROUTER_API_KEY = os.getenv("ROUTER_API_KEY", "")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "auto")

OCP_MCP_URL = os.getenv("OCP_MCP_URL", "")
PROMETHEUS_MCP_URL = os.getenv("PROMETHEUS_MCP_URL", "http://prometheus-mcp-server.agentic-triage.svc:8080/mcp")
TICKETING_MCP_URL = os.getenv("TICKETING_MCP_URL", "http://ticketing-mcp-server.agentic-triage.svc:8080/mcp")
KNOWLEDGE_FILE = os.getenv("KNOWLEDGE_FILE", "/app/knowledge.md")

AGENT_TIMEOUT_SECONDS = int(os.getenv("AGENT_TIMEOUT_SECONDS", "600"))
MAX_INFER_ITERS = int(os.getenv("MAX_INFER_ITERS", "18"))

_client = AsyncOpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY or "unset")

# Cache of (tool_defs, tool_to_server): the MCP servers are stateless and their tool
# list does not change at runtime, so discovering it once per process is enough.
_tools_cache: tuple[list[dict], dict[str, str]] | None = None


def _mcp_servers() -> list[McpServer]:
    servers = [
        McpServer("Prometheus MCP", PROMETHEUS_MCP_URL),
        McpServer("Ticketing System MCP", TICKETING_MCP_URL),
    ]
    if OCP_MCP_URL:
        servers.append(McpServer("OpenShift MCP", OCP_MCP_URL))
    return servers


async def _tools() -> tuple[list[dict], dict[str, str]]:
    global _tools_cache
    if _tools_cache is None:
        _tools_cache = await list_openai_tools(_mcp_servers())
        if not _tools_cache[0]:
            logger.warning("No MCP tools discovered — the agent will answer from the knowledge base alone")
    return _tools_cache


def _load_knowledge() -> str:
    try:
        with open(KNOWLEDGE_FILE) as f:
            content = f.read().strip()
        logger.info("Loaded knowledge base from %s (%d chars)", KNOWLEDGE_FILE, len(content))
        return content
    except FileNotFoundError:
        logger.warning("Knowledge file not found at %s — running without app context", KNOWLEDGE_FILE)
        return ""


SYSTEM_PROMPT = """You are an OpenShift SRE troubleshooting assistant. Diagnose application \
problems using the tools available to you, then open an incident ticket and report your findings.

Tool sources: Prometheus MCP (query_prometheus, query_prometheus_range), Ticketing System \
MCP (create_incident, list_incidents, get_incident, update_incident, add_work_note), and \
optionally an OpenShift MCP server (pod/log/event tools) if one is configured.

Useful PromQL for the Quarkus demo app (metrics are summary type — use _count and _sum, \
never _bucket; the namespace is given in the Application Knowledge Base below):
- HTTP 5xx: rate(http_server_requests_seconds_count{outcome="SERVER_ERROR"}[5m])
- HTTP 503: rate(http_server_requests_seconds_count{status="503"}[5m])
- Avg latency: rate(http_server_requests_seconds_sum[5m]) / rate(http_server_requests_seconds_count[5m])
- Pod restarts: kube_pod_container_status_restarts_total
- If a query returns no data after 2 attempts, stop retrying and report metrics as missing.
- Call each tool at most once per fact you need; do not repeat an identical call.

Workflow:
1. If an OpenShift MCP server is available, list pods in the target namespace. Note status and restarts.
2. Query Prometheus for error rates and latency.
3. Synthesize a diagnosis from the data you collected.
4. Call create_incident with: short_description (concise summary), description (your full \
diagnosis), impact (1=High, 2=Medium, 3=Low), urgency (1/2/3), category ("Application" or \
"Infrastructure"). Skip this step if you were asked to investigate an existing incident \
instead (use get_incident / update_incident / add_work_note).
5. Output your diagnosis report.

Report format:
## Diagnosis Summary
**Application:** <name> | **Namespace:** <namespace>
**Symptoms:** <what you found>
**Root Cause:** <why it is happening>
**Affected Endpoints:** <endpoint, error rate, issue>
**Recommended Fix:** <actionable steps>
**Incident:** <ticket number, if one was created or updated>
"""


async def run_agent(user_message: str) -> AsyncIterator[str]:
    """Stream the agent's response for a given user message.

    Runs its own tool-calling loop against the router (OpenAI-compatible chat
    completions API): each round either returns tool_calls (executed against the MCP
    servers, results appended as role="tool" messages) or a final answer.
    """
    tool_defs, tool_to_server = await _tools()
    knowledge = _load_knowledge()
    instructions = (
        f"{SYSTEM_PROMPT}\n\n---\n## Application Knowledge Base\n\n{knowledge}"
        if knowledge
        else SYSTEM_PROMPT
    )

    messages: list[dict] = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": user_message},
    ]

    tool_calls_made = 0
    tool_failures = 0

    try:
        async with asyncio.timeout(AGENT_TIMEOUT_SECONDS):
            for _ in range(MAX_INFER_ITERS):
                logger.info("Calling %s (model=%s), %d messages so far", ROUTER_BASE_URL, ROUTER_MODEL, len(messages))
                response = await _client.chat.completions.create(
                    model=ROUTER_MODEL,
                    messages=messages,
                    tools=tool_defs or None,
                    tool_choice="auto" if tool_defs else None,
                )
                choice = response.choices[0]
                message = choice.message

                if not message.tool_calls:
                    content = message.content or ""
                    if not content:
                        logger.warning("Model returned no content and no tool calls")
                    yield content
                    return

                # Echo the assistant turn (with its tool_calls) back into history, then
                # run every requested tool call and append its result.
                messages.append(message.model_dump(exclude_none=True))
                for call in message.tool_calls:
                    name = call.function.name
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    server_url = tool_to_server.get(name)
                    if server_url is None:
                        result_text = f"Error: unknown tool {name!r} (not registered by any MCP server)"
                        tool_failures += 1
                    else:
                        tool_calls_made += 1
                        yield f"\n\n> 🔧 Calling `{name}`…\n\n"
                        result_text = await call_tool(server_url, name, arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": result_text,
                        }
                    )
    except TimeoutError:
        logger.error("Agent exceeded %ds timeout", AGENT_TIMEOUT_SECONDS)
        yield (
            f"\n\n⚠️ **Error:** The agent took longer than {AGENT_TIMEOUT_SECONDS}s and was stopped. "
            "This usually means the model got stuck in a tool-calling loop. Please try again "
            "with a more specific question.\n"
        )
        return
    except Exception as exc:
        logger.exception("Error during agent run")
        yield f"\n\n⚠️ **Error:** Agent encountered an error: {exc}\n"
        return
    finally:
        logger.info("Agent run finished — tool_calls=%d, tool_failures=%d", tool_calls_made, tool_failures)

    yield (
        f"\n\n⚠️ **Error:** Stopped after {MAX_INFER_ITERS} tool-calling rounds without a final "
        "answer. Please try again with a more specific question.\n"
    )
