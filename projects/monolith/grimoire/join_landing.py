"""The single-use invitation landing and its signed-in acceptance endpoints.

``/grimoire/join`` is the one anonymous Grimoire route. The page it serves is
self-contained: no bundle, fonts or telemetry. Its script moves the invitation
secret out of the URL fragment into an HttpOnly cookie scoped to this path, and
from then on the browser only ever sees allowlisted metadata.

Nothing a caller sends and nothing a failure carries is reflected. Every
message here is a fixed string owned by this module or by ``SAFE_ERRORS``.
"""

from __future__ import annotations

import base64
import contextlib
import html
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypeVar
from urllib.parse import parse_qsl

from auth.api import Principal
from core.db import get_session
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlmodel import Session

from grimoire import join_links
from grimoire.access import (
    browser_origin,
    get_authenticated_email,
    get_authenticated_identity,
)

logger = logging.getLogger("monolith.grimoire.join_landing")

JOIN_COOKIE = "grimoire-join-resume"
JOIN_PATH = "/grimoire/join"
# Short resume window, with the invitation's expiry always checked on use.
JOIN_COOKIE_MAX_AGE = 3600
MAX_BODY_BYTES = 2048

JOIN_UNAVAILABLE = "Campaign invitation links are unavailable right now. Ask the campaign owner for an invitation."
INCOMPLETE_LINK = "This invitation link is incomplete. Open the full link shared by the campaign owner."
INVALID_LINK = "This invitation is invalid, expired, revoked, or already used. Ask the campaign owner for a new link."
ACCOUNT_MAY_EXIST = (
    "An account may already exist. Sign in to continue with this invitation."
)
NO_ACCOUNT_CREATION = "Account creation is not available for this invitation. Sign in with the invited account."
ENROLLMENT_UNAVAILABLE = (
    "Account creation is unavailable right now. Please try again later."
)
INVITATION_CHANGED = "Invitation changed. Reopen the original link before continuing."
WRONG_ORIGIN = "Open this invitation from the original link."
INVALID_REQUEST = "Invalid invitation request."
INVALID_ACTION = "Invalid invitation action."
OPENING = "Opening your invitation…"
CLOSED = "Invitation closed. Reopen the original link when you are ready."

# Only fixed messages owned by the app may cross the invitation boundary.
# Other failures can carry an upstream URL, SQL parameters or a secret.
SAFE_ERRORS = frozenset(
    {
        "Your session has expired. Please sign in again.",
        "Invalid selection.",
        "Invitation not found.",
        "Campaign owner required.",
        "Enter a valid player's email address.",
        "This player must sign in to Grimoire first, or ask an account administrator for an enrollment invitation.",
        "Account enrollment is not enabled. Ask an administrator.",
        "This player is already a campaign member.",
        "A pending invitation already exists. Revoke it before creating a replacement.",
        "Sign in with the account this invitation was created for.",
        "This invitation is no longer available.",
        "This invitation has expired. Ask for a new link.",
        "You are already a campaign member.",
        "Membership changed. Please retry joining.",
        "This player already joined. Use Remove player instead.",
        "Account enrollment is temporarily unavailable. Please retry.",
        "Account invitation cancellation needs an administrator retry.",
    }
)

_UUID = r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"
_INVITATION_ID = re.compile(_UUID, re.IGNORECASE)
# The whole URL is pinned: one flow, one parameter, no credentials, port or
# fragment. Anything the provider adds later fails closed here.
_ENROLLMENT_URL = re.compile(
    rf"https://auth\.jomcgi\.dev/if/flow/grimoire-link-enrollment/\?itoken={_UUID}",
    re.IGNORECASE,
)

# Keep native form POST Origin intact while suppressing cross-origin referrers.
# no-referrer serializes navigation POST Origin to null, which the strict
# origin check below correctly rejects. Keep this header and the HTML meta
# aligned. https://fetch.spec.whatwg.org/#append-a-request-origin-header
PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Cloudflare-CDN-Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

# The landing's browser script lives in the web app, where its DOM tests run
# (web/grimoire/src/lib/join-landing.test.js). It is a module exporting one
# function; the page embeds it as a plain script that calls that function.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "grimoire"
    / "src"
    / "lib"
    / "join-landing.js"
)
_SCRIPT_EXPORT = "export async function startJoinLanding()"


def _embeddable(module: str) -> str:
    if module.count(_SCRIPT_EXPORT) != 1:
        raise ValueError("join-landing.js must export startJoinLanding exactly once")
    return (
        module.replace(_SCRIPT_EXPORT, "async function startJoinLanding()")
        + "\nstartJoinLanding();\n"
    )


_SCRIPT = _embeddable(_SCRIPT_PATH.read_text(encoding="utf-8"))
# The script is embedded in a script element, so it must not be able to end it.
if re.search(r"</script|<!--", _SCRIPT, re.IGNORECASE):
    raise ValueError("join-landing.js cannot be embedded in a script element")
_CLEAR_FRAGMENT = 'history.replaceState(null,"",location.pathname);'

# Technical-drawing colours from projects/design-system/tokens: system fonts,
# square corners, hairline borders, no shadow.
_STYLE = """
:root { color-scheme:light dark; --surface:#ffffff; --ink:#26231c; --muted:#625d50; --line:#d5d7dc; --line-strong:#77766f; --accent:#1f4f9f; --on-accent:#ffffff; }
@media (prefers-color-scheme: dark) { :root { --surface:#181a20; --ink:#e8e5dc; --muted:#a5a297; --line:#393b43; --line-strong:#82847e; --accent:#d3a45f; --on-accent:#181a20; } }
body { margin:0; font:1rem/1.6 "Avenir Next","Segoe UI",system-ui,sans-serif; background:var(--surface); color:var(--ink); }
main { max-width:40rem; margin:8vh auto; padding:1.5rem; }
h1 { line-height:1.15; font-size:2rem; font-weight:600; margin:0 0 1rem; }
h2 { font-size:1.25rem; font-weight:600; margin:0 0 1rem; }
.brand { margin:0 0 .5rem; color:var(--muted); font-size:.875rem; letter-spacing:.08em; text-transform:uppercase; }
section { border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:1rem 0 0; margin:1.5rem 0; }
dt { color:var(--muted); font-size:.875rem; } dd { margin:0 0 1rem; overflow-wrap:anywhere; }
a { color:var(--accent); }
button,.button { display:inline-block; font:inherit; padding:.6rem 1rem; border:1px solid var(--line-strong); border-radius:0; background:transparent; color:var(--ink); text-decoration:none; cursor:pointer; }
.button { border-color:var(--accent); background:var(--accent); color:var(--on-accent); }
a:focus-visible,button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
form { margin:1rem 0; } button:disabled { opacity:.65; cursor:wait; } [hidden] { display:none !important; }
"""

T = TypeVar("T")


class JoinLinkError(Exception):
    """A failure whose message is fixed text that is safe to show a visitor."""

    def __init__(self, message: str, *, sign_in: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.sign_in = sign_in


def safe_join_message(error: BaseException) -> str:
    if isinstance(error, JoinLinkError):
        return error.message
    if (
        isinstance(error, HTTPException)
        and isinstance(error.detail, str)
        and error.detail in SAFE_ERRORS
    ):
        return error.detail
    return JOIN_UNAVAILABLE


def join_token(value: object) -> str:
    if not isinstance(value, str) or not join_links.TOKEN_PATTERN.fullmatch(value):
        raise JoinLinkError(INCOMPLETE_LINK)
    return value


def enrollment_url(value: object) -> str:
    """Only the dedicated provider flow is ever a redirect target."""
    if not isinstance(value, str) or not _ENROLLMENT_URL.fullmatch(value):
        raise JoinLinkError(ENROLLMENT_UNAVAILABLE)
    return value


def join_metadata(link: join_links.InspectedLink) -> dict[str, Any]:
    # Explicit allowlist: neither a token nor the provider invitation can
    # escape through an inspect, state or error response if the view grows.
    row = link.model_dump(mode="json")
    metadata = {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "campaign_name": row["campaign_name"],
        "invitee_email": row["invitee_email"],
        "expires_at": row["expires_at"],
        "status": row["status"],
        "can_enroll": row["can_enroll"] is True,
    }
    if row.get("enrollment_cleanup_pending") is True:
        metadata["enrollment_cleanup_pending"] = True
    return metadata


def _capability(action: str, call: Callable[[], T]) -> T:
    """Run one bearer operation, replacing its refusal with fixed text."""
    try:
        return call()
    except HTTPException as error:
        status = error.status_code
        # Signup may have finished before the browser returned here.
        if action == "enroll" and status == 409:
            raise JoinLinkError(ACCOUNT_MAY_EXIST, sign_in=True) from None
        if status in (400, 404, 409, 410):
            raise JoinLinkError(INVALID_LINK) from None
        if status == 403:
            raise JoinLinkError(NO_ACCOUNT_CREATION) from None
        raise JoinLinkError(JOIN_UNAVAILABLE) from None


def _inspect(session: Session, token: str) -> join_links.InspectedLink:
    return _capability("inspect", lambda: join_links.inspect_link(session, token))


def check_join_selection(
    session: Session, token: str, invitation_id: object
) -> join_links.InspectedLink:
    """Refuse an older tab acting on the invitation a newer tab captured."""
    if not isinstance(invitation_id, str) or not _INVITATION_ID.fullmatch(
        invitation_id
    ):
        raise JoinLinkError(INVITATION_CHANGED)
    invitation = _inspect(session, token)
    if invitation.id != invitation_id:
        raise JoinLinkError(INVITATION_CHANGED)
    return invitation


def _failed(session: Session, action: str, error: Exception) -> None:
    """Leave the session usable and record only the failure's type."""
    # The original failure is what matters, not a failed rollback.
    with contextlib.suppress(Exception):
        session.rollback()
    if not isinstance(error, (JoinLinkError, HTTPException)):
        # The message may hold SQL parameters or a provider URL: never log it.
        logger.error("Join link %s failed: %s", action, type(error).__name__)


def _same_origin(request: Request) -> bool:
    """A state change must come from a page on this exact origin.

    Behind the gateway the app cannot see its public scheme, so the configured
    browser origin is the reference when set. Otherwise the request's own
    origin is. A missing, repeated or ``null`` Origin fails closed.
    """
    expected = browser_origin() or f"{request.url.scheme}://{request.url.netloc}"
    origins = request.headers.getlist("origin")
    return len(origins) == 1 and origins[0] == expected


@dataclass(frozen=True)
class _Body:
    raw: bytes | None  # None when the body is over the limit.
    is_json: bool


async def _read_body(request: Request) -> _Body:
    is_json = request.headers.get("content-type", "").startswith("application/json")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = MAX_BODY_BYTES + 1
    if declared > MAX_BODY_BYTES:
        return _Body(None, is_json)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return _Body(None, is_json)
        chunks.append(chunk)
    return _Body(b"".join(chunks), is_json)


def _parse(body: _Body) -> dict[str, Any]:
    """Return the posted fields. Raises ``ValueError`` for malformed JSON."""
    assert body.raw is not None
    text = body.raw.decode("utf-8", errors="replace")
    if not body.is_json:
        return dict(parse_qsl(text, keep_blank_values=True))
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _save(response: Response, token: str) -> None:
    response.set_cookie(
        JOIN_COOKIE,
        join_token(token),
        max_age=JOIN_COOKIE_MAX_AGE,
        path=JOIN_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear(response: Response) -> None:
    response.delete_cookie(
        JOIN_COOKIE, path=JOIN_PATH, secure=True, httponly=True, samesite="lax"
    )


def _json(content: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, headers=PRIVATE_HEADERS)


def landing(
    message: str, *, enabled: bool, status: int = 200, sign_in: bool = False
) -> HTMLResponse:
    nonce = base64.b64encode(secrets.token_bytes(18)).decode()
    script = _SCRIPT if enabled else _CLEAR_FRAGMENT
    resume = (
        '<p><a href="/grimoire/join/accept">Sign in to continue</a></p>'
        if sign_in
        else ""
    )
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="referrer" content="same-origin">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>Campaign invitation · Grimoire</title>"
        f'<style nonce="{nonce}">{_STYLE}</style></head><body><main>'
        '<p class="brand">Grimoire</p><h1>Join a campaign</h1>'
        f'<p id="status" role="status">{html.escape(message)}</p>'
        '<section id="invitation" aria-label="Invitation details" hidden>'
        '<h2 id="campaign"></h2><dl><dt>Invited email</dt><dd id="recipient"></dd>'
        '<dt>Expires</dt><dd id="expires"></dd>'
        '<dt>Status</dt><dd id="link-status"></dd></dl></section>'
        '<div id="actions" hidden>'
        '<a id="sign-in" class="button" href="/grimoire/join/accept">Sign in to accept</a>'
        '<form id="enrollment" method="POST" action="/grimoire/join" hidden>'
        '<input type="hidden" name="action" value="enroll">'
        '<input id="enrollment-invitation" type="hidden" name="invitation_id">'
        "<p>New to Grimoire? Create an account with the invited email, then return here to accept.</p>"
        "<button>Create account</button></form></div>"
        '<form method="POST" action="/grimoire/join">'
        '<input type="hidden" name="action" value="cancel">'
        "<button>Close invitation</button></form>"
        "<noscript><p>JavaScript is needed to open the private part of this link. "
        "Enable it, then open the original invitation again.</p></noscript>"
        f'{resume}<p><a href="/grimoire/join">Return to invitation</a></p></main>'
        f'<script nonce="{nonce}">{script}</script></body></html>'
    )
    return HTMLResponse(
        page,
        status_code=status,
        headers={
            **PRIVATE_HEADERS,
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                f"style-src 'nonce-{nonce}'; connect-src 'self'; "
                "form-action 'self' https://auth.jomcgi.dev; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )


router = APIRouter(include_in_schema=False)
DatabaseSession = Annotated[Session, Depends(get_session)]
PostedBody = Annotated[_Body, Depends(_read_body)]
# The identity dependency is named so the route audit can see it. The email
# dependency also registers the account and applies the access permission.
SignedIn = Annotated[Principal, Depends(get_authenticated_identity)]
SignedInEmail = Annotated[str, Depends(get_authenticated_email)]


@router.get(JOIN_PATH)
def open_landing() -> HTMLResponse:
    return landing(OPENING, enabled=True)


@router.post(JOIN_PATH)
def post_landing(
    request: Request, body: PostedBody, session: DatabaseSession
) -> Response:
    def failure(message: str, status: int, sign_in: bool = False) -> Response:
        if body.is_json:
            return _json({"error": message}, status)
        return landing(message, enabled=False, status=status, sign_in=sign_in)

    if not _same_origin(request):
        return failure(WRONG_ORIGIN, 403)
    if body.raw is None:
        return failure(INVALID_REQUEST, 413)
    try:
        data = _parse(body)
    except ValueError:
        return failure(INVALID_REQUEST, 400)
    action = data.get("action")

    if action == "cancel":
        response = (
            _json({"ok": True}) if body.is_json else landing(CLOSED, enabled=False)
        )
        _clear(response)
        return response

    # New links always replace stale resumptions, including an invalid link.
    replaces = action == "capture"
    try:
        if action in ("capture", "inspect"):
            token = join_token(
                data.get("token") if replaces else request.cookies.get(JOIN_COOKIE)
            )
            metadata = join_metadata(_inspect(session, token))
            response = _json(metadata)
            if metadata["status"] == "pending":
                _save(response, token)
            else:
                _clear(response)
            return response
        if action == "enroll":
            token = join_token(request.cookies.get(JOIN_COOKIE))
            check_join_selection(session, token, data.get("invitation_id"))
            target = enrollment_url(
                _capability("enroll", lambda: join_links.enroll_link(session, token))
            )
            return Response(
                status_code=303, headers={**PRIVATE_HEADERS, "Location": target}
            )
        return failure(INVALID_ACTION, 400)
    except Exception as error:  # noqa: BLE001 - nothing may escape unfiltered
        _failed(session, str(action), error)
        response = failure(
            safe_join_message(error),
            400,
            isinstance(error, JoinLinkError) and error.sign_in,
        )
        if replaces:
            _clear(response)
        return response


@router.post(f"{JOIN_PATH}/accept")
def accept(
    request: Request,
    body: PostedBody,
    _identity: SignedIn,
    email: SignedInEmail,
    session: DatabaseSession,
) -> JSONResponse:
    """Join the campaign as the signed-in player, then drop the resume cookie."""
    from grimoire.router import _registered_user

    if not _same_origin(request):
        return _json({"error": WRONG_ORIGIN}, 403)
    if body.raw is None:
        return _json({"error": INVALID_REQUEST}, 413)
    try:
        data = json.loads(body.raw.decode("utf-8", errors="replace"))
    except ValueError:
        return _json({"error": INVALID_REQUEST}, 400)
    invitation_id = data.get("invitation_id") if isinstance(data, dict) else None
    try:
        token = join_token(request.cookies.get(JOIN_COOKIE))
        check_join_selection(session, token, invitation_id)
        join_links.redeem_link(session, token, _registered_user(session, email))
    except Exception as error:  # noqa: BLE001 - nothing may escape unfiltered
        _failed(session, "redeem", error)
        # The cookie stays: a wrong account or a retry can still recover.
        return _json({"error": safe_join_message(error)}, 400)
    response = _json({"ok": True})
    _clear(response)
    return response


@router.get(f"{JOIN_PATH}/accept/state")
def accept_state(
    request: Request,
    _identity: SignedIn,
    email: SignedInEmail,
    session: DatabaseSession,
) -> JSONResponse:
    """What the signed-in accept screen shows before the player decides."""
    try:
        invitation = _inspect(session, join_token(request.cookies.get(JOIN_COOKIE)))
    except Exception as error:  # noqa: BLE001 - nothing may escape unfiltered
        _failed(session, "inspect", error)
        return _json({"error": safe_join_message(error)}, 400)
    return _json(
        {
            "invitation": join_metadata(invitation),
            "email": email,
            # Advisory only: redemption checks the immutable account identity.
            "matches": email.strip().lower()
            == invitation.invitee_email.strip().lower(),
        }
    )
