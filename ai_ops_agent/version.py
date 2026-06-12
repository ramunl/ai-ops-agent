from pathlib import Path

from .shell import run


ROOT_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT_DIR / "VERSION"


def get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return "unknown"


def get_git_branch() -> str:
    try:
        return run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    except Exception:
        return "unknown"


def get_git_commit() -> str:
    try:
        return run(["git", "rev-parse", "--short", "HEAD"]).strip()
    except Exception:
        return "unknown"


def get_runtime_version() -> str:
    return (
        f"ai_ops_agent v{get_version()}\n"
        f"branch: {get_git_branch()}\n"
        f"commit: {get_git_commit()}"
    )
