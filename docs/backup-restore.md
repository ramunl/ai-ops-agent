# Backup and restore

`ai-backup run` packs what is not in git — secrets, agent state and small
configs — into a tar.gz, encrypts it with OpenSSL AES-256-CBC and keeps the last
seven archives in `/var/backups/ai-agents`. The ops bot sends each archive to
the owner's chat, so a copy lives off the server. `ai-backup status` shows the
last run and the last successful run.

## What is backed up

`ai_ops_agent/backup.py` has a fixed list of default sources, plus any owner
paths in `/etc/ai-backup/extra-paths` (one per line, `#` comments). The default
sources are the four agents' env files, the coding agent's project registry and
state, the coding agent's context directory (`/var/lib/ai-coding-agent/context`),
the deployment manager's state, the active todo project and the Caddyfile.

Directories are archived recursively. A path that does not exist yet — for
example `/var/lib/ai-coding-agent/context` before the coding agent has written
it — is reported in `skipped`, not treated as an error. Symlinks are stored as
links and are never followed, so a link pointing outside the backed-up tree
does not pull its target's contents into the archive.

## Capacity cap

The archive is capped at `MAX_BYTES = 20 MiB`; an archive over the cap fails
the run and writes nothing, so a wrong extra path cannot fill the 1 GB VPS disk.
The cap is deliberately unchanged. The coding agent's context directory can
grow (its size and format are defined by the coding agent's CTX01 archive
contract, which is a separate repository), so verify the real context size
before production rollout. If it approaches the cap, do not raise the cap here
unreviewed — assess the contract first and get owner approval.

## Restore procedure

Restore is a manual Ops step. There is deliberately no `ai-backup restore`
subcommand: the CLI contract is `run|status`, and the dashboard parses that
output. Restore into a **fresh, empty temp directory first**, verify what was
extracted, then copy the files into place one by one. Never extract directly
over the live paths.

The encryption key lives in `/etc/ai-backup/key` and must never be passed on a
command line. OpenSSL reads it from the file with `-pass file:`, and the shell
never sees its value.

```bash
# On the machine holding the key, or after copying the archive and a key file
# to a trusted machine. KEYFILE holds the saved key line, never the key text
# in the command.
mkdir -m 700 restore && cd restore
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:KEYFILE \
    -in /var/backups/ai-agents/ai-backup-YYYYMMDD-HHMMSS.tar.gz.enc \
    | tar -xz
```

Then, still inside the fresh temp dir, list what came out and confirm the
member paths are only the expected relative paths before copying anything into
place:

```bash
tar -tzf <(openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:KEYFILE \
    -in /var/backups/ai-agents/ai-backup-YYYYMMDD-HHMMSS.tar.gz.enc)
```

Compare the extracted tree against the backup's `included`/`skipped` list
(from the bot message or `ai-backup status`) and against `/etc/ai-backup/key`
exclusion expectations. Only then copy the restored files over the live paths
with the same ownership and mode as the originals.

### Safety rules

- **Temp path only.** Always extract to a new empty directory; never `-C /`.
- **Key never on the command line.** Use `-pass file:KEYFILE`.
- **No member escapes.** Extraction must reject absolute paths and `..`
  components. GNU `tar` strips a leading `/`, but confirm the member list first
  rather than relying on it.
- **Links are not followed.** Restored symlinks stay symlinks; do not copy
  through them. Absolute or escaping symlinks must be inspected, not used as-is.
- **Live state untouched.** Extraction into the temp dir never modifies the
  running agents; copy back only after verification.

## Production rollout

Adding `/var/lib/ai-coding-agent/context` is a code change that reaches the
server only after the normal deploy. Activating it in production is an Ops
action the owner approves and schedules in an idle window; the coding sandbox
never mutates the server or `/var/lib/ai-coding-agent/context`. Until the
directory exists, `ai-backup run` skips it silently and the rest of the backup
proceeds.
