# Ops agent architecture

The ops agent remains independent of AI providers. `ai_ops_agent.telegram_bot`
builds the Telegram application and registers autocomplete commands. The existing
`python -m ai_ops_agent` entry point and all command names remain available.

## Responsibilities

| Module | Owns |
| --- | --- |
| `bot/catalog.py` | Command names and autocomplete descriptions |
| `bot/transport.py` | Owner authorization, bounded replies, HTML, rich-message fallback |
| `bot/help.py` | Help and running version commands |
| `bot/system.py` | Health, disk, memory, and uptime commands |
| `bot/services.py` | Whitelisted service status, logs, errors, restarts, and reboot |
| `bot/packages.py` | Package refresh and upgrade commands |
| `bot/package_reports.py` | Rich and fallback HTML package reports |
| `bot/agents.py` | Agent/tool command handling and result formatting |
| `metrics.py` | Parsing system resource measurements |
| `packages.py` | Typed package update records and release-channel classification |
| `inventory.py` | Configured target aliases and local version/model metadata |
| `fleet.py` | Checkout inspection, fast-forward updates, and service restart sequencing |
| `ai_tools.py` | CLI version inspection and configured npm updates |
| `command_output.py` | Async command execution and failure recognition |
| `shell.py` | Subprocess execution using argument lists |
| `config.py`, `settings.py` | Environment settings, target allowlists, locale, model keys |

Dependencies flow from command adapters to services and then to configuration and
command execution. Service modules do not import Telegram adapters. The existing
version module still resolves metadata from the agent checkout root.

## Preserved behavior

Every command checks the owner chat before reading data or invoking operations.
Service names are resolved against `MANAGED_SERVICES`; target aliases resolve
only to configured agent/tool records. Subprocesses receive argument lists.

`/ai_update` fast-forwards selected checkouts. An unchanged checkout does not
restart. Changed non-self agents restart immediately. A changed ops agent defers
its own restart until after the result has been sent, then requests a nonblocking
restart. A failed pull or unreadable revision never triggers a restart.

Package reports retain the security/stable/review categories, HTML escaping,
section limits, and fallback when rich messages are unavailable. Their summary
and detailed views now share one recommendation formatter.

## Error handling

`command_failed()` recognizes nonzero exits, execution errors, and timeouts.
Timeouts now follow the same failure path as other command errors; they cannot
be reported as a successful package upgrade or cause a service restart after a
failed Git operation. `AgentUpdateError` describes checkout-update failures,
which the fleet service converts into its existing result dictionary.

## Tests

CI runs Ruff lint, formatting, type-hint/docstring checks, and pytest. Regression
tests cover command registration, owner rejection, service allowlists, package
classification, resource parsing, metadata selection, update failures, and
self-restart ordering. System commands, package managers, network requests, and
service restarts are mocked in the operation tests.
