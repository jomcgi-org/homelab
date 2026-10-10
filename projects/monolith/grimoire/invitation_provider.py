"""Default-off Authentik invitation adapter. Never accepts caller-supplied flows.

This is NOT a flow-scoped credential: Authentik's add_invitation permission is
model-wide. Provisioning and enabling it require a separate access review.
"""

import os
from datetime import datetime
from uuid import UUID

import httpx
from fastapi import HTTPException
from opentelemetry.instrumentation.utils import suppress_http_instrumentation

AUTH_ORIGIN = "https://auth.jomcgi.dev"
FLOW_SLUG = "grimoire-link-enrollment"


def enrollment_enabled() -> bool:
    return os.getenv("GRIMOIRE_INVITATION_ENROLLMENT_ENABLED", "") == "true"


class InvitationProvider:
    def __init__(self):
        self.token = os.getenv("GRIMOIRE_INVITATION_API_TOKEN", "")
        flow = os.getenv("GRIMOIRE_INVITATION_FLOW_ID", "")
        try:
            self.flow_id = str(UUID(flow))
        except ValueError:
            self.flow_id = ""
        if not enrollment_enabled() or not self.token or not self.flow_id:
            raise HTTPException(
                503, "Account enrollment is not enabled. Ask an administrator."
            )

    def _request(self, method: str, path: str, body: dict | None = None):
        # Use the transport directly: httpx.Client's request log includes the
        # invitation UUID in the URL. These UUIDs are enrollment credentials.
        # Suppress HTTP telemetry too; never surface provider errors or payloads.
        request = httpx.Request(
            method,
            f"{AUTH_ORIGIN}/api/v3/stages/invitation/invitations/{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=body,
            extensions={
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), 5.0)
            },
        )
        try:
            with (
                suppress_http_instrumentation(),
                httpx.HTTPTransport(trust_env=False) as transport,
            ):
                response = transport.handle_request(request)
                response.read()
                return response
        except (httpx.HTTPError, ValueError):
            raise HTTPException(
                503, "Account enrollment is temporarily unavailable. Please retry."
            ) from None

    def create(
        self, *, link_id: str, email: str, username: str, expires: datetime
    ) -> str:
        response = self._request(
            "POST",
            "",
            {
                "name": f"grimoire-{link_id}",
                "flow": self.flow_id,
                "single_use": True,
                "expires": expires.isoformat(),
                "fixed_data": {"email": email, "username": username},
            },
        )
        if response.status_code != 201:
            raise HTTPException(
                503, "Account enrollment is temporarily unavailable. Please retry."
            )
        try:
            result = response.json()
            invitation_id = str(UUID(result["pk"]))
            if (
                result.get("flow") != self.flow_id
                or result.get("single_use") is not True
            ):
                raise ValueError()
            return invitation_id
        except (ValueError, KeyError, TypeError):
            raise HTTPException(
                503, "Account enrollment is temporarily unavailable. Please retry."
            ) from None

    def exists(self, invitation_id: str) -> bool:
        response = self._request("GET", f"{UUID(invitation_id)}/")
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise HTTPException(
                503, "Account enrollment is temporarily unavailable. Please retry."
            )
        return True

    def revoke(self, invitation_id: str) -> None:
        response = self._request("DELETE", f"{UUID(invitation_id)}/")
        if response.status_code not in (204, 404):
            raise HTTPException(
                503, "Account invitation cancellation needs an administrator retry."
            )

    @staticmethod
    def enrollment_url(invitation_id: str) -> str:
        return f"{AUTH_ORIGIN}/if/flow/{FLOW_SLUG}/?itoken={UUID(invitation_id)}"
