"""`patchpilot analyze`: a read-only report of what could be upgraded."""

from __future__ import annotations

from collections.abc import Callable

from patchpilot.agent import run_agent
from patchpilot.tools import READ_TOOL_SCHEMAS, Workspace

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


def analyze(ws: Workspace, on_tool_call: Callable[[str, dict], None] | None = None) -> str:
    return run_agent(
        ws,
        system=SYSTEM_PROMPT,
        task=f"Analyze the project at {ws.root} and write the report.",
        tools=READ_TOOL_SCHEMAS,
        on_tool_call=on_tool_call,
    )
