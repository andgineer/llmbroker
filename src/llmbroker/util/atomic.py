"""Replacing a file's contents without a window where it is half-written."""

import os
import tempfile
from pathlib import Path


def write_atomic(target: Path, text: str) -> None:
    """Write through a sibling temp file and rename, so a crash mid-write cannot
    truncate the config the user already has."""
    # NamedTemporaryFile creates at 0600 and os.replace carries that onto the target,
    # locking everyone but one user out of the config a sync just rewrote.
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
        tmp = Path(fh.name)
    try:
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
