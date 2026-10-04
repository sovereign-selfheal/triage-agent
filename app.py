"""
Triage Agent — Gradio Chat UI.

Launches a Gradio chat interface that streams responses from agent.py, which delegates
the actual agentic loop to the OGX sidecar (server-side Responses API): OGX calls the
platform's own LiteLLM router for inference and has native MCP tool calling to
Prometheus and ticketing-system (and optionally an OpenShift MCP server, if configured).
"""

from __future__ import annotations

import asyncio
import logging
import os

# Disable Gradio telemetry — prevents outbound calls to api.gradio.app / HuggingFace
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

import gradio as gr
from fastapi import BackgroundTasks, FastAPI
from gradio import mount_gradio_app
from pydantic import BaseModel

from agent import run_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Quick-start prompts shown as example buttons ────────────────────────────
EXAMPLE_PROMPTS = [
    "Troubleshoot the application quarkus-buggy-app in the agentic-triage namespace. Check Prometheus metrics for error rates and latency, and give me a full diagnosis.",
    "What HTTP endpoints in agentic-triage are returning 5xx errors? Show me the error rates from Prometheus.",
    "Investigate incident INC0000001: read it, troubleshoot the reported issue, and update the ticket with your findings.",
]

HEADER_HTML = """
<div style="text-align:center; padding: 12px 0 4px;">
  <h1 style="font-size:1.8rem; margin:0;">🔍 AI Triage Agent</h1>
  <p style="color:#666; margin:4px 0 0;">
    Powered by the platform's own LiteLLM router · Prometheus / Thanos · Ticketing System
  </p>
</div>
"""


async def chat(message: str, history: list[list[str]]) -> gr.ChatMessage:
    """
    Gradio streaming chat handler.

    Receives the user message and conversation history, streams the agent
    response back chunk by chunk.
    """
    if not message.strip():
        yield history + [{"role": "assistant", "content": "Please enter a question or troubleshooting request."}]
        return

    logger.info("User: %s", message[:120])

    # Add user turn immediately
    history = history + [{"role": "user", "content": message}]
    yield history

    # Stream agent response
    response_so_far = ""
    history = history + [{"role": "assistant", "content": ""}]

    async for chunk in run_agent(message):
        response_so_far += chunk
        history[-1] = {"role": "assistant", "content": response_so_far}
        yield history


def build_ui() -> gr.Blocks:
    with gr.Blocks(
        title="AI Triage Agent",
        theme=gr.themes.Soft(
            primary_hue="red",
            secondary_hue="orange",
            neutral_hue="slate",
        ),
        css="""
        .chatbot { background: #ffffff !important; border-radius: 8px; }
        .chatbot .message { font-size: 0.95rem; }
        .chatbot pre { background: #1e1e2e; color: #cdd6f4; padding: 12px; border-radius: 6px; }
        footer { display: none !important; }
        """,
    ) as demo:
        gr.HTML(HEADER_HTML)

        chatbot = gr.Chatbot(
            label="Troubleshooter",
            height=560,
            show_copy_button=True,
            render_markdown=True,
            type="messages",
            avatar_images=(
                None,
                "https://www.redhat.com/cms/managed-files/Asset-Red_Hat-Logo_page-Logo-RGB.svg",
            ),
        )

        with gr.Row():
            txt = gr.Textbox(
                placeholder="Ask the agent to troubleshoot an application, check pod status, query Prometheus…",
                show_label=False,
                scale=9,
                container=False,
            )
            send_btn = gr.Button("Send", variant="primary", scale=1, min_width=80)

        gr.Examples(
            examples=EXAMPLE_PROMPTS,
            inputs=txt,
            label="Quick-start prompts",
        )

        with gr.Accordion("ℹ️ About this demo", open=False):
            gr.Markdown(
                """
                This AI agent autonomously troubleshoots applications deployed on OpenShift.

                **How it works:**
                1. You describe the problem (or ask for a full health check)
                2. The agent queries **Prometheus / Thanos** for HTTP error rates and latency metrics
                3. The agent reads and updates incidents through the **ticketing-system MCP server**
                4. The platform's own LiteLLM router (local model, or SOTA when configured)
                   synthesises the findings into a structured diagnosis

                **Target application:** `quarkus-buggy-app` in the `agentic-triage` namespace  
                Intentional errors: 30% HTTP 500 on `/api/products`, 40% HTTP 503 on `/api/inventory`,
                20% 3-second delay on `/api/orders`
                """
            )

        # Wire up events
        submit_event = txt.submit(
            fn=chat,
            inputs=[txt, chatbot],
            outputs=[chatbot],
        )
        submit_event.then(lambda: "", outputs=[txt])

        send_event = send_btn.click(
            fn=chat,
            inputs=[txt, chatbot],
            outputs=[chatbot],
        )
        send_event.then(lambda: "", outputs=[txt])

    return demo


# ── Programmatic trigger endpoint (used by ogx-alert-translator) ───────────────
#
# This is the single entry point for callers that are not the Gradio UI (today: the
# Alertmanager bridge). It reuses `run_agent()` from agent.py — the one place that owns
# the system prompt, MCP tool wiring, knowledge base and model selection — so no other
# service re-implements or duplicates the agent definition. Callers only supply the
# user-facing input text; everything else about "how the agent investigates" lives here.
class TriggerRequest(BaseModel):
    input: str


async def _run_and_log(message: str) -> None:
    """Run the agent to completion in the background and log the outcome.

    Mirrors the UI's streaming consumption in `chat()`, but discards the chunks (no
    caller is listening synchronously) — only the final length/outcome is logged.
    """
    logger.info("Triggered run starting: %s", message[:120])
    response_so_far = ""
    async for chunk in run_agent(message):
        response_so_far += chunk
    logger.info("Triggered run finished (%d chars)", len(response_so_far))


def build_app() -> FastAPI:
    """Build the FastAPI app: Gradio UI mounted at '/', plus the /trigger API route."""
    fastapi_app = FastAPI(title="triage-agent")

    @fastapi_app.post("/trigger", status_code=202)
    async def trigger(payload: TriggerRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
        if not payload.input.strip():
            return {"status": "rejected", "reason": "empty_input"}
        background_tasks.add_task(_run_and_log, payload.input)
        return {"status": "accepted"}

    demo = build_ui()
    return mount_gradio_app(fastapi_app, demo, path="/")


app = build_app()

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("GRADIO_PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
