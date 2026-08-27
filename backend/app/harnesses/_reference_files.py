"""Write attached task reference files into a harness workspace."""
from __future__ import annotations

import os
from pathlib import Path


def reference_file_blobs(task) -> list[dict]:
    return list(getattr(task, "reference_file_blobs", None) or [])


def write_reference_files(workdir: str, task) -> list[str]:
    """Write attached files and return the filenames written successfully."""
    written = []
    for blob in reference_file_blobs(task):
        filename = os.path.basename(blob.get("filename") or "")
        content = blob.get("content")
        if not filename or not content:
            continue
        try:
            (Path(workdir) / filename).write_bytes(content)
        except OSError:
            continue
        written.append(filename)
    return written
