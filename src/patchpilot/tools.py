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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

MAX_OUTPUT_CHARS = 20_000
IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache"}

# Matches "name", "name==1.2", "name[extra]>=1.0,<2", etc. Group 1 is the name, group 3 the spec.
REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*)")
# What pip_install accepts: a name, optional extras, optional version specifier. No flags or URLs.
INSTALLABLE_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?([<>=!~]=?[0-9A-Za-z.*,<>=!~]+)?"
)


class ToolError(Exception):
    """A tool failed in a way the model should hear about and can recover from."""


class Sandbox(Protocol):
    """Somewhere to run the project's commands (see patchpilot.sandbox)."""

    python: str

    def run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]: ...


@dataclass
class Workspace:
    root: Path
    test_command: list[str]
    # Where commands run in upgrade mode. None runs them directly in `root`, using whatever
    # Python is on PATH (analyze mode, which never installs anything).
    sandbox: Sandbox | None = None
    changed_files: set[str] = field(default_factory=set)

    def resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ToolError(f"Path '{relative_path}' is outside the project directory.")
        return path

    def command(self, cmd: list[str]) -> list[str]:
        """Point a leading `python` at the sandbox's own interpreter."""
        if self.sandbox and cmd and cmd[0] in ("python", "python3"):
            return [self.sandbox.python, *cmd[1:]]
        return cmd

    def run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        cmd = self.command(cmd)
        try:
            if self.sandbox:
                proc = self.sandbox.run(cmd, timeout)
            else:
                proc = subprocess.run(
                    cmd, cwd=self.root, capture_output=True, text=True, timeout=timeout
                )
        except FileNotFoundError as e:
            raise ToolError(f"Command not found: {cmd[0]}") from e
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"Command timed out after {timeout} seconds.") from e
        if self.sandbox and proc.returncode == 124:  # exit code of coreutils `timeout`
            raise ToolError(f"Command timed out after {timeout} seconds.")
        return proc


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    # Test failures usually matter most at the end of the output, so keep the tail.
    dropped = len(text) - MAX_OUTPUT_CHARS
    return f"[... {dropped} characters truncated ...]\n" + text[-MAX_OUTPUT_CHARS:]


def _format_result(proc: subprocess.CompletedProcess[str]) -> str:
    status = "PASSED" if proc.returncode == 0 else f"FAILED (exit code {proc.returncode})"
    return _truncate(f"{status}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")


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


def _writable(ws: Workspace, path: str) -> Path:
    target = ws.resolve(path)
    rel = target.relative_to(ws.root.resolve())
    if any(part in IGNORED_DIRS for part in rel.parts):
        raise ToolError(f"Refusing to write inside '{rel.parts[0]}'.")
    return target


def write_file(ws: Workspace, path: str, content: str) -> str:
    target = _writable(ws, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    ws.changed_files.add(path)
    return f"Wrote {len(content)} characters to {path}."


def edit_file(ws: Workspace, path: str, old_text: str, new_text: str) -> str:
    target = _writable(ws, path)
    if not target.is_file():
        raise ToolError(f"File '{path}' does not exist. Use write_file to create it.")
    text = target.read_text()
    count = text.count(old_text)
    if count == 0:
        raise ToolError(f"old_text was not found in {path}. Read the file and copy it exactly.")
    if count > 1:
        raise ToolError(f"old_text appears {count} times in {path}. Include more context.")
    target.write_text(text.replace(old_text, new_text))
    ws.changed_files.add(path)
    return f"Edited {path}."


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


def normalize_name(name: str) -> str:
    """PyPI treats 'Foo_Bar', 'foo-bar' and 'foo.bar' as the same package (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def collect_dependencies(root: Path) -> dict[str, list[dict[str, str]]]:
    """Declared dependencies from requirements*.txt and pyproject.toml, keyed by source."""
    found: dict[str, list[dict[str, str]]] = {}
    root = root.resolve()

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
    return found


def read_dependencies(ws: Workspace) -> str:
    found = collect_dependencies(ws.root)
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


def pip_install(ws: Workspace, requirement: str) -> str:
    """Install one requirement (e.g. 'pydantic==2.9.2') into the sandbox's environment."""
    if ws.sandbox is None:
        raise ToolError("Installing packages is only available in upgrade mode.")
    if not INSTALLABLE_RE.fullmatch(requirement):
        raise ToolError(
            f"'{requirement}' is not allowed. Pass a single requirement like 'name==1.2.3'."
        )
    return _format_result(ws.run(["python", "-m", "pip", "install", requirement], timeout=600))


def run_tests(ws: Workspace) -> str:
    return _format_result(ws.run(ws.test_command, timeout=600))


TOOL_FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "read_dependencies": read_dependencies,
    "get_latest_version": get_latest_version,
    "pip_install": pip_install,
    "run_tests": run_tests,
}


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_PATH = {"type": "string", "description": "Path relative to the project root."}

READ_TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List project files recursively, skipping .git, virtualenvs and caches.",
        "input_schema": _schema(
            {
                "directory": {
                    "type": "string",
                    "description": "Directory relative to the project root. Defaults to '.'.",
                }
            },
            [],
        ),
    },
    {
        "name": "read_file",
        "description": "Read a text file from the project.",
        "input_schema": _schema({"path": _PATH}, ["path"]),
    },
    {
        "name": "read_dependencies",
        "description": (
            "Return the project's declared dependencies and version specifiers from "
            "requirements*.txt and pyproject.toml, grouped by source file."
        ),
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_latest_version",
        "description": (
            "Look up the latest released version of a package on PyPI, plus its project URLs "
            "(often including a changelog link)."
        ),
        "input_schema": _schema(
            {"package": {"type": "string", "description": "PyPI package name."}}, ["package"]
        ),
    },
    {
        "name": "run_tests",
        "description": "Run the project's test suite and return pass/fail status plus output.",
        "input_schema": _schema({}, []),
    },
]

WRITE_TOOL_SCHEMAS = [
    {
        "name": "edit_file",
        "description": (
            "Replace one exact occurrence of old_text with new_text in a file. old_text must "
            "match the file exactly (including indentation) and appear only once."
        ),
        "input_schema": _schema(
            {
                "path": _PATH,
                "old_text": {"type": "string", "description": "Exact text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            ["path", "old_text", "new_text"],
        ),
    },
    {
        "name": "write_file",
        "description": "Create a file, or overwrite one completely. Prefer edit_file for changes.",
        "input_schema": _schema(
            {"path": _PATH, "content": {"type": "string", "description": "Full file content."}},
            ["path", "content"],
        ),
    },
    {
        "name": "pip_install",
        "description": (
            "Install a single requirement into the project's isolated environment, "
            "e.g. 'pydantic==2.9.2' or 'pydantic-settings'."
        ),
        "input_schema": _schema(
            {"requirement": {"type": "string", "description": "One requirement specifier."}},
            ["requirement"],
        ),
    },
]
