"""Read-only identity bootstrap checks using the three existing demo seats."""

import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect
from rehearsal_report import expect_network_diagnostic
from rehearse_join_links import observe_page

CHECKS = (
    "A failed first identity read blocks private Grimoire reads and recovers through Retry",
    "A platform profile establishes the demo identity without Grimoire access",
    "A platform account switch clears the previous profile while the lobby stays denied",
)


def rehearse_bootstrap(pages, output, report, width):
    from rehearse import (
        API,
        LOBBY,
        ORIGIN,
        assert_fits,
        fetch_loopback_route,
        read,
        shot,
        use_loopback_requests,
    )

    campaign = next(
        row
        for row in read(pages["dm"], "/lobby").json()["campaigns"]
        if row["name"] == "The Lantern at the Crossroads"
    )
    context = pages["a"].context.browser.new_context(
        viewport={"width": width, "height": 844}
    )
    context.add_cookies(pages["a"].context.cookies(ORIGIN))
    use_loopback_requests(context)
    lobby_url = f"{API}/lobby"
    self_url = f"{ORIGIN}/api/auth/platform/self"

    def page_for(seat="a"):
        page = context.new_page()
        observe_page(page, report, seat)
        return page

    def refusal(page, status):
        def refuse(route):
            route.request.rehearsal_injected = True
            expect_network_diagnostic(
                report, "a", "GET", route.request.url, status=status
            )
            route.fulfill(
                status=status,
                json={"detail": "The local rehearsal is holding the identity read."},
            )

        page.route(lobby_url, refuse)

    def start():
        if hasattr(report["checks"], "ready"):
            report["checks"].ready()

    def failed(index, page, error):
        report["failed_checks"].append({"check": CHECKS[index], "observed": str(error)})
        shot(page, output / f"bootstrap-{index}-failed.png")
        start()

    page = page_for()
    try:
        private_reads = []
        reads_before_identity = []
        identity_answered = False

        def identity_response(response):
            nonlocal identity_answered
            if response.url == lobby_url and response.status == 200:
                identity_answered = True

        def private_request(request):
            if request.url.startswith(f"{API}/") and request.url != lobby_url:
                private_reads.append(request.url)
                if not identity_answered:
                    reads_before_identity.append(request.url)

        page.on("response", identity_response)
        page.on("request", private_request)
        refusal(page, 503)
        start()
        page.goto(f"{LOBBY}/campaigns/{campaign['id']}/notes")
        error = page.get_by_role(
            "heading", name="Could not load your account", exact=True
        )
        expect(error).to_be_visible()
        assert not private_reads, (
            f"Private reads started without an identity: {private_reads}"
        )
        expect(page.get_by_role("button", name="Feedback", exact=True)).to_have_count(0)
        assert_fits(page)
        shot(page, output / "bootstrap-initial-identity-error.png")
        page.unroute(lobby_url)
        page.get_by_role("button", name="Try again", exact=True).click()
        expect(
            page.get_by_role("region", name="Campaign notes", exact=True)
        ).to_be_visible()
        expect(error).to_have_count(0)
        assert identity_answered
        assert not reads_before_identity, (
            f"Private reads started before identity recovery: {reads_before_identity}"
        )
        assert any("/notes" in url for url in private_reads)
        assert_fits(page)
        shot(page, output / "bootstrap-identity-recovered.png")
        report["checks"].append(CHECKS[0])
    except (AssertionError, PlaywrightError) as error:
        failed(0, page, error)
    finally:
        page.close()

    page = page_for()
    switch = None
    try:
        refusal(page, 403)
        start()
        with (
            page.expect_response(
                lambda response: response.url == lobby_url and response.status == 403
            ),
            page.expect_response(
                lambda response: response.url == self_url and response.status == 200
            ) as response,
        ):
            page.goto(f"{LOBBY}/platform/profile")
        first = response.value.json()
        assert first["username"] == "a" and first["email"] == "a@example.test"
        assert first["permissions"] == []
        assert first["identities"] == [
            {"issuer": "http://127.0.0.1:8177/__local/", "subject": "a"}
        ]
        expect(page.get_by_text("Welcome, Elowen.", exact=True)).to_be_visible()
        expect(
            page.get_by_text(f"Your platform user ID: {first['id']}", exact=True)
        ).to_be_visible()
        assert_fits(page)
        shot(page, output / "bootstrap-profile-elowen.png")
        report["checks"].append(CHECKS[1])
    except (AssertionError, PlaywrightError) as error:
        failed(1, page, error)
        page.close()
        context.close()
        return

    try:
        # The second tab changes only this disposable browser context's signed
        # demo cookie. Neither account access nor platform membership changes.
        switch = page_for("b")
        # The fence reload can drop /self's body; capture it before delivery.
        selves = {}

        def observe_self(route):
            fetched = fetch_loopback_route(route)
            selves[route.request] = fetched.json() if fetched.ok else {}
            route.fulfill(response=fetched)

        page.route(self_url, observe_self)
        started = time.monotonic()
        with page.expect_response(
            lambda response: selves.get(response.request, {}).get("username") == "b",
            timeout=20000,
        ) as response:
            switch.goto(f"{ORIGIN}/__local/login/b")
            page.bring_to_front()
        report["timings"]["bootstrap_profile_identity_poll_seconds"] = (
            time.monotonic() - started
        )
        second = selves[response.value.request]
        assert second["id"] != first["id"] and second["email"] == "b@example.test"
        assert second["permissions"] == []
        scrub_started = time.monotonic()
        expect(page.get_by_text("Welcome, Elowen.", exact=True)).to_have_count(
            0, timeout=6000
        )
        expect(
            page.get_by_text(f"Your platform user ID: {first['id']}", exact=True)
        ).to_have_count(0)
        report["timings"]["bootstrap_profile_scrub_seconds"] = (
            time.monotonic() - scrub_started
        )
        # The application's own fence reloads; the rehearsal never reloads it.
        expect(page.get_by_text("Welcome, Bram.", exact=True)).to_be_visible()
        own = page.request.get(self_url)
        assert own.status == 200 and own.json()["id"] == second["id"]
        assert_fits(page)
        shot(page, output / "bootstrap-profile-bram.png")
        report["checks"].append(CHECKS[2])
    except (AssertionError, PlaywrightError) as error:
        failed(2, page, error)
    finally:
        if switch is not None:
            switch.close()
        page.close()
        context.close()
