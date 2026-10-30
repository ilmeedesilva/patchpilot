"""`patchpilot upgrade`: upgrade one package, fix what breaks, commit the result to a branch.

The user's own checkout is never modified. All work happens in a temporary git worktree
(a second checkout of the same repo), and the project's code only ever runs inside a
sandbox (see patchpilot.sandbox). Only the finished commit, on a new
`patchpilot/upgrade-<package>` branch, is left behind.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot import github
from patchpilot.agent import WEB_FETCH_TOOL, run_agent
from patchpilot.sandbox import DEFAULT_IMAGE, DockerSandbox, LocalSandbox
from patchpilot.tools import (
    IGNORED_DIRS,
    READ_TOOL_SCHEMAS,
    WRITE_TOOL_SCHEMAS,
    Workspace,
    collect_dependencies,
    normalize_name,
    run_tests,
)

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
    pr_url: str | None = None


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise UpgradeError(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def _pip(ws: Workspace, args: list[str]) -> None:
    proc = ws.run(["python", "-m", "pip", "install", "-q", *args], timeout=900)
    if proc.returncode != 0:
        raise UpgradeError(f"Installing the project failed:\n{proc.stderr.strip()[-3000:]}")


def install_project(ws: Workspace) -> None:
    """Install the project and its declared dependencies into the workspace's sandbox."""
    project = ws.root
    for req_file in sorted(project.glob("requirements*.txt")):
        _pip(ws, ["-r", req_file.name])

    pyproject = project / "pyproject.toml"
    if pyproject.is_file() and "project" in tomllib.loads(pyproject.read_text()):
        extras = tomllib.loads(pyproject.read_text())["project"].get("optional-dependencies", {})
        target = f".[{','.join(extras)}]" if extras else "."
        _pip(ws, ["-e", target])

    if ws.test_command == DEFAULT_TEST_COMMAND:
        if ws.run(["python", "-m", "pytest", "--version"], timeout=60).returncode != 0:
            _pip(ws, ["pytest"])


def _declares(directory: Path, package: str) -> list[str]:
    """Which of the directory's dependency files declare the package."""
    wanted = normalize_name(package)
    return [
        source
        for source, deps in collect_dependencies(directory).items()
        if any(normalize_name(d["name"]) == wanted for d in deps)
    ]


def check_declared(project: Path, package: str, repo_root: Path) -> None:
    """Fail early, with a hint, if the package isn't a dependency of this project.

    Otherwise the agent would run against a test suite that never exercises the package,
    and "tests pass" would mean nothing.
    """
    if _declares(project, package):
        return
    elsewhere = []
    for dep_file in sorted(
        [*repo_root.rglob("requirements*.txt"), *repo_root.rglob("pyproject.toml")]
    ):
        rel = dep_file.relative_to(repo_root)
        if any(part in IGNORED_DIRS for part in rel.parts) or dep_file.parent == project:
            continue
        if dep_file.parent not in elsewhere and _declares(dep_file.parent, package):
            elsewhere.append(dep_file.parent)

    message = f"{package} isn't a dependency of {project}."
    if elsewhere:
        dirs = [os.path.relpath(d) for d in elsewhere]
        message += "\nIt's declared in: " + ", ".join(dirs)
        message += f"\nTry: patchpilot upgrade {dirs[0]} {package}"
    raise UpgradeError(message)


def git_root(project: Path) -> Path:
    return Path(_git("rev-parse", "--show-toplevel", cwd=project)).resolve()


def upgrade(
    project: Path,
    package: str,
    test_command: list[str] | None = None,
    on_status: Callable[[str], None] = lambda _: None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    use_docker: bool = True,
    image: str = DEFAULT_IMAGE,
    open_pr: bool = False,
) -> UpgradeResult:
    test_command = test_command or DEFAULT_TEST_COMMAND
    project = project.resolve()
    repo_root = git_root(project)
    project_rel = project.relative_to(repo_root)
    check_declared(project, package, repo_root)
    branch = f"patchpilot/upgrade-{package.lower()}"

    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_root
    )
    if exists.returncode == 0:
        raise UpgradeError(
            f"Branch '{branch}' already exists. Delete it first: git branch -D {branch}"
        )
    if use_docker:
        DockerSandbox.check_available()
    # Check GitHub before spending anything on the agent, not after.
    base = github.check_ready(repo_root, branch) if open_pr else None

    tmp = Path(tempfile.mkdtemp(prefix="patchpilot-"))
    worktree = tmp / "repo"
    committed = False
    with ExitStack() as cleanup:
        cleanup.callback(_remove_worktree, repo_root, worktree, branch, lambda: committed, tmp)

        on_status(f"Creating an isolated copy of the repo on branch {branch}")
        _git("worktree", "add", "-q", "-b", branch, str(worktree), "HEAD", cwd=repo_root)
        work_project = worktree / project_rel

        if use_docker:
            on_status(f"Starting a Docker sandbox ({image}) and installing the project")
            sandbox = DockerSandbox(worktree, project_rel, image)
        else:
            on_status("Creating a local virtualenv and installing the project (no Docker)")
            sandbox = LocalSandbox(work_project, tmp / "venv")
        cleanup.enter_context(sandbox)

        ws = Workspace(root=work_project, test_command=test_command, sandbox=sandbox)
        install_project(ws)
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
        version = installed_version(ws, package)
        result = UpgradeResult(
            branch=branch,
            tests_passed=tests_passed,
            report=report,
            changed_files=sorted(ws.changed_files),
        )

        if result.changed_files:
            paths = [str(project_rel / p) for p in result.changed_files]
            _git("add", "--", *paths, cwd=worktree)
            title = f"Upgrade {package}" + (f" to {version}" if version else "")
            title += "" if tests_passed else " (tests still failing)"
            _git("commit", "-q", "-m", title, "-m", report, cwd=worktree)
            result.committed = committed = True

        if open_pr and result.committed:
            on_status("Pushing the branch and opening a pull request")
            where = f"in the Docker sandbox, {image}" if use_docker else "in a local virtualenv"
            result.pr_url = github.open_pull_request(
                repo_root,
                branch,
                base,
                title,
                pr_body(report, tests_passed, where),
                draft=not tests_passed,
            )
        return result


def installed_version(ws: Workspace, package: str) -> str | None:
    proc = ws.run(["python", "-m", "pip", "show", package], timeout=60)
    for line in proc.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return None


def pr_body(report: str, tests_passed: bool, sandbox: str) -> str:
    verdict = (
        "✅ The test suite passes."
        if tests_passed
        else "❌ The test suite still fails. This PR is a draft; see *Needs your attention*."
    )
    return (
        f"{report}\n\n---\n\n"
        f"**Verified by patchpilot:** after the agent finished, patchpilot re-ran the tests "
        f"itself ({sandbox}). {verdict}\n\n"
        "<sub>Opened by [patchpilot](https://github.com/ilmeedesilva/patchpilot), an AI agent "
        "that upgrades dependencies and fixes what breaks. Review it like any other PR.</sub>"
    )


def _remove_worktree(
    repo_root: Path, worktree: Path, branch: str, committed: Callable[[], bool], tmp: Path
) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_root, capture_output=True
    )
    # No commit means nothing worth reviewing, so don't leave an empty branch behind.
    if not committed():
        subprocess.run(["git", "branch", "-D", branch], cwd=repo_root, capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
