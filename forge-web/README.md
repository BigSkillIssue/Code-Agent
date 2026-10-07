# Forge Web

A multi-user server with a web UI for the [Forge](../README.md) coding agent. Run it on a Linux, macOS or
Windows server; people sign in with Google, GitHub or email and password, keep several projects, and chat with
Forge in each one — with a file tree and editor, a changes/git panel, a terminal and a live preview, much like
the Claude Code desktop app. Every project runs in its own hardened container, so users can be strangers.

> Status: under construction — see [`docs/STEPS.md`](docs/STEPS.md) and [`PROGRESS.md`](PROGRESS.md).
> German setup guide: [`docs/EINRICHTUNG.md`](docs/EINRICHTUNG.md) (from step W16).

## Try it from a checkout

```bash
cd forge-web
uv sync
uv run forge-web --version
uv run forge-web serve            # http://127.0.0.1:8420
```

Forge Web lives next to Forge and never changes it: `forge-web/` is a separate uv workspace that uses Forge
as a library.
