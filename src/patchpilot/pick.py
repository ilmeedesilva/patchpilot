"""Choose which package `patchpilot upgrade --auto` should upgrade, without the AI.

A package is a candidate when its newest release on PyPI is outside the range the
project declares, e.g. `pydantic>=1.10,<2` while 2.x is out. Unpinned packages never
are: pip already installs their newest release. Packages that already have a
patchpilot branch, locally or on GitHub, are skipped, so a weekly run moves on to the
next package instead of redoing an open PR.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from patchpilot.tools import collect_dependencies, normalize_name

BRANCH_PREFIX = "patchpilot/upgrade-"
PREFERENCES = ("smallest", "largest")

# (latest version, every version ever released) for a package name.
Fetcher = Callable[[str], tuple[str, list[str]]]


@dataclass
class Candidate:
    name: str
    spec: str
    current: Version  # the newest release the declared range allows
    latest: Version

    @property
    def jump(self) -> tuple[int, int, int]:
        """How far the upgrade goes: (majors, minors, patches), counting only the biggest part."""
        cur = (*self.current.release, 0, 0)[:3]
        new = (*self.latest.release, 0, 0)[:3]
        if new[0] != cur[0]:
            return (new[0] - cur[0], 0, 0)
        if new[1] != cur[1]:
            return (0, new[1] - cur[1], 0)
        return (0, 0, new[2] - cur[2])

    def describe(self) -> str:
        return f"{self.name} {self.current} → {self.latest} (declared: {self.spec})"


def fetch_releases(name: str) -> tuple[str, list[str]]:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=15) as resp:
        data = json.load(resp)
    return data["info"]["version"], list(data["releases"])


def _stable_versions(versions: list[str]) -> list[Version]:
    parsed = []
    for v in versions:
        try:
            version = Version(v)
        except InvalidVersion:
            continue
        if not version.is_prerelease and not version.is_devrelease:
            parsed.append(version)
    return parsed


def find_candidates(
    project: Path, fetch: Fetcher = fetch_releases
) -> tuple[list[Candidate], list[str]]:
    """Outdated packages in the project, plus notes on any that couldn't be checked."""
    specs: dict[str, tuple[str, list[str]]] = {}
    for deps in collect_dependencies(project).values():
        for dep in deps:
            name, found = specs.setdefault(normalize_name(dep["name"]), (dep["name"], []))
            if dep["spec"]:
                found.append(dep["spec"])

    candidates, notes = [], []
    for name, found in specs.values():
        if not found:
            continue
        spec = ",".join(found)
        try:
            allowed = SpecifierSet(spec)
        except InvalidSpecifier:
            notes.append(f"Skipped {name}: couldn't read its version range '{spec}'.")
            continue
        try:
            latest, releases = fetch(name)
            latest_v = Version(latest)
        except (urllib.error.URLError, KeyError, ValueError, InvalidVersion):
            notes.append(f"Skipped {name}: couldn't look it up on PyPI.")
            continue
        if latest_v in allowed:
            continue
        in_range = list(allowed.filter(_stable_versions(releases)))
        if not in_range:
            notes.append(f"Skipped {name}: no release on PyPI matches '{spec}'.")
            continue
        candidates.append(Candidate(name, spec, max(in_range), latest_v))
    return candidates, notes


def existing_branches(repo_root: Path, remote: str = "origin") -> set[str]:
    """Normalized names of packages that already have a patchpilot branch."""
    names = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    ).stdout.split()
    remotes = subprocess.run(
        ["git", "remote"], cwd=repo_root, capture_output=True, text=True
    ).stdout.split()
    if remote in remotes:
        # A fresh CI checkout only has the default branch, so ask GitHub directly.
        listing = subprocess.run(
            ["git", "ls-remote", "--heads", remote, f"{BRANCH_PREFIX}*"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        ).stdout
        names += [line.split("refs/heads/", 1)[1] for line in listing.splitlines()]
    return {normalize_name(n.removeprefix(BRANCH_PREFIX)) for n in names}


def pick_package(
    project: Path,
    repo_root: Path,
    prefer: str = "smallest",
    fetch: Fetcher = fetch_releases,
    on_status: Callable[[str], None] = lambda _: None,
) -> Candidate | None:
    """The package to upgrade next, or None when there's nothing left to do."""
    candidates, notes = find_candidates(project, fetch)
    for note in notes:
        on_status(note)

    taken = existing_branches(repo_root)
    open_ = []
    for c in candidates:
        if normalize_name(c.name) in taken:
            on_status(f"Skipped {c.name}: it already has a patchpilot branch.")
        else:
            open_.append(c)
    if not open_:
        return None

    # Sort by name first so ties always resolve the same way.
    open_.sort(key=lambda c: normalize_name(c.name))
    choose = max if prefer == "largest" else min
    return choose(open_, key=lambda c: c.jump)
