"""Parses the rubric-breakdown text format produced by an external AI judge
grading run (see scripts/import_seed_results.py for where these files come
from and how they're wired into the app).

Format, blocks separated by a blank line:

    Rubric breakdown
    <criterion_name>
    ×<weight>        (optional — omitted means weight 1)
    <earned>/<max>
    <narrative text, one paragraph>

    <criterion_name>
    <earned>/<max>
    <narrative text>
    ...

Two degenerate cases (verified against the actual files this parser will
see, not hypothesized):
- The whole file is a short "Not Graded ..." sentence with no rubric at
  all — the source judge declined to score this run.
- The file is empty — no judgement was recorded for this run at all.

Both produce a ParsedVerdict with `score=None` and an explanatory `note`,
never a crash — a judge result the app can't fully parse should degrade to
"no verdict", not break the page that would show it.
"""
from __future__ import annotations

import dataclasses
import re

CRITERION_RE = re.compile(r"^×(\d+)$")
SCORE_RE = re.compile(r"^(\d+)/(\d+)$")


@dataclasses.dataclass
class Criterion:
    name: str
    weight: int
    earned: int
    max: int
    narrative: str


@dataclasses.dataclass
class ParsedVerdict:
    score: float | None  # 0-10, weighted-average of criteria scaled to 10
    note: str
    criteria: list[Criterion]


def parse_rubric_text(raw: str) -> ParsedVerdict:
    # Normalized regardless of source: files pulled straight from a zip via
    # `zipfile` keep whatever line endings they were stored with (verified
    # against the actual seed data — CRLF), while extracting the same zip
    # with the `unzip` CLI silently converts them. Block-splitting on a
    # literal "\n\n" would silently fail to find any blocks over CRLF text.
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ParsedVerdict(score=None, note="No judgement recorded for this run.", criteria=[])

    blocks = text.split("\n\n")
    if len(blocks) == 1 and "/" not in blocks[0]:
        # A short explanatory sentence rather than a rubric — e.g.
        # "Not Graded due to some invalid deliverables produced by harness".
        return ParsedVerdict(score=None, note=blocks[0], criteria=[])

    criteria: list[Criterion] = []
    for i, block in enumerate(blocks):
        lines = [ln for ln in block.split("\n") if ln != ""]
        if not lines:
            continue
        if i == 0 and lines[0] == "Rubric breakdown":
            lines = lines[1:]
        if not lines:
            continue

        name = lines[0]
        idx = 1
        weight = 1
        if idx < len(lines) and (m := CRITERION_RE.fullmatch(lines[idx])):
            weight = int(m.group(1))
            idx += 1
        if idx >= len(lines) or not (m := SCORE_RE.fullmatch(lines[idx])):
            continue  # not a criterion block we recognize — skip rather than fail the whole parse
        earned, max_ = int(m.group(1)), int(m.group(2))
        narrative = "\n".join(lines[idx + 1 :]).strip()
        criteria.append(Criterion(name=name, weight=weight, earned=earned, max=max_, narrative=narrative))

    if not criteria:
        return ParsedVerdict(score=None, note="Judge rubric could not be parsed.", criteria=[])

    total_earned = sum(c.weight * c.earned for c in criteria)
    total_max = sum(c.weight * c.max for c in criteria)
    score = round((total_earned / total_max) * 10, 1) if total_max else None
    return ParsedVerdict(score=score, note="", criteria=criteria)
