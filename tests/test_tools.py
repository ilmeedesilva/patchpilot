import json
import sys

import pytest

from patchpilot.agent import execute_tool
from patchpilot.tools import (
    ToolError,
    Workspace,
    list_files,
    parse_requirements,
    read_dependencies,
    read_file,
    run_tests,
)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "# comment\nrequests==2.31.0\nflask[async]>=2.0,<3 ; python_version>'3.8'\n-e .\nrich\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["httpx>=0.27"]\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n'
    )
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("")
    return Workspace(root=tmp_path, test_command=[sys.executable, "-c", "print('ok')"])


def test_parse_requirements_handles_extras_markers_and_flags():
    deps = parse_requirements("requests==2.31.0\nflask[async]>=2.0,<3 ; extra\n-r other.txt\nrich")
    assert deps == [
        {"name": "requests", "spec": "==2.31.0"},
        {"name": "flask", "spec": ">=2.0,<3"},
        {"name": "rich", "spec": ""},
    ]


def test_read_dependencies_combines_sources(ws):
    found = json.loads(read_dependencies(ws))
    assert [d["name"] for d in found["requirements.txt"]] == ["requests", "flask", "rich"]
    assert found["pyproject.toml [project.dependencies]"] == [{"name": "httpx", "spec": ">=0.27"}]
    assert found["pyproject.toml [optional-dependencies.dev]"] == [{"name": "pytest", "spec": ""}]


def test_read_dependencies_errors_without_dependency_files(tmp_path):
    with pytest.raises(ToolError):
        read_dependencies(Workspace(root=tmp_path, test_command=[]))


def test_list_files_skips_ignored_dirs(ws):
    files = list_files(ws).splitlines()
    assert "app.py" in files
    assert not any(f.startswith(".git") for f in files)


def test_read_file_rejects_paths_outside_workspace(ws):
    with pytest.raises(ToolError, match="outside"):
        read_file(ws, "../../etc/passwd")


def test_run_tests_reports_pass_and_fail(ws):
    assert run_tests(ws).startswith("PASSED")
    ws.test_command = [sys.executable, "-c", "raise SystemExit(3)"]
    assert run_tests(ws).startswith("FAILED (exit code 3)")


def test_execute_tool_turns_errors_into_tool_results(ws):
    assert execute_tool(ws, "nope", {}) == ("Unknown tool 'nope'.", True)
    result, is_error = execute_tool(ws, "read_file", {"path": "missing.txt"})
    assert is_error and "does not exist" in result
    result, is_error = execute_tool(ws, "read_file", {"wrong_arg": "x"})
    assert is_error and "Invalid arguments" in result
