"""Exercise the running local table and save evidence from all three seats."""

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import expect, sync_playwright
from rehearse_join_links import redact_join_artifact, rehearse_join_links


def use_loopback_requests(context):
    """Send fixture inspections to IPv4 while retaining browser host and identity."""
    request = context.request
    for method in ("get", "post", "patch", "delete"):
        original = getattr(request, method)

        def send(url, *, _original=original, **kwargs):
            address = urlsplit(url)
            if address.hostname == "friends.localhost":
                cookies = context.cookies(url)
                kwargs["headers"] = {
                    **kwargs.get("headers", {}),
                    "host": address.netloc,
                    "cookie": "; ".join(
                        f"{item['name']}={item['value']}" for item in cookies
                    ),
                }
                url = urlunsplit(address._replace(netloc=f"127.0.0.1:{address.port}"))
            return _original(url, **kwargs)

        setattr(request, method, send)


def fetch_loopback_route(route):
    """Replay the real intercepted request without relying on runner DNS."""
    address = urlsplit(route.request.url)
    if address.hostname == "friends.localhost":
        return route.fetch(
            url=urlunsplit(address._replace(netloc=f"127.0.0.1:{address.port}")),
            headers={**route.request.all_headers(), "host": address.netloc},
        )
    return route.fetch()


def open_reveal(page):
    """Open the DM reveal drawer; it covers the session controls until closed."""
    page.get_by_role("button", name="Reveal knowledge", exact=True).click()
    expect(page.get_by_role("dialog", name="Reveal to your players")).to_be_visible()


def close_reveal(page):
    page.get_by_role("button", name="Close reveal panel", exact=True).click()
    expect(page.get_by_role("dialog", name="Reveal to your players")).to_have_count(0)


def assert_fits(page):
    """A view must not scroll sideways."""
    widths = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
    assert widths[0] <= widths[1], f"{page.url} scrolls sideways: {widths}"


def state_text(page, query=""):
    """Fetch a seat's state view; a failed response would pass privacy checks."""
    response = page.request.get(page.url + "/state" + query)
    assert response.ok, f"{page.url}/state{query}: {response.status}"
    return response.text()


def open_campaign_players(page, article):
    # The lobby's controlled details can be reset during hydration. Wait until
    # navigation has settled, then open it only if it is currently closed.
    page.wait_for_load_state("networkidle")
    panel = article.locator(":scope > details")
    expect(panel).to_have_count(1)
    if panel.get_attribute("open") is None:
        panel.locator(":scope > summary").click()
    expect(panel).to_have_attribute("open", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/grimoire-rehearsal"))
    parser.add_argument(
        "--join-links", action="store_true", help="Rehearse registered-player links"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "simulated": True,
        "mode": "join-links" if args.join_links else "session",
        "checks": [],
        "errors": [],
        "timings": {},
        "console_errors": [],
        "network_failures": [],
    }
    contexts = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--host-resolver-rules=MAP friends.localhost 127.0.0.1"]
        )
        try:
            if args.join_links:
                rehearse_join_links(
                    browser, args.output, report, contexts, use_loopback_requests
                )
                assert not report["errors"], report["errors"]
                report["passed"] = True
                return
            pages = []
            for role, width in [("dm", 1280), ("a", 390), ("b", 390)]:
                context = browser.new_context(viewport={"width": width, "height": 844})
                contexts.append((role, context))
                use_loopback_requests(context)
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
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
                page.goto(f"http://friends.localhost:8177/__local/login/{role}")
                page.get_by_role("link", name="Open session", exact=True).click()
                # Wait for the mounted UI's first poll before interacting with
                # SSR-visible controls, including while Vite warms its modules.
                page.wait_for_event(
                    "response",
                    predicate=lambda response: response.url.endswith("/session/state"),
                    timeout=10000,
                )
                pages.append(page)
            dm, a, b = pages
            dm.bring_to_front()
            if dm.get_by_role("button", name="Start session", exact=True).count():
                dm.get_by_role("button", name="Start session", exact=True).click()
            expect(dm.get_by_label("Set the scene", exact=True)).to_be_visible()
            for viewer in (a, b):
                expect(viewer.get_by_text("active", exact=True)).to_be_visible(
                    timeout=6000
                )
            expect(
                a.get_by_role("heading", name="The story begins here.", exact=True)
            ).to_be_visible()
            for role, viewer in (("dm", dm), ("a", a), ("b", b)):
                viewer.screenshot(
                    path=str(args.output / f"{role}-empty-feed.png"), full_page=True
                )
            stamp = str(time.time_ns())
            scene = f"A stranger leaves a sealed letter at your table. ({stamp})"
            dm.get_by_label("Set the scene", exact=True).fill(scene)
            dm.get_by_label("Send to").select_option("table")
            held_write = []

            def hold_first_write(route):
                if route.request.method == "POST":
                    held_write.append(route)
                else:
                    route.continue_()

            dm.route("**/session/state", hold_first_write)
            dm.get_by_role("button", name="Send", exact=True).click()
            expect(
                dm.get_by_role("button", name="Sending…", exact=True)
            ).to_be_disabled()
            dm.screenshot(path=str(args.output / "dm-sending.png"), full_page=True)
            assert len(held_write) == 1
            next_draft = "A new scene drafted while the previous message sends"
            dm.get_by_label("Set the scene", exact=True).fill(next_draft)
            held_write[0].continue_()
            dm.unroute("**/session/state", hold_first_write)
            expect(dm.get_by_role("button", name="Send", exact=True)).to_be_visible()
            report["checks"].append(
                "Empty feeds explain the next step; an in-flight write shows a disabled Sending control"
            )
            expect(dm.get_by_label("Set the scene", exact=True)).to_have_value(
                next_draft
            )
            dm.get_by_label("Set the scene", exact=True).fill("")
            expect(
                dm.get_by_role("button", name="Pin to notes", exact=True)
            ).to_have_count(0)
            report["checks"].append(
                "A successful in-flight write preserves the next draft; DM has no unsupported private pin control"
            )
            a.bring_to_front()
            start = time.monotonic()
            expect(a.get_by_text(scene, exact=True)).to_be_visible(timeout=5000)
            report["timings"]["foreground_catchup_seconds"] = time.monotonic() - start
            assert report["timings"]["foreground_catchup_seconds"] <= 3
            report["checks"].append("Table narration reaches player A")
            # Keep A visible while the DM sends through the real composer.
            # Measure regular polling separately from foreground recovery.
            a.get_by_label("What do you do?", exact=True).focus()
            a.keyboard.press("Tab")
            expect(a.get_by_label("Send to", exact=True)).to_be_focused()
            a.keyboard.press("Shift+Tab")
            expect(a.get_by_label("What do you do?", exact=True)).to_be_focused()
            report["checks"].append(
                "Keyboard Tab and Shift+Tab move between the composer and audience selector"
            )
            assert a.evaluate("document.visibilityState") == "visible"
            live_scene = f"The lantern flickers twice. ({stamp})"
            dm.get_by_label("Set the scene", exact=True).fill(live_scene)
            sent = time.monotonic()
            dm.get_by_role("button", name="Send", exact=True).click()
            expect(a.get_by_text(live_scene, exact=True)).to_be_visible(timeout=5000)
            report["timings"]["visible_tab_propagation_seconds"] = (
                time.monotonic() - sent
            )
            assert report["timings"]["visible_tab_propagation_seconds"] <= 3
            assert a.evaluate("document.visibilityState") == "visible"
            expect(a.get_by_label("What do you do?", exact=True)).to_be_focused()
            expect(a.get_by_text(live_scene, exact=True)).to_have_count(1)
            report["checks"].append(
                "Visible-tab polling delivers narration within three seconds without duplicate entries or stealing composer focus"
            )
            # Exercise the DM editor through the BFF and normal backend authority.
            dm.bring_to_front()
            reveal_start = time.monotonic()
            open_reveal(dm)
            dm.get_by_label("Find knowledge").fill("Mara")
            dm.get_by_role("button", name="Search knowledge", exact=True).click()
            dm.get_by_role("button", name="Mara, the innkeeper npc", exact=True).click()
            dm.get_by_label("Elowen", exact=True).check()
            dm.get_by_label("Knowledge scope").select_option("partial")
            dm.get_by_label("occupation", exact=True).check()
            dm.get_by_label("Detail to share").fill("Mara recognizes the seal.")
            dm.get_by_role("button", name="Preview knowledge", exact=True).click()
            report["timings"]["reveal_interaction_seconds"] = (
                time.monotonic() - reveal_start
            )
            assert report["timings"]["reveal_interaction_seconds"] < 15
            reveal_sent = time.monotonic()
            dm.get_by_role("button", name="Share knowledge", exact=True).click()
            expect(dm.get_by_text("Knowledge shared.", exact=True)).to_be_visible()
            a.bring_to_front()
            expect(
                a.get_by_text("Mara recognizes the seal.", exact=False)
            ).to_be_visible(timeout=5000)
            report["timings"]["reveal_propagation_seconds"] = (
                time.monotonic() - reveal_sent
            )
            assert report["timings"]["reveal_propagation_seconds"] < 3
            a_payload = state_text(a)
            assert "DM_ONLY_INNKEEPER_SECRET" not in a_payload
            assert "Mara recognizes the seal." not in state_text(b)
            report["checks"].append(
                "Partial grant reveal reaches only its recipient without the DM canary"
            )
            a.get_by_label("What do you do?", exact=True).fill(
                "A draft while checking knowledge"
            )
            a.get_by_role(
                "button", name="Explore Mara, the innkeeper", exact=True
            ).click()
            drawer = a.get_by_role("region", name="Knowledge detail")
            expect(
                drawer.get_by_text("Keeps the Lantern Inn", exact=True)
            ).to_be_visible()
            assert "DM_ONLY_INNKEEPER_SECRET" not in drawer.inner_text()
            entity_id = next(
                (row["body"].get("reveals", [row["body"]])[0])["entity_id"]
                for row in a.request.get(a.url + "/state").json()["events"]
                if row["kind"] == "reveal"
            )
            entity_page = a.url.rsplit("/", 1)[0] + "/entities/" + entity_id
            page_response = a.request.get(entity_page)
            assert page_response.status == 200
            assert "Keeps the Lantern Inn" in page_response.text()
            assert "DM_ONLY_INNKEEPER_SECRET" not in page_response.text()
            assert b.request.get(entity_page).status == 404
            a.screenshot(
                path=str(args.output / "partial-knowledge.png"), full_page=True
            )
            a.get_by_role("button", name="Close knowledge", exact=True).click()
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                "A draft while checking knowledge"
            )
            a.get_by_label("What do you do?", exact=True).fill("")
            dm.bring_to_front()
            dm.get_by_role("button", name="Mara, the innkeeper npc", exact=True).click()
            dm.get_by_label("Bram", exact=True).check()
            dm.get_by_label("Knowledge scope").select_option("full")
            dm.get_by_role("button", name="Preview knowledge", exact=True).click()
            dm.get_by_role("button", name="Share knowledge", exact=True).click()
            expect(dm.get_by_text("Knowledge shared.", exact=True)).to_be_visible()
            b.bring_to_front()
            b.get_by_role(
                "button", name="Explore Mara, the innkeeper", exact=True
            ).click()
            expect(
                b.get_by_role("region", name="Knowledge detail").get_by_text(
                    "DM_ONLY_INNKEEPER_SECRET", exact=True
                )
            ).to_be_visible(timeout=5000)
            b.screenshot(path=str(args.output / "full-knowledge.png"), full_page=True)
            b.get_by_role("button", name="Close knowledge", exact=True).click()
            # B holds the full scope now; A's card and payload must stay partial.
            a.bring_to_front()
            expect(
                a.get_by_text("DM_ONLY_INNKEEPER_SECRET", exact=False)
            ).to_have_count(0)
            assert "DM_ONLY_INNKEEPER_SECRET" not in state_text(a)
            dm.bring_to_front()
            dm.get_by_role("button", name="Mara, the innkeeper npc", exact=True).click()
            dm.get_by_role("button", name="Retract from Bram", exact=True).click()
            expect(dm.get_by_text("Knowledge retracted.", exact=True)).to_be_visible()
            close_reveal(dm)
            report["checks"].append(
                "Field preview and knowledge drawer enforce partial A/full B scopes and preserve the session draft"
            )
            dm.bring_to_front()
            secret = f"A_SECRET_{stamp}: You recognize your mentor's seal."
            dm.get_by_label("Send to").select_option(label="Elowen and DM")
            dm.get_by_label("Set the scene", exact=True).fill(secret)
            dm.get_by_role("button", name="Send", exact=True).click()
            a.bring_to_front()
            expect(a.get_by_text(secret, exact=True)).to_be_visible(timeout=5000)
            b.bring_to_front()
            expect(b.get_by_text(scene, exact=True)).to_be_visible(timeout=5000)
            expect(b.get_by_text(secret, exact=True)).to_have_count(0)
            payload = state_text(b)
            assert secret not in payload, "Player A secret leaked into B's response"
            report["checks"].append(
                "Targeted message appears for A and is absent from B's page and payload"
            )
            a.bring_to_front()
            action = f"A_WHISPER_{stamp}: I quietly check the letter for a trap."
            a.get_by_label("What do you do?", exact=True).fill(action)
            a.get_by_label("Send to").select_option("dm")
            a.get_by_role("button", name="Send", exact=True).click()
            expect(a.get_by_text(action, exact=True)).to_be_visible(timeout=5000)
            dm.bring_to_front()
            expect(dm.get_by_text(action, exact=True)).to_be_visible(timeout=5000)
            b.bring_to_front()
            b.reload()
            expect(b.get_by_text(action, exact=True)).to_have_count(0)
            assert action not in state_text(b)
            expect(
                b.get_by_role("button", name="End session", exact=True)
            ).to_have_count(0)
            report["checks"].append(
                "Private player action reaches DM and sender, never B after reload"
            )
            dm.bring_to_front()
            dm.get_by_role(
                "button", name="Reply privately to Elowen", exact=True
            ).click()
            reply = f"DM_REPLY_{stamp}: The seal is intact. There is no trap."
            dm.get_by_label("Set the scene", exact=True).fill(reply)
            dm.get_by_role("button", name="Send", exact=True).click()
            a.bring_to_front()
            expect(a.get_by_text(reply, exact=True)).to_be_visible(timeout=5000)
            expect(a.get_by_text("Resolved", exact=True)).to_be_visible()
            assert reply not in state_text(b)
            report["checks"].append(
                "DM replies privately to the sender and resolves the waiting action"
            )
            player_state = b.request.get(b.url + "/state").json()
            denied = b.request.post(
                b.url + "/state",
                data={
                    "operation": "status",
                    "sessionId": player_state["session"]["id"],
                    "status": "ended",
                },
            )
            assert denied.status == 400, "Player could end the session"
            assert (
                b.request.get(b.url + "/state").json()["session"]["status"] == "active"
            )
            denied = b.request.post(
                b.url + "/state",
                data={
                    "operation": "post",
                    "sessionId": player_state["session"]["id"],
                    "kind": "narration",
                    "audience": "table",
                    "text": "Unauthorized narration",
                },
            )
            assert denied.status == 400, "Player could post DM narration"
            report["checks"].append(
                "Backend rejects player attempts to end the session or narrate as DM"
            )

            # Rolls originate on the server and follow the same audience policy.
            a.bring_to_front()
            a.get_by_text("Roll dice", exact=True).click()
            a.get_by_label("Sheet roll type").select_option("saves")
            a.get_by_role("radio", name="Advantage", exact=True).check()
            a.get_by_role("button", name="Strength save", exact=True).click()
            expect(a.get_by_text("Strength save", exact=False).last).to_be_visible()
            sheet_state = a.request.get(a.url + "/state").json()
            sheet_roll = next(
                row
                for row in sheet_state["events"]
                if row["body"].get("label") == "Strength save"
            )
            approved_bonus = sheet_state["characters"][0]["approved"][
                "saving_throw_bonuses"
            ]["strength"]
            assert sheet_roll["body"]["modifier"] == approved_bonus
            assert len(sheet_roll["body"]["rolls"]) == 2
            assert sheet_roll["body"]["kept"] == [max(sheet_roll["body"]["rolls"])]
            report["checks"].append(
                "Quick saving throws use the approved sheet bonus and advantage"
            )
            a.get_by_label("Dice formula", exact=True).fill("d20adv+3")
            roll_secret = f"A_ROLL_{stamp}"
            a.get_by_label("Roll label", exact=True).fill(roll_secret)
            a.get_by_label("Roll visibility", exact=True).select_option("dm")
            a.get_by_role("button", name="Roll", exact=True).click()
            expect(a.get_by_text(roll_secret, exact=False)).to_be_visible(timeout=5000)
            dm.bring_to_front()
            expect(dm.get_by_text(roll_secret, exact=False)).to_be_visible(timeout=5000)
            b.bring_to_front()
            assert roll_secret not in state_text(b)
            b.get_by_text("Roll dice", exact=True).click()
            b.get_by_label("Dice formula", exact=True).fill("2d6+3")
            public_label = f"B_PUBLIC_ROLL_{stamp}"
            b.get_by_label("Roll label", exact=True).fill(public_label)
            b.get_by_role("button", name="Roll", exact=True).click()
            expect(b.get_by_text(public_label, exact=False)).to_be_visible(timeout=5000)
            a.bring_to_front()
            expect(a.get_by_text(public_label, exact=False)).to_be_visible(timeout=5000)
            rows = a.request.get(a.url + "/state").json()["events"]
            rolled = next(
                row for row in rows if row["body"].get("label") == public_label
            )
            assert 5 <= rolled["body"]["total"] <= 15
            assert rolled["body"]["total"] == sum(rolled["body"]["kept"]) + 3
            report["checks"].append(
                "Server dice supports advantage, modifiers, public rolls and private rolls"
            )

            # A transient polling failure must retain the unsent draft.
            a.bring_to_front()
            a.get_by_label("What do you do?", exact=True).fill(
                "A draft I have not sent yet"
            )
            delayed_polls = []

            def delay_poll(route):
                if route.request.method == "GET" and not delayed_polls:
                    response = fetch_loopback_route(route)
                    time.sleep(1)
                    delayed_polls.append(route.request.url)
                    route.fulfill(response=response)
                else:
                    route.continue_()

            a.route("**/session/state", delay_poll)
            # A locator expectation waits while Playwright services the delayed route.
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                "A draft I have not sent yet"
            )
            a.wait_for_timeout(3500)
            assert len(delayed_polls) == 1
            expect(a.get_by_role("status", name="Session connection")).to_have_text(
                "Live"
            )
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                "A draft I have not sent yet"
            )
            a.unroute("**/session/state", delay_poll)
            report["checks"].append(
                "A delayed successful poll preserves the unsent draft and returns to a live feed"
            )
            a.route("**/session/state", lambda route: route.abort())
            expect(a.get_by_role("status", name="Session connection")).to_have_text(
                "Reconnecting", timeout=6000
            )
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                "A draft I have not sent yet"
            )
            a.screenshot(
                path=str(args.output / "player-reconnecting.png"), full_page=True
            )
            a.unroute("**/session/state")
            expect(a.get_by_role("status", name="Session connection")).to_have_text(
                "Live", timeout=6000
            )
            report["checks"].append("Polling recovers and preserves the unsent draft")
            retry_text = f"RETRY_{stamp}: I inspect the door."
            a.get_by_label("Send to").select_option("table")
            a.get_by_label("What do you do?", exact=True).fill(retry_text)

            def lose_write_response(route):
                if route.request.method == "POST":
                    fetch_loopback_route(
                        route
                    )  # Commit normally, then lose the response.
                    route.abort()
                else:
                    route.continue_()

            a.route("**/session/state", lose_write_response)
            a.get_by_role("button", name="Send", exact=True).click()
            expect(a.get_by_role("alert")).to_contain_text("Your draft is saved")
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                retry_text
            )
            a.screenshot(
                path=str(args.output / "player-send-error.png"), full_page=True
            )
            a.unroute("**/session/state", lose_write_response)
            a.get_by_role("button", name="Send", exact=True).click()
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value("")
            retry_events = a.request.get(a.url + "/state").json()["events"]
            assert (
                sum(
                    row["body"].get("text") == retry_text
                    for row in retry_events
                    if row["body"]
                )
                == 1
            )
            expect(a.get_by_text(retry_text, exact=True)).to_have_count(1)
            report["checks"].append(
                "Lost send response retains an actionable error and draft; retry creates exactly one event"
            )
            dm.bring_to_front()
            dm.set_viewport_size({"width": 390, "height": 844})
            dm.get_by_role("button", name="Pause session", exact=True).click()
            expect(
                dm.get_by_role("button", name="Resume session", exact=True)
            ).to_be_visible()
            dm.get_by_role("button", name="Resume session", exact=True).click()
            mobile_scene = f"PHONE_DM_{stamp}: The lantern flickers."
            dm.get_by_label("Set the scene", exact=True).fill(mobile_scene)
            dm.get_by_label("Send to").select_option("table")
            dm.get_by_role("button", name="Send", exact=True).click()
            expect(dm.get_by_text(mobile_scene, exact=True)).to_be_visible()
            assert_fits(dm)
            dm.screenshot(
                path=str(args.output / "dm-phone-controls.png"), full_page=True
            )
            dm.set_viewport_size({"width": 1280, "height": 844})
            report["checks"].append(
                "DM phone controls pause, resume and narrate without horizontal overflow"
            )
            for role, page in zip(["dm", "elowen", "bram"], pages):
                page.bring_to_front()
                assert_fits(page)
                page.screenshot(path=str(args.output / f"{role}.png"), full_page=True)
            report["checks"].append(
                "DM desktop and player phone views have no horizontal overflow"
            )
            dm.bring_to_front()
            open_reveal(dm)
            dm.get_by_role("button", name="Mara, the innkeeper npc", exact=True).click()
            dm.get_by_role("button", name="Retract from Elowen", exact=True).click()
            expect(dm.get_by_text("Knowledge retracted.", exact=True)).to_be_visible()
            a.bring_to_front()
            expect(
                a.get_by_text("Knowledge retracted: Mara, the innkeeper.", exact=True)
            ).to_be_visible(timeout=5000)
            assert "Mara recognizes the seal." not in state_text(a)
            dm.bring_to_front()
            dm.get_by_role("button", name="Mara, the innkeeper npc", exact=True).click()
            dm.get_by_label("Knowledge scope").select_option("partial")
            dm.get_by_label("Elowen", exact=True).check()
            dm.get_by_label("Bram", exact=True).check()
            dm.get_by_label("Detail to share").fill("Mara opens the inn to the party.")
            dm.get_by_role("button", name="Preview knowledge", exact=True).click()
            dm.get_by_role("button", name="Share knowledge", exact=True).click()
            expect(dm.get_by_text("Knowledge shared.", exact=True)).to_be_visible()
            close_reveal(dm)
            for player in (a, b):
                player.bring_to_front()
                expect(
                    player.get_by_text("Mara opens the inn to the party.", exact=False)
                ).to_be_visible(timeout=5000)
            report["checks"].append(
                "DM editor retracts old knowledge and atomically shares with both players"
            )
            # Private notes stay private until the author explicitly opts in.
            a.bring_to_front()
            a.get_by_role("button", name="Notes", exact=True).click()
            note_title = f"A_NOTE_{stamp}"
            a.get_by_label("Note title", exact=True).fill(note_title)
            a.get_by_label("Note text", exact=True).fill(
                "**A private thought** about the seal."
            )
            a.get_by_role("button", name="Save note", exact=True).click()
            expect(
                a.get_by_role("heading", name=note_title, exact=True)
            ).to_be_visible()
            assert note_title in state_text(a, "?notes=character")
            for viewer in (dm, b):
                assert note_title not in state_text(viewer, "?notes=character")
            a.get_by_role("button", name=f"Edit {note_title}", exact=True).click()
            a.get_by_label("Share this note with the DM").check()
            a.get_by_role("button", name="Save note", exact=True).click()
            dm.bring_to_front()
            dm.get_by_role("button", name="Notes", exact=True).click()
            expect(
                dm.get_by_role("heading", name=note_title, exact=True)
            ).to_be_visible(timeout=6000)
            assert note_title in state_text(dm, "?notes=character")
            assert note_title not in state_text(b, "?notes=character")
            a.bring_to_front()
            a.get_by_role("button", name="Party notes", exact=True).click()
            party_title = f"PARTY_NOTE_{stamp}"
            a.get_by_label("Note title", exact=True).fill(party_title)
            a.get_by_label("Note text", exact=True).fill(
                "The inn is open to our party."
            )
            held_write.clear()
            a.route("**/session/state", hold_first_write)
            a.get_by_role("button", name="Save note", exact=True).click()
            expect(a.get_by_label("Note title", exact=True)).to_be_disabled()
            expect(a.get_by_label("Note text", exact=True)).to_be_disabled()
            expect(
                a.get_by_role("button", name="My notes", exact=True)
            ).to_be_disabled()
            assert len(held_write) == 1
            held_write[0].continue_()
            a.unroute("**/session/state", hold_first_write)
            expect(a.get_by_label("Note title", exact=True)).to_be_enabled()
            report["checks"].append(
                "An in-flight note save locks its fields and audience until completion"
            )
            expect(
                a.get_by_role("heading", name=party_title, exact=True)
            ).to_be_visible()
            for viewer in (dm, b):
                viewer.bring_to_front()
                viewer.get_by_role(
                    "button", name="Notes", exact=True
                ).click() if viewer is b else None
                viewer.get_by_role("button", name="Party notes", exact=True).click()
                expect(
                    viewer.get_by_role("heading", name=party_title, exact=True)
                ).to_be_visible(timeout=6000)
            assert party_title in state_text(b, "?notes=party")
            a.bring_to_front()
            a.screenshot(path=str(args.output / "party-notes.png"), full_page=True)
            a.get_by_role("button", name=f"Delete {party_title}", exact=True).click()
            expect(
                a.get_by_role("heading", name=party_title, exact=True)
            ).to_have_count(0)
            assert party_title not in state_text(b, "?notes=party")
            a.get_by_role("button", name="My notes", exact=True).click()
            a.get_by_label("Note title", exact=True).fill("An unsaved note")
            a.get_by_role("button", name="Story", exact=True).click()
            a.get_by_role("button", name="Notes", exact=True).click()
            expect(a.get_by_label("Note title", exact=True)).to_have_value(
                "An unsaved note"
            )
            a.screenshot(path=str(args.output / "private-notes.png"), full_page=True)
            for viewer in (a, b, dm):
                viewer.get_by_role("button", name="Story", exact=True).click()
            a.bring_to_front()
            a.get_by_role(
                "button", name="Pin Mara, the innkeeper to notes", exact=True
            ).click()
            expect(
                a.get_by_role(
                    "heading", name="Mara, the innkeeper · Pinned", exact=True
                )
            ).to_be_visible()
            expect(
                a.get_by_role("region", name="Campaign notes").get_by_text(
                    "Mara opens the inn to the party.", exact=False
                )
            ).to_be_visible()
            a_notes = state_text(a, "?notes=character")
            assert "Mara opens the inn to the party." in a_notes
            assert "DM_ONLY_INNKEEPER_SECRET" not in a_notes
            dm_notes = state_text(dm, "?notes=character")
            assert note_title in dm_notes and "Mara, the innkeeper" not in dm_notes
            a.screenshot(path=str(args.output / "pinned-notes.png"), full_page=True)
            a.get_by_role("region", name="Campaign notes").get_by_role(
                "button", name="Mara, the innkeeper", exact=True
            ).click()
            expect(
                a.get_by_role("region", name="Knowledge detail").get_by_text(
                    "Mara opens the inn to the party.", exact=True
                )
            ).to_be_visible()
            a.get_by_role("button", name="Close knowledge", exact=True).click()
            a.get_by_role("button", name="Source event", exact=True).click()
            expect(a.get_by_label("What do you do?", exact=True)).to_be_visible()
            report["checks"].append(
                "Pin to notes copies visible knowledge, stays private, and links back to its event"
            )
            report["checks"].append(
                "Notes editor enforces private default, DM opt-in, party sharing, deletion and draft retention"
            )
            a.bring_to_front()
            a.get_by_role("button", name="Journal", exact=True).click()
            journal_region = a.get_by_role("region", name="Session journal")
            # The reusable panel dumps the snapshot's revealed details, so the
            # clue sits inside a JSON field rather than a text node of its own.
            expect(
                journal_region.get_by_role("region", name="Learned").get_by_text(
                    "Mara opens the inn to the party.", exact=False
                )
            ).to_be_visible()
            expect(
                journal_region.get_by_role("region", name="Rolls").get_by_text(
                    roll_secret, exact=False
                )
            ).to_be_visible()
            journal_data = a.request.get(a.url + "/state").json()["journal"]
            assert journal_data["mine"]["open_threads"] == []
            assert len(journal_data["mine"]["learned"]) == 1
            assert journal_data["party"]["learned"] == []
            a.screenshot(path=str(args.output / "my-journal.png"), full_page=True)
            journal_region.get_by_role("region", name="Learned").get_by_role(
                "button", name="Explore Mara, the innkeeper", exact=True
            ).click()
            expect(
                a.get_by_role("region", name="Knowledge detail").get_by_text(
                    "Mara opens the inn to the party.", exact=True
                )
            ).to_be_visible()
            a.get_by_role("button", name="Close knowledge", exact=True).click()
            journal_region.get_by_role("button", name="Party", exact=True).click()
            expect(
                journal_region.get_by_role("button", name="Party", exact=True)
            ).to_have_attribute("aria-pressed", "true")
            expect(journal_region.get_by_text(roll_secret, exact=False)).to_have_count(
                0
            )
            a.screenshot(path=str(args.output / "party-journal.png"), full_page=True)
            b_journal = b.request.get(b.url + "/state").json()["journal"]
            assert roll_secret not in json.dumps(b_journal)
            assert secret not in json.dumps(b_journal)
            a.get_by_role("button", name="Story", exact=True).click()
            report["checks"].append(
                "Journal uses latest visible knowledge, own rolls and resolved threads; party journal excludes private events"
            )
            dm.bring_to_front()
            dm.get_by_role("button", name="Pause session", exact=True).click()
            matrix = dm.context.new_page()
            await_url = dm.url.rsplit("/", 1)[0] + "/grants"
            matrix.goto(await_url)
            expect(
                matrix.get_by_role(
                    "heading", name="Knowledge at your table", exact=True
                )
            ).to_be_visible()
            assert a.request.get(await_url).status == 403
            matrix.get_by_label("Find knowledge", exact=True).fill("Mara")
            matrix.get_by_role(
                "button", name="Edit Mara, the innkeeper for Elowen", exact=True
            ).click()
            expect(matrix.get_by_label("Detail to share", exact=True)).to_have_value(
                "Mara opens the inn to the party."
            )
            matrix.get_by_label("Detail to share", exact=True).fill(
                "Mara remembers our promise."
            )
            matrix.get_by_role("button", name="Preview knowledge", exact=True).click()
            matrix.screenshot(
                path=str(args.output / "grants-matrix.png"), full_page=True
            )
            matrix.get_by_role("button", name="Confirm knowledge", exact=True).click()
            expect(
                matrix.get_by_role("region", name="Edit character knowledge")
            ).to_have_count(0)
            a.bring_to_front()
            expect(
                a.get_by_role("region", name="Session feed").get_by_text(
                    "Mara remembers our promise.", exact=False
                )
            ).to_be_visible(timeout=5000)
            a_state = a.request.get(a.url + "/state").json()
            assert (
                a_state["journal"]["mine"]["learned"][0]["entity"]["revealed_details"][
                    "clue"
                ]
                == "Mara remembers our promise."
            )
            assert "Mara opens the inn to the party." not in json.dumps(a_state)
            assert "Mara remembers our promise." not in state_text(b)
            matrix.bring_to_front()
            matrix.get_by_role(
                "button", name="Edit Mara, the innkeeper for Elowen", exact=True
            ).click()
            matrix.get_by_label("Knowledge scope", exact=True).select_option(
                "name_only"
            )
            matrix.get_by_role("button", name="Preview knowledge", exact=True).click()
            matrix.get_by_role("button", name="Confirm knowledge", exact=True).click()
            expect(
                matrix.get_by_role("region", name="Edit character knowledge")
            ).to_have_count(0)
            assert "Mara remembers our promise." not in state_text(a)
            matrix.get_by_role(
                "button", name="Edit Mara, the innkeeper for Elowen", exact=True
            ).click()
            matrix.get_by_role("button", name="Retract knowledge", exact=True).click()
            expect(
                matrix.get_by_role("region", name="Edit character knowledge")
            ).to_have_count(0)
            report["checks"].append(
                "DM grants matrix edits partial details, reduces scope and retracts; players cannot open it"
            )
            matrix.close()
            dm.bring_to_front()
            expect(
                dm.get_by_role("button", name="Resume session", exact=True)
            ).to_be_visible()
            dm.get_by_role("button", name="Resume session", exact=True).click()
            expect(
                dm.get_by_role("button", name="Pause session", exact=True)
            ).to_be_visible()
            # Seed a grouped reveal through the real batch API. Inspect and
            # pin it through the player UI, then retract one item as the DM.
            current = dm.request.get(dm.url + "/state").json()
            campaign_id = current["campaign"]["id"]
            elowen_id = next(
                pc["id"]
                for pc in current["characters"]
                if pc["character_name"] == "Elowen"
            )
            batch_entities = [
                dm.request.get(dm.url + f"/state?q={name}").json()["items"][0]
                for name in ("Tessa", "Orrin")
            ]
            token = next(
                cookie["value"]
                for cookie in dm.context.cookies()
                if cookie["name"] == "grimoire-id-token"
            )
            a.bring_to_front()
            a.get_by_role("button", name="Story", exact=True).focus()
            a.evaluate("window.scrollTo(0, 0)")
            reading_position = a.evaluate("window.scrollY")
            batch = dm.request.post(
                f"http://friends.localhost:8177/api/grimoire/campaigns/{campaign_id}/grants/bulk",
                headers={"x-grimoire-token": token},
                data={
                    "grants": [
                        {
                            "entity_id": item["id"],
                            "player_character_id": elowen_id,
                            "grant_scope": "partial",
                            "revealed_details": {"clue": f"BATCH_{item['name']}"},
                        }
                        for item in batch_entities
                    ]
                },
            )
            assert batch.ok, batch.status
            a.bring_to_front()
            expect(a.get_by_text("BATCH_Mapmaker Tessa", exact=False)).to_be_visible(
                timeout=5000
            )
            expect(a.get_by_text("BATCH_Ferryman Orrin", exact=False)).to_be_visible()
            grouped = [
                row
                for row in a.request.get(a.url + "/state").json()["events"]
                if row["body"] and "reveals" in row["body"]
            ]
            assert len(grouped) == 1 and len(grouped[0]["body"]["reveals"]) == 2
            assert "DM_ONLY_BATCH_SECRET" not in json.dumps(grouped)
            assert "BATCH_Mapmaker Tessa" not in state_text(b)
            assert abs(a.evaluate("window.scrollY") - reading_position) <= 2
            expect(a.get_by_role("button", name="Story", exact=True)).to_be_focused()
            report["checks"].append(
                "Incoming grouped knowledge preserves the reader's scroll position and keyboard focus"
            )
            a.screenshot(path=str(args.output / "grouped-reveal.png"), full_page=True)
            a.get_by_role(
                "button", name="Pin Mapmaker Tessa, Ferryman Orrin to notes", exact=True
            ).click()
            expect(
                a.get_by_role("region", name="Campaign notes").get_by_text(
                    "BATCH_Mapmaker Tessa", exact=False
                )
            ).to_be_visible()
            expect(
                a.get_by_role("region", name="Campaign notes").get_by_text(
                    "BATCH_Ferryman Orrin", exact=False
                )
            ).to_be_visible()
            a.get_by_role("button", name="Story", exact=True).click()
            dm.bring_to_front()
            open_reveal(dm)
            dm.get_by_label("Find knowledge").fill("Tessa")
            dm.get_by_role("button", name="Search knowledge", exact=True).click()
            dm.get_by_role("button", name="Mapmaker Tessa npc", exact=True).click()
            dm.get_by_role("button", name="Retract from Elowen", exact=True).click()
            close_reveal(dm)
            a.bring_to_front()
            expect(
                a.get_by_role("region", name="Session feed").get_by_text(
                    "BATCH_Mapmaker Tessa", exact=False
                )
            ).to_have_count(0, timeout=5000)
            expect(
                a.get_by_role("region", name="Session feed").get_by_text(
                    "BATCH_Ferryman Orrin", exact=False
                )
            ).to_be_visible()
            remaining = next(
                row
                for row in a.request.get(a.url + "/state").json()["events"]
                if row["id"] == grouped[0]["id"]
            )
            assert len(remaining["body"]["reveals"]) == 1
            a.screenshot(
                path=str(args.output / "grouped-reveal-retracted.png"), full_page=True
            )
            report["checks"].append(
                "Grouped reveal renders and pins both items; retracting one hides its projection while preserving the other"
            )
            # Combat: both PCs and a hidden monster. Each player sees only their
            # own turn, and the monster stays masked in pages and payloads.
            bram_id = next(
                pc["id"]
                for pc in current["characters"]
                if pc["character_name"] == "Bram"
            )
            monster = f"HIDDEN_MONSTER_{stamp}"
            dm.bring_to_front()
            dm.get_by_text("Initiative order", exact=True).click()
            order = dm.get_by_role("region", name="Initiative order")
            for label, character, score in (
                ("", "Bram", "12"),
                (monster, "", "15"),
                ("", "Elowen", "18"),
            ):
                order.get_by_role("button", name="Add row", exact=True).click()
                row = order.get_by_role("listitem").last
                if character:
                    row.get_by_label("Character").select_option(label=character)
                else:
                    row.get_by_label("Name", exact=True).fill(label)
                    row.get_by_label("Hidden", exact=True).check()
                row.get_by_label("Initiative", exact=True).fill(score)
            order.get_by_role("button", name="Sort by initiative", exact=True).click()
            order.get_by_role("button", name="Save order", exact=True).click()
            expect(
                order.get_by_text("Initiative order saved.", exact=True)
            ).to_be_visible()
            dm_turn = dm.get_by_role("region", name="Turn order")
            expect(dm_turn.get_by_text(monster, exact=True)).to_be_visible()
            dm.screenshot(path=str(args.output / "dm-initiative.png"), full_page=True)

            def turn_banner(viewer):
                return viewer.get_by_role("status").filter(has_text="Your turn")

            def active_turn(viewer):
                return viewer.get_by_role("region", name="Turn order").locator(
                    "[aria-current=step]"
                )

            a.bring_to_front()
            expect(turn_banner(a)).to_be_visible(timeout=5000)
            expect(active_turn(a)).to_contain_text("Elowen")
            b.bring_to_front()
            expect(active_turn(b)).to_contain_text("Elowen", timeout=5000)
            expect(turn_banner(b)).to_have_count(0)
            dm.bring_to_front()
            order.get_by_role("button", name="Next turn", exact=True).click()
            expect(active_turn(dm)).to_contain_text(monster)
            for viewer in (a, b):
                viewer.bring_to_front()
                expect(active_turn(viewer)).to_contain_text("???", timeout=5000)
                expect(turn_banner(viewer)).to_have_count(0)
            dm.bring_to_front()
            order.get_by_role("button", name="Next turn", exact=True).click()
            expect(active_turn(dm)).to_contain_text("Bram")
            b.bring_to_front()
            expect(turn_banner(b)).to_be_visible(timeout=5000)
            assert_fits(b)
            b.screenshot(path=str(args.output / "bram-your-turn.png"), full_page=True)
            a.bring_to_front()
            expect(active_turn(a)).to_contain_text("Bram", timeout=5000)
            expect(turn_banner(a)).to_have_count(0)
            for viewer, own in ((a, elowen_id), (b, bram_id)):
                payload = viewer.request.get(viewer.url + "/state")
                assert monster not in payload.text()
                entries = payload.json()["initiative"]["entries"]
                assert [entry["label"] for entry in entries] == [
                    "Elowen",
                    "???",
                    "Bram",
                ]
                assert {entry["player_character_id"] for entry in entries} == {
                    own,
                    None,
                }
                expect(viewer.get_by_text(monster)).to_have_count(0)
            dm.bring_to_front()
            dm.once("dialog", lambda dialog: dialog.accept())
            order.get_by_role("button", name="End encounter", exact=True).click()
            expect(dm_turn).to_have_count(0)
            for viewer in (a, b):
                viewer.bring_to_front()
                expect(viewer.get_by_role("region", name="Turn order")).to_have_count(
                    0, timeout=5000
                )
                expect(
                    viewer.get_by_text("The encounter has ended.", exact=True)
                ).to_be_visible()
            dm.bring_to_front()
            dm.get_by_text("Initiative order", exact=True).click()
            report["checks"].append(
                "Initiative orders both PCs and a hidden monster; only the active player sees their turn banner, the monster stays masked in pages and payloads, and the DM ends the encounter"
            )
            # Handouts: one for the table and one for Elowen only.
            dm.get_by_text("Send a handout", exact=True).click()
            composer = dm.get_by_role("form", name="Send a handout")
            public_title = f"Wanted poster {stamp}"
            private_title = f"ELOWEN_LETTER_{stamp}"
            private_text = f"Meet me at the mill at midnight, {stamp}."
            for title, text, recipient in (
                (public_title, "**Reward:** fifty gold for the smuggler.", None),
                (private_title, private_text, "Elowen"),
            ):
                composer.get_by_label("Handout title", exact=True).fill(title)
                composer.get_by_label("Handout text", exact=True).fill(text)
                if recipient:
                    composer.get_by_label("Everyone at the table", exact=True).uncheck()
                    composer.get_by_label(recipient, exact=True).check()
                composer.get_by_role("button", name="Send handout", exact=True).click()
                expect(
                    composer.get_by_label("Handout title", exact=True)
                ).to_have_value("")
            dm.get_by_text("Send a handout", exact=True).click()
            a.bring_to_front()
            for title in (public_title, private_title):
                expect(a.get_by_role("heading", name=title, exact=True)).to_be_visible(
                    timeout=5000
                )
            expect(a.get_by_text(private_text, exact=True)).to_be_visible()
            assert_fits(a)
            a.screenshot(path=str(args.output / "elowen-handouts.png"), full_page=True)
            b.bring_to_front()
            expect(
                b.get_by_role("heading", name=public_title, exact=True)
            ).to_be_visible(timeout=5000)
            expect(
                b.get_by_role("heading", name=private_title, exact=True)
            ).to_have_count(0)
            b_payload = state_text(b)
            assert private_title not in b_payload and private_text not in b_payload
            a.bring_to_front()
            a.get_by_role(
                "button", name=f"Pin {private_title} to my notes", exact=True
            ).click()
            expect(
                a.get_by_role("heading", name=f"{private_title} · Pinned", exact=True)
            ).to_be_visible()
            expect(
                a.get_by_role("region", name="Campaign notes").get_by_text(
                    private_text, exact=False
                )
            ).to_be_visible()
            assert_fits(a)
            assert private_title in state_text(a, "?notes=character")
            for viewer in (dm, b):
                assert private_title not in state_text(viewer, "?notes=character")
            a.get_by_role("button", name="Story", exact=True).click()
            report["checks"].append(
                "Handouts reach the table and one PC; Bram never receives Elowen's handout in page or payload, and Elowen pins it to private notes"
            )
            # Inventory: Give item turns the table handout into a party item.
            dm.bring_to_front()
            dm.get_by_role(
                "button", name=f"Give item from {public_title}", exact=True
            ).click()
            give = dm.locator("form").filter(
                has=dm.get_by_role("heading", name="Give item", exact=True)
            )
            expect(give.get_by_label("Item name", exact=True)).to_have_value(
                public_title
            )
            expect(give.get_by_label("Give to")).to_have_value("party")
            give.get_by_role("button", name=f"Give {public_title}", exact=True).click()
            dm_pool = dm.get_by_role("region", name="Party pool")
            expect(dm_pool.get_by_role("heading", name=public_title)).to_be_visible()
            hidden_item = f"HIDDEN_ITEM_{stamp}"
            give.get_by_label("Item name", exact=True).fill(hidden_item)
            give.get_by_label("Hidden from party", exact=True).check()
            give.get_by_role("button", name=f"Give {hidden_item}", exact=True).click()
            expect(dm_pool.get_by_role("heading", name=hidden_item)).to_be_visible()
            dm.screenshot(path=str(args.output / "dm-inventory.png"), full_page=True)
            item_ids = {
                item["name"]: item["id"]
                for item in dm.request.get(dm.url + "/state?inventory=items").json()
            }
            public_id, hidden_id = item_ids[public_title], item_ids[hidden_item]
            for viewer in (a, b):
                viewer.bring_to_front()
                viewer.get_by_role("button", name="Inventory", exact=True).click()
                expect(
                    viewer.get_by_role("region", name="Party pool").get_by_role(
                        "heading", name=public_title
                    )
                ).to_be_visible(timeout=6000)
                expect(viewer.get_by_text(hidden_item)).to_have_count(0)
                assert_fits(viewer)
                # Each view must carry the public item, so its absence checks
                # cannot pass on an empty or failed response.
                for query, shown in (
                    ("?inventory=items", (public_title, public_id)),
                    ("?inventory=changes", (public_id,)),
                    ("", (public_title,)),
                ):
                    payload = state_text(viewer, query)
                    assert all(value in payload for value in shown), query
                    assert hidden_item not in payload and hidden_id not in payload
            b.screenshot(path=str(args.output / "bram-inventory.png"), full_page=True)
            for viewer in (a, b, dm):
                viewer.get_by_role("button", name="Story", exact=True).click()
            report["checks"].append(
                "Give item turns a handout into a party-pool item both players see; a hidden party item never reaches player pages or payloads"
            )
            # Knowledge search: run the play embedder on demand, then search
            # as a player. DM-only and other-PC text must stay out of results.
            dm.bring_to_front()
            dm_canary = f"DM_ONLY_SEARCH_{stamp}"
            dm.get_by_label("Send to").select_option("dm")
            dm.get_by_label("Set the scene", exact=True).fill(
                f"{dm_canary}: Mara pays the smugglers."
            )
            dm.get_by_role("button", name="Send", exact=True).click()
            expect(
                dm.get_by_text(f"{dm_canary}: Mara pays the smugglers.", exact=True)
            ).to_be_visible()
            dm.get_by_label("Send to").select_option("table")
            # Bram holds Ilsa's full knowledge only until the retraction below.
            ilsa_secret = "HARBOUR_LEDGER_SECRET"
            drawer = dm.get_by_role("dialog", name="Reveal to your players")

            def choose_ilsa():
                open_reveal(dm)
                drawer.get_by_label("Find knowledge").fill("Ilsa")
                drawer.get_by_role(
                    "button", name="Search knowledge", exact=True
                ).click()
                drawer.get_by_role(
                    "button", name="Harbourmaster Ilsa npc", exact=True
                ).click()

            choose_ilsa()
            drawer.get_by_label("Bram", exact=True).check()
            drawer.get_by_label("Knowledge scope").select_option("full")
            drawer.get_by_role("button", name="Preview knowledge", exact=True).click()
            drawer.get_by_role("button", name="Share knowledge", exact=True).click()
            expect(drawer.get_by_text("Knowledge shared.", exact=True)).to_be_visible()
            close_reveal(dm)
            embedded = dm.request.post(
                "http://friends.localhost:8177/__local/embed-play"
            )
            assert embedded.status == 204, embedded.status

            def search(viewer, query):
                response = viewer.request.get(
                    viewer.url.rsplit("/", 1)[0] + "/knowledge/search",
                    params={"q": query},
                )
                assert response.ok, response.status
                return response.text()

            # Positive controls: the DM note and Elowen's handout are indexed.
            assert dm_canary in search(dm, dm_canary)
            assert private_text in search(a, private_text)
            b.bring_to_front()
            finder = b.get_by_role("region", name="Character knowledge search")

            def find(query):
                """Search through Bram's page and wait for this response to render."""
                finder.get_by_label("Search what your character knows").fill(query)
                with b.expect_response(
                    lambda response: "/knowledge/search" in response.url
                ) as found:
                    finder.get_by_role(
                        "button", name="Search knowledge", exact=True
                    ).click()
                assert found.value.ok, found.value.status
                count = len(found.value.json())
                # Submitting clears the previous status, so this count is new.
                expect(
                    finder.get_by_role("status", name="Knowledge search status")
                ).to_have_text(f"{count} result{'' if count == 1 else 's'} found.")
                return found.value.text()

            hits = find("Mara")
            assert "Mara opens the inn to the party." in hits and dm_canary not in hits
            expect(
                finder.get_by_text("Mara opens the inn to the party.").first
            ).to_be_visible()
            assert_fits(b)
            b.screenshot(
                path=str(args.output / "bram-knowledge-search.png"), full_page=True
            )
            assert dm_canary not in find(dm_canary)
            expect(b.get_by_text(dm_canary)).to_have_count(0)
            assert ilsa_secret in find(ilsa_secret)
            expect(finder.get_by_text(ilsa_secret, exact=False).first).to_be_visible()
            dm.bring_to_front()
            choose_ilsa()
            drawer.get_by_role("button", name="Retract from Bram", exact=True).click()
            expect(
                drawer.get_by_text("Knowledge retracted.", exact=True)
            ).to_be_visible()
            close_reveal(dm)
            b.bring_to_front()
            expect(
                b.get_by_text("Knowledge retracted: Harbourmaster Ilsa.", exact=True)
            ).to_be_visible(timeout=5000)
            # No embedder run since the grant: retraction alone removes it.
            assert ilsa_secret not in find(ilsa_secret)
            expect(b.get_by_text(ilsa_secret, exact=False)).to_have_count(0)
            for query in (private_text, private_title):
                assert query not in search(b, query)
            report["checks"].append(
                "Player knowledge search finds granted knowledge and a full reveal after the play embedder runs; retracting that grant removes it without re-embedding; DM-only and other-PC text stays out of results"
            )
            dm.bring_to_front()
            dm.on("dialog", lambda dialog: dialog.accept())
            dm.get_by_role("button", name="End session", exact=True).click()
            expect(
                dm.get_by_text("This session has ended. Your story is saved here.")
            ).to_be_visible()
            report["checks"].append(
                "DM can pause, resume and end; ended story remains readable"
            )
            # A second campaign exercises the real invitation-to-character
            # path with the same signed identities, without preassigned PCs.
            dm.goto("http://friends.localhost:4177/grimoire")
            dm.get_by_label("Campaign name", exact=True).fill("The Newcomers")
            dm.get_by_role("button", name="Create campaign", exact=True).click()
            newcomers = dm.get_by_role("article").filter(
                has=dm.get_by_role("heading", name="The Newcomers", exact=True)
            )
            for email in ("a@example.test", "b@example.test"):
                open_campaign_players(dm, newcomers)
                email_input = newcomers.get_by_label("Registered player's email")
                expect(email_input).to_be_visible()
                email_input.fill(email)
                with dm.expect_navigation(wait_until="networkidle") as submitted:
                    newcomers.get_by_role(
                        "button", name="Invite player", exact=True
                    ).click()
                assert submitted.value.status == 200
                expect(dm.get_by_text("Saved.", exact=True)).to_be_visible()
            for player in (a, b):
                player.goto("http://friends.localhost:4177/grimoire")
                invitation = player.get_by_role("article").filter(
                    has=player.get_by_role("heading", name="The Newcomers", exact=True)
                )
                invitation.get_by_role(
                    "button", name="Accept invitation", exact=True
                ).click()
            new_a = a.get_by_role("article").filter(
                has=a.get_by_role("heading", name="The Newcomers", exact=True)
            )
            new_a.get_by_label("Character name", exact=True).fill("Nyx")
            new_a.get_by_role(
                "button", name="Create your character", exact=True
            ).click()
            expect(a.get_by_role("heading", name="Nyx", exact=True)).to_be_visible()
            a.goto("http://friends.localhost:4177/grimoire")
            expect(new_a.get_by_text("Your character: Nyx", exact=True)).to_be_visible()
            dm.reload()
            open_campaign_players(dm, newcomers)
            new_b_member = newcomers.get_by_role("listitem").filter(
                has_text="b@example.test"
            )
            new_b_member.get_by_text("Assign character", exact=True).click()
            new_b_member.get_by_label("New character name", exact=True).fill("Wren")
            new_b_member.get_by_role(
                "button", name="Create and assign character", exact=True
            ).click()
            b.reload()
            new_b = b.get_by_role("article").filter(
                has=b.get_by_role("heading", name="The Newcomers", exact=True)
            )
            expect(
                new_b.get_by_text("Your character: Wren", exact=True)
            ).to_be_visible()
            a.screenshot(
                path=str(args.output / "player-onboarding.png"), full_page=True
            )
            dm.screenshot(path=str(args.output / "dm-onboarding.png"), full_page=True)
            report["checks"].append(
                "Invited players accept into a new campaign; player creates Nyx and DM creates and assigns Wren"
            )
            for player, name, class_name in (
                (a, "Nyx", "ranger"),
                (b, "Wren", "fighter"),
            ):
                player.goto("http://friends.localhost:4177/grimoire/sheets")
                player.wait_for_load_state("networkidle")
                sheet = player.get_by_role("article").filter(
                    has=player.get_by_role("heading", name=name, exact=True)
                )
                sheet.get_by_label("Ancestry", exact=True).fill("Human")
                sheet.get_by_label("Class", exact=True).fill(class_name)
                for ability, score in (
                    ("STR", "14"),
                    ("DEX", "14"),
                    ("CON", "12"),
                    ("INT", "10"),
                    ("WIS", "12"),
                    ("CHA", "10"),
                ):
                    sheet.get_by_label(ability, exact=True).fill(score)
                sheet.get_by_role("button", name="Start new draft", exact=True).click()
                sheet.get_by_role("button", name="Submit to DM", exact=True).click()
                expect(sheet.get_by_text("submitted", exact=True)).to_be_visible()
                player.screenshot(
                    path=str(args.output / f"{name.lower()}-submitted.png"),
                    full_page=True,
                )
            dm.goto("http://friends.localhost:4177/grimoire/sheets")
            dm.wait_for_load_state("networkidle")
            for name in ("Nyx", "Wren"):
                sheet = dm.get_by_role("article").filter(
                    has=dm.get_by_role("heading", name=name, exact=True)
                )
                sheet.get_by_role("button", name="Approve version", exact=True).click()
                expect(sheet.get_by_text("approved", exact=True)).to_be_visible()
            dm.goto("http://friends.localhost:4177/grimoire")
            # Same guard as the first open above: the session page is visible
            # before it is mounted, and a click on its SSR "Start session"
            # button before hydration is lost. Wait for the mounted page's
            # first poll, listening from before the navigation so an early
            # response cannot be missed.
            with dm.expect_response(
                lambda response: response.url.endswith("/session/state"),
                timeout=10000,
            ):
                newcomers.get_by_role("link", name="Open session", exact=True).click()
            dm.get_by_role("button", name="Start session", exact=True).click()
            expect(
                dm.get_by_role("button", name="Pause session", exact=True)
            ).to_be_visible()
            for player in (a, b):
                player.goto("http://friends.localhost:4177/grimoire")
                player.get_by_role("article").filter(
                    has=player.get_by_role("heading", name="The Newcomers", exact=True)
                ).get_by_role("link", name="Open session", exact=True).click()
            expect(a.get_by_text("Nyx", exact=True).first).to_be_visible()
            expect(b.get_by_text("Wren", exact=True).first).to_be_visible()
            a.get_by_text("Roll dice", exact=True).click()
            a.get_by_role("button", name="Strength check", exact=True).click()
            expect(a.get_by_text("Strength check", exact=False).last).to_be_visible()
            fresh_state = a.request.get(a.url + "/state").json()
            assert fresh_state["characters"][0]["approved"]["max_hit_points"] == 11
            assert (
                next(
                    row["body"]["modifier"]
                    for row in fresh_state["events"]
                    if row["body"].get("label") == "Strength check"
                )
                == 2
            )
            a.screenshot(
                path=str(args.output / "new-player-session.png"), full_page=True
            )
            dm.screenshot(path=str(args.output / "new-dm-session.png"), full_page=True)
            report["checks"].append(
                "Both invited players submit sheets, DM approves them, and the new table enters a session with approved-sheet rolls"
            )
            assert not report["errors"], report["errors"]
            report["passed"] = True
        except Exception as error:
            report["passed"] = False
            report["failure"] = str(error) or repr(error)
            if args.join_links:
                raise RuntimeError(redact_join_artifact(report["failure"])) from None
            raise
        finally:
            report["capture_errors"] = []
            for role, context in contexts:
                for index, page in enumerate(context.pages):
                    try:
                        page.screenshot(
                            path=str(args.output / f"{role}-final-{index}.png"),
                            full_page=True,
                            **(
                                {
                                    "mask": [
                                        page.get_by_label(
                                            "Private invitation link", exact=True
                                        )
                                    ]
                                }
                                if args.join_links
                                else {}
                            ),
                        )
                    except Exception as error:
                        report["capture_errors"].append(f"{role} screenshot: {error}")
                try:
                    context.tracing.stop(path=str(args.output / f"{role}-trace.zip"))
                    context.close()
                except Exception as error:
                    report["capture_errors"].append(f"{role} trace/close: {error}")
            try:
                browser.close()
            except Exception as error:
                report["capture_errors"].append(f"browser close: {error}")
            if report["capture_errors"]:
                report["passed"] = False
            if args.join_links:
                report = redact_join_artifact(report)
            (args.output / "report.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            evidence = sorted(
                path.name
                for path in args.output.iterdir()
                if path.suffix in {".png", ".zip"}
            )
            lines = [
                "# Simulated Grimoire session",
                "",
                f"Result: {'passed' if report.get('passed') else 'failed'}",
                "",
                "## Scenario checks",
                "",
            ]
            lines.extend(f"- {check}" for check in report["checks"])
            lines.extend(["", "## Timings", ""])
            lines.extend(
                f"- {name}: {seconds:.3f} seconds"
                for name, seconds in report["timings"].items()
            )
            if report.get("failure"):
                lines.extend(["", "## Failure", "", report["failure"]])
            lines.extend(["", "## Captured evidence", ""])
            lines.extend(f"- [{name}]({name})" for name in evidence)
            lines.extend(
                [
                    "",
                    "These artifacts are captured automatically. Screenshot review must be recorded separately.",
                    "",
                ]
            )
            (args.output / "report.md").write_text("\n".join(lines))
            print(json.dumps(report, indent=2))
            if report["capture_errors"]:
                raise RuntimeError(
                    "Rehearsal evidence capture failed; inspect report.json"
                )


if __name__ == "__main__":
    main()
