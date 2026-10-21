"""Safe shell command execution for ops tasks."""

import logging
import os
import signal
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120
TIMEOUT_CLEANUP_TIMEOUT = 1


def _cleanup_timeout(proc: subprocess.Popen[str]) -> None:
    """Kill ordinary descendants and reap without draining inherited pipes."""
    try:
        # The group may still have descendants after its leader has exited.
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        logger.exception("Failed to kill timed-out process group %s", proc.pid)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                logger.exception("Failed to close timed-out process pipe %s", proc.pid)
    try:
        proc.wait(timeout=TIMEOUT_CLEANUP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("Failed to reap timed-out process %s", proc.pid)


def run(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> str:
    """Run an argument list, preserving silent exit codes as '(exit N)'."""
    logger.info("Running command: %s timeout=%s", cmd, timeout)
    try:
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=process_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _cleanup_timeout(proc)
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
