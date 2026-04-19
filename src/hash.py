"""Chunked MD5 computation for local files.

Computes md5 in 8KB chunks. Returns None for files exceeding
hash_max_file_size_mb or on read errors.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_MAX_FILE_SIZE_MB = 100
_CHUNK_SIZE = 8192  # 8KB


def compute_md5(
    path: str | Path,
    max_bytes: int | None = None,
) -> str | None:
    """Return hex md5 digest of *path*, or None on skip/error.

    Parameters
    ----------
    path:
        Absolute path to the file.
    max_bytes:
        If the file size exceeds this, return None immediately.
        Defaults to ``DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024``.
    """
    if max_bytes is None:
        max_bytes = DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024

    path = Path(path)

    try:
        file_size = path.stat().st_size
    except OSError:
        return None

    if file_size > max_bytes:
        return None

    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None

    return h.hexdigest()
