"""Command-line entry point: `patchpilot <project-path>`."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from dotenv import load_dotenv

from patchpilot.agent import run_agent
from patchpilot.tools import Workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patchpilot",
        description="Analyze a Python project's dependencies and report what to upgrade.",
    )
    parser.add_argument("project", type=Path, help="Path to the project to analyze.")
    parser.add_argument(
        "--test-command",
        default="python -m pytest -q",
        help="Command that runs the project's tests (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    project = args.project.expanduser().resolve()
    if not project.is_dir():
        parser.error(f"{project} is not a directory")

    load_dotenv()
    ws = Workspace(root=project, test_command=shlex.split(args.test_command))

    def log_tool_call(name: str, tool_input: dict) -> None:
        print(f"→ {name} {json.dumps(tool_input)}", file=sys.stderr)

    try:
        report = run_agent(ws, on_tool_call=log_tool_call)
    except RuntimeError as e:
        print(f"patchpilot: {e}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
