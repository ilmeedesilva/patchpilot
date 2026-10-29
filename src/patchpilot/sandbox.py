"""Where the project's own code runs: a Docker container (default) or a local virtualenv.

Only commands run here: installing packages and running the test suite. File reads and
edits happen on the host, inside the temporary worktree, and the agent loop (with the API
key) never enters the sandbox.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

DEFAULT_IMAGE = "python:3.13-slim"
CONTAINER_ROOT = "/workspace"
CONTAINER_VENV = "/tmp/venv"


class SandboxError(Exception):
    """The sandbox couldn't be created; the message is meant for the user."""


class LocalSandbox:
    """A fresh virtualenv on the host. No isolation beyond Python packages."""

    def __init__(self, project: Path, venv_dir: Path):
        self.project = project
        self.venv_dir = venv_dir
        self.python = str(venv_dir / "bin" / "python")

    def __enter__(self) -> LocalSandbox:
        subprocess.run([sys.executable, "-m", "venv", str(self.venv_dir)], check=True)
        return self

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.venv_dir, ignore_errors=True)

    def run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd, cwd=self.project, capture_output=True, text=True, timeout=timeout
        )


class DockerSandbox:
    """A locked-down container that can see only the temporary worktree.

    - mounts the worktree at /workspace and nothing else from the host
    - runs as the host user (not root), with all Linux capabilities dropped
    - caps memory, CPU and process count so runaway tests can't take down the machine
    """

    def __init__(self, worktree: Path, project_rel: Path, image: str = DEFAULT_IMAGE):
        self.worktree = worktree
        rel = project_rel.as_posix()
        self.workdir = CONTAINER_ROOT if rel == "." else f"{CONTAINER_ROOT}/{rel}"
        self.image = image
        self.name = f"patchpilot-{uuid.uuid4().hex[:12]}"
        self.python = f"{CONTAINER_VENV}/bin/python"

    @staticmethod
    def check_available() -> None:
        if shutil.which("docker") is None:
            raise SandboxError(
                "Docker isn't installed. Install Docker Desktop, or run with --no-docker "
                "to use a local virtualenv instead (less isolated)."
            )
        info = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if info.returncode != 0:
            raise SandboxError(
                "Docker is installed but not running. Start Docker Desktop and try again."
            )

    def __enter__(self) -> DockerSandbox:
        self.check_available()
        cmd = [
            "docker", "run", "--detach", "--rm",
            "--name", self.name,
            "--volume", f"{self.worktree}:{CONTAINER_ROOT}",
            "--workdir", self.workdir,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--env", "HOME=/tmp",
            "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", "2g",
            "--cpus", "2",
            "--pids-limit", "512",
            self.image,
            "sleep", "infinity",
        ]  # fmt: skip
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SandboxError(f"Couldn't start the Docker sandbox:\n{proc.stderr.strip()}")
        venv = self.run(["python", "-m", "venv", CONTAINER_VENV], timeout=120)
        if venv.returncode != 0:
            self.__exit__()
            raise SandboxError(f"Couldn't create a virtualenv in the sandbox:\n{venv.stderr}")
        return self

    def __exit__(self, *exc) -> None:
        subprocess.run(["docker", "rm", "--force", self.name], capture_output=True)

    def run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        # `timeout` inside the container kills the command itself; killing only the
        # `docker exec` client would leave it running in the container.
        return subprocess.run(
            ["docker", "exec", self.name, "timeout", str(timeout), *cmd],
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
