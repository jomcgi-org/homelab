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
            a_payload = a.request.get(a.url + "/state").text()
            assert "DM_ONLY_INNKEEPER_SECRET" not in a_payload
            assert (
                "Mara recognizes the seal."
                not in b.request.get(b.url + "/state").text()
            )
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
            assert (
                "DM_ONLY_INNKEEPER_SECRET" not in a.request.get(a.url + "/state").text()
            )
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
            payload = b.request.get(b.url + "/state").text()
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
            assert action not in b.request.get(b.url + "/state").text()
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
            assert reply not in b.request.get(b.url + "/state").text()
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
            a.get_by_label("Sheet roll mode").select_option("adv")
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
            assert roll_secret not in b.request.get(b.url + "/state").text()
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
            expect(a.get_by_role("status")).to_have_text("Live")
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                "A draft I have not sent yet"
            )
            a.unroute("**/session/state", delay_poll)
            report["checks"].append(
                "A delayed successful poll preserves the unsent draft and returns to a live feed"
            )
            a.route("**/session/state", lambda route: route.abort())
            expect(a.get_by_role("status")).to_have_text("Reconnecting", timeout=6000)
            expect(a.get_by_label("What do you do?", exact=True)).to_have_value(
                "A draft I have not sent yet"
            )
            a.screenshot(
                path=str(args.output / "player-reconnecting.png"), full_page=True
            )
            a.unroute("**/session/state")
            expect(a.get_by_role("status")).to_have_text("Live", timeout=6000)
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
            assert dm.evaluate("document.documentElement.scrollWidth <= innerWidth")
            dm.screenshot(
                path=str(args.output / "dm-phone-controls.png"), full_page=True
            )
            dm.set_viewport_size({"width": 1280, "height": 844})
            report["checks"].append(
                "DM phone controls pause, resume and narrate without horizontal overflow"
            )
            for role, page in zip(["dm", "elowen", "bram"], pages):
                page.bring_to_front()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), role
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
            assert (
                "Mara recognizes the seal."
                not in a.request.get(a.url + "/state").text()
            )
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
            for viewer in (dm, b):
                assert (
                    note_title
                    not in viewer.request.get(
                        viewer.url + "/state?notes=character"
                    ).text()
                )
            a.get_by_role("button", name=f"Edit {note_title}", exact=True).click()
            a.get_by_label("Share this note with the DM").check()
            a.get_by_role("button", name="Save note", exact=True).click()
            dm.bring_to_front()
            dm.get_by_role("button", name="Notes", exact=True).click()
            expect(
                dm.get_by_role("heading", name=note_title, exact=True)
            ).to_be_visible(timeout=6000)
            assert (
                note_title not in b.request.get(b.url + "/state?notes=character").text()
            )
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
            a.bring_to_front()
            a.screenshot(path=str(args.output / "party-notes.png"), full_page=True)
            a.get_by_role("button", name=f"Delete {party_title}", exact=True).click()
            expect(
                a.get_by_role("heading", name=party_title, exact=True)
            ).to_have_count(0)
            assert party_title not in b.request.get(b.url + "/state?notes=party").text()
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
            assert (
                "DM_ONLY_INNKEEPER_SECRET"
                not in a.request.get(a.url + "/state?notes=character").text()
            )
            assert (
                "Mara, the innkeeper"
                not in dm.request.get(dm.url + "/state?notes=character").text()
            )
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
            expect(
                journal_region.get_by_role("region", name="Learned").get_by_text(
                    "Mara opens the inn to the party.", exact=True
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
            a.get_by_role("button", name="Party journal", exact=True).click()
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
                a_state["journal"]["mine"]["learned"][0]["projection"][
                    "revealed_details"
                ]["clue"]
                == "Mara remembers our promise."
            )
            assert "Mara opens the inn to the party." not in json.dumps(a_state)
            assert (
                "Mara remembers our promise."
                not in b.request.get(b.url + "/state").text()
            )
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
            assert (
                "Mara remembers our promise."
                not in a.request.get(a.url + "/state").text()
            )
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
            assert "BATCH_Mapmaker Tessa" not in b.request.get(b.url + "/state").text()
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
