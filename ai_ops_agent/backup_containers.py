"""Include configured Docker directories without storing plaintext snapshots."""

import io
import re
import subprocess
import tarfile


def is_container_path(value: str) -> bool:
    """Accept docker:container:/absolute/path entries from extra-paths."""
    return bool(re.fullmatch(r"docker:[a-zA-Z0-9][a-zA-Z0-9_.-]*:/[^\r\n]+", value))


def docker_output(args: list[str]) -> bytes:
    """Run a read-only Docker command, failing the backup if it fails."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, timeout=60, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("Container backup timed out") from error
    if result.returncode:
        raise OSError("Container backup failed; check Docker and configured paths")
    return result.stdout


def add_container(archive: tarfile.TarFile, source: str) -> None:
    """Add a nested directory tar and container metadata to the encrypted archive."""
    _, container, path = source.split(":", 2)
    stem = f"containers/{container}/{path.strip('/').replace('/', '_')}"
    contents = {
        f"{stem}.tar": docker_output(["cp", f"{container}:{path}", "-"]),
        f"containers/{container}/inspect.json": docker_output(["inspect", container]),
    }
    for name, data in contents.items():
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(data))
