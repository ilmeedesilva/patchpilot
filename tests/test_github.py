"""--pr flow against a local bare repo as 'origin' and a fake `gh` that records its arguments."""

import os
import subprocess

import pytest

from patchpilot import github
from patchpilot import upgrade as upgrade_mod
from patchpilot.github import GitHubError
from patchpilot.tools import edit_file
from patchpilot.upgrade import upgrade

FAKE_GH = """#!/bin/sh
if [ "$1" = "auth" ]; then exit 0; fi
printf '%s\\n' "$@" > "$GH_ARGS_FILE"
echo "Creating pull request for patchpilot/upgrade-demo into main"
echo "https://github.com/test/repo/pull/7"
"""
# Passes on the original code; fails once the fake agent bumps VERSION to 2.
VERSION_IS_1 = ["python", "-c", "import app; assert app.VERSION == 1"]


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    git("config", "user.name", "Test User", cwd=work)
    git("config", "user.email", "test@example.com", cwd=work)
    (work / "app.py").write_text("VERSION = 1\n")
    (work / "requirements.txt").write_text("demo==1.0\n")
    git("add", ".", cwd=work)
    git("commit", "-q", "-m", "init", cwd=work)
    git("remote", "add", "origin", str(origin), cwd=work)
    git("push", "-q", "-u", "origin", "main", cwd=work)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_ARGS_FILE", str(tmp_path / "gh-args.txt"))
    monkeypatch.setattr(upgrade_mod, "install_project", lambda *args: None)

    def fake_agent(ws, **kwargs):
        edit_file(ws, "app.py", "VERSION = 1", "VERSION = 2")
        return "## Summary\nUpgraded demo."

    monkeypatch.setattr(upgrade_mod, "run_agent", fake_agent)
    return work, origin, tmp_path / "gh-args.txt"


def run_upgrade(work, test_command):
    return upgrade(work, "demo", test_command=test_command, use_docker=False, open_pr=True)


def test_opens_pr_with_report_and_verification(repo):
    work, origin, gh_args = repo
    result = run_upgrade(work, ["python", "-c", "pass"])

    assert result.pr_url == "https://github.com/test/repo/pull/7"
    git("rev-parse", "--verify", "refs/heads/patchpilot/upgrade-demo", cwd=origin)
    args = gh_args.read_text()
    assert "--base\nmain\n" in args and "--title\nUpgrade demo\n" in args
    assert "## Summary\nUpgraded demo." in args
    assert "**Verified by patchpilot:**" in args and "✅ The test suite passes." in args
    assert "--draft" not in args


def test_failing_tests_open_a_draft(repo):
    work, _, gh_args = repo
    result = run_upgrade(work, VERSION_IS_1)
    assert not result.tests_passed
    args = gh_args.read_text()
    assert "--draft" in args and "(tests still failing)" in args


def test_checks_github_before_doing_any_work(repo, monkeypatch):
    work, _, _ = repo
    git("remote", "remove", "origin", cwd=work)
    monkeypatch.setattr(upgrade_mod, "run_agent", lambda *a, **k: pytest.fail("agent ran"))
    with pytest.raises(GitHubError, match="no 'origin' remote"):
        run_upgrade(work, ["python", "-c", "pass"])


def test_refuses_branch_that_already_exists_on_github(repo):
    work, _, _ = repo
    git("push", "-q", "origin", "main:patchpilot/upgrade-demo", cwd=work)
    with pytest.raises(GitHubError, match="already exists on GitHub"):
        run_upgrade(work, ["python", "-c", "pass"])


def test_base_branch_must_be_on_github(repo):
    work, _, _ = repo
    git("checkout", "-q", "-b", "local-only", cwd=work)
    with pytest.raises(GitHubError, match="isn't on GitHub yet"):
        github.check_ready(work, "patchpilot/upgrade-demo")


def test_needs_the_gh_cli(repo, monkeypatch):
    work, _, _ = repo
    monkeypatch.setattr(github.shutil, "which", lambda name: None)
    with pytest.raises(GitHubError, match="needs the GitHub CLI"):
        github.check_ready(work, "patchpilot/upgrade-demo")
