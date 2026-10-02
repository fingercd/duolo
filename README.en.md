<div align="center">

<img src="docs/assets/brand.svg" alt="Duolo" width="88" />

# Duolo

**Write code locally, run it on your server, and sync changes both ways.**

[中文](README.md) · [Get started](#get-started) · [Agent integration](#use-with-an-agent) · [Documentation](#documentation-and-help)

![Python 3.10+](https://img.shields.io/badge/local-Python%203.10%2B-blue)
![Experimental 0.4.0](https://img.shields.io/badge/version-0.4.0%20experimental-orange)
![MIT License](https://img.shields.io/badge/license-MIT-green)

</div>

Edit code with your local editor or coding agent, then train, evaluate, or debug on a Linux server? **Duolo connects the two project directories and automatically sends saved code and documents to the other side.** Changes made on the server can come back to your computer too, reducing manual uploads and downloads.

A few commands show sync results and files that differ. Your coding agent can also check and operate the pair directly.

<!-- PROMO-VIDEO: Embed the project promo video here; the project maintainer will supply the actual media link. -->

## Why Duolo

- **Edit on either side.** Changes made on one side sync to the other, including source code and project documents.
- **See conflicts clearly.** Different edits to the same file stop synchronization and are reported, so you can choose which version to keep.
- **Keep Git commits aligned.** When conditions allow, a commit on one side updates the other to the same commit. Saving a file does not automatically create a commit.
- **Use it yourself or with an agent.** Check status in your terminal, or use MCP to let a coding agent inspect, sync, and resolve conflicts.

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
| `duo status --short` | Show current state and files that differ. |
| `duo watch` | Show state changes continuously; Ctrl+C ends observation. |
| `duo sync --wait` | Sync now and wait for confirmation. |
| `duo pause` / `duo resume` | Pause / resume automatic sync. |
| `duo checkpoint -m "experiment setup" --tag exp-001` | Explicitly save a Git commit and an optional tag. |
| `duo stop` | Stop this project's background synchronization. |

`duo status` returns JSON by default for scripts and agents; `--short` is for people. Status comes from the service's latest check and reports disconnections or outdated observations. Use `duo --help` for more options, or see [configuration and states](docs/configuration.md).

## Use with an agent

The optional MCP interface lets a coding agent **check whether both copies match, request synchronization, resolve conflicts, or save a commit**.

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
