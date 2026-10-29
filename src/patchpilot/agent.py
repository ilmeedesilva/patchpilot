"""The agent loop: send the conversation to Claude, run the tools it asks for, repeat."""

from __future__ import annotations

from collections.abc import Callable

import anthropic

from patchpilot.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, ToolError, Workspace

MODEL = "claude-opus-5-5"
MAX_TURNS = 30

SYSTEM_PROMPT = """\
You are patchpilot, an agent that keeps a Python project's dependencies up to date.

Your job right now is to produce an upgrade report. Use the tools to:
1. Read the project's declared dependencies.
2. Look up the latest version of each one on PyPI.
3. Run the test suite to establish a baseline.

Then finish with a concise report in Markdown:
- A table of dependencies: name, current specifier, latest version, and whether the
  current specifier already allows the latest version.
- Which upgrades look like major-version jumps that are likely to need code changes.
- The baseline test result.
- A recommended order to upgrade in, with one line of reasoning each.

You can only read and run tests in this phase; do not suggest that you changed any files.\
"""


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
    client: anthropic.Anthropic | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
) -> str:
    """Run the agent until it finishes and return its final text."""
    client = client or anthropic.Anthropic()
    messages: list[dict] = [
        {"role": "user", "content": f"Analyze the project at {ws.root} and write the report."}
    ]

    for _ in range(MAX_TURNS):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
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
            result, is_error = execute_tool(ws, block.name, block.input)
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

    raise RuntimeError(f"Agent did not finish within {MAX_TURNS} turns.")
