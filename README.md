# triage-agent

Gradio chat UI backed by an **OGX sidecar** (`ogxai/distribution-starter`, server-side
Responses API) that investigates `quarkus-buggy-app` using the `prometheus-mcp-server`
and `ticketing-mcp-server` MCP tools.

Unlike the upstream demo this is based on, this agent has **no external Nemotron/MaaS
endpoint**: OGX is configured to call the platform's own LiteLLM router
(`VLLM_URL` = `https://router.<appsDomain>/v1`, model `auto`) instead, so the same
policy hook, privacy gate and local/SOTA routing that apply to every other client also
apply to this agent's traffic. Everything else about OGX — the server-side agentic loop,
native MCP tool calling, streaming — is unchanged from upstream.

## Layout

- `app.py` — Gradio UI (chat, streams `agent.py`'s output).
- `agent.py` — thin client of the OGX Responses API (`ogx_client.AsyncOgxClient`): sends
  one `responses.create(..., tools=[{"type": "mcp", "server_url": ...}, ...], stream=True)`
  call per user message and relays the streamed text/tool-call events to the UI. The
  actual ReAct loop (calling tools, feeding results back to the model, looping until a
  final answer) runs server-side, inside the OGX sidecar — not in this repo.
- `start.sh` — waits for the OGX sidecar's `/v1/models` to answer before starting Gradio
  (the two containers of the pod start independently).
- `knowledge.md` — sample knowledge base for local dev (see `KNOWLEDGE_FILE`). On cluster,
  gitops mounts the real content from a ConfigMap (`components/triage-agent/files/knowledge.md`)
  so each agent instance can ship different knowledge without rebuilding the image.

This repo does **not** own the OGX image or its config (`stack_run_config.yaml`): those
are the gitops repo's ownership, same as any other Kubernetes object (see
`gitops/components/triage-agent/templates/stack-run-config.yaml`).

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OGX_BASE_URL` | `http://localhost:8321` | OGX sidecar, same pod |
| `ROUTER_MODEL` | `auto` | Model name passed to OGX; must match the model registered in the sidecar's `stack_run_config.yaml` |
| `PROMETHEUS_MCP_URL` | `http://prometheus-mcp-server.agentic-triage.svc:8080/mcp` | |
| `TICKETING_MCP_URL` | `http://ticketing-mcp-server.agentic-triage.svc:8080/mcp` | |
| `OCP_MCP_URL` | _(empty)_ | Optional Kubernetes-API MCP server, not part of this import; omitted from the tool list when empty |
| `KNOWLEDGE_FILE` | `/etc/triage-agent/knowledge.md` | Path to the mounted knowledge file; set to `./knowledge.md` for local dev |
| `MAX_INFER_ITERS` | `18` | Max tool-calling rounds per user message (passed to OGX as `extra_body.max_infer_iters`) |
| `AGENT_TIMEOUT_SECONDS` | `600` | Hard timeout for one agent run |

The router credential (`VLLM_API_TOKEN`) and the router URL (`VLLM_URL`) are OGX sidecar
env vars, not read by this repo's Python code — see the gitops Deployment.

## Local development

Requires a running OGX server pointed at some OpenAI-compatible backend (the platform
router, or any local vLLM/Ollama endpoint) — see the [OGX
docs](https://github.com/ogxai/distribution-starter) for `stack_run_config.yaml` and how
to run it standalone (e.g. via `uvicorn` or its own container image).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export OGX_BASE_URL=http://localhost:8321
export PROMETHEUS_MCP_URL=http://localhost:8081/mcp   # oc port-forward
export TICKETING_MCP_URL=http://localhost:8082/mcp    # oc port-forward
export KNOWLEDGE_FILE="$(pwd)/knowledge.md"
python app.py
# Open http://localhost:7860
```

## Build

```bash
podman build -t quay.io/sovereign-selfheal/triage-agent:<tag> .
```

CI (`.github/workflows/build.yml`) runs on pull requests and pushes to `main` to verify
that this image builds (no push). Quay builds/pushes the tagged release image separately.
This only concerns the Gradio UI image; the OGX sidecar image
(`ogxai/distribution-starter`) is a third-party image pulled straight from Docker Hub,
pinned by digest in the gitops repo.

## Consumer

Kubernetes manifests (ServiceAccount, tier credential, OGX and knowledge ConfigMaps,
Deployment with both containers, Service, Route) and the pinned image digests live in the
`gitops` repo, `components/triage-agent/`. This repo only owns the source and the build of
the Gradio UI container; deploy-time knowledge lives under
`gitops/components/triage-agent/files/`.
