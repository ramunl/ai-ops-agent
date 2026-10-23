# Ops agent architecture

The ops agent remains independent of AI providers. `ai_ops_agent.telegram_bot`
builds the Telegram application and registers autocomplete commands. The existing
`python -m ai_ops_agent` entry point and all command names remain available.

## Responsibilities

| Module | Owns |
| --- | --- |
| `bot/catalog.py` | Command names and autocomplete descriptions |
| `bot/transport.py` | Owner authorization, bounded replies, HTML, rich-message fallback |
| `bot/help.py` | Help, shared runtime version, and core status commands |
| `bot/system.py` | Health, disk, memory, and uptime commands |
| `bot/services.py` | Whitelisted service status, logs, errors, restarts, and reboot |
| `bot/packages.py` | Package refresh and upgrade commands |
| `bot/package_reports.py` | Rich and fallback HTML package reports |
| `bot/agents.py` | Agent/tool command handling and result formatting |
| `metrics.py` | Parsing system resource measurements |
| `packages.py` | Typed package update records and release-channel classification |
| `inventory.py` | Configured target aliases and local version/model metadata |
| `fleet.py` | Checkout inspection and central deployment CLI client |
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

`/ai_update` queues one target or an entire sequential fleet batch through
`ai-deploy submit deploy`. `/rollback` queues exactly one fixed target;
`/deployments` reads persisted current and previous verified releases. The bot
never schedules a second self restart: an independent deployment job owns
installation, verification, restart, and recovery. Missing or failed manager
calls are reported, without falling back to bare Git updates.


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
supervised deployment submission. System commands, package managers, network requests, and
service restarts are mocked in the operation tests.

## Package upgrade outcomes and dashboard handoff

`ai-packages check|upgrade` retains its fixed arguments, one JSON document,
exit convention (0 for `ok`, 1 for failure), and existing field names. Both
CLI upgrades and the bot's asynchronous start launch a standard-library Python
worker in `ai-packages-upgrade.service`. The launcher never runs apt directly.
The worker owns `/run/ai-packages.lock` through apt exit and verification. A
caller disappearing, or its wait expiring, does not stop the service or release
the worker's lock. The apt deadline records `timeout` but deliberately waits for
apt to exit, keeping the lock: it cannot safely assume dpkg descendants stopped.

Before launch, a UUID reservation is atomically persisted under
`/var/lib/ai-packages/active.json`, pointing to `<run_id>.json`. Outcome writes
use atomic replacement and fsync. Completed run files remain available after
caller loss. Each worker receives its specific run ID, refuses to execute a
completed or superseded run, and writes only that run's evidence. An unresolved
reservation blocks another upgrade even if the worker or caller died and the
kernel released its lock. No automatic retry occurs. Recovery means inspecting
that run file and the unit's state in Ops controls; do not clear the reservation
until an administrator confirms apt/dpkg completion and reconciles the result.
A failed or timed-out launcher may still have submitted a service, so its
unresolved reservation is also retained conservatively.

Upgrade JSON adds these independent facts:

- `execution`: `queued`, `running`, `succeeded`, `launch_failed`, `apt_failed`,
  `timeout`, or `unknown`. `completion_confirmed` becomes true only after apt
  exit and the verification attempt. A caller wait timeout returns `unknown`
  and `caller_timed_out: true` without changing the worker's durable outcome.
- `apt_exit_code`: actual apt status, or null before confirmed exit.
  `launcher_exit_code` and `launcher_error` describe systemd-run separately.
  `diagnostics` contains at most 4096 characters from this run's apt output.
  Journals are not used as outcome evidence, so unavailable or unrelated
  journal entries cannot overwrite launcher errors or apt diagnostics.
- `upgraded`: integer only when a successful apt exit has a parseable summary;
  otherwise null. Apt's planned count is not a completed count on failure.
- `verification`: `not_run`, `succeeded`, or `failed`, with separate
  `verification_error`. Verification runs after confirmed apt exit, including
  nonzero exit and completion following a deadline. `ok` requires successful
  apt execution within its deadline and successful verification.
- `remaining`: verified integer total, otherwise null. `remaining_packages`
  contains `ok`, `total`, `security`, `stable`, `untested`, `checked_at`, and
  `reboot_required`; failed queries have null totals/lists and an error.
  Before verification the entire object is null. Names are observed remaining
  packages, with no inferred explanation such as holds or phasing.
- `started_at`, `worker_started_at`, `apt_finished_at`, `finished_at`, and
  `checked_at` are Unix timestamps for reservation, worker start, apt exit,
  completion of the verification attempt, and that attempt respectively.
  Unobserved timestamps and reboot status are null (worker/apt timestamps are
  added when reached). Top-level reboot status is sampled during verification.

Consumer review: the bot consumes only start `ok`, `busy`, `error`, and `unit`,
which retain their meanings. The available dashboard implementation accepts
additive JSON fields, but its successful-upgrade UI interpolates `upgraded`
directly and would display `null upgraded` for an unknown count. Its cache
rebuild also discards package names and only reconciles successful upgrades;
its nonzero-exit handling needs to preserve structured failure evidence.
These are explicit part 2 requirements, not resolved by this repository change.
The dashboard should render null as unknown, use `remaining_packages` and its
own timestamp even after apt failure, keep upgrade and verification outcomes
separate, and recover detached results by run ID. Cached counts, including the
supplied nine-package snapshot, and unrelated bot journal messages do not
establish an upgrade's cause or current remaining packages. No dashboard code
or live server state is changed here.
