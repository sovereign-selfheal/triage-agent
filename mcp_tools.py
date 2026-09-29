"""
MCP tool discovery and invocation.

Talks to one or more MCP servers over the `streamable-http` transport (FastMCP servers,
same as prometheus-mcp-server and ticketing-mcp-server). Each server is stateless
(`mcp.stateless: true` in the gitops MCPServer CRs), so a short-lived session per call is
fine — no long-running connection to keep alive across a Gradio request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpServer:
    label: str
    url: str


async def list_openai_tools(servers: list[McpServer]) -> tuple[list[dict], dict[str, str]]:
    """
    Discover every tool of every reachable server.

    Returns (tool_defs, tool_to_server) where tool_defs is a list of OpenAI
    "function" tool definitions (input_schema translated to JSON schema "parameters"),
    and tool_to_server maps a tool name back to the server that serves it.

    A server that cannot be reached is skipped (logged as a warning): the agent still
    starts with whatever tools remain, rather than failing outright.
    """
    tool_defs: list[dict] = []
    tool_to_server: dict[str, str] = {}

    for server in servers:
        try:
            async with streamablehttp_client(server.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    for tool in result.tools:
                        if tool.name in tool_to_server:
                            logger.warning(
                                "Tool name collision: %s is served by both %s and %s",
                                tool.name, tool_to_server[tool.name], server.label,
                            )
                            continue
                        tool_defs.append(
                            {
                                "type": "function",
                                "function": {
                                    "name": tool.name,
                                    "description": tool.description or "",
                                    "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                                },
                            }
                        )
                        tool_to_server[tool.name] = server.url
                        logger.info("Discovered tool %s from %s", tool.name, server.label)
        except Exception:
            logger.warning("Could not reach MCP server %s (%s) — skipping its tools", server.label, server.url, exc_info=True)

    return tool_defs, tool_to_server


async def call_tool(server_url: str, name: str, arguments: dict[str, Any]) -> str:
    """Call one MCP tool and return its result as plain text for the model."""
    try:
        async with streamablehttp_client(server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                if result.isError:
                    logger.warning("Tool %s returned an error: %s", name, result.content)
                parts = []
                for item in result.content:
                    text = getattr(item, "text", None)
                    if text is not None:
                        parts.append(text)
                    else:
                        parts.append(str(item))
                return "\n".join(parts) if parts else "(no output)"
    except Exception as exc:
        logger.exception("Tool call failed: %s", name)
        return f"Error calling tool {name}: {exc}"
