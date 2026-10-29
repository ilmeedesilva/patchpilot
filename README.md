# patchpilot

An AI agent that upgrades your dependencies, fixes the code that breaks, and opens a pull request explaining what changed and why.

> 🚧 Work in progress.

## Why

Tools like Dependabot bump version numbers, but when an upgrade breaks your build, fixing it is still on you. patchpilot goes further: it upgrades a dependency, runs your tests, reads the failures and changelogs, patches your code, and only opens a PR once everything passes.

## How it works (planned)

1. Scan the project for outdated or vulnerable dependencies
2. Upgrade one dependency at a time inside a sandboxed Docker container
3. Run the test suite
4. If tests fail, the agent reads the errors and the library's changelog, then edits the code to fix them
5. Open a PR with the changes and a clear explanation

## Status

- [x] Agent loop with tool use
- [x] Read declared dependencies (`requirements*.txt`, `pyproject.toml`)
- [x] Look up latest versions on PyPI
- [x] Run the test suite and report a baseline
- [x] Upgrade one dependency and edit code to fix breakages, in an isolated git worktree
- [x] Run the project's code in a locked-down Docker container
- [x] Open a pull request with the agent's report (`--pr`)

## Getting started

```bash
git clone git@github.com:ilmeedesilva/patchpilot.git
cd patchpilot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then add your Anthropic API key
```

### See what could be upgraded

```bash
patchpilot analyze path/to/your/project
```

This is read-only: it prints a Markdown report of outdated dependencies, likely breaking upgrades and the current test result.

### Upgrade a package and fix what breaks

```bash
cd path/to/your/project          # must be inside a git repo
patchpilot upgrade . pydantic
git diff HEAD...patchpilot/upgrade-pydantic   # review what it did
```

What happens:

1. patchpilot makes a separate checkout of your repo (a git worktree) on a new branch, `patchpilot/upgrade-<package>`. **Your own files are never touched.**
2. It starts a Docker sandbox (see below), installs your project into it and checks that the tests pass.
3. The agent upgrades the package, runs the tests, reads the failures and the package's changelog, and edits the code until the tests pass.
4. patchpilot re-runs the tests itself, rather than trusting the agent's word, then commits the changes to the branch. The commit message is the agent's report: what broke, how it was fixed, and what still needs your attention.

Or let patchpilot open the pull request for you:

```bash
patchpilot upgrade . pydantic --pr
```

This pushes the branch and opens a PR whose description is the agent's report, plus a note confirming that patchpilot re-ran the tests itself. If the tests still fail, the PR is opened as a **draft** so it can't be merged by accident. `--pr` uses the [GitHub CLI](https://cli.github.com/) (`gh auth login` once), so patchpilot never handles a GitHub token itself. It checks that GitHub is set up *before* the agent starts, so a missing login doesn't waste a run.

If you like the result, merge the branch. If you don't, delete it: `git branch -D patchpilot/upgrade-pydantic`.

Options:

- `--test-command "..."` if your tests don't run with `python -m pytest -q`
- `--image python:3.11-slim` to match your project's Python version (default `python:3.13-slim`)
- `--no-docker` to use a local virtualenv instead of Docker: faster, but less isolated

### The sandbox

Upgrading means installing packages from PyPI and running code: your tests, and anything those packages run on install. patchpilot does all of that inside a Docker container that:

- can see **only the temporary copy of your repo**, not your home folder, SSH keys or other projects
- runs as your normal user, not root, with all extra Linux privileges removed
- is limited to 2 CPUs, 2 GB of memory and 512 processes, so runaway tests can't freeze your machine
- is deleted when the run ends

The agent itself, and your API key, stay outside the container. Inside it, the agent can only run the commands patchpilot exposes: `pip install <one package>` and your test command.

### Try it on the demo project

[`examples/pydantic-demo`](examples/pydantic-demo) is a small app written for pydantic v1 that breaks on v2. Copy it somewhere and let patchpilot fix it:

```bash
cp -R examples/pydantic-demo /tmp/demo && cd /tmp/demo
git init -q && git add . && git commit -qm "demo"
patchpilot upgrade . pydantic
```

You need Docker Desktop running (or pass `--no-docker`).

Run patchpilot's own tests:

```bash
pytest
```

## Tech stack

- Python
- Claude API (tool use)
- Docker (sandboxed execution)
- GitHub API

## License

MIT
