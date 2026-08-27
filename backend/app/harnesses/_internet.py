"""Write the Internet Agent API schema into a harness workspace."""
from __future__ import annotations

import shutil
from pathlib import Path

SCHEMA_FILENAME = "internet_plugin_schema.yaml"
_SCHEMA_SOURCE = Path(__file__).with_name(SCHEMA_FILENAME)

INTERNET_ACCESS_NOTE = (
    "Internet access is enabled. The current working directory contains "
    f"{SCHEMA_FILENAME}, which documents the available Internet Agent API "
    "(a POST endpoint that takes a natural-language search query and returns "
    "search results with page excerpts). Use it for current web research when "
    "helpful — follow its documented request/response shape rather than "
    "inventing a different tool protocol. Do not submit, modify, or reference "
    f"the {SCHEMA_FILENAME} file itself as a deliverable."
)


def write_schema(workdir: str) -> bool:
    """Copies the bundled schema into the run's workdir. Never raises —
    a missing/moved source file just means this run proceeds without the
    note being true, not a failed battle over a documentation file."""
    try:
        shutil.copy2(_SCHEMA_SOURCE, Path(workdir) / SCHEMA_FILENAME)
        return True
    except OSError:
        return False
