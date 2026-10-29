"""`patchpilot upgrade`: upgrade one package, fix what breaks, commit the result to a branch.

The user's own checkout is never modified. All work happens in a temporary git worktree
(a second checkout of the same repo) with its own virtualenv. Only the finished commit,
on a new `patchpilot/upgrade-<package>` branch, is left behind.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot.agent import WEB_FETCH_TOOL, run_agent
from patchpilot.tools import READ_TOOL_SCHEMAS, WRITE_TOOL_SCHEMAS, Workspace, run_tests

DEFAULT_TEST_COMMAND = ["python", "-m", "pytest", "-q"]

SYSTEM_PROMPT = """\
You are patchpilot, an agent that upgrades one dependency in a Python project and fixes
the code that breaks because of it.

You are working in an isolated copy of the project with its own virtualenv. The test
suite passed before you started. Your job:
1. Find out the latest version of the package and install it with pip_install.
2. Update the declared version specifier in requirements*.txt or pyproject.toml so it
   requires the new version.
3. Run the tests. If they fail, work out why: read the failing code, and read the package's
   changelog or migration guide with web_fetch (use URLs from get_latest_version).
4. Fix the project's code with small, targeted edits, then run the tests again. Repeat
   until they pass.

Rules:
- Only upgrade the requested package. If the new version moved features into a separate
  package (for example pydantic-settings), install it and add it to the declared
  dependencies too.
- Never delete, skip or weaken tests to make them pass. Change a test only when the test
  itself uses an API that was removed, and keep what it checks the same.
- Keep changes minimal and in the existing code style.
- If you can't get the tests passing, stop and explain what's left.

When you're done, reply with only this Markdown report (it becomes the commit message and
the pull request description):

## Summary
One or two sentences: what was upgraded, from which version to which.

## Breaking changes
Each breaking change in the new version that affected this project, and how it was fixed.

## Files changed
A bullet per file with a short description of the change.

## Needs your attention
Anything you couldn't fix or a human should double-check. Write "Nothing." if none.\
"""


class UpgradeError(Exception):
    """Upgrade could not start or finish; the message is meant for the user."""


@dataclass
class UpgradeResult:
    branch: str
    tests_passed: bool
    report: str
    changed_files: list[str] = field(default_factory=list)
    committed: bool = False


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise UpgradeError(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def _pip(python: Path, args: list[str], cwd: Path) -> None:
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", *args], cwd=cwd, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise UpgradeError(f"Installing the project failed:\n{proc.stderr.strip()[-3000:]}")


def install_project(python: Path, project: Path, test_command: list[str]) -> None:
    """Install the project and its declared dependencies into the given interpreter."""
    for req_file in sorted(project.glob("requirements*.txt")):
        _pip(python, ["-r", req_file.name], project)

    pyproject = project / "pyproject.toml"
    if pyproject.is_file() and "project" in tomllib.loads(pyproject.read_text()):
        extras = tomllib.loads(pyproject.read_text())["project"].get("optional-dependencies", {})
        target = f".[{','.join(extras)}]" if extras else "."
        _pip(python, ["-e", target], project)

    if test_command == DEFAULT_TEST_COMMAND:
        check = subprocess.run([str(python), "-m", "pytest", "--version"], capture_output=True)
        if check.returncode != 0:
            _pip(python, ["pytest"], project)


def upgrade(
    project: Path,
    package: str,
    test_command: list[str] | None = None,
    on_status: Callable[[str], None] = lambda _: None,
    on_tool_call: Callable[[str, dict], None] | None = None,
) -> UpgradeResult:
    test_command = test_command or DEFAULT_TEST_COMMAND
    project = project.resolve()
    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=project)).resolve()
    project_rel = project.relative_to(repo_root)
    branch = f"patchpilot/upgrade-{package.lower()}"

    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_root
    )
    if exists.returncode == 0:
        raise UpgradeError(
            f"Branch '{branch}' already exists. Delete it first: git branch -D {branch}"
        )

    tmp = Path(tempfile.mkdtemp(prefix="patchpilot-"))
    worktree = tmp / "repo"
    committed = False
    try:
        on_status(f"Creating an isolated copy of the repo on branch {branch}")
        _git("worktree", "add", "-q", "-b", branch, str(worktree), "HEAD", cwd=repo_root)
        work_project = worktree / project_rel

        on_status("Creating a fresh virtualenv and installing the project")
        subprocess.run([sys.executable, "-m", "venv", str(tmp / "venv")], check=True)
        python = tmp / "venv" / "bin" / "python"
        install_project(python, work_project, test_command)

        ws = Workspace(root=work_project, test_command=test_command, python=python)
        on_status("Running the tests before upgrading")
        baseline = run_tests(ws)
        if not baseline.startswith("PASSED"):
            raise UpgradeError(
                "The tests fail before any upgrade, so patchpilot can't tell what the upgrade "
                f"breaks. Fix them first.\n\n{baseline[-3000:]}"
            )

        on_status(f"Agent is upgrading {package}")
        report = run_agent(
            ws,
            system=SYSTEM_PROMPT,
            task=f"Upgrade '{package}' to its latest version in the project at {work_project}.",
            tools=[*READ_TOOL_SCHEMAS, *WRITE_TOOL_SCHEMAS, WEB_FETCH_TOOL],
            max_turns=60,
            on_tool_call=on_tool_call,
        )

        on_status("Re-running the tests to verify the agent's work")
        tests_passed = run_tests(ws).startswith("PASSED")
        result = UpgradeResult(
            branch=branch,
            tests_passed=tests_passed,
            report=report,
            changed_files=sorted(ws.changed_files),
        )

        if result.changed_files:
            paths = [str(project_rel / p) for p in result.changed_files]
            _git("add", "--", *paths, cwd=worktree)
            title = f"Upgrade {package}" + ("" if tests_passed else " (tests still failing)")
            _git("commit", "-q", "-m", title, "-m", report, cwd=worktree)
            result.committed = committed = True
        return result
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo_root,
            capture_output=True,
        )
        # No commit means nothing worth reviewing, so don't leave an empty branch behind.
        if not committed:
            subprocess.run(["git", "branch", "-D", branch], cwd=repo_root, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
