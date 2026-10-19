from __future__ import annotations

import os
import re

_SENSITIVE_NAME = re.compile(
    r"SECRET|TOKEN|PASSW|CREDENTIAL|PRIVATE|API_KEY|ACCESS_KEY|_KEY$|^MONGODB|DATABASE_URL|REDIS|"
    r"SENDGRID|VERCEL|^CONNECTOR_|^AWS_|^GOOGLE_APPLICATION",
    re.IGNORECASE,
)


def agent_env() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if not _SENSITIVE_NAME.search(name)}
