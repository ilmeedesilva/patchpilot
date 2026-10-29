"""Checks that the Docker sandbox really is isolated. Skipped when Docker isn't running."""

import os
from pathlib import Path

import pytest

from patchpilot.sandbox import DockerSandbox, SandboxError
from patchpilot.tools import ToolError, Workspace


def docker_running() -> bool:
    try:
        DockerSandbox.check_available()
    except SandboxError:
        return False
    return True


pytestmark = pytest.mark.skipif(not docker_running(), reason="Docker is not running")


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    worktree = tmp_path_factory.mktemp("worktree")
    (worktree / "proj").mkdir()
    with DockerSandbox(worktree, Path("proj")) as sb:
        yield sb, worktree


def sh(sb, script, timeout=60):
    return sb.run(["sh", "-c", script], timeout=timeout)


def test_runs_in_the_project_folder_as_the_host_user(sandbox):
    sb, _ = sandbox
    assert sh(sb, "pwd").stdout.strip() == "/workspace/proj"
    assert sh(sb, "id -u").stdout.strip() == str(os.getuid())


def test_can_only_see_the_worktree(sandbox):
    sb, worktree = sandbox
    home = Path.home()
    assert sh(sb, f"test -e '{home}'").returncode != 0
    sh(sb, "echo hello > /workspace/proj/out.txt")
    assert (worktree / "proj" / "out.txt").read_text() == "hello\n"


def test_has_its_own_virtualenv(sandbox):
    sb, _ = sandbox
    proc = sb.run([sb.python, "-c", "import sys; print(sys.prefix)"], timeout=60)
    assert proc.stdout.strip() == "/tmp/venv"


def test_timeouts_kill_the_command(sandbox):
    sb, worktree = sandbox
    ws = Workspace(root=worktree / "proj", test_command=[], sandbox=sb)
    with pytest.raises(ToolError, match="timed out"):
        ws.run(["sleep", "30"], timeout=1)
