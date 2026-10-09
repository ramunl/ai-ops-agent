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
