import json
import sys
from types import SimpleNamespace

import pytest

from patchpilot.agent import execute_tool
from patchpilot.tools import (
    ToolError,
    Workspace,
    edit_file,
    list_files,
    parse_requirements,
    pip_install,
    read_dependencies,
    read_file,
    run_tests,
    write_file,
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


def test_edit_file_replaces_exactly_one_match(ws):
    (ws.root / "app.py").write_text("a = 1\nb = 1\n")
    edit_file(ws, "app.py", "a = 1", "a = 2")
    assert (ws.root / "app.py").read_text() == "a = 2\nb = 1\n"
    assert ws.changed_files == {"app.py"}
    with pytest.raises(ToolError, match="not found"):
        edit_file(ws, "app.py", "zzz", "y")
    with pytest.raises(ToolError, match="2 times"):
        edit_file(ws, "app.py", "= ", "=")


def test_write_file_refuses_git_dir_and_tracks_changes(ws):
    write_file(ws, "pkg/new.py", "x = 1\n")
    assert (ws.root / "pkg" / "new.py").read_text() == "x = 1\n"
    assert "pkg/new.py" in ws.changed_files
    with pytest.raises(ToolError, match="Refusing"):
        write_file(ws, ".git/hooks/pre-commit", "evil")


@pytest.mark.parametrize(
    "requirement", ["--index-url=http://x", "-r other.txt", "https://x/y.whl", "a b", "pkg; rm"]
)
def test_pip_install_rejects_anything_but_one_requirement(ws, requirement):
    ws.sandbox = SimpleNamespace(python=sys.executable)
    with pytest.raises(ToolError, match="not allowed"):
        pip_install(ws, requirement)


def test_pip_install_needs_an_isolated_environment(ws):
    with pytest.raises(ToolError, match="upgrade mode"):
        pip_install(ws, "requests")


def test_workspace_points_python_at_its_own_interpreter(ws):
    ws.sandbox = SimpleNamespace(python="/tmp/venv/bin/python")
    assert ws.command(["python", "-m", "pytest"]) == ["/tmp/venv/bin/python", "-m", "pytest"]
    assert ws.command(["make", "test"]) == ["make", "test"]
