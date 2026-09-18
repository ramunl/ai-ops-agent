"""Runtime version reporting delegated to the shared agent core."""

from pathlib import Path

from ai_agent_common import get_runtime_version as _shared_runtime_version

ROOT_DIR = Path(__file__).resolve().parent.parent


def get_runtime_version() -> str:
    """Return the standard version, branch, and commit text."""
    return _shared_runtime_version("ai-ops-agent", ROOT_DIR)
