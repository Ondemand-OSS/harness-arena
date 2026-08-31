"""Lists the signed-in user's own OnDemand skills, for picking which ones
to stage into a run's workdir (see ../ondemand_skills.py and runner.py)."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from pymongo.database import Database

from ..db import get_db
from ..ondemand_skills import fetch_user_skills, subscribed_skills
from ..users import require_user

router = APIRouter(prefix="/api/ondemand-skills", tags=["ondemand-skills"])


class OndemandSkillOut(BaseModel):
    id: str
    name: str
    description: str = ""
    logo_url: str = ""
    bundle_file_name: str = ""


def _out(skill: dict) -> OndemandSkillOut:
    bundle = skill.get("bundle") or {}
    return OndemandSkillOut(
        id=skill["id"],
        name=skill.get("name") or skill["id"],
        description=skill.get("description") or "",
        logo_url=skill.get("logoUrl") or "",
        bundle_file_name=bundle.get("fileName") or "",
    )


@router.get("", response_model=list[OndemandSkillOut])
async def list_ondemand_skills(db: Database = Depends(get_db), user: dict = Depends(require_user)):
    api_key = (user or {}).get("ondemand_api_key") or ""
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No OnDemand API key set. Add your OnDemand API key in Setup to use skills.",
        )
    try:
        skills = await fetch_user_skills(api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(status_code=400, detail="OnDemand rejected this API key as invalid. Check your OnDemand API key in Setup.") from exc
        raise HTTPException(status_code=502, detail="Could not fetch OnDemand skills right now.") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not fetch OnDemand skills right now.") from exc
    return [_out(s) for s in subscribed_skills(skills) if s.get("id")]
