"""Fetching a user's OnDemand skills and staging selected ones into a run.

A "skill" here is OnDemand's user-defined skill store
(`GET /plugin/v1/skill`) — a packaged SKILL.md + helper files bundle a user
authored on app.on-demand.io, downloadable as a zip. This has nothing to do
with the `ondemand` harness adapter specifically: a skill a user picks gets
extracted into the workdir of WHICHEVER harness(es) they're running, same as
a reference file would be, using their own OnDemand API key purely as the
means to look the skill up and download its bundle.
"""
from __future__ import annotations

import logging
import zipfile
from io import BytesIO

import httpx

log = logging.getLogger(__name__)

SKILL_LIST_URL = "https://api.on-demand.io/plugin/v1/skill"


async def fetch_user_skills(api_key: str) -> list[dict]:
    """The signed-in user's own user_defined OnDemand skills, newest first.

    Raises httpx.HTTPError / ValueError on failure — callers decide how to
    surface that (the router turns it into an HTTP error; the runner-side
    extraction step treats it as best-effort, see
    `download_and_extract_skills`)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            SKILL_LIST_URL,
            params={"sortBy": "createdAt", "sortOrder": "desc", "type": "user_defined"},
            headers={"apikey": api_key},
        )
        resp.raise_for_status()
        data = resp.json()
    skills = data.get("data") if isinstance(data, dict) else None
    return skills if isinstance(skills, list) else []


def subscribed_skills(skills: list[dict]) -> list[dict]:
    """Only skills the user has actually subscribed to — an unsubscribed
    one may be listed (e.g. someone else's public skill) but isn't theirs
    to run."""
    return [s for s in skills if isinstance(s, dict) and s.get("isSubscribed")]


async def resolve_skill_names(api_key: str, skill_ids: list[str]) -> list[str]:
    """Selected skill ids -> their names, for the `ondemand` harness itself.

    OnDemand's own chat query API (`POST chat/v1/sessions/{id}/query`) takes
    a `skillNames` field (by name, not id — undocumented, found in the
    on-demand-chat source) and handles fetching/injecting the skill's
    SKILL.md + bundle into its own Goose-backed execution itself. That's a
    better path than the workdir zip extraction for OnDemand specifically
    (see harnesses/ondemand.py), which is why only THIS harness needs names
    rather than the extraction `download_and_extract_skills` does for every
    other harness. Best-effort like the rest of this module: an unresolved
    id is just dropped rather than failing the run."""
    if not skill_ids or not api_key:
        return []
    try:
        skills = subscribed_skills(await fetch_user_skills(api_key))
    except (httpx.HTTPError, ValueError):
        log.warning("could not fetch OnDemand skills to resolve skillNames", exc_info=True)
        return []
    wanted = set(skill_ids)
    return [skill["name"] for skill in skills if skill.get("id") in wanted and skill.get("name")]


async def download_and_extract_skills(workdir: str, api_key: str, skill_ids: list[str]) -> list[str]:
    """Downloads each selected, still-subscribed skill's bundle zip and
    extracts it into `workdir/skills/<skill-name>/`.

    Best-effort throughout, matching harnesses/ondemand.py's own posture on
    optional extras (reference files, plugin suggestions): a user's skill
    selection going stale (unsubscribed since, bundle removed) or one zip
    failing to download must never fail the run itself — it should just run
    without that skill. Returns the names of skills actually extracted."""
    if not skill_ids or not api_key:
        return []
    try:
        skills = subscribed_skills(await fetch_user_skills(api_key))
    except (httpx.HTTPError, ValueError):
        log.warning("could not fetch OnDemand skills to stage into run", exc_info=True)
        return []
    wanted = set(skill_ids)
    extracted: list[str] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for skill in skills:
            if skill.get("id") not in wanted:
                continue
            bundle = skill.get("bundle") or {}
            url = bundle.get("url")
            name = skill.get("name") or skill["id"]
            if not url:
                continue
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                with zipfile.ZipFile(BytesIO(resp.content)) as zf:
                    zf.extractall(f"{workdir}/skills/{name}")
            except (httpx.HTTPError, zipfile.BadZipFile, OSError):
                log.warning("could not stage OnDemand skill %r into workdir", name, exc_info=True)
                continue
            extracted.append(name)
    return extracted
