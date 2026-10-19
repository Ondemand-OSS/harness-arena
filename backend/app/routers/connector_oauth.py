from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pymongo.database import Database

from ..connector import oauth
from ..connector.security import revoke_token, rotate_refresh_token, verify_client
from ..db import get_db

router = APIRouter(prefix="/api/connector/oauth", tags=["connector-oauth"])

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
AUTHORIZE_FIELDS = ("client_id", "redirect_uri", "response_type", "scope", "state", "code_challenge", "code_challenge_method")


def _page(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers={**NO_STORE, "Referrer-Policy": "no-referrer"})


def _redirect(url: str) -> HTMLResponse:
    return _page(oauth.render_redirect(url))


def _oauth_error(error: str, description: str = "", status_code: int = 400) -> JSONResponse:
    headers = dict(NO_STORE)
    if status_code == 401:
        headers["WWW-Authenticate"] = 'Basic realm="connector"'
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status_code, headers=headers)


def _client_credentials(request: Request, form) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:].strip()).decode()
            client_id, _, secret = decoded.partition(":")
            return client_id, secret
        except (binascii.Error, UnicodeDecodeError):
            return "", ""
    return str(form.get("client_id", "")), str(form.get("client_secret", ""))


@router.get("/authorize")
def authorize_form(request: Request, db: Database = Depends(get_db)):
    params = dict(request.query_params)
    parsed, error, description = oauth.resolve_request(db, params)
    if parsed is None:
        return _page(oauth.render_error(description or "Invalid request."), 400)
    if error:
        return _redirect(
            oauth.redirect_with(parsed.redirect_uri, error=error, error_description=description or "", state=parsed.state)
        )
    mode = "signup" if params.get("mode") == "signup" else "login"
    return _page(oauth.render_form(parsed, mode))


@router.post("/authorize")
async def authorize_submit(request: Request, db: Database = Depends(get_db)):
    form = await request.form()
    params = {key: str(form.get(key, "")) for key in AUTHORIZE_FIELDS}
    parsed, error, description = oauth.resolve_request(db, params)
    if parsed is None:
        return _page(oauth.render_error(description or "Invalid request."), 400)
    if error:
        return _redirect(
            oauth.redirect_with(parsed.redirect_uri, error=error, error_description=description or "", state=parsed.state)
        )
    mode = "signup" if form.get("mode") == "signup" else "login"
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))
    if mode == "signup":
        user, message = oauth.create_account(db, email, password)
    else:
        user, message = oauth.authenticate(db, email, password)
    if user is None:
        return _page(oauth.render_form(parsed, mode, error=message or "", email=email), 400)
    code = oauth.issue_code(db, parsed, user["_id"])
    return _redirect(oauth.redirect_with(parsed.redirect_uri, code=code, state=parsed.state))


@router.post("/token")
async def token(request: Request, db: Database = Depends(get_db)):
    form = await request.form()
    client_id, client_secret = _client_credentials(request, form)
    client = verify_client(db, client_id, client_secret)
    if client is None:
        return _oauth_error("invalid_client", status_code=401)
    grant_type = str(form.get("grant_type", ""))
    if grant_type == "authorization_code":
        tokens, error = oauth.exchange_code(
            db,
            client["_id"],
            str(form.get("code", "")),
            str(form.get("redirect_uri", "")),
            str(form.get("code_verifier", "")),
        )
        if tokens is None:
            return _oauth_error(error or "invalid_grant")
        return JSONResponse(tokens, headers=NO_STORE)
    if grant_type == "refresh_token":
        tokens = rotate_refresh_token(db, str(form.get("refresh_token", "")), client["_id"])
        if tokens is None:
            return _oauth_error("invalid_grant")
        return JSONResponse(tokens, headers=NO_STORE)
    return _oauth_error("unsupported_grant_type")


@router.post("/revoke")
async def revoke(request: Request, db: Database = Depends(get_db)):
    form = await request.form()
    client_id, client_secret = _client_credentials(request, form)
    client = verify_client(db, client_id, client_secret)
    if client is None:
        return _oauth_error("invalid_client", status_code=401)
    value = str(form.get("token", ""))
    if value:
        revoke_token(db, value, client["_id"])
    return JSONResponse({}, headers=NO_STORE)
