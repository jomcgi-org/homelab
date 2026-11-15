"""Pending keyboard writes and private reply recovery against the local API."""

from playwright.sync_api import expect
from rehearsal_report import expect_network_diagnostic

CHECKS = (
    "Repeated Enter while campaign creation is pending creates exactly one campaign",
    "Repeated Enter while a dice roll is pending creates exactly one roll",
    "A private DM reply survives confirmed sign-in expiry and resolves the same action",
    "Private replies respect reassigned authors and reset cancelled sends",
)


def rehearse_submission(pages, output, report, width):
    from rehearse import (
        LOBBY,
        choose,
        campaign_read,
        composer,
        events_of,
        feed,
        fetch_loopback_route,
        read,
        say,
        send_button,
        shot,
        toast,
    )
    from rehearse_join_links import observe_page

    dm, a, b = (pages[role] for role in ("dm", "a", "b"))
    campaign = next(
        row
        for row in read(dm, "/lobby").json()["campaigns"]
        if row["name"] == "The Lantern at the Crossroads"
    )
    cid = campaign["id"]
    session_url = f"{LOBBY}/campaigns/{cid}/session"

    def run(index, action):
        try:
            action()
            report["checks"].append(CHECKS[index])
        except Exception as error:
            report["failed_checks"].append(
                {"check": CHECKS[index], "observed": str(error) or repr(error)}
            )
            for role, page in pages.items():
                try:
                    shot(page, output / f"submission-{index}-{role}-failed.png")
                except Exception as capture_error:
                    report.setdefault("capture_errors", []).append(str(capture_error))
            report["checks"].ready()

    def twice(page, locator, pattern):
        held = []

        def hold(route):
            if route.request.method == "POST":
                route.request.rehearsal_injected = True
                held.append(route)
            else:
                route.continue_()

        page.route(pattern, hold)
        try:
            locator.press("Enter")
            locator.press("Enter")
            # Let both native keyboard handlers and their request dispatch run
            # while the first real write has not received a response.
            page.wait_for_timeout(150)
            assert len(held) == 1, f"Repeated Enter sent {len(held)} pending requests"
        finally:
            for route in held:
                route.fulfill(response=fetch_loopback_route(route))
            page.unroute(pattern, hold)

    def campaign():
        dm.goto(LOBBY)
        name = f"Pending keyboard campaign {width}"
        field = dm.get_by_role("textbox", name="Campaign name", exact=True)
        field.fill(name)
        twice(dm, field, "**/api/grimoire/campaigns")
        expect(dm.get_by_role("article", name=name, exact=True)).to_be_visible()
        assert (
            sum(row["name"] == name for row in read(dm, "/lobby").json()["campaigns"])
            == 1
        )
        shot(dm, output / "pending-campaign-created-once.png")

    run(0, campaign)

    def roll():
        for page in (dm, a, b):
            page.goto(session_url)
        if dm.get_by_role("button", name="Start session", exact=True).count():
            dm.get_by_role("button", name="Start session", exact=True).click()
        expect(a.get_by_role("button", name="Roll dice", exact=True)).to_be_visible(
            timeout=6000
        )
        a.get_by_role("button", name="Roll dice", exact=True).click()
        tray = a.get_by_role("dialog", name="Roll dice", exact=True)
        label = f"Pending keyboard roll {width}"
        tray.get_by_role("textbox", name="Roll label", exact=True).fill(label)
        formula = tray.get_by_role("textbox", name="Dice formula", exact=True)
        formula.fill("d20+3")
        twice(a, formula, "**/api/grimoire/campaigns/*/sessions/*/rolls")
        a.keyboard.press("Escape")
        expect(feed(a).get_by_text(label, exact=False)).to_be_visible()
        assert (
            sum((row.get("body") or {}).get("label") == label for row in events_of(a))
            == 1
        )
        shot(a, output / "pending-roll-created-once.png")

    run(1, roll)

    def reply():
        for page in (dm, a, b):
            page.goto(session_url)
        a.bring_to_front()
        action = f"Private action awaiting an answer {width}"
        say(a, action, "DM privately")
        dm.bring_to_front()
        item = feed(dm).get_by_role("listitem").filter(has_text=action)
        item.get_by_role("button", name="Reply privately to Elowen", exact=True).click()
        message = f"Private reply restored after sign-in {width}"
        composer(dm).fill(message)
        source = next(
            row for row in events_of(a) if (row.get("body") or {}).get("text") == action
        )
        shot(dm, output / "private-reply-before-expiry.png")
        restored = False

        def navigating(request):
            nonlocal restored
            if request.is_navigation_request() and request.url == session_url:
                restored = True

        def expired(route):
            request = route.request
            if request.method == "GET" and not restored:
                expect_network_diagnostic(report, "dm", "GET", request.url, status=401)
                route.fulfill(
                    status=401,
                    content_type="application/json",
                    body='{"detail":"Synthetic local sign-in expiry"}',
                )
            else:
                route.continue_()

        dm.on("request", navigating)
        pattern = "**/api/grimoire/**"
        try:
            # All API reads expire together, as at the gateway; a successful
            # unrelated poll must not reset the two-signal confirmation.
            with dm.expect_event("domcontentloaded", timeout=20000):
                dm.route(pattern, expired)
                dm.wait_for_timeout(6000)
            assert restored, "The confirmed expiry did not reload the page"
            expect(composer(dm)).to_have_value(message)
            expect(
                dm.get_by_text("Replying privately to Elowen", exact=True)
            ).to_be_visible()
            expect(
                dm.get_by_role("combobox", name="Send to", exact=True)
            ).to_be_disabled()
        finally:
            dm.unroute(pattern, expired)
            dm.remove_listener("request", navigating)
        shot(dm, output / "private-reply-after-expiry.png")
        send_button(dm).click()
        expect(composer(dm)).to_have_value("")
        received = next(
            row
            for row in events_of(a)
            if (row.get("body") or {}).get("text") == message
        )
        assert received["body"]["reply_to"] == source["id"]
        assert received["audience"] == "pcs"
        assert (
            message
            not in campaign_read(b, f"/sessions/{source['session_id']}/events").text()
        )
        assert all(
            row["id"] != source["id"]
            for row in campaign_read(
                a, f"/sessions/{source['session_id']}/journal"
            ).json()["open_threads"]
        )
        shot(a, output / "private-reply-resolved.png")
        reply_after_session_change()

    def reply_after_session_change():
        # A fresh Rowan tab has its own sign-in reload guard. Never clear that
        # guard just to simulate a second expiry in the same minute.
        replying = dm.context.new_page()
        other_dm = dm.context.new_page()
        observe_page(replying, report, "dm")
        observe_page(other_dm, report, "dm")
        old_request = []
        held_reads = []
        changed = False
        restored = False
        pattern = "**/api/grimoire/**"

        def refuse_send(route):
            if route.request.method != "POST":
                route.continue_()
                return
            old_request.append(route.request.post_data_json)
            route.request.rehearsal_injected = True
            expect_network_diagnostic(
                report, "dm", "POST", route.request.url, status=503
            )
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"Local rehearsal refused this reply."}',
            )

        def expire(route):
            expect_network_diagnostic(
                report, "dm", "GET", route.request.url, status=401
            )
            route.request.rehearsal_injected = True
            route.fulfill(
                status=401,
                content_type="application/json",
                body='{"detail":"Synthetic local sign-in expiry"}',
            )

        def expired(route):
            if route.request.method != "GET" or restored:
                route.continue_()
            elif changed:
                expire(route)
            else:
                route.request.rehearsal_injected = True
                held_reads.append(route)

        def navigating(request):
            nonlocal restored
            if request.is_navigation_request() and request.url == session_url:
                restored = True

        try:
            for page in (replying, other_dm, a, b):
                page.goto(session_url)
            a.bring_to_front()
            action = f"Old session action awaiting a reply {width}"
            say(a, action, "DM privately")
            source = next(
                row
                for row in events_of(a)
                if (row.get("body") or {}).get("text") == action
            )
            old_session = source["session_id"]
            replying.bring_to_front()
            feed(replying).get_by_role("listitem").filter(has_text=action).get_by_role(
                "button", name="Reply privately to Elowen", exact=True
            ).click()
            message = f"Text kept when the session changed {width}"
            composer(replying).fill(message)
            send_pattern = "**/api/grimoire/campaigns/*/sessions/*/events"
            replying.route(send_pattern, refuse_send)
            try:
                send_button(replying).click()
                toast(replying, "Local rehearsal refused this reply.")
                expect(composer(replying)).to_have_value(message)
                assert len(old_request) == 1
                assert old_request[0]["body"]["reply_to"] == source["id"]
            finally:
                replying.unroute(send_pattern, refuse_send)
            shot(replying, output / "private-reply-before-session-change.png")
            replying.on("request", navigating)
            with replying.expect_event("domcontentloaded", timeout=20000):
                replying.route(pattern, expired)
                other_dm.get_by_role("button", name="End session", exact=True).click()
                other_dm.get_by_role(
                    "alertdialog", name="End this session?"
                ).get_by_role("button", name="End session", exact=True).click()
                other_dm.get_by_role("button", name="Start session", exact=True).click()
                expect(
                    other_dm.get_by_role("button", name="Pause session", exact=True)
                ).to_be_visible()
                new_session = campaign_read(other_dm, "/sessions").json()[0]["id"]
                assert new_session != old_session
                changed = True
                for route in held_reads:
                    expire(route)
                held_reads.clear()
                replying.bring_to_front()
                replying.wait_for_timeout(6000)
            assert restored, "Confirmed expiry did not reload the reply tab"
            expect(composer(replying)).to_have_value(message)
            region = replying.get_by_role("region", name="Message", exact=True)
            expect(
                region.get_by_text(
                    "The session changed. Your text was kept for the DM. Choose who should receive it before sending.",
                    exact=True,
                )
            ).to_be_visible()
            audience = region.get_by_role("combobox", name="Send to", exact=True)
            expect(audience).to_be_enabled()
            expect(audience).to_have_text("DM notes")
            expect(
                region.get_by_role("button", name="Cancel reply", exact=True)
            ).to_have_count(0)
            expect(
                region.get_by_role("checkbox", name="Mark action resolved", exact=True)
            ).to_have_count(0)
            replying.unroute(pattern, expired)
            with replying.expect_request(
                lambda request: (
                    request.method == "POST"
                    and request.url.endswith(f"/sessions/{new_session}/events")
                )
            ) as sent:
                send_button(replying).click()
            payload = sent.value.post_data_json
            assert payload["request_id"] != old_request[0]["request_id"]
            assert payload["audience"] == "dm" and payload["audience_pc_ids"] == []
            assert (
                "reply_to" not in payload["body"] and "resolved" not in payload["body"]
            )
            expect(composer(replying)).to_have_value("")
            old_journal = campaign_read(a, f"/sessions/{old_session}/journal").json()
            assert any(row["id"] == source["id"] for row in old_journal["open_threads"])
            for page in (a, b):
                assert (
                    message
                    not in campaign_read(page, f"/sessions/{new_session}/events").text()
                )
            shot(replying, output / "private-reply-after-session-change.png")
        finally:
            replying.unroute(pattern, expired)
            replying.remove_listener("request", navigating)
            for route in held_reads:
                route.abort()
            replying.close()
            other_dm.close()

    run(2, reply)

    def reply_after_reassignment():
        # Keep the actual action author distinct from the character's next
        # holder. A fresh tab also has its own normal sign-in reload guard.
        replying = dm.context.new_page()
        other_dm = dm.context.new_page()
        new_holder = b.context.new_page()
        for page, role in ((replying, "dm"), (other_dm, "dm"), (new_holder, "b")):
            observe_page(page, report, role)
        held_reads, refused = [], []
        changed = restored = assignments_changed = False
        pattern = "**/api/grimoire/**"
        send_pattern = "**/api/grimoire/campaigns/*/sessions/*/events"
        characters = {
            row["character_name"]: row["id"]
            for row in read(dm, f"/campaigns/{cid}/characters").json()
        }
        article = other_dm.get_by_role(
            "article", name="The Lantern at the Crossroads", exact=True
        )

        def manage(email):
            article.get_by_role("listitem").filter(has_text=email).get_by_role(
                "button", name="Manage", exact=True
            ).click()
            return other_dm.get_by_role("dialog", name=email, exact=True)

        def assign(email, character):
            dialog = manage(email)
            choose(dialog, "Unassigned character", character)
            dialog.get_by_role("button", name="Assign", exact=True).click()
            toast(other_dm, "Character assignment saved.")

        def assigned(page):
            return next(
                row["player_character_id"]
                for row in read(page, "/lobby").json()["campaigns"]
                if row["id"] == cid
            )

        def refuse_send(route):
            if route.request.method != "POST":
                route.continue_()
                return
            refused.append(route.request.post_data_json)
            route.request.rehearsal_injected = True
            expect_network_diagnostic(
                report, "dm", "POST", route.request.url, status=503
            )
            route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"Local rehearsal refused this reply."}',
            )

        def expire(route):
            route.request.rehearsal_injected = True
            expect_network_diagnostic(
                report, "dm", "GET", route.request.url, status=401
            )
            route.fulfill(
                status=401,
                content_type="application/json",
                body='{"detail":"Synthetic local sign-in expiry"}',
            )

        def expired(route):
            if route.request.method != "GET" or restored:
                route.continue_()
            elif changed:
                expire(route)
            else:
                route.request.rehearsal_injected = True
                held_reads.append(route)

        def navigating(request):
            nonlocal restored
            if request.is_navigation_request() and request.url == session_url:
                restored = True

        try:
            for page in (replying, dm, a):
                page.goto(session_url)
            public = f"Author reassignment public table marker {width}"
            say(dm, public, "Everyone")
            action = f"Original Elowen author awaiting a private reply {width}"
            say(a, action, "DM privately")
            source = next(
                row
                for row in events_of(a)
                if (row.get("body") or {}).get("text") == action
            )
            session_id = source["session_id"]
            item = feed(replying).get_by_role("listitem").filter(has_text=action)
            message = f"PRIVATE_REPLY_MUST_NOT_REACH_NEW_CHARACTER_HOLDER_{width}"
            replying.route(send_pattern, refuse_send)
            try:
                for attempt in range(2):
                    item.get_by_role(
                        "button", name="Reply privately to Elowen", exact=True
                    ).click()
                    composer(replying).fill(message)
                    with replying.expect_response(
                        lambda response: (
                            response.request.method == "POST"
                            and response.url.endswith(f"/sessions/{session_id}/events")
                            and response.status == 503
                        )
                    ):
                        send_button(replying).click()
                    toast(replying, "Local rehearsal refused this reply.")
                    expect(send_button(replying)).to_be_enabled()
                    assert len(refused) == attempt + 1
                    assert refused[-1]["body"]["reply_to"] == source["id"]
                    if attempt == 0:
                        replying.get_by_role(
                            "button", name="Cancel reply", exact=True
                        ).click()
                assert refused[0]["request_id"] != refused[1]["request_id"], (
                    "Cancelling and reselecting the same reply reused its request ID"
                )
            finally:
                replying.unroute(send_pattern, refuse_send)
            shot(replying, output / "private-reply-before-reassignment.png")
            # Viewer-boundary cleanup has a separate open-page check. Here the
            # original player pages stay parked while the new holder opens a
            # fresh real session, avoiding any stale-page positive control.
            a.goto("about:blank")
            b.goto("about:blank")
            other_dm.goto(LOBBY)
            article.get_by_role(
                "button", name="Players and invitations", exact=True
            ).click()
            replying.on("request", navigating)
            with replying.expect_event("domcontentloaded", timeout=20000):
                replying.route(pattern, expired)
                manage("a@example.test").get_by_role(
                    "button", name="Clear assignment", exact=True
                ).click()
                assignments_changed = True
                toast(other_dm, "Character assignment saved.")
                assign("b@example.test", "Elowen")
                assert assigned(a) is None
                assert assigned(b) == characters["Elowen"]
                changed = True
                for route in held_reads:
                    expire(route)
                held_reads.clear()
                replying.bring_to_front()
                replying.wait_for_timeout(6000)
            assert restored, "Confirmed expiry did not reload the reply tab"
            replying.unroute(pattern, expired)
            expect(composer(replying)).to_have_value(message)
            region = replying.get_by_role("region", name="Message", exact=True)
            expect(
                region.get_by_text(
                    "The recipient is no longer available. Your text was kept for the DM. Choose who should receive it before sending.",
                    exact=True,
                )
            ).to_be_visible()
            audience = region.get_by_role("combobox", name="Send to", exact=True)
            expect(audience).to_be_enabled()
            expect(audience).to_have_text("DM notes")
            expect(
                region.get_by_role("button", name="Cancel reply", exact=True)
            ).to_have_count(0)
            expect(
                region.get_by_role("checkbox", name="Mark action resolved", exact=True)
            ).to_have_count(0)
            with replying.expect_request(
                lambda request: (
                    request.method == "POST"
                    and request.url.endswith(f"/sessions/{session_id}/events")
                )
            ) as sent:
                send_button(replying).click()
            payload = sent.value.post_data_json
            assert payload["request_id"] not in {row["request_id"] for row in refused}
            assert payload["audience"] == "dm" and payload["audience_pc_ids"] == []
            assert (
                "reply_to" not in payload["body"] and "resolved" not in payload["body"]
            )
            expect(composer(replying)).to_have_value("")
            receipt = next(
                row
                for row in read(
                    dm, f"/campaigns/{cid}/sessions/{session_id}/events"
                ).json()
                if (row.get("body") or {}).get("text") == message
            )
            assert receipt["audience"] == "dm" and receipt["audience_pc_ids"] == []
            new_holder.goto(session_url)
            expect(new_holder.get_by_text(public, exact=True)).to_be_visible()
            expect(
                new_holder.get_by_role("complementary", name="Your character")
            ).to_contain_text("Elowen")
            assert assigned(b) == characters["Elowen"]
            # Private player actions belong to their member author even when
            # that character is reassigned. Neither the action nor reply may
            # become visible to the character's new holder.
            visible = read(b, f"/campaigns/{cid}/sessions/{session_id}/events").text()
            for secret in (action, message):
                expect(new_holder.get_by_text(secret, exact=True)).to_have_count(0)
                assert secret not in visible
            shot(replying, output / "private-reply-after-reassignment.png")
            shot(new_holder, output / "private-reply-new-holder-excluded.png")
        finally:
            replying.unroute(pattern, expired)
            replying.remove_listener("request", navigating)
            for route in held_reads:
                route.abort()
            new_holder.close()
            if assignments_changed:
                if assigned(b) != characters["Bram"]:
                    assign("b@example.test", "Bram")
                if assigned(a) != characters["Elowen"]:
                    assign("a@example.test", "Elowen")
                assert assigned(a) == characters["Elowen"]
                assert assigned(b) == characters["Bram"]
            for page in (a, b):
                page.goto(LOBBY)
                expect(
                    page.get_by_role("button", name="Feedback", exact=True)
                ).to_be_visible()
            replying.close()
            other_dm.close()
        assert any(
            row["id"] == source["id"]
            for row in read(
                a, f"/campaigns/{cid}/sessions/{session_id}/journal"
            ).json()["open_threads"]
        ), "The original author's action was resolved by the fallback DM note"

    run(3, reply_after_reassignment)
