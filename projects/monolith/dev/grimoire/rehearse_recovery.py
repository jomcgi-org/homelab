"""Recover deliberately refused first reads through the real session UI."""

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect
from rehearsal_report import expect_network_diagnostic
from rehearse_join_links import observe_page

CHECKS = (
    "A failed first session feed read offers Retry and loads the real story",
    "A failed first journal read offers Retry and keeps its audience controls",
)


def rehearse_recovery(pages, output, report, width):
    from rehearse import API, LOBBY, assert_fits, campaign_of, read, shot

    campaign_id = campaign_of(pages["dm"])
    base = f"/campaigns/{campaign_id}"
    session_id = read(pages["dm"], f"{base}/sessions").json()[0]["id"]
    session_path = f"{base}/sessions/{session_id}"
    url = f"{LOBBY}{base}/session?session={session_id}"

    for index, role in enumerate(("dm", "a")):
        page = pages[role].context.new_page()
        page.set_viewport_size({"width": width, "height": 844})
        observe_page(page, report, role)
        endpoint = "events" if index == 0 else "journal"
        request_url = f"{API}{session_path}/{endpoint}"
        pattern = request_url + "*"
        refusing = True
        refused = []

        def intercept(route):
            if refusing and route.request.method == "GET":
                refused.append(route.request.url)
                route.request.rehearsal_injected = True
                expect_network_diagnostic(
                    report, role, "GET", route.request.url, status=503
                )
                route.fulfill(
                    status=503,
                    json={
                        "detail": f"The {'story' if index == 0 else 'journal'} is temporarily unavailable."
                    },
                )
            else:
                route.continue_()

        page.route(pattern, intercept)
        try:
            page.goto(url)
            if index == 1:
                page.get_by_role("tab", name="Journal", exact=True).click()
            failure = page.get_by_role(
                "heading",
                name="Could not open the session"
                if index == 0
                else "Could not load the journal",
                exact=True,
            )
            expect(failure).to_be_visible()
            assert refused, "The initial read must actually receive the injected503"
            if index == 1:
                journal = page.get_by_role("region", name="Session journal", exact=True)
                expect(
                    journal.get_by_role("radio", name="Mine", exact=True)
                ).to_be_checked()
                expect(
                    journal.get_by_role("radio", name="Party", exact=True)
                ).to_be_enabled()
                expect(
                    journal.get_by_text("Nothing recorded yet.", exact=True)
                ).to_have_count(0)
            assert_fits(page)
            shot(page, output / f"{role}-{endpoint}-initial-error.png")
            refusing = False
            with page.expect_response(
                lambda response: (
                    response.url.startswith(request_url)
                    and response.request.method == "GET"
                    and response.status == 200
                )
            ) as recovered:
                page.get_by_role("button", name="Try again", exact=True).click()
            assert (
                recovered.value.json()
                == read(
                    page,
                    f"{session_path}/{endpoint}",
                    **({"after": 0, "limit": 500} if index == 0 else {"view": "mine"}),
                ).json()
            )
            expect(failure).to_have_count(0)
            if index == 0:
                expect(
                    page.get_by_role("list", name="Session feed", exact=True)
                ).to_be_visible()
            else:
                expect(
                    journal.get_by_role("radio", name="Mine", exact=True)
                ).to_be_checked()
                expect(journal.locator('[aria-busy="true"]')).to_have_count(0)
            assert_fits(page)
            shot(page, output / f"{role}-{endpoint}-recovered.png")
            report["checks"].append(CHECKS[index])
        except (AssertionError, PlaywrightError) as error:
            report["failed_checks"].append(
                {"check": CHECKS[index], "observed": str(error)}
            )
            shot(page, output / f"{role}-{endpoint}-recovery-failure.png")
            if hasattr(report["checks"], "ready"):
                report["checks"].ready()
        finally:
            page.close()
