import subprocess
import urllib.error
from types import SimpleNamespace

import pytest

from patchpilot import cli
from patchpilot.pick import find_candidates, pick_package

# A fake PyPI: package -> (latest, all releases).
PYPI = {
    "pydantic": ("2.9.0", ["1.10.0", "1.10.15", "2.0.0", "2.9.0", "3.0.0b1"]),
    "requests": ("2.31.1", ["2.31.0", "2.31.1"]),
    "rich": ("13.9.0", ["13.0.0", "13.9.0"]),
    "flask": ("3.1.0", ["2.3.3", "3.0.0", "3.1.0"]),
    "httpx": ("0.28.1", ["0.27.0", "0.28.1"]),
}


def fake_pypi(name):
    if name.lower() not in PYPI:
        raise urllib.error.HTTPError(name, 404, "Not Found", None, None)
    return PYPI[name.lower()]


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.name", "Test User", cwd=root)
    git("config", "user.email", "test@example.com", cwd=root)
    (root / "requirements.txt").write_text(
        "pydantic>=1.10,<2\nrequests==2.31.0\nrich\nFlask<3\nprivate-lib==1.0\n"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["httpx>=0.27,<0.28"]\n'
    )
    git("add", ".", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def test_finds_packages_whose_latest_release_is_out_of_range(repo):
    candidates, notes = find_candidates(repo, fake_pypi)
    found = {c.name: (str(c.current), str(c.latest)) for c in candidates}
    assert found == {
        "pydantic": ("1.10.15", "2.9.0"),
        "requests": ("2.31.0", "2.31.1"),
        "Flask": ("2.3.3", "3.1.0"),
        "httpx": ("0.27.0", "0.28.1"),
    }
    assert notes == ["Skipped private-lib: couldn't look it up on PyPI."]


def test_specs_from_several_files_are_combined(repo):
    (repo / "requirements-dev.txt").write_text("requests>=2.30\n")
    candidates, _ = find_candidates(repo, fake_pypi)
    assert [c.spec for c in candidates if c.name == "requests"] == [">=2.30,==2.31.0"]


def test_prefers_the_smallest_jump_by_default(repo):
    assert pick_package(repo, repo, fetch=fake_pypi).name == "requests"


def test_can_prefer_the_largest_jump(repo):
    # Two major jumps tie; the name breaks the tie so the choice is predictable.
    assert pick_package(repo, repo, prefer="largest", fetch=fake_pypi).name == "Flask"


def test_skips_packages_with_a_local_patchpilot_branch(repo):
    git("branch", "patchpilot/upgrade-requests", cwd=repo)
    assert pick_package(repo, repo, fetch=fake_pypi).name == "httpx"


def test_skips_packages_with_a_branch_on_github(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-q", "origin", "HEAD:refs/heads/patchpilot/upgrade-requests", cwd=repo)
    messages = []
    assert pick_package(repo, repo, fetch=fake_pypi, on_status=messages.append).name == "httpx"
    assert "Skipped requests: it already has a patchpilot branch." in messages


def test_returns_none_when_everything_is_handled(repo):
    for name in ("pydantic", "requests", "flask", "httpx"):
        git("branch", f"patchpilot/upgrade-{name}", cwd=repo)
    assert pick_package(repo, repo, fetch=fake_pypi) is None


def test_cli_needs_a_package_or_auto(repo, capsys):
    for argv in (["upgrade", str(repo)], ["upgrade", str(repo), "rich", "--auto"]):
        with pytest.raises(SystemExit):
            cli.main(argv)
        assert "--auto" in capsys.readouterr().err


def test_cli_auto_upgrades_the_picked_package(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "pick_package", lambda *a, **k: pick_package(*a, **k, fetch=fake_pypi))

    def fake_upgrade(project, package, **kwargs):
        calls.append(package)
        return SimpleNamespace(report="", committed=False)

    monkeypatch.setattr(cli, "upgrade", fake_upgrade)
    cli.main(["upgrade", str(repo), "--auto"])
    assert calls == ["requests"]


def test_cli_auto_exits_cleanly_when_nothing_is_outdated(repo, monkeypatch, capsys):
    monkeypatch.setattr(cli, "pick_package", lambda *a, **k: None)
    assert cli.main(["upgrade", str(repo), "--auto"]) == 0
    assert "Nothing to upgrade" in capsys.readouterr().err
