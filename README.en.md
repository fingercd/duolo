<div align="center">

<img src="docs/assets/brand.svg" alt="Duolo" width="88" />

# Duolo

**Develop locally and over SSH: sync files, follow Git commits, and let your agent check the state.**

[中文](README.md) | [**English**](README.en.md)

[Get started](#get-started) · [Agent integration](#use-with-an-agent) · [Documentation](#documentation-and-help)

![Python 3.10+](https://img.shields.io/badge/local-Python%203.10%2B-blue)
![Experimental 0.4.0](https://img.shields.io/badge/version-0.4.0%20experimental-orange)
![MIT License](https://img.shields.io/badge/license-MIT-green)

</div>

Edit code with a local editor or coding agent while also debugging and running it on an SSH server? **Duolo connects the two Git working directories, bringing file synchronization, commit coordination, and agent-queryable state into one workflow.**

Uncommitted source code and project documents can move in both directions. When one side creates a commit, the other follows when conditions allow. You and your agent can see whether files match, whether Git has caught up, and what needs attention.

**15-second concept video** · Local and SSH development

https://github.com/user-attachments/assets/c69f531f-c31a-4bcc-bc56-32c1617054a3

## Why Duolo

- **Sync work before committing.** Save source code or project documents on either side, and changes made on one side automatically reach the other.
- **Coordinate files and Git together.** Sync work in progress and follow existing commits when protective checks pass. Saving a file does not automatically create a commit.
- **Share one view with your agent.** Query files, Git, connections, and conflicts through the CLI or connect a coding agent through MCP.
- **Know what needs attention.** File conflicts, divergent history, and network problems are reported with their cause, so you can review and choose the next step.

## Get started

Your computer needs **Python 3.10+, Git, and OpenSSH**. The server needs **Python 3.9+ and Git**. Set up SSH key login and trust the target host key first.

**1. Install Duolo** from GitHub. It is not currently published on PyPI:

```console
python -m pip install "duolo @ git+https://github.com/fingercd/duolo.git@v0.4.0"
```

**2. Open your existing project and tell Duolo where its server copy lives:**

```console
cd D:/work/my-project
duo init --remote gpu-dev --path /home/researcher/projects/my-project
```

Replace `gpu-dev` with your SSH hostname or config alias, and use your own project paths. `init` only remembers the two directories; synchronization starts when you launch the service.

**Before first use, both copies must have the same Git commit, the same branch, and matching files to synchronize.** If they already contain different changes, preserve and reconcile both sides using the [onboarding guide](docs/onboarding.md).

**3. Start and check the result:**

```console
duo start
duo status --short
```

Keep editing. Duolo syncs in the background, without an extra console window on Windows. Run subsequent `duo` commands inside this project or its subdirectories; no need to repeat the server path.

## Everyday commands

| Command | What it does |
|---|---|
| `duo status --short` | Show file, Git, connection, and conflict state. |
| `duo watch` | Show state changes continuously; Ctrl+C ends observation. |
| `duo sync --wait` | Sync now and wait for confirmation. |
| `duo pause` / `duo resume` | Pause / resume automatic sync. |
| `duo checkpoint -m "experiment setup" --tag exp-001` | Explicitly save a Git commit and an optional tag. |
| `duo stop` | Stop this project's background synchronization. |

`duo status` returns JSON by default for scripts and agents; `--short` is for people. Status comes from the service's latest check and reports disconnections or outdated observations. Use `duo --help` for more options, or see [configuration and states](docs/configuration.md).

## Use with an agent

The optional MCP interface lets a coding agent **query the same file, Git, and connection state as the CLI**, request synchronization, wait for a new check, resolve conflicts, or save a commit.

Install MCP support, then run the adapter inside your connected project:

```console
python -m pip install "duolo[mcp] @ git+https://github.com/fingercd/duolo.git@v0.4.0"
duo mcp
```

Follow the [MCP guide](docs/mcp.md) to connect your agent client. You can also install the [project Skill](skills/duolo/SKILL.md), which helps an agent reread changed project rules after synchronization.

## Scope

**Experimental release 0.4.0.** Local tests, official MCP SDK tests, and real Windows ↔ Linux SSH acceptance are complete. See the [validation record](docs/validation.md) for results.

- Sync covers source code and project documents. Training data, model weights, and common outputs are excluded. Deletion is off by default; see [configuration](docs/configuration.md) and [design](docs/design.md) for the full scope.
- Divergent Git history, file conflicts, and network problems are reported. Duolo does not automatically merge conflicts or push your project to GitHub, and does not guarantee zero loss under arbitrary simultaneous editing.
- Active training should use a fixed code copy. Sync connects only the two development directories you select; it does not adopt other projects or running snapshots.

## Documentation and help

[Onboarding](docs/onboarding.md) · [Configuration and states](docs/configuration.md) · [MCP](docs/mcp.md) · [Implementation and limits](docs/design.md) · [Validation](docs/validation.md)

Detailed reference pages are currently in Chinese. Report bugs or suggestions in [Issues](https://github.com/fingercd/duolo/issues), with reproduction steps. Remove real hosts, private paths, and secret contents from logs before sharing.

To develop from source:

```console
git clone https://github.com/fingercd/duolo.git
cd duolo
python -m pip install -e .
python -m unittest discover -s tests -v
```

Licensed under [MIT](LICENSE).
