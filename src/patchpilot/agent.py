"""The agent loop: send the conversation to Claude, run the tools it asks for, repeat."""

from __future__ import annotations

from collections.abc import Callable

import anthropic

from patchpilot.tools import TOOL_FUNCTIONS, ToolError, Workspace

MODEL = "claude-opus-5-5"

# Anthropic-hosted tool: Claude fetches URLs (like changelogs) itself, no code needed on our side.
# It only fetches URLs that already appear in the conversation, e.g. from get_latest_version.
WEB_FETCH_TOOL = {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8}


def execute_tool(ws: Workspace, name: str, tool_input: dict) -> tuple[str, bool]:
    """Run one tool. Returns (result_text, is_error)."""
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Unknown tool '{name}'.", True
    try:
        return func(ws, **tool_input), False
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return f"Invalid arguments for '{name}': {e}", True


def run_agent(
    ws: Workspace,
    system: str,
    task: str,
    tools: list[dict],
    max_turns: int = 30,
    client: anthropic.Anthropic | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
) -> str:
    """Run the agent until it finishes and return its final text."""
    client = client or anthropic.Anthropic()
    allowed = {t["name"] for t in tools if "input_schema" in t}
    messages: list[dict] = [{"role": "user", "content": task}]

    for _ in range(max_turns):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": "high"},
            # If a request is declined by a safety classifier, retry on the server's default
            # fallback model instead of failing outright.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Response hit max_tokens before finishing.")
        if response.stop_reason == "pause_turn":
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return "\n".join(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in tool_uses:
            if on_tool_call:
                on_tool_call(block.name, block.input)
            if block.name in allowed:
                result, is_error = execute_tool(ws, block.name, block.input)
            else:
                result, is_error = f"Tool '{block.name}' is not available here.", True
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                }
            )
        # All results for one turn go back in a single user message.
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"Agent did not finish within {max_turns} turns.")
