"""The Grimoire API paths the friends gateway forwards from a browser.

The friends lane's UI is static files, so the browser calls the API port
directly. Only these prefixes are routed there. Library reads such as
``/api/grimoire/books`` carry no identity check of their own (the private
lane's Cloudflare Access session is their gate), so they must stay unrouted.

``chart/templates/httproute-grimoire.yaml`` renders one rule per prefix and
``chart/grimoire_browser_lane_test.py`` pins the rendered list to this one.
"""

from auth.browser_cookie import PLATFORM_BROWSER_API_PREFIXES

GRIMOIRE_API_PREFIXES = (
    "/api/grimoire/lobby",
    "/api/grimoire/campaigns",
    "/api/grimoire/invitations",
    "/api/grimoire/feedback",
)

# Everything the gateway routes to the API port from the friends lane: the
# campaign API above and the account API the platform pages call.
BROWSER_API_PREFIXES = (*GRIMOIRE_API_PREFIXES, *PLATFORM_BROWSER_API_PREFIXES)


def is_browser_path(path: str) -> bool:
    """Match as the Gateway API PathPrefix does: whole path segments only."""
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in BROWSER_API_PREFIXES
    )
