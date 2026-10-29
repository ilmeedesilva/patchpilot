import subprocess

import pytest

from patchpilot import upgrade as upgrade_mod
from patchpilot.tools import edit_file
from patchpilot.upgrade import UpgradeError, upgrade

# Runs in the workspace's virtualenv, so no real test framework is needed.
PASSING_TESTS = ["python", "-c", "pass"]


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git("init", "-q", "-b", "main", cwd=tmp_path)
    git("config", "user.name", "Test User", cwd=tmp_path)
    git("config", "user.email", "test@example.com", cwd=tmp_path)
    (tmp_path / "app.py").write_text("VERSION = 1\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-q", "-m", "init", cwd=tmp_path)
    return tmp_path


def fake_agent(edit=True):
    def run(ws, **kwargs):
        if edit:
            edit_file(ws, "app.py", "VERSION = 1", "VERSION = 2")
        return "## Summary\nUpgraded demo."

    return run


def test_upgrade_commits_to_new_branch_without_touching_checkout(repo, monkeypatch):
    monkeypatch.setattr(upgrade_mod, "run_agent", fake_agent())
    result = upgrade(repo, "demo", test_command=PASSING_TESTS)

    assert result.committed and result.tests_passed
    assert result.branch == "patchpilot/upgrade-demo"
    assert (repo / "app.py").read_text() == "VERSION = 1\n"
    assert git("status", "--porcelain", cwd=repo).stdout == ""
    assert "VERSION = 2" in git("show", f"{result.branch}:app.py", cwd=repo).stdout
    log = git("log", "-1", "--format=%an|%s|%b", result.branch, cwd=repo).stdout
    assert log.startswith("Test User|Upgrade demo|## Summary")
    assert git("worktree", "list", cwd=repo).stdout.count("\n") == 1


def test_upgrade_without_changes_leaves_no_branch(repo, monkeypatch):
    monkeypatch.setattr(upgrade_mod, "run_agent", fake_agent(edit=False))
    result = upgrade(repo, "demo", test_command=PASSING_TESTS)
    assert not result.committed
    assert "patchpilot/upgrade-demo" not in git("branch", cwd=repo).stdout


def test_upgrade_refuses_when_baseline_tests_fail(repo, monkeypatch):
    monkeypatch.setattr(upgrade_mod, "run_agent", fake_agent())
    with pytest.raises(UpgradeError, match="fail before any upgrade"):
        upgrade(repo, "demo", test_command=["python", "-c", "raise SystemExit(1)"])
    assert "patchpilot/upgrade-demo" not in git("branch", cwd=repo).stdout


def test_upgrade_refuses_existing_branch(repo):
    git("branch", "patchpilot/upgrade-demo", cwd=repo)
    with pytest.raises(UpgradeError, match="already exists"):
        upgrade(repo, "demo", test_command=PASSING_TESTS)


def test_upgrade_requires_git_repo(tmp_path):
    with pytest.raises(UpgradeError, match="rev-parse"):
        upgrade(tmp_path, "demo", test_command=PASSING_TESTS)
