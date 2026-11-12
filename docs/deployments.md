# Verified fleet deployments and rollback

`ai-deploy` owns deployments for `ai-coding-agent`, `ai-pm-agent`,
`ai-ops-agent`, and `ai-dashboard`. It runs using system Python and no bot
credentials. The bot and dashboard submit jobs; an independent transient
systemd service completes them even when the requesting bot restarts.

## One-time migration

Review the coordinated Ops, coding-agent, and dashboard PRs. Stage the reviewed
Ops branch in a temporary clone, so installing the manager does not change a
live bot checkout:

```bash
git clone --branch feature/safe-deployments https://github.com/ramunl/ai-ops-agent.git /tmp/ai-deploy-install
sudo bash /tmp/ai-deploy-install/deploy/install-ai-deploy
sudo ai-deploy status
```

Then merge the coordinated PRs and bootstrap the new code manually:

```bash
sudo ai-deploy submit deploy all main
```

The old coding bot calls its updater with `--no-restart`; the new compatibility
wrapper deliberately refuses that unsafe path. Until the new coding bot is
running, automatic webhook deployment may therefore report failure. Use the
manual `ai-deploy` command above for this initial migration, not the old bot's
`/deploy` or old Ops `/ai_update`. Once the new bots are healthy, their commands
submit independent jobs normally. Initial `status` reports observed checkouts
as untracked; the first managed deployment restarts and verifies each baseline.

The batch resolves all refs first and deploys sequentially, coding first. It
stops after a failed target; remaining targets are explicitly `not_run`.
Successful earlier targets stay deployed. It is not an atomic whole-fleet
rollback. Check `ai-deploy status` for completion before resubmitting.

`ai-deploy remote` reports, read-only, the commit `main` points to on each
target's origin (`git ls-remote`). The dashboard compares it with the deployed
commit, so **Deploy latest main** is offered only when there is something new.
The manager is a frozen copy, so after merging this, run `deploy/install-ai-deploy`
again to pick it up.

After a managed deployment, checkouts are detached: use `ai-deploy`, not
`git pull`. The temporary installer clone can be removed after installation;
the installed manager has its own copy of the implementation.

Existing `/opt` checkouts, virtualenvs, enabled service units, Git remotes, and
service configuration must already be installed. Target paths are fixed:

| Target | Runtime | Unit source |
| --- | --- | --- |
| ai-coding-agent | /opt/ai_coding_venv | deploy/ai-coding-agent.service |
| ai-pm-agent | /opt/ai_pm_venv | ai-pm-agent.service |
| ai-ops-agent | /opt/ai_ops_venv | ai-ops-agent.service |
| ai-dashboard | /opt/ai_dashboard_venv | deploy/ai-dashboard.service |

The PM deployment automatically installs `deploy/ai-pm-todos` into
`/usr/local/sbin/ai-pm-todos`; Ops installs `deploy/ai-cleanup` similarly. Unit
files present in the selected revision are installed and daemon-reloaded.
A release without a bridge removes that bridge instead of leaving a script
pointing to a missing module. A release without a versioned unit retains the
existing server unit. Rollback restores the exact installed artifact bundle,
including removing artifacts that did not exist before deployment.

## Safety and state

The first deployment restarts the existing checkout and verifies it before
adopting it as a working baseline. Observed Git HEAD is shown separately and
is never automatically treated as proof of the running version. Later
checkout drift is refused. Dirty working trees (including untracked files and
submodule changes) are refused, not discarded.

Branches, tags, and full 40-character commits are resolved from the checkout's
configured remote. The coding agent retains its existing deploy-key SSH
transport; other targets use their clone's normal transport. Named local
branches are never rewritten: deployment uses a detached checkout and pinned
recursive submodules.

A fleet-wide reservation plus file lock prevents overlapping changes. Before
changing code, the manager snapshots the **entire existing virtualenv** and
installed integration files. It then stops the target, changes code and
requirements, installs its artifacts, and restarts it. Commands have bounded
timeouts; timed-out Git/pip descendants are killed as a process group before
recovery starts. Startup must occur within 30 seconds; the same fresh process
must stay active/running for another 10 seconds. Dashboard also must return
`{"ok": true}` from local `/healthz` throughout verification.

This verifies service startup, not every bot command or external provider.
Bot credentials, operating-system packages, databases, and user todo/rules
repositories are outside the rollback bundle. No migrations of these are
performed by this manager.

Private, atomic, owner-only state lives in `/var/lib/ai-deploy/state.json` and
tracks commit, VERSION, ref, verification time, previous verified version,
operation, errors, and the last 20 events per target. One previous runtime
bundle per target is retained, plus the current transaction/recovery bundle.
Unreferenced partial bundles are pruned before the next reservation.
Dependencies are restored from the exact copied runtime, without assuming an
old requirements file recreates the old package versions. Snapshot creation
requires twice the runtime size plus 100 MiB of free headroom; retaining a
full previous fleet costs roughly the combined runtime size. System Python
and external symlink targets are outside that runtime snapshot.

A failed deployment restores and verifies the pre-deployment version. It
still reports `failed` with the recovery result rather than calling the failed
request successful. If recovery fails, state is `rollback_failed`, its bundle
and reservation remain, and later deployment requests are refused until
recovery completes. Progress is appended to each existing
`/var/log/TARGET/update.log`; full worker results are also in the systemd
journal.

## CLI and recovery

```bash
sudo ai-deploy status
sudo ai-deploy submit deploy ai-pm-agent main
sudo ai-deploy submit rollback ai-pm-agent --expected-commit FULL_PREVIOUS_SHA
sudo ai-deploy deploy ai-dashboard main   # independent worker, waits for result
sudo ai-deploy rollback ai-dashboard      # independent worker, waits for result
```

`coding`, `pm`, `ops`, and `dashboard` aliases are accepted. `status` returns
`{"targets": [...], "active": ...}`. Submit returns
`{"operation": "...", "target": "...", "action": "...", "status": "queued",
"requested_ref": "..."}`. Queued is not deployment success. Errors are JSON
on stdout with nonzero exit status. Status contains private bundle paths for
administrative inspection; UI responses expose only public revision fields.

Inspect an operation with:

```bash
sudo journalctl -u ai-deploy-OPERATION.service
sudo ai-deploy recover
```

`recover` refuses a live worker. After a killed worker/reboot, it starts a new
independent worker to restore the journaled baseline and verify it. If the
worker stopped before recording target changes, it clears only the interrupted
queue and retains completed fleet targets. Never delete the state or bundles
to bypass a recovery failure. A recovered interrupted request still returns a
failed request result, with a verified restored current version.

## Manager upgrades and legacy entrypoints

`deploy/install-ai-deploy` copies a deliberate manager release independently
to `/usr/local/lib/ai-deploy/releases` and atomically selects `current`.
Rolling back the Ops checkout cannot remove or downgrade the running manager.
Re-run this installer deliberately to upgrade the manager; keep the prior
manager release available for administrative recovery. Do not run the
installer while a transaction is queued or running (it refuses both).

The installer owns `/usr/local/sbin/ai-deploy` and all four `update-TARGET`
compatibility scripts. These wrappers are independent of managed artifact
bundles, so rolling back an old checkout cannot replace them with an unsafe
legacy updater. They preserve `/var/log/TARGET/update.log`, invoke an
independent verified worker, and return its exit status. `--no-restart` is
explicitly rejected: it cannot establish a healthy deployed version. Bot
callers must submit a job instead of updating themselves and scheduling a
separate unverified restart.

## Failure evidence APIs (milestone 1)

`deployment.failure_events.new_failure()` builds a version 1 record from trusted
job metadata: operation ID, configured target name/repository/service, requested
ref and commit, previous revision (commit), UTC failure time, stable failing
stage, exit code (or null if unavailable), rollback outcome, and sanitized
bounded diagnostics. `failure_identity()` computes SHA-256 over UTF-8 JSON with
sorted keys, compact separators and ASCII escaping, containing only `operation`,
`target` (name, repo, service), and `stage`. Time, diagnostics, requested revisions
and mutable rollback outcomes do not participate. Later callers must reuse the
existing job's `operation` ID and the same configured target and stable stage
names across retries and recovery; a new job or stage is a distinct failure.

`FailureEvents(Store(...)).save(record)` and `.load(identity)` acquire
`Store.lock()` internally; call them **outside** an existing store lock scope.
The lock covers the full read/modify/write cycle and rejects contention.
Records live in `Store.root/failure-events/<identity>.json` (normally
`/var/lib/ai-deploy/failure-events`), independently of `state.json`, reservation
and rollback recovery data. The directory is mode 0700 and atomic JSON files
are mode 0600. Writes use `atomic_json`, including file and directory fsync.
A replay preserves all original failure evidence and can update only the
rollback outcome (`pending`, `not_required`, `succeeded`, or `failed`). A pending
replay cannot erase an outcome; an explicit later outcome can replace an earlier
one, such as successful recovery after failed recovery. Save returns the merged
record. Load returns null for an absent identity; corrupt, oversized, mismatched
or unsupported records raise `FailureRecordError` and are never overwritten by
save. Storage I/O failures propagate to callers.

`deployment.failure_diagnostics.parse_diagnostics(status, journal)` is pure and
standard-library-only. It recognizes systemd startup failures including 217/USER,
missing `ai-coding-runner`, traceback presence and allowlisted Python exception
types. It emits only fixed diagnostic phrases, never raw lines, paths, exception
messages, traceback source, command arguments or unknown payloads. Each input is
limited to its first 65,536 characters, with a truncation marker; deduplicated
output is bounded to 1,024 characters. Persistence validates the same vocabulary
and bounds even for records constructed without the parser. Job metadata must
come from trusted deployment configuration, never raw logs.

The failure-event milestone provides APIs only; deployment capture and recovery
wiring are deferred. Shipping these modules
requires the owner to re-run `deploy/install-ai-deploy` through Ops controls when
no transaction is queued or running; updating the Ops checkout alone does not
update the installed manager. No server changes are part of this milestone.


## PM delivery APIs (milestone 2)

`deployment.pm_bridge.build_ticket(event)` validates existing failure evidence
and builds a bounded, single-line bug ticket. It reuses the fixed diagnostic
vocabulary, traceback summary and truncation marker. It omits raw logs, traceback
frames/source, environments, tokens, command arguments, repository paths and
arbitrary refs. Target/stage metadata must come from trusted configuration;
commit fields are included only as hexadecimal revisions. No remote errors,
warnings, messages or task lists are written to logs or delivery state.

The installed contract was verified against `/usr/local/sbin/ai-pm-todos`,
`ai_pm_agent.task_cli`, `task_service` and `task_model`: invoke the bridge with
**no arguments**, sending JSON on stdin. Read the explicit project's workspace,
then add with its revision, `type=bug`, `status=open`, `priority=high`, ID and
text. Requests are capped at 32,768 characters; text must be one nonempty line
of at most 10,000 characters without PM metadata markers. Each invocation has a
30-second timeout and 256 KiB stdout limit; stderr is discarded. At most two
conflict retries refresh the revision (six calls total), with no sleeping.
Responses must identify the expected project and revision and confirm the exact
ID/text. `published=false` still confirms the same locally saved ticket ID;
GitHub publication is PM's responsibility.

`PMDelivery(Store(...), bridge=PMBridge())` provides these caller APIs:

- `enqueue(event, now=utc_datetime)` persists the first ticket before any bridge
  call. Its ID is the first 32 hex characters of SHA-256 over ASCII
  `ai-deploy:<failure identity>`. Replays preserve the original text, including
  its original rollback evidence, because PM retry-safe add requires matching
  ID **and text**. Failure evidence can continue to record newer rollback results.
- `load(identity)` returns delivery status, attempts, next-attempt UTC and
  confirmed ticket ID independently of deployment/failure state.
- `due(now=..., limit=20)` returns at most 20 due identities without calling PM.
- `attempt_due(identity, now=...)` reserves at most one due attempt, calls PM
  outside the lock, then persists confirmation or the next retry time. A caller
  must schedule subsequent calls; there is no worker integration in this milestone.

Omitting `now` uses the current UTC clock. Call these APIs outside `Store.lock()`;
contention raises `DeploymentError` immediately. Claims persist a 300-second
recovery deadline before bridge I/O. A crash consumes an attempt; after the
deadline, restart replay uses identical ID/text. Attempt numbers fence stale
completions. No sleeps or PM I/O occur under locks. Retries use 60, 120, 240,
480, 960, 1,920 and 3,600-second delays, with at most eight attempts total.
The eighth claim is provisionally exhausted, allowing a successful confirmation
but preventing further attempts even if it crashes. An uncertain final response
may therefore leave a saved PM task with unconfirmed exhausted delivery state.

Records live under `Store.root/pm-delivery/<failure identity>.json`, with mode
0700 directories and atomic, fsynced mode 0600 files. Status is `pending`,
`delivered` or `exhausted`; confirmed ID remains null until validated success.
Records are bounded to 64 KiB and capacity is 1,024 retained records, including
completed records. Capacity errors and corrupt state fail closed without erasing
existing records. There is no automatic pruning or retry-budget reset; archival
policy and exhausted-ticket presentation belong to a later milestone.

Event capture, rollback wiring, worker retry scheduling and result presentation
remain outside this milestone. Installing these deployment modules requires the
owner to run `deploy/install-ai-deploy` through Ops controls when no transaction
is queued or running. Updating the checkout alone is insufficient. No server
mutation is performed by this change.

## Deployment failure integration (milestone 3)

This supersedes the deferred integration notes in milestones 1 and 2. Preflight,
independent-worker scheduling, baseline verification, snapshot, stop, checkout,
install, health, explicit rollback and recovery failures now create failure events.
Interrupted transactions capture an `interrupted` event before recovery, unless
an original failure intent already identifies the failure. Invalid target/action
requests and reservation contention do not create deployment failure tickets.

The manager collects systemd properties and the last 80 journal entries with
10-second deadlines and 64 KiB output limits. Baseline evidence is captured before
restart can erase a pre-start failure. Other failure evidence is captured before
recovery changes the service. Only the existing allowlisted diagnostic vocabulary
is persisted: no raw logs, traceback frames, source or arbitrary messages enter
failure events or PM tickets. Command exit codes are structured; timeout is 124,
and failures without an OS command status use null.

Under the transaction lock, `state.json` records sanitized `failure_intents`,
including stable identity and recovery outcome. After releasing that lock the
manager saves through `FailureEvents` and enqueues through `PMDelivery`. Pending
recovery intents may be saved as events but are never enqueued until an outcome
is recorded. Successful handoffs remove the intent; active recovery retains it
so later recovery attempts can update the event. Replaying a crash between save,
enqueue and acknowledgment preserves identity and does not create another ticket.
The first enqueued ticket remains immutable, including the recovery result known
at that time. Later recovery results update event evidence, not frozen PM text.

Target and history results retain their existing fields and add:

- `failure_identity`, `failure_stage`, `failure_exit_code`: the original failure's
  identity, execution stage and command status, where available.
- `original_error`: the original deployment error, separate from the existing
  combined human-readable `error` field.
- `rollback_outcome`: `pending`, `not_required`, `succeeded` or `failed`.
- `recovery_error`: the separate recovery error, cleared after successful recovery.
- `ticket_status`: null without a failure, otherwise `pending`, `delivered` or
  `exhausted`. Pending also covers intent awaiting outbox persistence.
- `confirmed_ticket_id`: null until PM confirms the exact ticket ID and text.
- `reporting_error`: a fixed safe message when reporting storage is unavailable;
  raw storage and bridge diagnostics are not returned in this field.

Failure metadata is populated when applicable. History ticket status and confirmed
ID are resolved from the outbox at read time. Waited `deploy`/`rollback` failures
return nonzero and include `targets` and `active` alongside the existing top-level
`error`; `execute` retains its status JSON and zero/nonzero success convention.
Reporting failures remain secondary and cannot prevent rollback. If the state
filesystem itself cannot persist any writes, durable recovery/reporting naturally
requires restoring that filesystem; reporting does not substitute for the
transaction journal.

`ai-deploy retry-tickets` is the fixed, independent retry worker. It reconciles
unflushed intents and attempts at most 20 due records per invocation, respecting
the existing UTC due times, leases, eight-attempt limit and bounded exponential
backoff. It does not deploy, restart services, reset exhausted records or require
an AI provider. PM bridge calls occur outside the deployment lock. A busy lock or
storage failure produces a safe reporting error and nonzero exit; offline PM
retains pending delivery and its next due time. Deployment execution only enqueues,
so PM availability never delays restoration.

The installer installs `ai-deploy-tickets.service` and enables
`ai-deploy-tickets.timer`: first run two minutes after boot, then one minute after
the previous worker finishes. The timer is independent of all managed agents.
After merge, the **owner must run `deploy/install-ai-deploy` through Ops controls**
when no deployment is queued or running, to update the installed manager and
activate the timer. Updating the checkout alone does not install either component.
Owner verification can use `ai-deploy status` and
`systemctl status ai-deploy-tickets.timer`; a manual retry uses
`ai-deploy retry-tickets`. No installer or other server mutation is run by this
repository change.
