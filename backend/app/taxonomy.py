"""Coarse task grouping + deliverable parsing.

The dataset's `category` column is fine-grained ("Market commercialization",
"Geospatial site selection", …). The UI also wants a coarse top-level
grouping to navigate by — Code / Research / Analysis & Risk / Operations —
so this module maps categories onto groups.

Unmapped categories fall back to `DEFAULT_GROUP` rather than being dropped,
so a newly uploaded dataset with unfamiliar categories still renders (its
tasks just land under "Other" until a mapping is added here).
"""
from __future__ import annotations

import os

GROUPS = ["Code", "Research", "Analysis & Risk", "Operations"]
DEFAULT_GROUP = "Other"

CATEGORY_GROUPS = {
    "market commercialization": "Research",
    "community ecosystem analysis": "Research",
    "supply policy risk": "Analysis & Risk",
    "platform risk analysis": "Analysis & Risk",
    "geospatial site selection": "Operations",
}


def group_for_category(category: str) -> str:
    return CATEGORY_GROUPS.get((category or "").strip().lower(), DEFAULT_GROUP)


def category_key(category: str) -> str:
    """Stable, case-insensitive key for an uploaded category label."""
    return (category or "").strip().casefold()


def is_builtin_category(category: str) -> bool:
    return category_key(category) in CATEGORY_GROUPS


def group_for_category_with_approvals(category: str, approved_groups: dict[str, str] | None = None) -> str:
    """Resolve a built-in or admin-approved custom category to its lane."""
    key = category_key(category)
    return (approved_groups or {}).get(key) or CATEGORY_GROUPS.get(key, DEFAULT_GROUP)


def parse_deliverables(expected: str) -> list[str]:
    """The dataset stores expected deliverables as a comma-separated list of
    filenames ("a.xlsx, b.docx, c.pdf"). Split it into a real list so the UI
    can show exact file chips and counts."""
    if not expected:
        return []
    return [part.strip() for part in expected.split(",") if part.strip()]


# What a dataset row writes in `reference_files` to mean "this task needs no
# reference material" — the bundled template tells users to write "na" for
# exactly this case (see routers/tasks.py's template instructions). Treated
# as equivalent to blank, not as a literal filename to require.
_NO_REFERENCE_TOKENS = {"na", "n/a", "none", "-"}


def parse_reference_filenames(reference_files: str) -> list[str]:
    """`reference_files` is the same comma-separated-filenames shape as
    `expected_deliverables` ("a.md, b.md") — split it the same way, so a
    task naming more than one reference file can have each one matched
    against attached bytes (see routers/tasks.py's reference-files
    endpoints and scripts/import_reference_files.py). Any part that's just
    a "no reference material" token (see _NO_REFERENCE_TOKENS) is dropped
    rather than treated as a filename to require."""
    return [name for name in parse_deliverables(reference_files) if name.strip().lower() not in _NO_REFERENCE_TOKENS]


def deliverable_types(expected: str) -> list[str]:
    """Distinct uppercase file extensions, in first-seen order — the
    "XLSX DOCX PDF HTML TXT" chips on a task card."""
    seen: list[str] = []
    for name in parse_deliverables(expected):
        ext = os.path.splitext(name)[1].lstrip(".").upper()
        if ext and ext not in seen:
            seen.append(ext)
    return seen
