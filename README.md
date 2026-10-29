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

## Tech stack

- Python
- Claude API (tool use)
- Docker (sandboxed execution)
- GitHub API

## License

MIT
