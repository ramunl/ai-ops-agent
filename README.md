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
| /health | CPU load, RAM, disk summary |
| /disk | Disk usage details |
| /memory | Memory details |
| /uptime | Uptime and load average |
| /services | Status of managed services |
| /logs [service] | Last 30 log lines (default: ai-agent) |
| /errors [service] | Recent error-level log lines |
| /restart <service> | Restart a whitelisted service |
| /update | apt update + list upgradable packages |
| /upgrade | apt upgrade -y |

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
