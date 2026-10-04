"""Real-app invitation checks against the disposable, signed local fixture."""

import json
import re
from urllib.parse import urlsplit

from playwright.sync_api import expect

FRONTEND = "http://friends.localhost:4177"
BACKEND = "http://friends.localhost:8177"
JOIN = f"{FRONTEND}/grimoire/join"
LOBBY = f"{FRONTEND}/grimoire"
COOKIE = "grimoire-join-resume"
CAMPAIGN = "The Invitation Rehearsal"


def redact_join_artifact(value):
    """Reports and failure text must not publish the fixture's capabilities."""
    if isinstance(value, str):
        return re.sub(
            r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])",
            "[redacted invitation]",
            value,
        )
    if isinstance(value, dict):
        return {key: redact_join_artifact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_join_artifact(item) for item in value]
    return value


def rehearse_join_links(browser, output, report, contexts, use_loopback_requests):
    """Keep mutations and all credentials inside run.py's disposable database."""
    requests = []

    def observe(page):
        page.on(
            "pageerror",
            lambda error: report["errors"].append(error.stack or str(error)),
        )
        page.on(
            "console",
            lambda message: (
                report["console_errors"].append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on(
            "requestfailed",
            lambda request: report["network_failures"].append(
                {
                    "url": request.url,
                    "method": request.method,
                    "failure": request.failure,
                }
            ),
        )
        page.on(
            "request",
            lambda request: requests.append(
                (request.url, request.headers.get("referer", ""), page)
            ),
        )
        return page

    def seat(role):
        context = browser.new_context(viewport={"width": 1280, "height": 844})
        contexts.append((f"join-{role}", context))
        use_loopback_requests(context)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = observe(context.new_page())
        if role != "guest":
            page.goto(f"{BACKEND}/__local/login/{role}")
            expect(
                page.get_by_role("heading", name="Your campaigns", exact=True)
            ).to_be_visible()
        return page

    def api(page, method, path, **kwargs):
        token = next(
            cookie["value"]
            for cookie in page.context.cookies()
            if cookie["name"] == "grimoire-id-token"
        )
        return getattr(page.request, method)(
            f"{BACKEND}/api/grimoire{path}",
            headers={"x-grimoire-token": token},
            **kwargs,
        )

    def resume_cookies(page):
        return [cookie for cookie in page.context.cookies() if cookie["name"] == COOKIE]

    def campaign_article(page):
        return page.get_by_role("article").filter(
            has=page.get_by_role("heading", name=CAMPAIGN, exact=True)
        )

    def open_owner():
        dm.goto(LOBBY)
        dm.wait_for_load_state("networkidle")
        article = campaign_article(dm)
        panel = article.locator(":scope > details")
        if panel.get_attribute("open") is None:
            panel.locator(":scope > summary").click()
        expect(panel).to_have_attribute("open", "")
        return article

    def members():
        response = api(dm, "get", f"/campaigns/{campaign_id}/members")
        assert response.ok, response.status
        return response.json()

    def assert_players(emails):
        rows = members()
        assert sorted(row["email"] for row in rows if row["role"] == "player") == emails
        assert sum(row["role"] == "dm" for row in rows) == 1
        assert all(row["player_character_id"] is None for row in rows)

    def issue(email):
        article = open_owner()
        links = article.get_by_role("region", name="Single-use campaign links")
        links.get_by_label("Player's email", exact=True).fill(email)
        links.get_by_role("button", name="Create invitation link", exact=True).click()
        private_link = links.get_by_label("Private invitation link", exact=True)
        expect(private_link).to_be_visible()
        url = private_link.input_value()
        address = urlsplit(url)
        assert address.scheme == "http" and address.netloc == "friends.localhost:4177"
        assert address.path == "/grimoire/join" and not address.query
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", address.fragment)
        # The one-time copy surface is dismissed through the real UI.
        dm.wait_for_load_state("networkidle")
        links.get_by_role("button", name="Hide link", exact=True).click()
        expect(private_link).to_have_count(0)
        dm.goto(LOBBY)
        expect(dm.get_by_label("Private invitation link", exact=True)).to_have_count(0)
        assert address.fragment not in dm.content()
        return url

    def open_link(page, url):
        # A fragment-only goto on the current landing page does not execute
        # its script again. Reopening a link here must load a new document.
        page.goto("about:blank")
        return page.goto(url)

    def capture(page, url, email):
        with page.expect_response(
            lambda response: response.url == JOIN and response.request.method == "POST"
        ) as inspected:
            response = open_link(page, url)
        assert response.status == 200
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "no-store" in response.headers["cache-control"]
        assert inspected.value.ok, inspected.value.status
        metadata = inspected.value.json()
        expect(page.get_by_role("heading", name=CAMPAIGN, exact=True)).to_be_visible()
        expect(page.locator("#recipient")).to_have_text(email)
        expect(page).to_have_url(JOIN)
        token = urlsplit(url).fragment
        assert token not in json.dumps(metadata)
        assert token not in page.content()
        assert token not in page.evaluate(
            "JSON.stringify({local: Object.entries(localStorage), session: Object.entries(sessionStorage)})"
        )
        expect(
            page.get_by_role("button", name="Create account", exact=True)
        ).to_be_hidden()
        assert metadata["can_enroll"] is False
        return metadata

    def accept_page(page, url, email):
        metadata = capture(page, url, email)
        page.get_by_role("link", name="Sign in to accept", exact=True).click()
        expect(page).to_have_url(f"{JOIN}/accept")
        expect(
            page.get_by_role("button", name="Accept and join campaign", exact=True)
        ).to_be_visible()
        page.wait_for_load_state("networkidle")
        return metadata

    dm, a, b, guest = (seat(role) for role in ("dm", "a", "b", "guest"))
    lobby = api(dm, "get", "/lobby").json()
    assert lobby["invitation_links_enabled"] is True
    assert lobby["invitation_enrollment_enabled"] is False
    dm.get_by_label("Campaign name", exact=True).fill(CAMPAIGN)
    dm.get_by_role("button", name="Create campaign", exact=True).click()
    article = open_owner()
    campaign_id = article.locator(
        'form[action="?/createLink"] input[name="campaign_id"]'
    ).input_value()
    assert_players([])
    link_a = issue("a@example.test")
    link_b = issue("b@example.test")
    open_owner()
    dm.screenshot(path=str(output / "join-owner-links.png"), full_page=True)
    report["checks"].append(
        "Owner creates recipient-bound links for a fresh campaign; hide and a fresh lobby read do not restore the private link"
    )

    capture(guest, link_a, "a@example.test")
    assert not any(
        cookie["name"] == "grimoire-id-token" for cookie in guest.context.cookies()
    )
    cookies = resume_cookies(guest)
    assert len(cookies) == 1
    cookie = cookies[0]
    assert cookie["secure"] is True and cookie["httpOnly"] is True
    assert cookie["sameSite"] == "Lax" and cookie["path"] == "/grimoire/join"
    assert cookie["value"] == urlsplit(link_a).fragment
    assert COOKIE not in guest.evaluate("document.cookie")
    guest.reload()
    expect(
        guest.get_by_role("link", name="Sign in to accept", exact=True)
    ).to_be_visible()
    expect(guest).to_have_url(JOIN)
    guest.screenshot(path=str(output / "join-public-resume.png"), full_page=True)
    guest.get_by_role("button", name="Close invitation", exact=True).click()
    expect(
        guest.get_by_text(
            "Invitation closed. Reopen the original link when you are ready.",
            exact=True,
        )
    ).to_be_visible()
    assert not resume_cookies(guest)
    assert_players([])
    report["checks"].append(
        "Anonymous landing removes the fragment, stores a Secure HttpOnly Lax scoped cookie, resumes on reload and clears it on close"
    )

    accept_page(b, link_a, "a@example.test")
    expect(b.get_by_text("Signed in as b@example.test.", exact=True)).to_be_visible()
    b.get_by_role("button", name="Accept and join campaign", exact=True).click()
    expect(b.get_by_role("alert")).to_have_text(
        "Sign in with the account this invitation was created for."
    )
    assert_players([])
    b.screenshot(path=str(output / "join-wrong-account.png"), full_page=True)
    report["checks"].append(
        "Wrong signed account is rejected by the real backend and receives no membership"
    )

    accept_page(a, link_a, "a@example.test")
    a.get_by_role("button", name="Close invitation", exact=True).click()
    expect(a).to_have_url(LOBBY)
    assert not resume_cookies(a)
    a.go_back()
    expect(a.get_by_role("alert")).to_contain_text(
        "This invitation link is incomplete."
    )
    expect(
        a.get_by_role("button", name="Accept and join campaign", exact=True)
    ).to_have_count(0)
    assert_players([])
    report["checks"].append(
        "Closing authenticated acceptance clears the resume cookie; browser back cannot accept the dismissed invitation"
    )

    accept_page(a, link_a, "a@example.test")
    second_tab = observe(a.context.new_page())
    capture(second_tab, link_b, "b@example.test")
    a.bring_to_front()
    a.get_by_role("button", name="Accept and join campaign", exact=True).click()
    expect(a.get_by_role("alert")).to_have_text(
        "Invitation changed. Reopen the original link before continuing."
    )
    assert_players([])
    a.screenshot(path=str(output / "join-changed-tab.png"), full_page=True)
    second_tab.get_by_role("button", name="Close invitation", exact=True).click()
    expect(
        second_tab.get_by_text(
            "Invitation closed. Reopen the original link when you are ready.",
            exact=True,
        )
    ).to_be_visible()
    second_tab.close()
    report["checks"].append(
        "Opening another recipient's link in a second tab cannot switch the invitation accepted by a stale form"
    )

    accept_page(a, link_a, "a@example.test")
    a.screenshot(path=str(output / "join-review.png"), full_page=True)
    a.get_by_role("button", name="Accept and join campaign", exact=True).click()
    expect(a).to_have_url(LOBBY)
    expect(campaign_article(a)).to_be_visible()
    assert not resume_cookies(a)
    assert_players(["a@example.test"])
    # Retry the original capability while membership exists, then verify the
    # same member row survives. This uses the real authenticated backend.
    member_id = next(row["id"] for row in members() if row["email"] == "a@example.test")
    retry = api(
        a, "post", "/join-links/redeem", data={"token": urlsplit(link_a).fragment}
    )
    assert retry.status == 200
    assert_players(["a@example.test"])
    assert (
        next(row["id"] for row in members() if row["email"] == "a@example.test")
        == member_id
    )
    a.screenshot(path=str(output / "join-accepted.png"), full_page=True)
    capture(a, link_a, "a@example.test")
    expect(
        a.get_by_role("link", name="Open your campaigns", exact=True)
    ).to_be_visible()
    expect(a.get_by_role("link", name="Sign in to accept", exact=True)).to_have_count(0)
    assert not resume_cookies(a)
    report["checks"].append(
        "Correct account explicitly joins as one unassigned player; a retry keeps the same membership and a used link has no accept control"
    )

    article = open_owner()
    member = article.get_by_role("listitem").filter(
        has=dm.get_by_role("button", name="Remove player", exact=True)
    )
    member.get_by_role("button", name="Remove player", exact=True).click()
    assert_players([])
    open_link(a, link_a)
    expect(a.get_by_role("alert")).to_contain_text(
        "invalid, expired, revoked, or already used"
    )
    expect(a).to_have_url(JOIN)
    expect(a.get_by_role("link", name="Sign in to accept", exact=True)).to_have_count(0)
    assert not resume_cookies(a)
    replay = api(
        a, "post", "/join-links/redeem", data={"token": urlsplit(link_a).fragment}
    )
    assert replay.status == 409
    assert_players([])
    a.screenshot(path=str(output / "join-replay-blocked.png"), full_page=True)
    report["checks"].append(
        "Owner removes the player; reopening or replaying the accepted capability cannot regrant membership"
    )

    accept_page(b, link_b, "b@example.test")
    article = open_owner()
    pending = (
        article.get_by_role("region", name="Single-use campaign links")
        .get_by_role("listitem")
        .filter(has_text="b@example.test")
    )
    pending.get_by_role("button", name="Revoke link", exact=True).click()
    b.get_by_role("button", name="Accept and join campaign", exact=True).click()
    expect(b.get_by_role("alert")).to_contain_text(
        "invalid, expired, revoked, or already used"
    )
    assert_players([])
    open_link(b, link_b)
    expect(b.get_by_role("alert")).to_contain_text(
        "invalid, expired, revoked, or already used"
    )
    expect(b).to_have_url(JOIN)
    assert not resume_cookies(b)
    revoked = api(
        b, "post", "/join-links/redeem", data={"token": urlsplit(link_b).fragment}
    )
    assert revoked.status == 409
    assert_players([])
    b.screenshot(path=str(output / "join-revoked.png"), full_page=True)
    report["checks"].append(
        "Revocation blocks an already-open acceptance form, a fresh landing and direct replay without granting membership"
    )

    for link in (link_a, link_b):
        token = urlsplit(link).fragment
        assert all(
            token not in url and token not in referrer for url, referrer, _ in requests
        )
        assert all(token not in message for message in report["console_errors"])
    assert all(
        urlsplit(url).hostname in {"friends.localhost", "127.0.0.1"}
        for url, _, page in requests
        if page is guest
    )
    # Authenticated pages retain the friends layout's existing external fonts.
    assert all(urlsplit(url).hostname != "auth.jomcgi.dev" for url, _, _ in requests)
    report["checks"].append(
        "Browser URLs and referrers contain no raw link capability; the anonymous landing loads no external resources and no browser contacts Authentik"
    )
