"""Command-line entry point.

patchpilot analyze <project>            read-only upgrade report
patchpilot upgrade <project> <package>  upgrade one package and fix what breaks
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

import anthropic
from dotenv import find_dotenv, load_dotenv

from patchpilot.agent import MODEL
from patchpilot.analyze import analyze
from patchpilot.github import GitHubError
from patchpilot.sandbox import DEFAULT_IMAGE, SandboxError
from patchpilot.tools import Workspace
from patchpilot.upgrade import UpgradeError, upgrade


def log_tool_call(name: str, tool_input: dict) -> None:
    # File contents can be long; show just the path for writes.
    shown = {k: v for k, v in tool_input.items() if k not in ("content", "old_text", "new_text")}
    print(f"  → {name} {json.dumps(shown)}", file=sys.stderr)


def explain_api_error(e: anthropic.AnthropicError) -> str:
    """Turn an Anthropic SDK error into one sentence the user can act on."""
    detail = str(e)
    if isinstance(e, anthropic.APIStatusError) and isinstance(e.body, dict):
        detail = e.body.get("error", {}).get("message", detail)

    if isinstance(e, anthropic.BadRequestError) and "credit balance" in detail:
        return (
            "Your Anthropic account is out of credit. Add some at "
            "https://console.anthropic.com/settings/billing and run patchpilot again."
        )
    if isinstance(e, anthropic.AuthenticationError):
        return (
            "Your Anthropic API key was rejected. Check ANTHROPIC_API_KEY in your .env file "
            "(keys are at https://console.anthropic.com/settings/keys)."
        )
    if isinstance(e, anthropic.PermissionDeniedError):
        return f"Your API key doesn't have access to {MODEL}: {detail}"
    if isinstance(e, anthropic.RateLimitError):
        return "The Anthropic API is rate-limiting this key. Wait a minute and try again."
    if isinstance(e, anthropic.APIConnectionError):
        return "Couldn't reach the Anthropic API. Check your internet connection and try again."
    if isinstance(e, anthropic.APIStatusError):
        return f"The Anthropic API returned an error ({e.status_code}): {detail}"
    # e.g. no API key configured at all
    return f"Couldn't call the Anthropic API: {detail}"


def log_status(message: str) -> None:
    print(f"• {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patchpilot",
        description="An AI agent that upgrades your dependencies and fixes what breaks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    analyze_p = sub.add_parser("analyze", help="Report what could be upgraded (changes nothing).")
    analyze_p.add_argument("project", type=Path, help="Path to the project.")

    upgrade_p = sub.add_parser(
        "upgrade", help="Upgrade one package, fix the code, and commit it to a new branch."
    )
    upgrade_p.add_argument("project", type=Path, help="Path to the project (inside a git repo).")
    upgrade_p.add_argument("package", help="Name of the package to upgrade, e.g. pydantic.")
    upgrade_p.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Docker image to run the project in; pick one matching your Python version "
        "(default: %(default)s).",
    )
    upgrade_p.add_argument(
        "--pr",
        action="store_true",
        help="Push the branch and open a GitHub pull request (needs the gh CLI, logged in).",
    )
    upgrade_p.add_argument(
        "--no-docker",
        action="store_true",
        help="Run in a local virtualenv instead of Docker. Faster, but the project's code "
        "runs directly on your machine.",
    )

    for p in (analyze_p, upgrade_p):
        p.add_argument(
            "--test-command",
            default="python -m pytest -q",
            help="Command that runs the project's tests (default: %(default)s).",
        )

    args = parser.parse_args(argv)
    project = args.project.expanduser().resolve()
    if not project.is_dir():
        parser.error(f"{project} is not a directory")
    test_command = shlex.split(args.test_command)

    # Look for .env in the directory patchpilot is run from, not where it's installed.
    load_dotenv(find_dotenv(usecwd=True))

    try:
        if args.command == "analyze":
            ws = Workspace(root=project, test_command=test_command)
            print(analyze(ws, on_tool_call=log_tool_call))
            return 0

        result = upgrade(
            project,
            args.package,
            test_command=test_command,
            on_status=log_status,
            on_tool_call=log_tool_call,
            use_docker=not args.no_docker,
            image=args.image,
            open_pr=args.pr,
        )
    except (RuntimeError, UpgradeError, SandboxError, GitHubError) as e:
        print(f"patchpilot: {e}", file=sys.stderr)
        return 1
    except anthropic.AnthropicError as e:
        print(f"patchpilot: {explain_api_error(e)}", file=sys.stderr)
        return 1

    print(result.report)
    print(file=sys.stderr)
    if not result.committed:
        log_status("The agent didn't change any files, so no branch was created.")
        return 1
    verdict = "✅ tests pass" if result.tests_passed else "❌ tests still failing"
    log_status(f"Committed to branch {result.branch} ({verdict})")
    if result.pr_url:
        log_status(f"Pull request: {result.pr_url}")
    else:
        log_status(f"Review it with: git diff HEAD...{result.branch}")
    return 0 if result.tests_passed else 1


if __name__ == "__main__":
    sys.exit(main())
