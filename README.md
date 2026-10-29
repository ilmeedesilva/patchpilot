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
- [ ] Upgrade one dependency and edit code to fix breakages
- [ ] Sandbox all commands in Docker
- [ ] Open a pull request with the changes

## Getting started

```bash
git clone git@github.com:ilmeedesilva/patchpilot.git
cd patchpilot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then add your Anthropic API key
```

Analyze a project (right now patchpilot only reads files and runs tests; it doesn't change anything yet):

```bash
patchpilot path/to/your/project
patchpilot path/to/your/project --test-command "python -m pytest -x"
```

The agent prints each tool call as it works, then a Markdown upgrade report.

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
