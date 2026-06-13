"""Safe shell command execution for ops tasks."""

import logging
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120


def run(cmd: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a command given as an argument list (never shell=True)."""
    logger.info("Running command: %s timeout=%s", cmd, timeout)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            logger.error("Command timed out: %s", cmd)
            return f"Command timed out after {timeout}s"
        output = (stdout + stderr).strip()
        if not output:
            return f"(exit {proc.returncode})"
        if proc.returncode != 0:
            return f"[exit {proc.returncode}] {output}"
        return output
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.error("Command failed: %s — %s", cmd, e)
        return f"Error: {e}"
