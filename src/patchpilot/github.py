"""Push the upgrade branch and open a pull request, using the GitHub CLI (`gh`).

Going through `gh` means patchpilot never handles a GitHub token itself: it uses
whatever account the user already logged in with (`gh auth login`).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitHubError(Exception):
    """Pushing or opening the PR failed; the message is meant for the user."""


def _run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(f"`{' '.join(cmd[:3])}` failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def check_ready(repo_root: Path, branch: str, remote: str = "origin") -> str:
    """Check everything a PR needs before any work starts. Returns the base branch."""
    if shutil.which("gh") is None:
        raise GitHubError(
            "--pr needs the GitHub CLI. Install it (brew install gh) and run: gh auth login"
        )
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        raise GitHubError("You're not logged in to the GitHub CLI. Run: gh auth login")

    remotes = _run(["git", "remote"], repo_root).split()
    if remote not in remotes:
        raise GitHubError(
            f"This repo has no '{remote}' remote, so there's nowhere to open a PR. "
            "Push it to GitHub first."
        )

    base = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if base == "HEAD":
        raise GitHubError("You're in 'detached HEAD' state. Check out a branch first.")
    on_remote = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", remote, base],
        cwd=repo_root,
        capture_output=True,
    )
    if on_remote.returncode != 0:
        raise GitHubError(
            f"Branch '{base}' isn't on GitHub yet, so a PR can't target it. "
            f"Push it first: git push -u {remote} {base}"
        )
    taken = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", remote, branch],
        cwd=repo_root,
        capture_output=True,
    )
    if taken.returncode == 0:
        raise GitHubError(
            f"Branch '{branch}' already exists on GitHub (probably an earlier patchpilot PR). "
            f"Merge or close that PR and delete the branch first."
        )
    return base


def open_pull_request(
    repo_root: Path,
    branch: str,
    base: str,
    title: str,
    body: str,
    draft: bool,
    remote: str = "origin",
) -> str:
    """Push the branch and open a PR. Returns the PR's URL."""
    _run(["git", "push", "--quiet", "--set-upstream", remote, branch], repo_root)
    cmd = ["gh", "pr", "create", "--head", branch, "--base", base, "--title", title]
    cmd += ["--body", body]
    if draft:
        cmd.append("--draft")
    # gh prints the new PR's URL as the last line of its output.
    return _run(cmd, repo_root).splitlines()[-1]
