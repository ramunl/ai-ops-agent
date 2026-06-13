from pathlib import Path

from .shell import run


ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"

_ERROR_PREFIXES = ("Error:", "Command timed out", "[exit", "(exit")


def get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


def get_git_branch() -> str:
    result = run(["git", "-C", str(ROOT_DIR), "rev-parse", "--abbrev-ref", "HEAD"])
    if any(result.startswith(p) for p in _ERROR_PREFIXES):
        return "unknown"
    return result.strip()


def get_git_commit() -> str:
    result = run(["git", "-C", str(ROOT_DIR), "rev-parse", "--short", "HEAD"])
    if any(result.startswith(p) for p in _ERROR_PREFIXES):
        return "unknown"
    return result.strip()


def get_runtime_version() -> str:
    return (
        f"ai_ops_agent v{get_version()}\n"
        f"branch: {get_git_branch()}\n"
        f"commit: {get_git_commit()}"
    )
