"""Keep every feed pin visibly locked while one real note write is pending."""

import re
import time

from playwright.sync_api import expect
from rehearsal_report import expect_network_diagnostic
from rehearse_join_links import observe_page

CHECKS = (
    "Feed pins disable every target while pending and recover after success or refusal",
)


def rehearse_pins(pages, output, report, width):
    from rehearse import (
        API,
        LOBBY,
        assert_fits,
        campaign_of,
        fetch_loopback_route,
        read,
        shot,
        toast,
    )

    page = pages["a"].context.new_page()
    page.set_viewport_size({"width": width, "height": 844})
    observe_page(page, report, "a")
    held = []
    writes = []
    released = False
    try:
        base = f"/campaigns/{campaign_of(pages['dm'])}"
        session_id = read(page, f"{base}/sessions").json()[0]["id"]
        entries = read(
            page, f"{base}/sessions/{session_id}/events", after=0, limit=500
        ).json()
        public = [
            row
            for row in entries
            if row["audience"] == "table"
            and not row["retracted_at"]
            and row["body"]
            and not row["body"].get("retracted")
        ]
        narration = next(row for row in public if row["kind"] == "narration")
        handout = next(row for row in public if row["kind"] == "handout")
        notes_path = f"{base}/notes"
        notes_url = API + notes_path

        def note_ids():
            return {
                row["id"]
                for row in read(page, notes_path, kind="character", limit=500).json()
            }

        before = note_ids()

        def intercept(route):
            if route.request.method != "POST":
                route.continue_()
                return
            writes.append(route.request.post_data_json)
            if len(writes) == 1:
                # The database commits first; only delivery to the UI is held.
                route.request.rehearsal_injected = True
                accepted = fetch_loopback_route(route)
                assert accepted.ok, accepted.status
                held.append((route, accepted))
            elif len(writes) == 2:
                route.request.rehearsal_injected = True
                expect_network_diagnostic(report, "a", "POST", notes_url, status=503)
                route.fulfill(status=503, json={"detail": "Pinning is unavailable."})
            else:
                route.continue_()

        page.route(notes_url, intercept)
        page.goto(f"{LOBBY}{base}/session?session={session_id}")
        feed = page.get_by_role("list", name="Session feed", exact=True)
        first = feed.get_by_role(
            "button",
            name=f"Pin {narration['body']['text'].splitlines()[0]} to notes",
            exact=True,
        )
        second = feed.get_by_role(
            "button", name=f"Pin {handout['body']['title']} to my notes", exact=True
        )
        expect(first).to_be_visible()
        expect(second).to_be_visible()
        pins = feed.get_by_role("button", name=re.compile(r"^Pin "))
        assert pins.count() >= 2
        first.click()
        for button in pins.all():
            expect(button).to_be_disabled()
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            page.wait_for_timeout(25)
        assert len(held) == 1 and len(writes) == 1
        first_note = held[0][1].json()
        assert first_note["links"]["event_ids"] == [narration["id"]]
        assert first_note["pinned"] and first_note["is_mine"]
        assert note_ids() - before == {first_note["id"]}
        assert_fits(page)
        shot(page, output / "a-feed-pins-pending.png")
        held[0][0].fulfill(response=held[0][1])
        released = True
        toast(page, "Pinned to your notes.")

        page.get_by_role("tab", name="Story", exact=True).click()
        for button in pins.all():
            expect(button).to_be_enabled()
        second.click()
        toast(page, "Pinning is unavailable.")
        for button in pins.all():
            expect(button).to_be_enabled()
        assert note_ids() - before == {first_note["id"]}
        assert_fits(page)
        shot(page, output / "a-feed-pins-refused.png")

        with page.expect_response(
            lambda response: (
                response.url == notes_url
                and response.request.method == "POST"
                and response.ok
            )
        ) as retried:
            second.click()
        second_note = retried.value.json()
        toast(page, "Pinned to your notes.")
        assert second_note["links"]["event_ids"] == [handout["id"]]
        assert second_note["pinned"] and second_note["is_mine"]
        assert first_note["id"] != second_note["id"]
        assert note_ids() - before == {first_note["id"], second_note["id"]}
        assert writes == [
            {"kind": "character", "from_event_id": row["id"]}
            for row in (narration, handout, handout)
        ]
        notes = page.get_by_role("region", name="Campaign notes", exact=True)
        expect(
            notes.get_by_role(
                "heading", name=handout["body"]["title"], exact=True
            ).first
        ).to_be_visible()
        assert_fits(page)
        shot(page, output / "a-feed-pins-recovered.png")
        report["checks"].append(CHECKS[0])
    except Exception as error:
        report["failed_checks"].append(
            {"check": CHECKS[0], "observed": str(error) or repr(error)}
        )
        shot(page, output / "a-feed-pins-failure.png")
        if hasattr(report["checks"], "ready"):
            report["checks"].ready()
    finally:
        if held and not released:
            held[0][0].fulfill(response=held[0][1])
        page.close()
