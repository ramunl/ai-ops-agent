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
| /logs [service] | Last 30 log lines (default: ai-agent) |
| /errors [service] | Recent error-level log lines (default: all managed services) |
| /restart <service> | Restart a whitelisted service |
| /reboot | Reboot the whole system |
| /update | apt update + classify upgradable packages by critical, stable, and not recommended channels |
| /upgrade | apt upgrade -y |
| /version | Running bot version, branch, and commit |
| /ai_version | Installed AI agents, versions, git refs, service states, configured models, and latest status |
| /ai_update [agent] | Update all AI agents by default, or one agent by name/alias |

Only whitelisted services can be managed (see `config.py`), and only the authorized chat ID can issue commands.

## Setup

```bash
# 1. Create a SECOND Telegram bot via @BotFather (e.g. @channelcast_ops_bot)

# 2. Clone and install
sudo git clone git@github.com:ramunl/ai-ops-agent.git /opt/ai-ops-agent
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
ai_ops_agent v<VERSION>
branch: <git-branch>
commit: <short-sha>
```

The version string comes from the top-level `VERSION` file.
