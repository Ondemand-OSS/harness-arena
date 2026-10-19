from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import hmac
import html
import re
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from ..db import next_id
from ..users import hash_password, verify_password
from . import settings
from .security import issue_tokens, new_token
from .store import hash_secret, utcnow

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_HASH = hash_password("connector-dummy-password")


@dataclasses.dataclass
class AuthorizeRequest:
    client_id: str
    redirect_uri: str
    scope: str
    state: str
    code_challenge: str
    code_challenge_method: str

    def hidden(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": self.code_challenge_method,
        }


def resolve_request(db: Database, params: dict) -> tuple[AuthorizeRequest | None, str | None, str | None]:
    client = db.connector_clients.find_one({"_id": params.get("client_id", "")})
    if client is None:
        return None, "invalid_client", "Unknown client."
    redirect_uri = params.get("redirect_uri", "")
    if redirect_uri not in client["redirect_uris"]:
        return None, "invalid_request", "The redirect URI is not registered for this client."
    allowed = set(client["scopes"])
    requested = [item for item in (params.get("scope") or "").split() if item]
    scope = " ".join(requested) if requested else " ".join(client["scopes"])
    challenge = params.get("code_challenge", "") or ""
    method = params.get("code_challenge_method", "") or ("S256" if challenge else "")
    request = AuthorizeRequest(
        client_id=client["_id"],
        redirect_uri=redirect_uri,
        scope=scope,
        state=params.get("state", "") or "",
        code_challenge=challenge,
        code_challenge_method=method,
    )
    if params.get("response_type") != "code":
        return request, "unsupported_response_type", "Only the authorization code flow is supported."
    if not set(scope.split()) <= allowed:
        return request, "invalid_scope", "One or more requested scopes are not allowed."
    if challenge and method != "S256":
        return request, "invalid_request", "Only the S256 code challenge method is supported."
    return request, None, None


def redirect_with(redirect_uri: str, **params: str) -> str:
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(key, value) for key, value in params.items() if value != ""]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def create_account(db: Database, email: str, password: str) -> tuple[dict | None, str | None]:
    email = (email or "").strip()
    if not EMAIL_PATTERN.match(email) or len(email) > 254:
        return None, "Enter a valid email address."
    if len(password or "") < settings.MIN_PASSWORD_LENGTH:
        return None, f"Password must be at least {settings.MIN_PASSWORD_LENGTH} characters."
    user = {
        "_id": next_id(db, "connector_users"),
        "email": email,
        "email_lower": email.lower(),
        "password_hash": hash_password(password),
        "created_at": utcnow(),
        "failed_attempts": 0,
        "locked_until": None,
    }
    try:
        db.connector_users.insert_one(user)
    except DuplicateKeyError:
        return None, "An account with this email already exists. Sign in instead."
    return user, None


def authenticate(db: Database, email: str, password: str) -> tuple[dict | None, str | None]:
    generic = "Incorrect email or password."
    user = db.connector_users.find_one({"email_lower": (email or "").strip().lower()})
    now = utcnow()
    if user is None:
        verify_password(password or "", DUMMY_HASH)
        return None, generic
    locked_until = user.get("locked_until")
    if locked_until and locked_until > now:
        return None, "Too many failed attempts. Try again later."
    if not verify_password(password or "", user["password_hash"]):
        attempts = user.get("failed_attempts", 0) + 1
        update = {"failed_attempts": attempts}
        if attempts >= settings.LOGIN_MAX_ATTEMPTS:
            update = {"failed_attempts": 0, "locked_until": now + dt.timedelta(minutes=settings.LOGIN_LOCK_MINUTES)}
        db.connector_users.update_one({"_id": user["_id"]}, {"$set": update})
        return None, generic
    if user.get("failed_attempts") or user.get("locked_until"):
        db.connector_users.update_one({"_id": user["_id"]}, {"$set": {"failed_attempts": 0, "locked_until": None}})
    return user, None


def issue_code(db: Database, request: AuthorizeRequest, user_id: int) -> str:
    code = new_token()
    db.connector_codes.insert_one(
        {
            "_id": hash_secret(code),
            "user_id": user_id,
            "client_id": request.client_id,
            "redirect_uri": request.redirect_uri,
            "scope": request.scope,
            "code_challenge": request.code_challenge,
            "expires_at": utcnow() + dt.timedelta(seconds=settings.CODE_TTL_SECONDS),
        }
    )
    return code


def _pkce_matches(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def exchange_code(
    db: Database, client_id: str, code: str, redirect_uri: str, verifier: str
) -> tuple[dict | None, str | None]:
    record = db.connector_codes.find_one_and_delete({"_id": hash_secret(code or "")})
    if record is None or record["expires_at"] <= utcnow():
        return None, "invalid_grant"
    if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
        return None, "invalid_grant"
    if record["code_challenge"] and not _pkce_matches(verifier or "", record["code_challenge"]):
        return None, "invalid_grant"
    return issue_tokens(db, record["user_id"], client_id, record["scope"]), None


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#14171f;--muted:#5b6475;--line:#d9dde5;--accent:#2f5bea;--err:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#181c25;--fg:#eceff5;--muted:#9aa3b5;--line:#2a3040;--accent:#6c8cff;--err:#ff8a80}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:16px}}
main{{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}}
h1{{font-size:20px;margin:0 0 4px}}
p{{margin:0 0 20px;color:var(--muted);font-size:14px}}
label{{display:block;font-size:13px;margin:14px 0 6px}}
input[type=email],input[type=password]{{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg);font:inherit}}
button{{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}}
.error{{margin:0 0 14px;padding:10px 12px;border-radius:8px;border:1px solid var(--err);color:var(--err);font-size:14px}}
.switch{{margin:18px 0 0;text-align:center;font-size:14px}}
a{{color:var(--accent)}}
</style>
</head>
<body>
<main>
<h1>{heading}</h1>
<p>{subtitle}</p>
{error}
<form method="post" action="">
{hidden}
<input type="hidden" name="mode" value="{mode}">
<label for="email">Email</label>
<input id="email" name="email" type="email" value="{email}" autocomplete="email" required>
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="{autocomplete}" minlength="{minlength}" required>
<button type="submit">{button}</button>
</form>
<p class="switch">{switch}</p>
</main>
</body>
</html>"""


def render_form(request: AuthorizeRequest, mode: str, error: str = "", email: str = "") -> str:
    signup = mode == "signup"
    other = urlencode({**request.hidden(), "mode": "login" if signup else "signup"})
    switch = (
        f'Already have an account? <a href="?{html.escape(other)}">Sign in</a>'
        if signup
        else f'New here? <a href="?{html.escape(other)}">Create an account</a>'
    )
    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(value)}">'
        for key, value in request.hidden().items()
    )
    return PAGE.format(
        title="Connect your account",
        heading="Create your account" if signup else "Sign in to connect",
        subtitle="Create an account to connect your agents." if signup else "Sign in to authorize this connection.",
        error=f'<div class="error" role="alert">{html.escape(error)}</div>' if error else "",
        hidden=hidden,
        mode="signup" if signup else "login",
        email=html.escape(email),
        autocomplete="new-password" if signup else "current-password",
        minlength=settings.MIN_PASSWORD_LENGTH if signup else 1,
        button="Create account and connect" if signup else "Sign in and connect",
        switch=switch,
    )


def render_error(message: str) -> str:
    head, _, rest = PAGE.partition("<main>")
    body = f"<main>\n<h1>Cannot connect</h1>\n<p>{html.escape(message)}</p>\n</main>\n</body>\n</html>"
    return head.format(title="Cannot connect") + body
