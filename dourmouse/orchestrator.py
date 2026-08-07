"""Lead Orchestrator (Phase 1) — NVIDIA NIM backed.

ARCHITECTURE DEVIATION FROM BUILD PROMPT SECTION 4, flagged and agreed with
the user: the orchestrator was originally built on the Claude Agent SDK
(driving the local `claude` CLI). Per explicit user decision ("everything run
off nvidia, the best nvidia model practically usable"), it now runs on NVIDIA
NIM via its OpenAI-compatible API instead. NIM has no equivalent
agent/dispatch framework, so this module hand-rolls the tool-calling loop
that claude_agent_sdk previously provided for free: send messages + tool
specs, receive tool_calls, execute them, feed results back, repeat.

Per Architecture Contract Section 4, this orchestrator NEVER places orders
itself. Per Rule 2.8, none of this touches the deterministic Risk/Guardrail
engine (dourmouse/guardrails.py) — that module has no LLM in its path,
regardless of which model drives the orchestrator.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from openai import OpenAI

from dourmouse.config import NvidiaConfig, OllamaConfig, load_llm_config
from dourmouse.research_agent import RESEARCH_TOOL_SPEC, call_research_tool

_SYSTEM_PROMPT = (
    "You are the Dourmouse Lead Orchestrator. You interpret requests and "
    "delegate to subagent tools. You never place trades yourself — that is "
    "the Execution Agent's job, and it does not exist yet in this build. "
    "When asked to research something, use the run_atlas_research tool. "
    "If a tool reports NOT CONFIGURED or ATLAS RUN FAILED, report that "
    "honestly in your final answer — never invent research results yourself."
)

_TOOLS: list[dict[str, Any]] = [RESEARCH_TOOL_SPEC]

_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "run_atlas_research": call_research_tool,
}


def _build_client(config: NvidiaConfig) -> OpenAI:
    return OpenAI(api_key=config.api_key, base_url=config.base_url)


def dispatch(
    prompt: str,
    max_turns: int = 6,
    client: Any | None = None,
    config: NvidiaConfig | None = None,
) -> dict[str, Any]:
    """Send one request through the NVIDIA-backed orchestrator.

    `client`/`config` are injectable for isolated testing (Integration Rule
    7.3) without a real NVIDIA_API_KEY or network call. In production,
    neither is passed: config loads from .env, client is a real OpenAI SDK
    instance pointed at NVIDIA NIM.

    Returns {"final_text": str, "transcript": [...]}. Raises whatever the
    NVIDIA API raises (auth errors, etc.) rather than masking failures.
    """
    if client is None:
        config = config or load_llm_config()
        client = _build_client(config)
        model = config.model
    else:
        # Test double path: model name doesn't matter, the fake client
        # ignores it, but load it if a config was still supplied.
        model = config.model if config is not None else "test-model"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    transcript: list[dict[str, Any]] = []

    # v4.0: thinking-tuned local models need enable_thinking=False (Ollama)
    # or the token budget is consumed by reasoning before any content.
    extra_body = {"enable_thinking": False} if isinstance(config, OllamaConfig) else None

    for _ in range(max_turns):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
            extra_body=extra_body,
        )
        message = response.choices[0].message

        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            text = message.content or ""
            transcript.append({"type": "assistant_text", "text": text})
            return {"final_text": text, "transcript": transcript}

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }
        messages.append(assistant_msg)

        for tool_call in tool_calls:
            name = tool_call.function.name
            transcript.append(
                {
                    "type": "tool_use",
                    "name": name,
                    "raw_arguments": tool_call.function.arguments,
                }
            )
            handler = _TOOL_HANDLERS.get(name)
            if handler is None:
                result_text = (
                    f"ERROR: unknown tool '{name}' — not in this "
                    "orchestrator's declared roster."
                )
            else:
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError as exc:
                    result_text = (
                        f"ERROR: model returned invalid JSON tool arguments: {exc}"
                    )
                else:
                    result_text = handler(arguments)

            transcript.append({"type": "tool_result", "name": name, "text": result_text})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_text,
                }
            )

    transcript.append({"type": "result", "is_error": True, "reason": "max_turns exceeded"})
    return {"final_text": "", "transcript": transcript}


if __name__ == "__main__":
    import sys

    user_prompt = " ".join(sys.argv[1:]) or "Run ATLAS research on SPY."
    report = dispatch(user_prompt)
    print(json.dumps(report, indent=2, default=str))
