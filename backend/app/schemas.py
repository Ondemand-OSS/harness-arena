"""Pydantic request/response models for the API layer."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id_aa: str
    title: str
    category: str
    prompt: str
    system_prompt: str
    rubric: str
    expected_deliverables: str
    reference_files: str
    dataset_version: str
    # Derived (see taxonomy.py) — not stored columns.
    group: str = ""
    deliverable_files: list[str] = []
    deliverable_types: list[str] = []
    # Whether any AI judge verdict exists for this task at all. Only a
    # yes/no here — the scores themselves stay behind the reveal gate in
    # compare(), so listing tasks can't leak them.
    has_judge_verdict: bool = False
    submitted_by: str | None = None
    submitted_by_avatar: str = ""
    is_deleted: bool = False
    results_deleted: bool = False
    # When this id_aa most recently appeared in an import (fresh or a
    # re-import) — lets the UI sort/badge "just uploaded" tasks.
    imported_at: dt.datetime | None = None


class ReferenceFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    filename: str
    media_type: str
    size_bytes: int
    uploaded_at: dt.datetime | None = None


class CategoryReviewApproveIn(BaseModel):
    group: str


class StatsOut(BaseModel):
    """Counts behind the "N tasks · N harnesses · N recorded runs" strips."""

    tasks: int
    harnesses: int
    models: int
    recorded_runs: int
    judged_tasks: int
    categories: int


class ProviderConfigIn(BaseModel):
    name: str = ""
    model: str
    base_url: str
    api_key: str = ""  # blank on update means "keep the stored key"
    is_shared: bool = False
    is_free: bool = False
    reasoning_effort: str = ""
    # Which model family this free profile belongs to (deepseek / kimi /
    # glm / minimax / qwen) — required for free profiles; see
    # routers/config.py's FREE_MODEL_FAMILIES. Lets the picker group
    # multiple exact model names under one family.
    family: str = ""
    # The admin-preset OnDemand model this free profile maps to, so a
    # battle that includes OnDemand alongside other harnesses resolves it
    # automatically instead of the caller picking one by hand — see
    # routers/runs.py's require_ondemand_selection.
    ondemand_model_id: int | None = None
    # Admin on/off switch for a free profile — a disabled one still exists
    # (so past runs still resolve it, and the admin can flip it back on)
    # but is hidden from a non-admin's model picker and can't be newly
    # selected for a battle. See routers/config.py's toggle endpoint.
    enabled: bool = True

    # A trailing/leading space here is invisible in the admin form but not
    # to an HTTP client: harnesses/opencode_cli.py builds the request URL as
    # f"{base_url}/chat/completions", so `"https://openrouter.ai/api/v1 "`
    # (note the space) becomes ".../v1 /chat/completions" — a real 404 to
    # OpenRouter, surfaced as an opaque HTML/RSC error blob rather than
    # anything naming the actual problem. Stripped at the boundary so this
    # can't be saved again, not just worked around downstream.
    @field_validator("base_url", "model", mode="after")
    @classmethod
    def _strip_whitespace(cls, value: str) -> str:
        return value.strip()


class ProviderConfigOut(BaseModel):
    id: int
    name: str
    model: str
    base_url: str
    # api_key deliberately omitted from responses — never echo secrets back.
    has_api_key: bool
    is_shared: bool = False
    is_free: bool = False
    is_owned_by_me: bool = False
    updated_at: dt.datetime | None = None
    reasoning_effort: str = ""
    family: str = ""
    ondemand_model_id: int | None = None
    enabled: bool = True


class DeliverableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    media_type: str
    size_bytes: int
    # Path relative to the run's workspace root ("src/App.jsx"), where
    # `filename` is only the basename. A web project is a TREE, so without
    # this the UI cannot tell `src/App.jsx` from `public/App.jsx` and has
    # to render one flat list. Empty for older rows and for the ordinary
    # single-file deliverables that predate nested output.
    relpath: str = ""


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # Shared by every run created in the same battle trigger (one per
    # harness) — see runner.run_task. None for runs created before this
    # existed. This is "the battle" as a single thing to reference; `id`
    # above stays this one harness's own individual run.
    # UUID for new battles; the integer form remains accepted for records
    # stored before round ids changed from sequential counters.
    round_id: str | int | None = None
    task_id: str
    harness_key: str
    provider_config_id: int | None = None
    model: str = ""
    status: str
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    error_message: str
    deliverables_done: int = 0
    deliverables_expected: int = 0
    deliverables: list[DeliverableOut] = []
    # Display name of whoever triggered this run (None for runs predating
    # submitter tracking, and for the imported seed results).
    submitted_by: str | None = None
    # True only for a failed run the *current viewer* is allowed to retry —
    # its submitter, or the admin. See routers/runs.py::_may_retry.
    can_retry: bool = False
    can_stop: bool = False
    ondemand_session_id: str | None = None
    ondemand_session_ids: list[str] | None = None
    # True while this same run record is executing after a failed attempt.
    # Kept separate from status so active runs continue to use the normal
    # pending/running lifecycle everywhere else.
    is_retrying: bool = False
    # This viewer's own submitted score for this run's (harness, profile)
    # pairing, and the community aggregate across every user who's scored
    # it — same fields CompareEntry carries for the resolved comparison,
    # populated here too so a SUPERSEDED round (Battle Log's standalone
    # past-round cards) can show real judged status instead of a fixed
    # "Completed" label pretending nothing happened to it. Only populated
    # by POST /api/runs/overview's bulk path, which already has every
    # score for the task in memory; other endpoints leave these at their
    # defaults rather than adding a per-run query nothing else needs.
    already_scored: float | None = None
    community_avg_score: float | None = None
    community_vote_count: int = 0
    # A truncated tail of the adapter's stdout — admin-only (None for
    # everyone else), so the admin can see what a harness was actually
    # doing without needing host/DB access. See runner.py's RAW_LOG_MAX_CHARS.
    raw_log: str | None = None


class RunRequest(BaseModel):
    task_id: str
    harness_keys: list[str] | None = None  # default: all enabled harnesses
    provider_config_id: int | None = None
    # Retained for API compatibility. Every selected harness now starts a
    # fresh run; see runner.run_task.
    force: bool = False
    # Deprecated — retained only for API compatibility with older clients.
    # The OnDemand model to run is no longer picked by hand; it's resolved
    # server-side from the shared free profile's admin-set mapping (see
    # routers/config.py's ondemand_model_id, routers/runs.py's
    # require_ondemand_selection). Any value sent here is ignored.
    ondemand_model_id: int | None = None


class RunTriggerOut(BaseModel):
    runs: list[RunOut]
    # Historical result reuse is disabled, so this is currently always empty.
    reused_same_model: list[str] = []


class ScoreIn(BaseModel):
    task_id: str
    provider_config_id: int | None = None
    # When a Battle Log card opens a historical/current comparison, these
    # preserve the exact run set shown on that card instead of silently
    # substituting a different latest run for the same task/model.
    run_ids: list[int] | None = None
    # keyed by run_id (as string, since JSON object keys are strings) rather
    # than harness_key: the judge only ever sees anonymized run ids
    # ("Response A/B/C" -> run_id) until after submitting.
    scores: dict[str, dict[str, float]]


class JudgeCriterionOut(BaseModel):
    name: str
    weight: int
    earned: int
    max: int
    narrative: str


class CompareEntry(BaseModel):
    label: str  # "Output A", "Output B", ...
    run_id: int
    deliverables: list[DeliverableOut]
    # Splits `deliverables` for judging a web-project run (see
    # webproject.partition_deliverables): ids inside the deployed frontend
    # root collapse into one score tied to the live Preview tab instead of
    # one row per source file; ids in `extra_deliverable_ids` are outside
    # that root but still named in the task's own expected deliverables,
    # so they keep the ordinary one-score-per-file treatment. Both empty
    # for a non-web task — the UI's existing "score every deliverable"
    # behavior is exactly what an empty `website_deliverable_ids` means.
    website_deliverable_ids: list[int] = []
    extra_deliverable_ids: list[int] = []
    # This is common to every output in a battle, so it does not reveal which
    # anonymous output came from which harness before judging.
    model: str = ""
    # populated only once the current user has fully judged this task — never
    # sent while that person's own verdict is still pending, so seeing the AI
    # judge's opinion can't anchor it. See routers/scores.py.
    harness_key: str | None = None
    harness_name: str | None = None
    already_scored: float | None = None
    deliverable_scores: dict[str, float] = {}
    # Aggregate across EVERY signed-in user who has scored this harness for
    # this task+profile so far, not just the caller's own verdict — see
    # `include_community_stats` on GET /compare. Unlike the fields above,
    # deliberately NOT gated on `revealed`: showing "3 people rated this
    # 8.2" never anchors anyone's blind judgment on its own, since it says
    # nothing about which anonymized output it belongs to unless the
    # caller already knows (Battle Log always does; the blind Eval page
    # never asks for this).
    community_avg_score: float | None = None
    community_vote_count: int = 0
    judge_score: float | None = None
    judge_note: str = ""  # e.g. "Not graded — invalid deliverables", when judge_score is None
    judge_breakdown: list[JudgeCriterionOut] = []


class CompareOut(BaseModel):
    task_id: str
    revealed: bool
    entries: list[CompareEntry]


class RunsOverviewIn(BaseModel):
    task_ids: list[str]


class TaskOverviewOut(BaseModel):
    task_id: str
    # Same shape GET /api/runs/by-task/{id} and its /history sibling
    # return, bulk-fetched for every requested task in one request — see
    # POST /api/runs/overview.
    runs: list[RunOut]
    history: list[RunOut]
    compare: CompareOut


class ScoreOut(BaseModel):
    task_id: str
    harness_key: str
    provider_config_id: int | None = None
    value: float
    judged_at: dt.datetime


class HarnessInfo(BaseModel):
    key: str
    name: str
    tagline: str
    enabled: bool
    is_custom: bool = False  # true for a bring-your-own-harness (webhook) entry, editable/removable from Setup


class CustomHarnessIn(BaseModel):
    # Slug only — this ends up as a path segment (/api/harnesses/custom/{key})
    # and a Run.harness_key value, so it can't contain "/" or other
    # URL-unsafe characters.
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,48}$")
    name: str
    tagline: str = ""
    webhook_url: str
    auth_header: str = "Authorization"
    auth_token: str = ""
    enabled: bool = True


class CustomHarnessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    tagline: str
    webhook_url: str
    auth_header: str
    has_auth_token: bool
    enabled: bool


class LeaderboardRow(BaseModel):
    harness_key: str
    elo: float
    battles: int
    wins: float
    losses: float
    ties: float
    win_rate: float
    # Rating right after each judgement this harness took part in, oldest
    # first — the real trajectory behind the leaderboard's trend sparkline
    # and movement indicator, computed fresh alongside `elo` in elo.py.
    history: list[float] = []
    # Positive means the harness climbed that many places since its previous
    # ranked judgement; negative means it fell. None means no prior rank.
    rank_movement: int | None = None
    # AI judge aggregate for this harness, computed from JudgeVerdict rows
    # (see judge_stats.py). None when nothing has been graded for it.
    judge_mean: float | None = None
    judge_graded: int = 0
    # Count of submitted output scores and median wall-clock time across
    # completed runs. These are observed values, not model estimates.
    votes: int = 0
    median_time_seconds: float | None = None


class ImportResult(BaseModel):
    imported: int
    dataset_version: str
    # Exactly the id_aas that didn't already exist before this import —
    # for badging genuinely new tasks (see dataset_import.import_xlsx;
    # dataset_version alone can't answer this since it's stamped fresh on
    # every touched row, new or not).
    new_task_ids: list[str] = []
    # Rows whose id_aa already existed and were therefore left untouched
    # (import is insert-only — see dataset_import.import_xlsx) — surfaced
    # so a re-import silently doing nothing for a task isn't mistaken for
    # that task having been updated.
    skipped_existing_ids: list[str] = []
