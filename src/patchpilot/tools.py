"""Tools the agent can call, plus the JSON schemas that describe them to Claude.

Every tool works inside a single project directory (the "workspace"). Paths the
model sends are resolved against that directory and rejected if they escape it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 20_000
IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}

# Matches "name", "name==1.2", "name[extra]>=1.0,<2", etc. Group 1 is the name, group 3 the spec.
REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*)")


class ToolError(Exception):
    """A tool failed in a way the model should hear about and can recover from."""


@dataclass
class Workspace:
    root: Path
    test_command: list[str]

    def resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ToolError(f"Path '{relative_path}' is outside the project directory.")
        return path


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    # Test failures usually matter most at the end of the output, so keep the tail.
    dropped = len(text) - MAX_OUTPUT_CHARS
    return f"[... {dropped} characters truncated ...]\n" + text[-MAX_OUTPUT_CHARS:]


def list_files(ws: Workspace, directory: str = ".") -> str:
    base = ws.resolve(directory)
    if not base.is_dir():
        raise ToolError(f"'{directory}' is not a directory.")
    files = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(ws.root.resolve())
        if any(part in IGNORED_DIRS for part in rel.parts) or not path.is_file():
            continue
        files.append(str(rel))
    return "\n".join(files) if files else "(no files)"


def read_file(ws: Workspace, path: str) -> str:
    target = ws.resolve(path)
    if not target.is_file():
        raise ToolError(f"File '{path}' does not exist.")
    return _truncate(target.read_text(errors="replace"))


def parse_requirements(text: str) -> list[dict[str, str]]:
    deps = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        match = REQUIREMENT_RE.match(line)
        if match:
            deps.append({"name": match.group(1), "spec": match.group(3).strip()})
    return deps


def read_dependencies(ws: Workspace) -> str:
    """Collect declared dependencies from requirements*.txt and pyproject.toml."""
    found: dict[str, list[dict[str, str]]] = {}
    root = ws.root.resolve()

    for req_file in sorted(root.glob("requirements*.txt")):
        found[req_file.name] = parse_requirements(req_file.read_text())

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text())
        project = data.get("project", {})
        found["pyproject.toml [project.dependencies]"] = parse_requirements(
            "\n".join(project.get("dependencies", []))
        )
        for group, reqs in project.get("optional-dependencies", {}).items():
            found[f"pyproject.toml [optional-dependencies.{group}]"] = parse_requirements(
                "\n".join(reqs)
            )

    if not found:
        raise ToolError("No requirements*.txt or pyproject.toml found in the project.")
    return json.dumps(found, indent=2)


def get_latest_version(ws: Workspace, package: str) -> str:
    """Look up the newest release of a package on PyPI."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package):
        raise ToolError(f"'{package}' is not a valid package name.")
    url = f"https://pypi.org/pypi/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            info = json.load(resp)["info"]
    except urllib.error.HTTPError as e:
        raise ToolError(f"PyPI returned {e.code} for '{package}'.") from e
    except urllib.error.URLError as e:
        raise ToolError(f"Could not reach PyPI: {e.reason}") from e
    return json.dumps(
        {
            "name": info["name"],
            "latest_version": info["version"],
            "requires_python": info.get("requires_python"),
            "project_urls": info.get("project_urls"),
        },
        indent=2,
    )


def run_tests(ws: Workspace) -> str:
    try:
        proc = subprocess.run(
            ws.test_command,
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as e:
        raise ToolError(f"Test command not found: {ws.test_command[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise ToolError("Tests timed out after 10 minutes.") from e
    status = "PASSED" if proc.returncode == 0 else f"FAILED (exit code {proc.returncode})"
    return _truncate(f"{status}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")


TOOL_FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "read_dependencies": read_dependencies,
    "get_latest_version": get_latest_version,
    "run_tests": run_tests,
}

TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List project files recursively, skipping .git, virtualenvs and caches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Directory relative to the project root. Defaults to '.'.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the project root."}
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_dependencies",
        "description": (
            "Return the project's declared dependencies and version specifiers from "
            "requirements*.txt and pyproject.toml, grouped by source file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_latest_version",
        "description": "Look up the latest released version of a package on PyPI.",
        "input_schema": {
            "type": "object",
            "properties": {"package": {"type": "string", "description": "PyPI package name."}},
            "required": ["package"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_tests",
        "description": "Run the project's test suite and return pass/fail status plus output.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
]
