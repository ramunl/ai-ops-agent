# Recovering package checks when Caddy's repository is unavailable

Both the Ops `/update` command and the dashboard Updates card refresh all apt
sources. A failure in one third-party source can therefore fail the whole check.
On 2026-10-10, the Caddy Cloudsmith repository returned HTTP 402 (Payment Required).
The subsequent apt message that the repository was "no longer signed" was caused
by the failed download; do not disable signature verification or import random keys.

To restore Ubuntu package checks temporarily, retain the installed Caddy and move
only its source list out of apt's sources directory. First verify the HTTP error
and confirm that this file contains only the Caddy source:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 20 \
  https://dl.cloudsmith.io/public/caddy/stable/deb/debian/dists/any-version/InRelease
cat /etc/apt/sources.list.d/caddy-stable.list
sudo install -d -m 700 /var/backups/ai-ops-agent
sudo mv -n /etc/apt/sources.list.d/caddy-stable.list \
  /var/backups/ai-ops-agent/caddy-stable.list
sudo ai-packages check
```

This does not uninstall, downgrade, or restart Caddy. **Caddy package updates are
paused while this source is disabled.** Other packages can be checked normally.
Do not run this workaround automatically whenever any repository fails.

When the official repository serves a valid signed index again, restore it:

```bash
sudo cp -n /var/backups/ai-ops-agent/caddy-stable.list \
  /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update -q
```

Then use **Check for updates** in the dashboard to replace its saved error.
An upgrade is a separate action; a successful check does not install packages.
See the [official Caddy installation instructions](https://caddyserver.com/docs/install).
