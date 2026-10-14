# ai-ops-agent

The ops bot of the owner's AI agent ecosystem (Telegram, one owner): server
health, logs, service restarts, package and AI-tool updates, deployments,
rollbacks and backups on a 1 GB Ubuntu VPS. It is the **rescue tool**: no
AI provider dependency, so it must keep working when the coding agent or API
credits are down.

## How it works

- `ai_ops_agent/bot/` – Telegram adapters (owner check first in every
  handler); service modules below never import Telegram.
- `deploy/ai-*` – root CLIs installed to `/usr/local/sbin` (`ai-deploy`,
  `ai-backup`, `ai-packages`, `ai-tools`, `ai-service`, `ai-cleanup`,
  `ai-diagnostics`). They run with the system `python3`, **standard library
  only, no bot credentials**, and are also called by `ai-dashboard`. Keep
  their arguments fixed and their output stable (the dashboard parses it).
- `ai_ops_agent/deployment/` – the independent deployment manager behind
  `ai-deploy` (queued jobs, verify, restart, rollback to the previous verified
  release; state in `/var/lib/ai-deploy`). The bot only submits jobs; it never
  restarts itself or falls back to bare `git pull`.
- Services are resolved only against `MANAGED_SERVICES`; targets only against
  configured agent/tool records. See `docs/architecture.md`, `docs/deployments.md`.

## Commands

- Tests: `python -m pytest -q`. Lint: `ruff check ai_ops_agent tests` and
  `ruff format --check ai_ops_agent tests` (annotations and docstrings are
  checked). Tests use temp dirs and fakes – never the real `/etc`, `/var/lib`,
  `/usr/local/sbin`, systemd or apt.
- Changes to `deploy/ai-deploy` or `deploy/ai-backup` reach the server only
  after re-running `deploy/install-ai-deploy` / `deploy/install-ai-backup`;
  say so in the PR.

## Conventions and traps

- Subprocesses get argument lists via `shell.py` / `command_output.py`; never
  `shell=True` or user-built strings.
- Timeouts and nonzero exits are failures (`command_failed()`); never report
  them as success or continue to a restart.
- Replies are bounded and HTML-escaped (`bot/transport.py`).
- `ai_agent_common/` is a shared submodule; change it in its own repo.
