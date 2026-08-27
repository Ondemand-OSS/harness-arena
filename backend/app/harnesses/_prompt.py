"""Build task prompts for harness adapters."""
from __future__ import annotations

from ..taxonomy import parse_deliverables
from ..webproject import is_web_project
from ._internet import INTERNET_ACCESS_NOTE

# Web-development tasks get deployed and previewed rather than read file
# by file (see webproject.py / sandbox_deploy.py), which imposes real
# constraints the generic deliverables instruction actively contradicts —
# most obviously that a web project NEEDS subdirectories (src/, public/)
# where an ordinary task is told not to create any.
WEB_PROJECT_NOTE = """\
This is a web-development task. The files you write will be installed and
served as a running website, so build a real, self-contained project:

- Write a complete project tree, using subdirectories where a real project
  would (`src/`, `public/`, ...). The deliverable names listed above are
  the files that must exist, not a restriction on creating others.
- Include a `package.json` with the dependencies you actually import and a
  `dev` script that starts the app. Do not assume any package is
  preinstalled.
- If you use Vite, set `server.host` to `0.0.0.0` and `server.allowedHosts`
  to `true` in `vite.config.js` — the app is served from a generated
  hostname, and anything narrower refuses the request.
- Any dev server must listen on `0.0.0.0`, never only on `localhost`.
- Only the FRONTEND is deployed. If the task needs a backend, still write
  it, but make sure the frontend renders something meaningful without it
  (mock/seed the data rather than leaving blank screens on a failed fetch).
- Prefer plain dependency-light code. Every extra dependency is one more
  thing that can fail to install and take the whole preview down with it.\
"""


def build_prompt(
    task,
    include_system_prompt: bool = True,
    internet_access: bool = False,
    attached_reference_filenames: list[str] | None = None,
    attached_reference_location: str = "the current working directory",
    deliverable_urls: bool = False,
    attached_reference_uploaded: bool = False,
    inlined_reference_files: list[tuple[str, str]] | None = None,
) -> str:
    parts = []

    system_prompt = (getattr(task, "system_prompt", "") or "").strip()
    if include_system_prompt and system_prompt:
        parts += ["## System instructions", system_prompt, ""]

    parts += [
        f"# Task: {task.title}",
        "",
        (task.prompt or "").strip(),
    ]

    if internet_access:
        parts += ["", INTERNET_ACCESS_NOTE]

    deliverables = parse_deliverables(getattr(task, "expected_deliverables", ""))
    web_project = is_web_project(getattr(task, "expected_deliverables", ""))
    if deliverables:
        if deliverable_urls:
            parts += [
                "",
                "## Required deliverables",
                "IMPORTANT: USE YOUR TERMINAL EXECUTION AGENT TO ACTUALLY CREATE EACH OF THE "
                "FOLLOWING REQUIRED DELIVERABLE FILES — DO NOT JUST WRITE OUT OR DESCRIBE THEIR CONTENT "
                "IN YOUR ANSWER INSTEAD OF CREATING THEM.",
                *(f"- {name}" for name in deliverables),
            ]
        elif web_project:
            # No "don't create subdirectories" here — see WEB_PROJECT_NOTE.
            parts += [
                "",
                "## Required deliverables",
                "Write at least these files, with these exact filenames, as part of the project "
                "in the current working directory:",
                *(f"- {name}" for name in deliverables),
            ]
        else:
            parts += [
                "",
                "## Required deliverables",
                "Write exactly these files, with these exact filenames, into the current working "
                "directory (do not create subdirectories for them):",
                *(f"- {name}" for name in deliverables),
            ]

    if web_project:
        parts += ["", "## Web project requirements", WEB_PROJECT_NOTE]

    attached = list(attached_reference_filenames or [])
    inlined = list(inlined_reference_files or [])
    reference_files = (getattr(task, "reference_files", "") or "").strip()
    if attached or inlined:
        parts += ["", "## Reference material for this task"]
        if attached:
            reference_intro = (
                "The following reference file(s) have already been uploaded to this chat session "
                "— read them before starting:"
                if attached_reference_uploaded
                else f"The following reference file(s) have already been placed in {attached_reference_location} "
                "— read them before starting:"
            )
            parts += [reference_intro, *(f"- {name}" for name in attached)]
        if inlined:
            if attached:
                parts += [""]
            parts += [
                "The following reference file(s) could not be uploaded, so their full content is "
                "included directly below instead — read it before starting:"
            ]
            for name, content in inlined:
                parts += ["", f"### {name}", content.strip()]
        if reference_files:
            parts += ["", reference_files]
    elif reference_files:
        parts += [
            "",
            "## Reference material mentioned by this task",
            reference_files,
        ]

    return "\n".join(parts).strip() + "\n"
