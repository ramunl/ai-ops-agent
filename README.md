# ai-ops-agent

Telegram bot for server operations: health checks, logs, service restarts, and system updates.

This is the **rescue tool** of the agent ecosystem — it has no Claude/OpenAI dependency, so it keeps working even when API credits run out or the coding agent crashes.

## Agent ecosystem

- [ai-coding-agent](https://github.com/ramunl/ai-coding-agent) — implements features and bugfixes
- [ai-pm-agent](https://github.com/ramunl/ai-pm-agent) — project management, rules enforcement
- [ai-rules](https://github.com/ramunl/ai-rules) — coding rules storage (not an agent)
- **ai-ops-agent** (this repo) — server health, logs, updates

## Commands

| Command | Description |
|---|---|
| /help | Command list |
| /health | CPU load, RAM, disk summary |
| /disk | Disk usage details |
| /memory | Memory details |
| /uptime | Uptime and load average |
| /services | Status of managed services |
| /logs [service] | Last 30 log lines (default: ai-coding-agent) |
| /errors [service] | Recent error-level log lines (default: all managed services) |
| /restart <service> | Restart a whitelisted service |
| /reboot | Reboot the whole system |
| /update | apt update + classify upgradable packages by critical, stable, and not recommended channels |
| /upgrade | apt upgrade -y |
| /version | Running bot version, branch, and commit |
| /core | Pinned `ai-agent-common` core version |
| /my_agents | Installed AI agents, versions, git refs, service states, configured models, and latest status |
| /ai_tools | Installed AI tools, versions, configured models, and update status |
| /ai_tools update <codex\|claude\|all> | Update installed AI tools |
| /ai_update [agent] | Queue safe deployment of all targets, or one by name/alias |
| /deployments | Current and previous verified releases and operation status |
| /rollback <coding\|pm\|ops\|dashboard> | Queue rollback of one target to its previous verified release |

Only whitelisted services can be managed (see `config.py`), and only the authorized chat ID can issue commands.

## Setup

```bash
# 1. Create a SECOND Telegram bot via @BotFather (e.g. @channelcast_ops_bot)

# 2. Clone and install
sudo git clone --recurse-submodules git@github.com:ramunl/ai-ops-agent.git /opt/ai-ops-agent
python3 -m venv /opt/ai_ops_venv
/opt/ai_ops_venv/bin/pip install -r /opt/ai-ops-agent/requirements.txt

# 3. Configure environment
sudo tee /etc/ai-ops-agent.env > /dev/null <<ENVEOF
OPS_TELEGRAM_BOT_TOKEN=your-new-ops-bot-token
YOUR_CHAT_ID=your-chat-id
ENVEOF
sudo chmod 600 /etc/ai-ops-agent.env

# 4. Install as systemd service
sudo cp /opt/ai-ops-agent/ai-ops-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-ops-agent

# 5. Test in Telegram
# Send /start to your ops bot
```

## Deployment

`ai-ops-agent` deploys automatically when changes are pushed to `main`.

```text
git push origin main
-> GitHub Actions
-> deploy webhook
-> update script
-> ai-ops-agent.service restart
```

GitHub Actions workflow:

```text
.github/workflows/deploy.yml
```

Required GitHub Actions secret:

```text
DEPLOY_WEBHOOK_URL
```

Expected value format:

```text
http://161.35.17.201:9000/hooks/ai-ops-agent-update?secret=<webhook-secret>
```

Server files:

| Path | Purpose |
|---|---|
| /opt/ai-ops-agent | App checkout |
| /opt/ai_ops_venv | Python virtualenv |
| /etc/ai-ops-agent.env | Bot token and authorized chat ID |
| /etc/systemd/system/ai-ops-agent.service | systemd unit |
| /usr/local/sbin/update-ai-ops-agent | Deploy webhook update script |
| /etc/webhook.conf | Webhook hook configuration |
| /var/log/ai-ops-agent/update.log | Deploy update log |

Webhook hook:

```text
ai-ops-agent-update
```

Manual deploy verification:

```bash
tail -100 /var/log/ai-ops-agent/update.log
systemctl status ai-ops-agent.service --no-pager
```

Successful deploy log shape:

```text
[YYYY-MM-DDTHH:MM:SS+00:00] update started
...
[YYYY-MM-DDTHH:MM:SS+00:00] update finished
```

## Versioning

The `/version` command reports:

```text
ai-ops-agent v<VERSION>
branch: <git-branch>
commit: <short-sha>
```

The version string comes from the top-level `VERSION` file.

## Development

Follow the Python rules in `ai-rules/global/python.md`. Command adapters live in
`ai_ops_agent/bot`; system and fleet services remain independent of Telegram.
See [architecture and failure handling](docs/architecture.md).

```bash
python -m pip install -r requirements-dev.txt
ruff check ai_ops_agent tests
ruff format --check ai_ops_agent tests
python -m pytest -q
```

Run `ruff format ai_ops_agent tests` before committing. CI uses Python 3.12 and
runs these checks on pull requests and pushes to `main`. Command timeouts are
reported as failures, including package upgrades and agent checkout updates.

## Disk cleanup (`ai-cleanup`)

`ai_ops_agent/cleanup.py`, installed as `/usr/local/sbin/ai-cleanup`, measures and
reclaims disk space. The dashboard's Ops window calls it, and an ops-bot command
can call the same code.

```bash
ai-cleanup report   # measure only; changes nothing
ai-cleanup run      # clean; one run at a time
```

Both print one JSON document. The cleanups are fixed; callers choose only the
mode, so no command or path ever comes from a caller:

| Cleanup | What it does |
|---|---|
| Unused packages and apt cache | `apt-get autoremove --purge -y` (old kernels included), `apt-get clean` |
| Old system logs | `journalctl --vacuum-time=7d --vacuum-size=200M` |
| Old snap revisions | removes revisions marked `disabled` in `snap list --all` |
| pip and npm caches | deletes `/root/.cache/pip` and `/root/.npm/_cacache` |

Freed space is measured before and after each step. Runs are logged to
`/var/log/ai-cleanup.log`. `report` also lists the largest directories (two
levels deep, innermost only), which is where to look when the disk grows for
another reason.

It uses only the Python standard library and runs with the system `python3`, so
it needs neither the bot's virtualenv nor its credentials. Install once (the
wrapper loads the code from `/opt/ai-ops-agent`, so later deploys update it):

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-cleanup /usr/local/sbin/ai-cleanup
```

### Verified deployments and rollback

Install the independent deployment manager once before enabling deployment
controls: `sudo bash /opt/ai-ops-agent/deploy/install-ai-deploy`. Thereafter,
`ai-deploy submit deploy TARGET main` queues a verified deployment and
`ai-deploy submit rollback TARGET` restores its previous verified version.
Required PM/Ops bridges and versioned units are installed automatically.
See [deployment migration, safety checks, and recovery](docs/deployments.md)
for the installation order, fixed server paths, dependency snapshots,
compatibility updaters, and interrupted-job recovery.

## Service restarts (`ai-service`)

`ai_ops_agent/service_control.py`, installed as `/usr/local/sbin/ai-service`,
restarts one service from the single whitelist in `services_list.py`
(`ai-coding-agent`, `ai-pm-agent`, `ai-ops-agent`, `ai-dashboard`), which the
bot's `/restart` and `/logs` use too.

```bash
ai-service list                 # {"ok": true, "services": [...]}
ai-service restart ai-pm-agent  # queued with systemctl --no-block
```

The restart is queued so a caller can answer before it goes down (the dashboard
restarting itself). Runs are logged to `/var/log/ai-service.log`. Standard
library only, no bot credentials. Install once:

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-service /usr/local/sbin/ai-service
```

## Package updates (`ai-packages`)

`ai_ops_agent/package_updates.py`, installed as `/usr/local/sbin/ai-packages`,
runs the same commands as the bot's `/update` and `/upgrade` for the dashboard.

```bash
ai-packages check     # apt-get update, then what can be upgraded, by kind
ai-packages upgrade   # apt-get upgrade -y, keeping installed config files
```

The mode is the only argument: no package names or apt options can be passed.
It never removes packages (`upgrade`, not `full-upgrade`). The upgrade runs in
its own systemd unit (`systemd-run --wait`), so restarting the dashboard
mid-run cannot interrupt dpkg. Upgrade output goes to the system journal,
not a pipe owned by the caller. The fixed `ai-packages-upgrade` unit refuses
a second upgrade while the first is still running. One run at a time;
runs are logged to `/var/log/ai-packages.log`. Standard library only, no bot
credentials. Install once:

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-packages /usr/local/sbin/ai-packages
```
