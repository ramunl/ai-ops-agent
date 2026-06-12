"""Safe shell command execution for ops tasks."""

import logging
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120


def run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command given as an argument list (never shell=True)."""
    logger.info("Running command: %s timeout=%s", cmd, timeout)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        isEmpty = not output
        if isEmpty:
            output = f"(no output, exit code {result.returncode})"
        return output
    except subprocess.TimeoutExpired:
        logger.error("Command timed out: %s", cmd)
        return f"Command timed out after {timeout}s"
