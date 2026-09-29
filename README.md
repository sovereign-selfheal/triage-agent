# triage-agent

Gradio chat UI + tool-calling agent that investigates `quarkus-buggy-app` using the
`prometheus-mcp-server` and `ticketing-mcp-server` MCP tools.

Unlike the upstream demo this is based on, this agent has **no OGX sidecar and no
external Nemotron/MaaS endpoint**: `agent.py` is a plain OpenAI-compatible client that
runs its own tool-calling loop directly against the platform's LiteLLM router
(`ROUTER_BASE_URL`, model `auto`), so the same policy hook, privacy gate and local/SOTA
routing that apply to every other client also apply to this agent's traffic.

## Layout

- `app.py` — Gradio UI (chat, streams `agent.py`'s output).
- `agent.py` — the tool-calling loop: calls the router's `/chat/completions` with
  `tools=`, executes any `tool_calls` against the MCP servers, repeats until a final
  answer or `MAX_INFER_ITERS` is reached.
- `mcp_tools.py` — MCP client (`streamable-http` transport): discovers tools from one or
  more MCP servers and calls them.
- `knowledge.md` — static knowledge base about `quarkus-buggy-app`, baked into the image
  (`COPY knowledge.md` in the `Dockerfile`). Edit it here and release a new tag; the
  gitops repo only pins the resulting image digest, same as `app.py`/`agent.py`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ROUTER_BASE_URL` | `http://localhost:4000/v1` | Platform LiteLLM router, OpenAI-compatible |
| `ROUTER_API_KEY` | _(empty)_ | Tier API key (gitops mounts it from a generated Secret) |
| `ROUTER_MODEL` | `auto` | Model alias; `auto` lets the router's policy hook decide |
| `PROMETHEUS_MCP_URL` | `http://prometheus-mcp-server.agentic-triage.svc:8080/mcp` | |
| `TICKETING_MCP_URL` | `http://ticketing-mcp-server.agentic-triage.svc:8080/mcp` | |
| `OCP_MCP_URL` | _(empty)_ | Optional Kubernetes-API MCP server, not part of this import |
| `KNOWLEDGE_FILE` | `/app/knowledge.md` | Baked into the image; override only for local dev |
| `MAX_INFER_ITERS` | `18` | Max tool-calling rounds per user message |
| `AGENT_TIMEOUT_SECONDS` | `600` | Hard timeout for one agent run |

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ROUTER_BASE_URL=https://router.<appsDomain>/v1
export ROUTER_API_KEY=<a tier API key>
export PROMETHEUS_MCP_URL=http://localhost:8081/mcp   # oc port-forward
export TICKETING_MCP_URL=http://localhost:8082/mcp    # oc port-forward
python app.py
# Open http://localhost:7860
```

## Build

```bash
podman build -t quay.io/sovereign-selfheal/triage-agent:<tag> .
```

CI (`.github/workflows/build.yml`) does this on every `v*` tag.

## Consumer

Kubernetes manifests (ServiceAccount, tier credential, Deployment, Service, Route) and
the pinned image digest live in the `gitops` repo, `components/triage-agent/`. This repo
only owns the source and the build.
