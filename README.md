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

Required GitHub Actions secrets:

```text
DEPLOY_WEBHOOK_URL    https://<deploy-host>/hooks/ai-ops-agent-update
DEPLOY_WEBHOOK_TOKEN  <webhook-secret>
```

The webhook is served over TLS only. The workflow fails before calling anything
if `DEPLOY_WEBHOOK_URL` is not `https://`, contains credentials, or has a query
string. The token is sent as an `Authorization` header, never in the URL, and
curl refuses plain HTTP and does not follow redirects (`--proto =https`,
`--proto-redir =https`, `--max-redirs 0`). The `Call deploy webhook` step is
therefore equivalent to:

```bash
curl --fail --show-error --silent -X POST \
  -H "Authorization: Bearer $DEPLOY_WEBHOOK_TOKEN" \
  https://<deploy-host>/hooks/ai-ops-agent-update
```

`<deploy-host>` must be a DNS name that the certificate covers. The default
certbot flow issues certificates for names, not bare IP addresses.

### TLS endpoint (server, owner-run)

The webhook listens on port 9000 without TLS today. Put a TLS reverse proxy in
front of it and keep the webhook itself on loopback:

1. Point a DNS name at `161.35.17.201`.
2. Obtain a certificate for it, for example with certbot's standalone or
   webroot plugin, and proxy `443` to the webhook on `127.0.0.1:9000`.
3. Close public access to port 9000 once the proxy works (firewall rule).
4. Check the endpoint from a machine outside the server. This request has no
   token, so it must be refused and does not trigger a deploy:

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
     https://<deploy-host>/hooks/ai-ops-agent-update
   ```

   Expect `401` over a valid certificate. A certificate error means the proxy
   or certificate is wrong; do not switch the workflow until this passes.

**Certificate renewal:** certbot renews through its systemd timer. Its renewal
hook must reload the proxy so the new certificate is served, for example
`systemctl reload nginx`. Confirm with `certbot renew --dry-run` and check the
expiry date with `certbot certificates` after each renewal.

### Webhook hook (server)

The hook `ai-ops-agent-update` in `/etc/webhook.conf` must accept the
`Authorization` header rule (the header value is `Bearer <webhook-secret>`).
Remove the old `secret` query-parameter rule once the workflow has switched.
Example rule (adjust to the existing hook definition):

```json
"trigger-rule": {
  "match": {
    "type": "value",
    "value": "Bearer <webhook-secret>",
    "parameter": { "source": "header", "name": "Authorization" }
  }
}
```

### Migration from the query-string secret

The old URL form put the secret in the query string, where it can appear in
logs, shell history and proxy access logs. Treat that value as exposed. Migrate
in this order and do not skip a step:

1. **Deploy the TLS endpoint first.** Set up the proxy and certificate above.
   Keep the old `secret` query rule and add the header rule so both forms
   work during the overlap.
2. **Switch the workflow.** Before the switch, confirm the service is healthy
   (`systemctl status ai-ops-agent.service --no-pager`) and not restarting. Then
   set `DEPLOY_WEBHOOK_URL` to the `https://<deploy-host>/hooks/ai-ops-agent-update`
   base URL, add `DEPLOY_WEBHOOK_TOKEN` with the current secret value, and merge.
   Confirm the next deploy succeeds and the update log shows it ran.
3. **Rotate the secret.** Generate a new value (`openssl rand -hex 32`), set it
   as `DEPLOY_WEBHOOK_TOKEN` in GitHub, and update the header rule in
   `/etc/webhook.conf`. Remove the old query rule, then reload the hook service.
4. **Confirm the old value is rejected.** Using the old value, expect `401`
   (any non-2xx means it no longer deploys):

   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
     -H "Authorization: Bearer <old-secret>" \
     https://<deploy-host>/hooks/ai-ops-agent-update
   ```

   Then confirm a deploy with the new value succeeds.

Steps 2–4 change live server and GitHub state. Run them through the owner's
Ops controls or an administrator, not from a coding run.

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
| Leftover temporary folders | deletes `/tmp/tmp*` folders (Python temp dirs, often left by test runs) untouched for over a day |
| Old Codex releases | deletes `/root/.codex/packages/app-server-daemon/releases/*` except the one `current` points to and the newest; nothing if `current` can't be read |

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
ai-service start ai-pm-agent    # unit verified active, then its port opened
ai-service stop ai-pm-agent     # unit verified inactive, then its port closed
ai-service reboot               # queued; refused while an upgrade, cleanup,
                                # tool update or deployment is running
```

The restart is queued so a caller can answer before it goes down (the dashboard
restarting itself). Runs are logged to `/var/log/ai-service.log`. Standard
library only, no bot credentials. Install once:

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-service /usr/local/sbin/ai-service
```

**Start and stop with a firewall port.** `SERVICE_PORTS` in `services_list.py`
maps a managed service to its TCP or UDP port. `start` runs `systemctl start`,
checks `ActiveState=active`, then `ufw allow PORT/proto`. `stop` runs
`systemctl stop`, checks `inactive`, then `ufw deny PORT/proto`, then checks
that nothing listens on the port and that no running Docker container publishes
it (ufw does not block Docker-published ports). Every step is a fixed argument
list; any timeout or nonzero exit stops the sequence. If the unit stops but the
port cannot be closed, the JSON reports `"ok": false`, `"unit": "inactive"` and
`"port_open"` as `true` or `null`. The bot asks for confirmation before stopping.
Services without a `SERVICE_PORTS` entry change only the unit.

**IPTV Docker controls.** `iptv-proxy` is whitelisted as an existing Docker
container with host TCP port 8080 (confirmed by a read-only server inspection).
The Services screen includes its Start and Stop buttons and displays Docker
state; `/restart iptv-proxy` and `/logs iptv-proxy` also use Docker. A confirmed
stop runs `docker stop iptv-proxy`, verifies the container exited, then checks
that no socket listens on 8080 and no running container publishes that port.
A remaining listener or publisher is reported as a partial failure. Start and
restart verify the container is running and publishes host TCP 8080. These
checks verify local state, not public internet reachability or application health.

Docker removes its port publication on stop and restores it on start. These
controls leave UFW unchanged because Docker forwarding rules bypass UFW input
rules. They do not create containers, change restart policies, or stop Docker
itself. A host reboot or an external operator can start the container again.
Other containers and shared ports are not managed by these buttons.

Deploy through the existing deployment workflow and restart the Ops bot to load
the new whitelist. The `deploy/ai-service` wrapper is unchanged and imports the
updated Python module; no separate installer is provided or needed. No live
start/stop or firewall changes were performed while preparing this change.

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

## Backups (`ai-backup`, `/backup`)

The code and the todos are in git; what is not is backed up: the four agents'
env files (bot tokens, API keys), the coding agent's project list and state
(tasks, queue), the deployment manager's state (rollback targets), the active
todo project and the Caddyfile, plus any path you add to
`/etc/ai-backup/extra-paths` (one per line, for example a proxy config).

Every night at 04:00 server time (`OPS_BACKUP_HOUR`), the ops bot runs
`ai-backup run`, which packs these into a tar.gz, encrypts it (AES-256 with the
key in `/etc/ai-backup/key`), keeps the last seven in `/var/backups/ai-agents`,
and the bot sends the archive to your chat, so a copy lives off the server.
`/backup` does the same at once. `ai-backup status` shows the last run.

Install once; it prints the key **once**, so save it in your password manager
(without it no archive can be opened):

```bash
sudo bash /opt/ai-ops-agent/deploy/install-ai-backup
```

Restore, on any machine with openssl (KEYFILE holds the saved key line):

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:KEYFILE \
    -in ai-backup-YYYYMMDD-HHMMSS.tar.gz.enc | tar -xz -C restore/
```

The archive keeps full paths (`etc/ai-pm-agent.env`, `var/lib/...`), so each
file goes back where it came from.

## AI tool updates (`ai-tools`)

`ai_ops_agent/tool_updates.py`, installed as `/usr/local/sbin/ai-tools`, does
what the bot's `/ai_tools` does for the dashboard, from the single list in
`tools_list.py` (`codex`, `claude`).

```bash
ai-tools check           # installed and published version of each tool
ai-tools update claude   # npm install -g <its package>@latest
```

Only listed tool names are accepted: no package names or npm options can be
passed. One update at a time; updates are logged to `/var/log/ai-tools.log`.
Standard library only, no bot credentials. Install once:

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-tools /usr/local/sbin/ai-tools
```

Reboots use a five-second systemd timer to let the dashboard answer first.
The guard also checks queued deployments and detached package upgrades.

### Package repository failures

If `/update` or the dashboard's package check fails because the Caddy repository
returns HTTP 402, see [package repository recovery](docs/package-repository-recovery.md).
Keep signature checks enabled; this is an upstream download failure.

### Read-only evidence for Coding

Install the diagnostic helper once after deploying this revision:

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-diagnostics /usr/local/sbin/ai-diagnostics
sudo ai-diagnostics ai-ops-agent
sudo bash /opt/ai-ops-agent/deploy/install-ai-deploy
```

The updated deployment manager maintains this wrapper on later Ops deployments.
`ai-diagnostics` accepts exactly one of the four managed service names. It returns
bounded, redacted service status, recent journal entries, deployment state and
cached package/tool check results. It runs no checks that change state and no
repairs. Missing evidence is reported explicitly. Cached checks retain timestamps;
refresh them in Ops if needed. Run it as the existing agent service user (root on
the current installation); it is not a setuid or public HTTP endpoint.

### Proxy and VPN backup paths

Add host configuration directories (for example `/etc/mtproxy`,
`/opt/mtg/config.toml`, and `/opt/amnezia`) to `/etc/ai-backup/extra-paths`.
For Amnezia configurations stored inside Docker, add:

```text
docker:amnezia-awg2:/opt/amnezia
```

Every backup reads that directory afresh using `docker cp` and includes Docker
inspection metadata. Nothing is copied back into the running container. A missing
container or failed copy fails the backup rather than silently omitting VPN keys.
After decrypting the outer archive, container data is in
`containers/amnezia-awg2/opt_amnezia.tar`; inspect this nested tar before restoring
its contents into a replacement container. The metadata records the container
settings, but the Docker image itself is not backed up. Preserve the Amnezia
Dockerfile too so the image can be rebuilt. These archives contain VPN private
keys and client records; keep the encryption key separately.

## Security check (ai-audit)

`ai_ops_agent/audit.py` (with `security_stats.py`), installed as
`/usr/local/sbin/ai-audit`, reads the server's security state and prints one
JSON report: `ai-audit check`. It only reads fixed commands (`journalctl` for
sshd, `fail2ban-client`, `who`, `ss`, `sshd -T`, `ufw`, `apt-get -s`) and file
permissions; it changes nothing and nothing leaves the server.

- **Stats:** failed and successful SSH logins in 24 h, top attacking IPs and
  tried user names, fail2ban bans per jail (now and in total), who is logged
  in, public ports with their process, firewall state.
- **Findings, with the exact fix:** SSH password or root login, secret files
  readable by others, no fail2ban, firewall off, waiting security updates,
  automatic updates off, reboot needed, unexpected public ports.

`/security` shows the report; the bot also sends it daily at `OPS_AUDIT_HOUR`
(server local time, default 9). Install (or update) the command once:

```bash
sudo install -m 755 /opt/ai-ops-agent/deploy/ai-audit /usr/local/sbin/ai-audit
```

## Connected clients and traffic

`ai-clients check` collects fixed, read-only metrics for AmneziaWG, Telegram
MTProxy (2398), mtg (9443), the HTTPS relay and the media/IPTV container.
Install its wrapper and 30-second background sampler after deploying:
`sudo sh /opt/ai-ops-agent/deploy/install-ai-clients`.
The collector uses only the standard library and stores its last sample in
`/var/lib/ai-clients/report.json` (private directory, atomic writes, flock).
It never reads VPN private keys or proxy secrets. VPN display names come from
Amnezia's clientsTable; anonymous device IDs are hashes of public keys.

VPN activity means a handshake within 180 seconds, not proof of continuous
use. TCP counts include only established inbound sockets on each listening
port; Telegram backend sockets are excluded. Media sockets are read inside
the container network namespace. Source IP addresses may represent several
people or a reverse proxy. Profiles can also be shared. Counts are not summed
across services. Raw profiles, browsing URLs and proxy credentials are omitted.

The relay's fixed loopback exporter is `127.0.0.1:18081/metrics`. mtg's exporter
is configured at `127.0.0.1:19445/metrics` using `[stats.prometheus]` with
`enabled = true`, `bind-to = "127.0.0.1:19445"`, `http-path = "/metrics"`, and
`metric-prefix = "mtg"`. Keep these exporters bound to localhost. Configuration
changes require a proxy restart; back up its config first. MTProxy's native
stats endpoint is unavailable on this installation, so it reports sockets only.

Traffic counters belong to the current service/interface lifetime. Media
traffic includes upstream and downstream container traffic (content can count
twice). Daily usage and peaks are *sampled*, start at the first observation
of each UTC day, and can miss short connections or bytes across counter resets.
Rates use counter deltas, never absolute counters; rates older than 120 seconds
are omitted. No historical data before installation is reconstructed.

SSH appears in Connected clients as authenticated SSH connections, detected
from authenticated sshd child processes (including non-terminal commands and
tunnels). Listener, privilege-separation and pre-authentication processes are
excluded. Multiplexed SSH channels count as one transport. Raw inbound TCP
connections and source addresses are shown separately and can include login
attempts. Live SSH settings supply the listening ports; missing process data
is unavailable rather than zero. SSH checks themselves briefly add a connection.

### SSH login alerts

Every minute the bot reads sshd's journal and sends successful SSH logins to
the owner's chat: a login from an address or with a key never seen before
arrives at once as **⚠️ New SSH login** (user, address, time, method, key);
logins from known addresses and keys stay quiet. The first run learns the last
30 days without alerting. `OPS_LOGIN_ALERTS=all` sends every login, `off`
none; known addresses and keys are kept in `/var/lib/ai-ops-agent/login-watch.json`
(`OPS_LOGIN_STATE`).
