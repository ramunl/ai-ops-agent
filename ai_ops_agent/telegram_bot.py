"""Telegram bot exposing server operations commands."""

import asyncio
from dataclasses import dataclass
import html
import json
import logging
import os
from pathlib import Path
import re
from urllib import error, request

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ai_agent_common import (
    Command,
    CoreCommand,
    build_command_list,
    is_authorized as shared_is_authorized,
    to_bot_commands,
)

from . import config
from .shell import run
from .version import get_runtime_version

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
_CORE_COMMAND = CoreCommand(
    submodule_dir=ROOT_DIR / "ai_agent_common",
    superproject_dir=ROOT_DIR,
    submodule_path="ai_agent_common",
    agent_name="ai-ops-agent",
)

_C_LOCALE_ENV = {"LC_ALL": "C"}
_APT_UPGRADABLE_RE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+"
    r"(?P<version>\S+)\s+(?P<arch>\S+)"
    r"(?:\s+\[upgradable from:\s+(?P<old>[^\]]+)\])?"
)
_UNTESTED_APT_CHANNELS = (
    "proposed",
    "backports",
    "testing",
    "unstable",
    "experimental",
    "devel",
)
_MAX_UPDATE_ITEMS_PER_SECTION = 30
_MODEL_ENV_KEYS = (
    "AI_MODEL",
    "MODEL",
    "OPENAI_MODEL",
    "CODEX_MODEL",
    "PM_MODEL",
    "ANTHROPIC_MODEL",
    "CLAUDE_MODEL",
    "LLM_MODEL",
)

COMMANDS = build_command_list(
    [
        Command("start", "Start the ops bot"),
        Command("health", "Show CPU, RAM, and disk summary"),
        Command("disk", "Show disk usage details"),
        Command("memory", "Show memory details"),
        Command("uptime", "Show uptime and load"),
        Command("services", "Show managed service status"),
        Command("logs", "Show recent service logs"),
        Command("errors", "Show recent service errors"),
        Command("restart", "Restart a managed service"),
        Command("reboot", "Reboot the whole system"),
        Command("update", "Check available system updates"),
        Command("upgrade", "Install system updates"),
        Command("core", "Show the shared core version"),
        Command("my_agents", "Show installed AI agents"),
        Command("ai_tools", "Show or update installed AI tools"),
        Command("ai_update", "Update installed AI agents"),
    ]
)
BOT_COMMANDS = tuple(to_bot_commands(COMMANDS))


@dataclass(frozen=True)
class AptUpdate:
    name: str
    suites: tuple[str, ...]
    version: str
    arch: str
    old_version: str | None

    @property
    def channel(self) -> str:
        return ",".join(self.suites)


@dataclass(frozen=True)
class AptUpdateReport:
    critical: list[AptUpdate]
    stable: list[AptUpdate]
    not_recommended: list[AptUpdate]

    @property
    def total(self) -> int:
        return len(self.critical) + len(self.stable) + len(self.not_recommended)


async def arun(cmd: list[str], **kwargs) -> str:
    return await asyncio.to_thread(run, cmd, **kwargs)


def is_authorized(update: Update) -> bool:
    authorized = shared_is_authorized(update, config.AUTHORIZED_CHAT_ID)
    if not authorized:
        logger.warning(
            "Ignored message from unauthorized chat: %s",
            getattr(getattr(update, "effective_chat", None), "id", "unknown"),
        )
    return authorized


_SUFFIX = "\n... (truncated)"


async def reply(update: Update, text: str) -> None:
    """Reply with plain text, truncated to fit Telegram's limit."""
    limit = config.TELEGRAM_MESSAGE_LIMIT - len(_SUFFIX)
    if len(text) > limit:
        text = text[:limit] + _SUFFIX
    await update.message.reply_text(text)


async def reply_code(update: Update, header: str, body: str) -> None:
    """Reply with a header line followed by body in a monospace code block."""
    overhead = len(header) + 1 + len("<pre></pre>")
    limit = config.TELEGRAM_MESSAGE_LIMIT - overhead - len(_SUFFIX)
    if len(body) > limit:
        body = body[:limit] + _SUFFIX
    text = f"{header}\n<pre>{html.escape(body)}</pre>"
    await update.message.reply_text(text, parse_mode="HTML")


async def reply_expandable(update: Update, header: str, body: str) -> None:
    """Reply with header + collapsible body (tap to expand). Use for long outputs."""
    overhead = len(header) + 1 + len("<blockquote expandable></blockquote>")
    limit = config.TELEGRAM_MESSAGE_LIMIT - overhead - len(_SUFFIX)
    if len(body) > limit:
        body = body[:limit] + _SUFFIX
    text = f"{header}\n<blockquote expandable>{html.escape(body)}</blockquote>"
    await update.message.reply_text(text, parse_mode="HTML")


async def reply_html(update: Update, text: str) -> None:
    """Reply with HTML-formatted text, truncated to fit Telegram's limit."""
    if len(text) > config.TELEGRAM_MESSAGE_LIMIT:
        text = text[: config.TELEGRAM_MESSAGE_LIMIT - len(_SUFFIX)] + _SUFFIX
    await update.message.reply_text(text, parse_mode="HTML")


async def send_rich_message(update: Update, html_text: str) -> bool:
    """Send a Bot API 10.1 rich message. Return False if unsupported/unavailable."""
    if update.message is None:
        return False

    payload = {
        "chat_id": update.message.chat_id,
        "rich_message": {"html": html_text},
    }
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{config.OPS_TELEGRAM_BOT_TOKEN}/sendRichMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _post() -> bool:
        try:
            with request.urlopen(req, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
                return bool(body.get("ok"))
        except (OSError, error.HTTPError, json.JSONDecodeError):
            logger.info("sendRichMessage unavailable; falling back to HTML", exc_info=True)
            return False

    return await asyncio.to_thread(_post)


def _usage_bar(pct: float, width: int = 10) -> str:
    """Render a Unicode progress bar for a 0-100 percentage."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _fmt_bytes(n: float) -> str:
    """Human-readable size, e.g. 4.0G."""
    for unit in ("B", "K", "M", "G", "T", "P"):
        if n < 1024 or unit == "P":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def _free_usage(free_b: str, label: str = "Mem:") -> tuple[int, int] | None:
    """(used_bytes, total_bytes) for a `free -b` row ("Mem:"/"Swap:"), or None."""
    for line in free_b.splitlines():
        if line.startswith(label):
            parts = line.split()
            try:
                total = int(parts[1])
                if label == "Mem:" and len(parts) > 6:
                    used = total - int(parts[6])  # total - available
                else:
                    used = int(parts[2])
                return used, total
            except (ValueError, IndexError):
                return None
    return None


def _disk_usage(df_hp: str) -> tuple[str, str, float] | None:
    """(used, size, percent) parsed from `df -hP /`, or None on failure."""
    lines = df_hp.splitlines()
    if len(lines) < 2:
        return None
    parts = lines[1].split()
    try:
        return parts[2], parts[1], float(parts[4].rstrip("%"))
    except (ValueError, IndexError):
        return None


def _disk_rows(df_hp: str) -> list[tuple[str, str, str, float]]:
    """[(mount, used, size, percent), ...] for real block devices from `df -hP`."""
    rows = []
    for line in df_hp.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[0].startswith("/dev/"):
            continue
        try:
            rows.append((parts[5], parts[2], parts[1], float(parts[4].rstrip("%"))))
        except (ValueError, IndexError):
            continue
    return rows


def _strip_exit_prefix(raw: str) -> str:
    """'[exit 3] inactive' → 'inactive'"""
    if raw.startswith("[exit") and "] " in raw:
        return raw.split("] ", 1)[1]
    return raw


def _filter_disk(raw: str) -> str:
    """Keep only real block-device lines from df output, dropping overlay/tmpfs noise."""
    lines = raw.splitlines()
    if not lines:
        return raw
    header = lines[0]
    real = [l for l in lines[1:] if l.startswith("/dev/")]
    return "\n".join([header] + real) if real else raw


def resolve_service(args: list[str] | None) -> str | None:
    """Return a whitelisted service name from args, or None if invalid."""
    if not args:
        return config.DEFAULT_SERVICE
    name = args[0]
    if name in config.MANAGED_SERVICES:
        return name
    logger.info("Rejected non-whitelisted service name: %s", name)
    return None


def _agent_match_keys(agent: dict) -> set[str]:
    name = agent["name"]
    keys = {
        name,
        name.removeprefix("ai-").removesuffix("-agent"),
    }
    service = agent.get("service")
    if service:
        keys.add(service)
    keys.update(agent.get("aliases", ()))
    return {key.lower() for key in keys}


def resolve_ai_agents(args: list[str] | None) -> list[dict] | None:
    """Return all configured AI agents, or one matched by name/alias."""
    if not args:
        return list(config.AI_AGENT_INSTALLS)

    requested = args[0].lower()
    for agent in config.AI_AGENT_INSTALLS:
        if requested in _agent_match_keys(agent):
            return [agent]
    return None


def _ai_system_match_keys(system: dict) -> set[str]:
    keys = {system["name"]}
    keys.update(system.get("aliases", ()))
    package = system.get("package")
    if package:
        keys.add(package)
        keys.add(package.rsplit("/", 1)[-1])
    return {key.lower() for key in keys}


def resolve_ai_systems(args: list[str] | None) -> list[dict] | None:
    """Return all configured AI systems, or one matched by name/alias/package."""
    if not args or args[0].lower() == "all":
        return list(config.AI_SYSTEM_INSTALLS)

    requested = args[0].lower()
    for system in config.AI_SYSTEM_INSTALLS:
        if requested in _ai_system_match_keys(system):
            return [system]
    return None


def _ai_agent_names() -> str:
    return ", ".join(agent["name"] for agent in config.AI_AGENT_INSTALLS)


def _ai_system_names() -> str:
    return ", ".join(system["name"] for system in config.AI_SYSTEM_INSTALLS)


def _self_ai_agent_service() -> str:
    for agent in config.AI_AGENT_INSTALLS:
        if agent["name"] == "ai-ops-agent":
            return agent.get("service", "")
    return ""


def _journalctl_unit_args(services: list[str]) -> list[str]:
    args = []
    for service in services:
        args.extend(["-u", service])
    return args


def _read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return None


def _parse_env_file(path: Path) -> dict[str, str]:
    raw = _read_text_file(path)
    if raw is None:
        return {}

    values = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _agent_version(path: Path) -> str:
    version = _read_text_file(path / "VERSION")
    if version:
        return version

    pyproject = _read_text_file(path / "pyproject.toml")
    if pyproject:
        match = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', pyproject)
        if match:
            return match.group(1)

    package_json = _read_text_file(path / "package.json")
    if package_json:
        try:
            version = json.loads(package_json).get("version")
        except json.JSONDecodeError:
            version = None
        if version:
            return str(version)

    return "unknown"


def _agent_model(env_file: str | None) -> str:
    if not env_file:
        return "unknown"
    env = _parse_env_file(Path(env_file))
    models = [env[key] for key in _MODEL_ENV_KEYS if env.get(key)]
    return ", ".join(models) if models else "unknown"


def _system_model(system: dict) -> str:
    values = []
    keys = system.get("model_env_keys") or _MODEL_ENV_KEYS
    env_file = system.get("env_file")
    if env_file:
        env = _parse_env_file(Path(env_file))
        values.extend(env[key] for key in keys if env.get(key))
    values.extend(os.environ[key] for key in keys if os.environ.get(key))
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return ", ".join(seen) if seen else "unknown"


def _parse_tool_version(raw: str) -> str:
    if _update_result_failed(raw) or raw.startswith("Command timed out"):
        return "not installed"
    return raw.splitlines()[0].strip() if raw.strip() else "unknown"


async def _npm_latest_version(package: str) -> str:
    latest = await arun(["npm", "view", package, "version"], timeout=60)
    if _update_result_failed(latest) or latest.startswith("Command timed out"):
        return "unknown"
    return latest.splitlines()[-1].strip() if latest.strip() else "unknown"


def _version_is_current(installed: str, latest: str) -> str:
    if installed == "not installed":
        return "not installed"
    if latest == "unknown":
        return "unknown"
    if latest and latest in installed:
        return "up to date"
    return f"update available: {latest}"


async def _inspect_ai_system(system: dict) -> dict[str, str]:
    version_cmd = list(system["version_cmd"])
    version_raw, latest, model = await asyncio.gather(
        arun(version_cmd, timeout=30),
        _npm_latest_version(system["package"])
        if system.get("package_manager") == "npm" and system.get("package")
        else asyncio.to_thread(lambda: "unknown"),
        asyncio.to_thread(lambda: _system_model(system)),
    )
    installed = _parse_tool_version(version_raw)
    return {
        "name": system["name"],
        "installed": "yes" if installed != "not installed" else "no",
        "version": installed,
        "model": model,
        "package_manager": system.get("package_manager", "unknown"),
        "package": system.get("package", "unknown"),
        "latest": latest,
        "status": _version_is_current(installed, latest),
        "update_command": f"/ai_tools update {system['name']}",
    }


def _format_ai_system_info(system: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(system['name'])}</b>",
        f"installed: <code>{html.escape(system['installed'])}</code>",
        f"version: <code>{html.escape(system['version'])}</code>",
        f"model: <code>{html.escape(system['model'])}</code>",
        f"package: <code>{html.escape(system['package_manager'])}</code> "
        f"<code>{html.escape(system['package'])}</code>",
        f"latest: <code>{html.escape(system['latest'])}</code>",
        f"status: <code>{html.escape(system['status'])}</code>",
        f"update: <code>{html.escape(system['update_command'])}</code>",
    ]


async def _update_ai_system(system: dict) -> dict[str, str]:
    name = system["name"]
    package_manager = system.get("package_manager")
    package = system.get("package")
    if package_manager != "npm" or not package:
        return {
            "name": name,
            "status": "failed",
            "detail": "no supported update command configured",
        }

    before = _parse_tool_version(await arun(list(system["version_cmd"]), timeout=30))
    update = await arun(["npm", "install", "-g", f"{package}@latest"], timeout=300)
    if _update_result_failed(update) or update.startswith("Command timed out"):
        return {
            "name": name,
            "status": "failed",
            "detail": update,
        }

    after = _parse_tool_version(await arun(list(system["version_cmd"]), timeout=30))
    if before == after:
        status = "already current"
        detail = after
    else:
        status = "updated"
        detail = f"{before} → {after}"
    return {
        "name": name,
        "status": status,
        "detail": detail,
    }


def _format_ai_system_update_result(result: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(result['name'])}</b>",
        f"status: <code>{html.escape(result['status'])}</code>",
        f"detail: <code>{html.escape(result['detail'])}</code>",
    ]


async def _agent_latest_status(path: Path) -> str:
    upstream = await arun(
        [
            "git",
            "-C",
            str(path),
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{u}",
        ]
    )
    if upstream.startswith("[exit") or upstream.startswith("Error:") or not upstream:
        return "unknown (no upstream)"

    fetch = await arun(
        ["git", "-C", str(path), "fetch", "--quiet", "--prune"],
        timeout=60,
    )
    if fetch.startswith("[exit") or fetch.startswith("Error:"):
        return "unknown (fetch failed)"

    counts = await arun(
        [
            "git",
            "-C",
            str(path),
            "rev-list",
            "--left-right",
            "--count",
            "HEAD...@{u}",
        ]
    )
    if counts.startswith("[exit") or counts.startswith("Error:"):
        return "unknown (compare failed)"

    try:
        ahead, behind = (int(part) for part in counts.split()[:2])
    except (ValueError, IndexError):
        return "unknown (bad compare output)"

    if ahead == 0 and behind == 0:
        return f"up to date with {upstream}"
    if ahead == 0:
        return f"behind {upstream} by {behind} commit(s)"
    if behind == 0:
        return f"ahead of {upstream} by {ahead} commit(s)"
    return f"diverged from {upstream}: ahead {ahead}, behind {behind}"


async def _inspect_ai_agent(agent: dict[str, str]) -> dict[str, str]:
    path = Path(agent["path"])
    installed = path.exists()
    version, model = await asyncio.to_thread(
        lambda: (
            _agent_version(path) if installed else "not installed",
            _agent_model(agent.get("env_file")) if installed else "unknown",
        )
    )

    branch = commit = latest = "unknown"
    if installed:
        branch, commit, latest = await asyncio.gather(
            arun(["git", "-C", str(path), "branch", "--show-current"]),
            arun(["git", "-C", str(path), "rev-parse", "--short", "HEAD"]),
            _agent_latest_status(path),
        )
        if branch.startswith("[exit") or branch.startswith("Error:") or not branch:
            branch = "unknown"
        if commit.startswith("[exit") or commit.startswith("Error:") or not commit:
            commit = "unknown"

    service = agent.get("service") or ""
    service_state = "not configured"
    if service:
        service_state = _strip_exit_prefix(
            await arun(["systemctl", "is-active", service])
        ).strip()

    return {
        "name": agent["name"],
        "installed": "yes" if installed else "no",
        "path": str(path),
        "service": service or "none",
        "service_state": service_state,
        "version": version,
        "model": model,
        "branch": branch,
        "commit": commit,
        "latest": latest,
    }


def _format_ai_agent_info(agent: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(agent['name'])}</b>",
        f"installed: <code>{html.escape(agent['installed'])}</code>",
        f"path: <code>{html.escape(agent['path'])}</code>",
        f"service: <code>{html.escape(agent['service'])}</code> "
        f"({html.escape(agent['service_state'])})",
        f"version: <code>{html.escape(agent['version'])}</code>",
        f"model: <code>{html.escape(agent['model'])}</code>",
        f"git: <code>{html.escape(agent['branch'])}</code> "
        f"<code>{html.escape(agent['commit'])}</code>",
        f"latest: <code>{html.escape(agent['latest'])}</code>",
    ]


def _update_result_failed(output: str) -> bool:
    return output.startswith("[exit") or output.startswith("Error:")


async def _restart_ai_agent_service(
    service: str,
    *,
    no_block: bool = False,
) -> str:
    if not service:
        return "not configured"
    command = ["systemctl", "restart"]
    if no_block:
        command.append("--no-block")
    command.append(service)
    result = await arun(command, timeout=30)
    if _update_result_failed(result):
        return f"restart failed: {result}"
    if no_block:
        return "restart queued"
    state = _strip_exit_prefix(await arun(["systemctl", "is-active", service])).strip()
    return f"restarted, state: {state}"


async def _update_ai_agent(agent: dict, *, defer_self_restart: bool) -> dict[str, str]:
    path = Path(agent["path"])
    name = agent["name"]
    service = agent.get("service") or ""
    if not path.exists():
        return {
            "name": name,
            "status": "skipped",
            "detail": f"not installed at {path}",
            "restart": "not run",
        }

    before = await arun(["git", "-C", str(path), "rev-parse", "--short", "HEAD"])
    if _update_result_failed(before) or not before:
        return {
            "name": name,
            "status": "failed",
            "detail": f"cannot read current commit: {before}",
            "restart": "not run",
        }

    pull = await arun(["git", "-C", str(path), "pull", "--ff-only"], timeout=180)
    if _update_result_failed(pull):
        return {
            "name": name,
            "status": "failed",
            "detail": pull,
            "restart": "not run",
        }

    submodules = await arun(
        [
            "git",
            "-C",
            str(path),
            "submodule",
            "update",
            "--init",
            "--recursive",
        ],
        timeout=180,
    )
    if _update_result_failed(submodules):
        return {
            "name": name,
            "status": "failed",
            "detail": f"submodule sync failed: {submodules}",
            "restart": "not run",
        }

    after = await arun(["git", "-C", str(path), "rev-parse", "--short", "HEAD"])
    if _update_result_failed(after) or not after:
        return {
            "name": name,
            "status": "failed",
            "detail": f"cannot read updated commit: {after}",
            "restart": "not run",
        }
    changed = after != before
    status = "updated" if changed else "already current"
    detail = f"{before} → {after}" if changed else before

    if not changed:
        restart = "not needed"
    elif name == "ai-ops-agent" and defer_self_restart:
        restart = "deferred until after report"
    else:
        restart = await _restart_ai_agent_service(service)

    return {
        "name": name,
        "status": status,
        "detail": detail,
        "restart": restart,
    }


def _format_ai_update_result(result: dict[str, str]) -> list[str]:
    return [
        f"<b>{html.escape(result['name'])}</b>",
        f"status: <code>{html.escape(result['status'])}</code>",
        f"detail: <code>{html.escape(result['detail'])}</code>",
        f"service: <code>{html.escape(result['restart'])}</code>",
    ]


def _parse_apt_upgradable(raw: str) -> list[AptUpdate]:
    updates = []
    for line in raw.splitlines():
        line = line.strip()
        if (
            not line
            or line == "Listing..."
            or line.startswith("WARNING:")
            or line.startswith("N:")
        ):
            continue
        match = _APT_UPGRADABLE_RE.match(line)
        if not match:
            continue
        suites = tuple(
            suite.strip() for suite in match.group("suites").split(",") if suite.strip()
        )
        updates.append(
            AptUpdate(
                name=match.group("name"),
                suites=suites,
                version=match.group("version"),
                arch=match.group("arch"),
                old_version=match.group("old"),
            )
        )
    return updates


def _group_apt_updates(raw: str) -> AptUpdateReport:
    grouped = {"critical": [], "stable": [], "not_recommended": []}
    for item in _parse_apt_upgradable(raw):
        grouped[_classify_apt_update(item)].append(item)
    return AptUpdateReport(
        critical=grouped["critical"],
        stable=grouped["stable"],
        not_recommended=grouped["not_recommended"],
    )


def _classify_apt_update(item: AptUpdate) -> str:
    channel = item.channel.lower()
    if "security" in channel:
        return "critical"
    if any(marker in channel for marker in _UNTESTED_APT_CHANNELS):
        return "not_recommended"
    return "stable"


def _format_update_summary_html(report: AptUpdateReport) -> str:
    if report.total == 0:
        return "✅ <b>Update check</b>\n\nNo upgradable packages found."

    action = "Review only."
    if report.critical:
        action = "Install security updates soon."
    elif report.stable and not report.not_recommended:
        action = "Regular update looks safe."
    elif report.not_recommended:
        action = "Review not recommended packages before upgrading."

    return "\n".join(
        [
            "📦 <b>Update check</b>",
            "",
            f"<b>Total:</b> <code>{report.total}</code> package(s)",
            f"🚨 <b>Critical/security:</b> <code>{len(report.critical)}</code>",
            f"✅ <b>Stable:</b> <code>{len(report.stable)}</code>",
            "⚠️ <b>Not tested / not recommended:</b> "
            f"<code>{len(report.not_recommended)}</code>",
            "",
            f"<b>Recommendation:</b> {html.escape(action)}",
        ]
    )


def _format_apt_item_html(item: AptUpdate) -> str:
    old = f" ← {item.old_version}" if item.old_version else ""
    return (
        f"• <code>{html.escape(item.name)}</code> "
        f"<b>{html.escape(item.version)}</b>{html.escape(old)}\n"
        f"  <i>{html.escape(item.arch)} · {html.escape(item.channel)}</i>"
    )


def _format_update_items_html(items: list[AptUpdate]) -> str:
    if not items:
        return "None."
    shown = items[:_MAX_UPDATE_ITEMS_PER_SECTION]
    lines = [_format_apt_item_html(item) for item in shown]
    hidden = len(items) - len(shown)
    if hidden > 0:
        lines.append(f"… and {hidden} more package(s)")
    return "\n".join(lines)


def _format_update_fallback_html(report: AptUpdateReport) -> str:
    if report.total == 0:
        return _format_update_summary_html(report)

    return "\n\n".join(
        [
            _format_update_summary_html(report),
            "<blockquote expandable>"
            "<b>🚨 Critical / security</b>\n"
            "Security repository updates.\n\n"
            f"{_format_update_items_html(report.critical)}"
            "</blockquote>",
            "<blockquote expandable>"
            "<b>✅ Stable</b>\n"
            "Regular distribution updates.\n\n"
            f"{_format_update_items_html(report.stable)}"
            "</blockquote>",
            "<blockquote expandable>"
            "<b>⚠️ Not tested / not recommended</b>\n"
            "Proposed, backports, testing, unstable, experimental, or devel channels.\n\n"
            f"{_format_update_items_html(report.not_recommended)}"
            "</blockquote>",
        ]
    )


def _format_update_rich_html(report: AptUpdateReport) -> str:
    if report.total == 0:
        return "<h3>Update check</h3><p>No upgradable packages found.</p>"

    return "\n".join(
        [
            "<h3>📦 Update check</h3>",
            "<ul>",
            f"<li><b>Total:</b> <code>{report.total}</code> package(s)</li>",
            f"<li>🚨 <b>Critical/security:</b> <code>{len(report.critical)}</code></li>",
            f"<li>✅ <b>Stable:</b> <code>{len(report.stable)}</code></li>",
            "<li>⚠️ <b>Not tested / not recommended:</b> "
            f"<code>{len(report.not_recommended)}</code></li>",
            "</ul>",
            f"<p><b>Recommendation:</b> {html.escape(_recommend_update_action(report))}</p>",
            "<details open>",
            "<summary>🚨 Critical / security</summary>",
            "<p>Security repository updates.</p>",
            f"<p>{_format_update_items_html(report.critical).replace(chr(10), '<br>')}</p>",
            "</details>",
            "<details>",
            "<summary>✅ Stable</summary>",
            "<p>Regular distribution updates.</p>",
            f"<p>{_format_update_items_html(report.stable).replace(chr(10), '<br>')}</p>",
            "</details>",
            "<details>",
            "<summary>⚠️ Not tested / not recommended</summary>",
            "<p>Proposed, backports, testing, unstable, experimental, or devel channels.</p>",
            f"<p>{_format_update_items_html(report.not_recommended).replace(chr(10), '<br>')}</p>",
            "</details>",
        ]
    )


def _recommend_update_action(report: AptUpdateReport) -> str:
    if report.critical:
        return "Install security updates soon."
    if report.stable and not report.not_recommended:
        return "Regular update looks safe."
    if report.not_recommended:
        return "Review not recommended packages before upgrading."
    return "Review only."


def _format_update_report(raw: str) -> str:
    return _format_update_fallback_html(_group_apt_updates(raw))


async def reply_update_report(update: Update, raw: str) -> None:
    report = _group_apt_updates(raw)
    rich_sent = await send_rich_message(update, _format_update_rich_html(report))
    if not rich_sent:
        await reply_html(update, _format_update_fallback_html(report))


# ---------------------------------------------------------------- commands


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply_html(
            update,
            "🛠 <b>Ops Agent</b> — ready\n\n"
            "/help — show this command list\n\n"
            "<b>📊 Server info</b>\n"
            "/health — CPU, RAM, disk summary\n"
            "/disk — disk usage details\n"
            "/memory — memory details\n"
            "/uptime — uptime and load\n\n"
            "<b>🔧 Services</b>\n"
            "/services — status of managed services\n"
            "/logs [service] — recent logs\n"
            "/errors [service] — recent errors, all services by default\n"
            "/restart &lt;service&gt; — restart a service\n\n"
            "<b>⚙️ System</b>\n"
            "/reboot — reboot the whole system\n\n"
            "<b>📦 Updates</b>\n"
            "/update — check available updates\n"
            "/upgrade — install updates\n\n"
            "<b>🤖 Agent</b>\n"
            "/version — running bot version, branch, and commit\n"
            "/core — shared core version\n"
            "/my_agents — installed AI agents, versions, models, and latest status\n"
            "/ai_tools — installed AI tools, versions, models, and update status\n"
            "/ai_tools update &lt;codex|claude|all&gt; — update AI tools\n"
            "/ai_update [agent] — update all AI agents or one by name",
        )


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        load_raw, memory_raw, disk_raw = await asyncio.gather(
            arun(["cat", "/proc/loadavg"]),
            arun(["free", "-b"], env=_C_LOCALE_ENV),
            arun(["df", "-hP", "/"], env=_C_LOCALE_ENV),
        )
        parts = load_raw.split()
        load_str = " · ".join(parts[:3]) if len(parts) >= 3 else load_raw
        lines = [
            "❤️ <b>Health</b>",
            "",
            f"<b>Load</b>  {html.escape(load_str)}   <i>1m·5m·15m</i>",
        ]
        mem = _free_usage(memory_raw)
        if mem:
            used, total = mem
            pct = used / total * 100 if total else 0
            lines.append(
                f"<b>RAM </b> <code>{_usage_bar(pct)}</code> {pct:.0f}%   "
                f"{_fmt_bytes(used)} / {_fmt_bytes(total)}"
            )
        disk = _disk_usage(disk_raw)
        if disk:
            used, size, pct = disk
            lines.append(
                f"<b>Disk</b> <code>{_usage_bar(pct)}</code> {pct:.0f}%   "
                f"{html.escape(used)} / {html.escape(size)}"
            )
        if not mem or not disk:
            # Parsing failed for at least one metric — fall back to raw tables.
            lines += [
                "",
                f"<pre>{html.escape(memory_raw)}</pre>",
                f"<pre>{html.escape(disk_raw)}</pre>",
            ]
        await reply_html(update, "\n".join(lines))


async def disk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        raw = await arun(["df", "-hP"], env=_C_LOCALE_ENV)
        rows = _disk_rows(raw)
        if not rows:
            # Parsing failed / no block devices — fall back to the raw table.
            await reply_code(update, "💾 Disk usage", _filter_disk(raw))
            return
        lines = ["💾 <b>Disk usage</b>", ""]
        for mount, used, size, pct in rows:
            lines.append(
                f"<b>{html.escape(mount)}</b> <code>{_usage_bar(pct)}</code> "
                f"{pct:.0f}%   {html.escape(used)} / {html.escape(size)}"
            )
        await reply_html(update, "\n".join(lines))


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        raw = await arun(["free", "-b"], env=_C_LOCALE_ENV)
        mem = _free_usage(raw, "Mem:")
        if not mem:
            await reply_code(update, "🧠 Memory", raw)
            return
        used, total = mem
        pct = used / total * 100 if total else 0
        lines = [
            "🧠 <b>Memory</b>",
            "",
            f"<b>RAM </b> <code>{_usage_bar(pct)}</code> {pct:.0f}%   "
            f"{_fmt_bytes(used)} / {_fmt_bytes(total)}",
        ]
        swap = _free_usage(raw, "Swap:")
        if swap and swap[1] > 0:
            sused, stotal = swap
            spct = sused / stotal * 100
            lines.append(
                f"<b>Swap</b> <code>{_usage_bar(spct)}</code> {spct:.0f}%   "
                f"{_fmt_bytes(sused)} / {_fmt_bytes(stotal)}"
            )
        elif swap:
            lines.append("<b>Swap</b> <i>none configured</i>")
        await reply_html(update, "\n".join(lines))


async def uptime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        pretty, load_raw = await asyncio.gather(
            arun(["uptime", "-p"]),
            arun(["cat", "/proc/loadavg"]),
        )
        parts = load_raw.split()
        load_str = " · ".join(parts[:3]) if len(parts) >= 3 else load_raw
        text = (
            "⏱ <b>Uptime</b>\n\n"
            f"{html.escape(pretty)}\n"
            f"<b>load:</b> {html.escape(load_str)}   <i>1m·5m·15m</i>"
        )
        await reply_html(update, text)


async def services(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        results = await asyncio.gather(
            *(arun(["systemctl", "is-active", name]) for name in config.MANAGED_SERVICES)
        )
        lines = ["🔧 <b>Services</b>", ""]
        for name, raw in zip(config.MANAGED_SERVICES, results):
            state = _strip_exit_prefix(raw).strip()
            icon = "✅" if state == "active" else "❌"
            lines.append(f"{icon} <code>{html.escape(name)}</code> — {html.escape(state)}")
        await reply_html(update, "\n".join(lines))


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        service = resolve_service(context.args)
        if service is not None:
            output = await arun(
                ["journalctl", "-u", service, "-n", "30", "--no-pager"]
            )
            await reply_expandable(
                update, f"📋 Logs — <code>{html.escape(service)}</code>", output
            )
        else:
            await reply(
                update,
                "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
            )


async def errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        if context.args:
            service = resolve_service(context.args)
            if service is None:
                await reply(
                    update,
                    "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
                )
                return
            services = [service]
            header = f"🚨 Errors — <code>{html.escape(service)}</code>"
        else:
            services = config.MANAGED_SERVICES
            header = "🚨 Errors — all managed services"

        output = await arun(
            [
                "journalctl",
                *_journalctl_unit_args(services),
                "-n",
                "300",
                "--no-pager",
                "-p",
                "err",
            ]
        )
        await reply_expandable(update, header, output)


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        if not context.args:
            await reply(update, "Usage: /restart <service>")
            return
        service = resolve_service(context.args)
        if service is None:
            await reply(
                update,
                "Unknown service. Allowed: " + ", ".join(config.MANAGED_SERVICES),
            )
            return
        svc = html.escape(service)
        await update.message.reply_text(f"♻️ Restarting <code>{svc}</code>...", parse_mode="HTML")
        result = await arun(["systemctl", "restart", service])
        if result.startswith("[exit") or result.startswith("Error:"):
            await reply_expandable(update, f"⚠️ Restart failed — <code>{svc}</code>", result)
            return
        # Poll up to 10 s for a stable state
        state = "activating"
        for _ in range(10):
            await asyncio.sleep(1)
            state = _strip_exit_prefix(
                await arun(["systemctl", "is-active", service])
            ).strip()
            if state not in ("activating", "deactivating"):
                break
        icon = "✅" if state == "active" else "❌"
        await update.message.reply_text(
            f"{icon} <code>{svc}</code> is now: <b>{html.escape(state)}</b>",
            parse_mode="HTML",
        )


async def reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "🔁 Rebooting the whole system now...")
        result = await arun(["systemctl", "reboot"], timeout=15)
        if result.startswith("[exit") or result.startswith("Error:"):
            await reply_expandable(update, "⚠️ Reboot failed", result)


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "🔍 Checking for updates...")
        update_result = await arun(["apt-get", "update", "-q"], timeout=300)
        if update_result.startswith("[exit") or update_result.startswith("Error:"):
            await reply_expandable(update, "⚠️ apt-get update failed", update_result)
            return
        output = await arun(["apt", "list", "--upgradable"], timeout=120)
        if output.startswith("[exit") or output.startswith("Error:"):
            await reply_expandable(update, "⚠️ apt list failed", output)
            return
        await reply_update_report(update, output)


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        await reply(update, "⬆️ Upgrading packages, this may take a while...")
        output = await arun(["apt-get", "upgrade", "-y", "-q"], timeout=900)
        failed = output.startswith("[exit") or output.startswith("Error:")
        if failed:
            # Show the beginning (where errors appear) + tail
            summary = (
                output[:1500] + "\n...\n" + output[-500:]
                if len(output) > 2000
                else output
            )
            await reply_expandable(update, "❌ Upgrade failed", summary)
        else:
            tail = output[-2000:] if len(output) > 2000 else output
            await reply_expandable(update, "✅ Upgrade complete", tail)


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        text = await asyncio.to_thread(get_runtime_version)
        await reply(update, f"{text}\n{_CORE_COMMAND.short_line()}")


async def core(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this bot's pinned shared-core version."""
    if is_authorized(update):
        text = await asyncio.to_thread(_CORE_COMMAND.status_text)
        await reply(update, text)


async def my_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        agents = await asyncio.gather(
            *(_inspect_ai_agent(agent) for agent in config.AI_AGENT_INSTALLS)
        )
        lines = ["🤖 <b>AI agents</b>", ""]
        for idx, agent in enumerate(agents):
            if idx:
                lines.append("")
            lines.extend(_format_ai_agent_info(agent))
        await reply_html(update, "\n".join(lines))


async def ai_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        if context.args and context.args[0].lower() == "update":
            systems = resolve_ai_systems(context.args[1:])
            if systems is None:
                await reply(
                    update,
                    "Unknown AI tool. Allowed: " + _ai_system_names(),
                )
                return

            target = "all AI tools" if len(systems) > 1 else systems[0]["name"]
            await reply(update, f"⬇️ Updating {target}...")
            results = []
            for system in systems:
                results.append(await _update_ai_system(system))
            lines = ["🤖 <b>AI tool update</b>", ""]
            for idx, result in enumerate(results):
                if idx:
                    lines.append("")
                lines.extend(_format_ai_system_update_result(result))
            await reply_html(update, "\n".join(lines))
            return

        systems = await asyncio.gather(
            *(_inspect_ai_system(system) for system in config.AI_SYSTEM_INSTALLS)
        )
        lines = ["🤖 <b>AI tools</b>", ""]
        for idx, system in enumerate(systems):
            if idx:
                lines.append("")
            lines.extend(_format_ai_system_info(system))
        lines += [
            "",
            "Use <code>/ai_tools update codex</code>, "
            "<code>/ai_tools update claude</code>, or "
            "<code>/ai_tools update all</code>.",
        ]
        await reply_html(update, "\n".join(lines))


async def ai_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_authorized(update):
        agents = resolve_ai_agents(context.args)
        if agents is None:
            await reply(
                update,
                "Unknown AI agent. Allowed: " + _ai_agent_names(),
            )
            return

        target = "all AI agents" if not context.args else agents[0]["name"]
        await reply(update, f"⬇️ Updating {target}...")
        results = []
        restart_self = False
        for agent in agents:
            result = await _update_ai_agent(agent, defer_self_restart=True)
            results.append(result)
            if (
                agent["name"] == "ai-ops-agent"
                and result["restart"] == "deferred until after report"
            ):
                restart_self = True

        lines = ["🤖 <b>AI update</b>", ""]
        for idx, result in enumerate(results):
            if idx:
                lines.append("")
            lines.extend(_format_ai_update_result(result))
        if restart_self:
            lines += [
                "",
                "<i>ai-ops-agent changed; restarting this bot after this report.</i>",
            ]
        await reply_html(update, "\n".join(lines))

        if restart_self:
            await _restart_ai_agent_service(
                _self_ai_agent_service(),
                no_block=True,
            )


async def register_bot_commands(application: Application) -> None:
    """Publish commands so Telegram clients show suggestions after typing `/`."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Registered %d Telegram command hints", len(BOT_COMMANDS))


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.OPS_TELEGRAM_BOT_TOKEN)
        .post_init(register_bot_commands)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("disk", disk))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("uptime", uptime))
    app.add_handler(CommandHandler("services", services))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("errors", errors))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("reboot", reboot))
    app.add_handler(CommandHandler("update", update_cmd))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(CommandHandler("core", core))
    app.add_handler(CommandHandler("my_agents", my_agents))
    app.add_handler(CommandHandler("ai_tools", ai_tools))
    app.add_handler(CommandHandler("ai_update", ai_update))
    return app
