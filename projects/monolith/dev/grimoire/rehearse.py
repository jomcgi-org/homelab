"""Exercise the running local table and save evidence from all three seats.

The browser drives the built React app at /grimoire. Every privacy and
outcome assertion reads the real backend with the seat's own cookie, through
the same /api/grimoire routes the app calls.
"""

import argparse
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright
from rehearse_join_links import (
    observe_page,
    redact_join_artifact,
    rehearse_join_links,
)

ORIGIN = "http://friends.localhost:8177"
API = f"{ORIGIN}/api/grimoire"
LOBBY = f"{ORIGIN}/grimoire"
EVENTS = "**/api/grimoire/campaigns/*/sessions/*/events*"
NOTES = "**/api/grimoire/campaigns/*/notes*"


def use_loopback_requests(context):
    """Send fixture inspections to IPv4 while retaining browser host and identity.

    A write also names the app's origin, as a browser fetch from the page
    does: the backend refuses the session cookie on a write without it.
    """
    request = context.request
    for method in ("get", "post", "put", "patch", "delete"):
        original = getattr(request, method)

        def send(url, *, _original=original, _write=method != "get", **kwargs):
            address = urlsplit(url)
            if address.hostname == "friends.localhost":
                cookies = context.cookies(url)
                kwargs["headers"] = {
                    **({"origin": ORIGIN} if _write else {}),
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


def campaign_of(page):
    """The campaign a seat has open, from its /grimoire/campaigns/{id}/… URL."""
    found = re.search(r"/campaigns/([0-9a-f-]{36})", page.url)
    assert found, f"{page.url} is not a campaign page"
    return found.group(1)


def read(page, path, **params):
    """One API read as this seat. A failed response would pass privacy checks."""
    response = page.request.get(API + path, params=params or None)
    assert response.ok, f"GET {path} {params}: {response.status}"
    return response


def campaign_read(page, path="", **params):
    return read(page, f"/campaigns/{campaign_of(page)}{path}", **params)


def current_session(page):
    """The session the table shows: the most recent one."""
    return campaign_read(page, "/sessions").json()[0]


def session_read(page, path, **params):
    return campaign_read(
        page, f"/sessions/{current_session(page)['id']}{path}", **params
    )


def events_of(page):
    """Every event this seat may see in the current session, oldest first."""
    return session_read(page, "/events", after=0, limit=500).json()


def journal_of(page, view):
    return session_read(page, "/journal", view=view).json()


def state_text(page):
    """Everything the session screen reads for this seat, as one text.

    These are the reads the React table makes: the lobby, the sessions, the
    characters, the feed, the turn order and both journals.
    """
    return "\n".join(
        response.text()
        for response in (
            read(page, "/lobby"),
            campaign_read(page, "/sessions"),
            campaign_read(page, "/characters"),
            session_read(page, "/events", after=0, limit=500),
            session_read(page, "/initiative"),
            session_read(page, "/journal", view="mine"),
            session_read(page, "/journal", view="party"),
        )
    )


def notes_text(page, kind):
    return campaign_read(page, "/notes", kind=kind).text()


def approved_sheet(page, character_id):
    """The derived numbers of a character's latest approved sheet."""
    versions = campaign_read(page, f"/characters/{character_id}/sheets").json()[
        "versions"
    ]
    return max(
        (row for row in versions if row["status"] == "approved"),
        key=lambda row: row["version"],
    )["derived"]


# Options a pointer could not reach, as {control, option, observed} rows.
# main() reports each as a failed check; the rehearsal continues by keyboard.
POINTER_MISSES = []


def choose(scope, control, option):
    """Pick an option in an Astryx Selector: a combobox that owns a listbox.

    The pointer is tried first, as a player would. An option the pointer
    cannot reach is recorded as a failure and then chosen with the keyboard,
    so one clipped menu does not hide every later check.
    """
    box = scope.get_by_role("combobox", name=control, exact=True)
    box.click()
    listbox = box.page.locator(f"[id='{box.get_attribute('aria-controls')}']")
    target = listbox.get_by_role("option", name=option, exact=True)
    try:
        target.click(timeout=3000)
    except PlaywrightError as error:
        POINTER_MISSES.append(
            {
                "control": control,
                "option": option,
                "observed": "element is outside of the viewport"
                if "outside of the viewport" in str(error)
                else str(error).splitlines()[0],
            }
        )
        index = [row.inner_text() for row in listbox.get_by_role("option").all()].index(
            option
        )
        box.page.keyboard.press("Escape")
        box.focus()
        box.page.keyboard.press("Enter")
        box.page.keyboard.press("Home")
        for _ in range(index):
            box.page.keyboard.press("ArrowDown")
        box.page.keyboard.press("Enter")
    expect(box).to_have_text(option)


def toast(page, text, timeout=5000):
    """Wait for an outcome toast, then dismiss it.

    Dismissing matters: an error toast stays until closed, so a later
    identical outcome would otherwise match the earlier message.
    """
    notices = page.get_by_role("region", name="Notifications")
    expect(notices.get_by_text(text, exact=True).first).to_be_visible(timeout=timeout)
    for button in notices.get_by_role("button", name="Dismiss notification").all():
        try:
            button.click(timeout=1000)
        except PlaywrightError:
            pass  # It closed by itself first.


def field(scope, label):
    """A text field by its label; a required field's name ends in "Required"."""
    return scope.get_by_role(
        "textbox", name=re.compile(rf"^{re.escape(label)}( Required)?$")
    )


def eventually(check, timeout=5.0):
    """Poll a backend assertion that follows a UI write.

    A toast's text stays in the page for screen readers, so a repeated
    outcome message cannot mark the moment the second write finished.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def composer(page):
    return page.get_by_role("region", name="Message").get_by_role("textbox").first


def send_button(page):
    return page.get_by_role("region", name="Message").get_by_role(
        "button", name="Send", exact=True
    )


def say(page, text, audience=None):
    """Send one message through the docked composer and wait for it to land."""
    if audience:
        choose(page.get_by_role("region", name="Message"), "Send to", audience)
    composer(page).fill(text)
    send_button(page).click()
    expect(composer(page)).to_have_value("")


def tab(page, name):
    """Switch a session section. These are real tabs."""
    control = page.get_by_role("tablist", name="Session sections").get_by_role(
        "tab", name=name, exact=True
    )
    control.click()
    expect(control).to_have_attribute("aria-selected", "true")


def feed(page):
    return page.get_by_role("list", name="Session feed")


def open_dialog(page, button, title):
    page.get_by_role("button", name=button, exact=True).click()
    dialog = page.get_by_role("dialog", name=title, exact=True)
    expect(dialog).to_be_visible()
    return dialog


def close_dialog(dialog):
    dialog.get_by_role("button", name="Close", exact=True).first.click()
    expect(dialog).to_have_count(0)


def reveal_drawer(page):
    return page.get_by_role("dialog", name="Reveal to your players", exact=True)


def open_reveal(page):
    """Open the DM reveal drawer; it covers the session controls until closed."""
    return open_dialog(page, "Reveal knowledge", "Reveal to your players")


def close_reveal(page):
    close_dialog(reveal_drawer(page))


def pick_knowledge(page, query, name):
    """Find an entity in the reveal drawer's typeahead and load it."""
    drawer = reveal_drawer(page)
    chosen = drawer.get_by_role("button", name="Clear selection", exact=True)
    if chosen.count():
        chosen.click()
    box = drawer.get_by_role("combobox", name="Find knowledge", exact=True)
    box.fill(query)
    page.get_by_role("option", name=re.compile(rf"^{re.escape(name)}")).click()
    expect(drawer.get_by_role("heading", name=name, exact=True)).to_be_visible()
    return drawer


def knowledge_drawer(page):
    return page.get_by_role("dialog", name="Knowledge detail", exact=True)


def assert_fits(page):
    """A view must not scroll sideways."""
    widths = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
    assert widths[0] <= widths[1], f"{page.url} scrolls sideways: {widths}"


def shot(page, path):
    page.screenshot(path=str(path), full_page=True)


def open_seat(page, role):
    """Sign in as the gateway would, then open the table from the lobby."""
    page.goto(f"{ORIGIN}/__local/login/{role}")
    expect(page).to_have_url(LOBBY)
    page.get_by_role("link", name="Open session", exact=True).click()
    expect(page.get_by_role("tablist", name="Session sections")).to_be_visible()


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
        "failed_checks": [],
        "errors": [],
        "timings": {},
        "console_errors": [],
        "network_failures": [],
    }

    @contextmanager
    def defect(check):
        """Record a check the app fails and keep rehearsing the rest.

        Used only for a failure already understood and reported; the run still
        ends failed. Every other check stops the rehearsal where it breaks.
        """
        try:
            yield
        except (AssertionError, PlaywrightError) as error:
            report["failed_checks"].append(
                {"check": check, "observed": str(error).strip().split("\n\n")[0]}
            )

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
                report["passed"] = not report["failed_checks"]
                return
            pages = []
            for role, width in [("dm", 1280), ("a", 390), ("b", 390)]:
                context = browser.new_context(viewport={"width": width, "height": 844})
                contexts.append((role, context))
                use_loopback_requests(context)
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                observe_page(page, report)
                open_seat(page, role)
                pages.append(page)
            dm, a, b = pages
            dm.bring_to_front()
            dm.get_by_role("button", name="Start session", exact=True).click()
            expect(composer(dm)).to_be_visible()
            expect(dm.get_by_label("Set the scene", exact=True)).to_be_visible()
            started = time.monotonic()
            with defect(
                "A waiting player's table shows the started session within six seconds"
            ):
                for viewer in (a, b):
                    expect(viewer.get_by_text("active", exact=True)).to_be_visible(
                        timeout=6000
                    )
            for viewer in (a, b):
                expect(viewer.get_by_text("active", exact=True)).to_be_visible(
                    timeout=20000
                )
            report["timings"]["session_start_propagation_seconds"] = (
                time.monotonic() - started
            )
            expect(
                a.get_by_role("heading", name="The story begins here.", exact=True)
            ).to_be_visible()
            for role, viewer in (("dm", dm), ("a", a), ("b", b)):
                shot(viewer, args.output / f"{role}-empty-feed.png")
            stamp = str(time.time_ns())
            scene = f"A stranger leaves a sealed letter at your table. ({stamp})"
            choose(dm.get_by_role("region", name="Message"), "Send to", "Everyone")
            composer(dm).fill(scene)
            held_write = []

            def hold_first_write(route):
                if route.request.method in ("POST", "PATCH"):
                    held_write.append(route)
                else:
                    route.continue_()

            dm.route(EVENTS, hold_first_write)
            send_button(dm).click()
            # An in-flight send is a busy, disabled Send button.
            expect(send_button(dm)).to_be_disabled()
            expect(send_button(dm)).to_have_attribute("aria-busy", "true")
            shot(dm, args.output / "dm-sending.png")
            assert len(held_write) == 1
            next_draft = "A new scene drafted while the previous message sends"
            composer(dm).fill(next_draft)
            held_write[0].continue_()
            dm.unroute(EVENTS, hold_first_write)
            expect(feed(dm).get_by_text(scene, exact=True)).to_be_visible()
            expect(send_button(dm)).not_to_have_attribute("aria-busy", "true")
            expect(send_button(dm)).to_be_enabled()
            report["checks"].append(
                "Empty feeds explain the next step; an in-flight write shows a disabled Sending control"
            )
            expect(composer(dm)).to_have_value(next_draft)
            composer(dm).fill("")
            expect(
                dm.get_by_role("button", name=re.compile(r"^Pin .* notes$"))
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
            a_box = a.get_by_label("What do you do?", exact=True)
            a_audience = a.get_by_role("combobox", name="Send to", exact=True)
            a_box.focus()
            a.keyboard.press("Tab")
            expect(a_audience).to_be_focused()
            a.keyboard.press("Shift+Tab")
            expect(a_box).to_be_focused()
            report["checks"].append(
                "Keyboard Tab and Shift+Tab move between the composer and audience selector"
            )
            assert a.evaluate("document.visibilityState") == "visible"
            live_scene = f"The lantern flickers twice. ({stamp})"
            composer(dm).fill(live_scene)
            sent = time.monotonic()
            send_button(dm).click()
            expect(a.get_by_text(live_scene, exact=True)).to_be_visible(timeout=5000)
            report["timings"]["visible_tab_propagation_seconds"] = (
                time.monotonic() - sent
            )
            assert report["timings"]["visible_tab_propagation_seconds"] <= 3
            assert a.evaluate("document.visibilityState") == "visible"
            expect(a_box).to_be_focused()
            expect(a.get_by_text(live_scene, exact=True)).to_have_count(1)
            report["checks"].append(
                "Visible-tab polling delivers narration within three seconds without duplicate entries or stealing composer focus"
            )
            # Exercise the DM reveal editor against normal backend authority.
            dm.bring_to_front()
            reveal_start = time.monotonic()
            open_reveal(dm)
            drawer = pick_knowledge(dm, "Mara", "Mara, the innkeeper")
            share_with = drawer.get_by_role("group", name="Share with")
            share_with.get_by_label("Elowen", exact=True).check()
            choose(drawer, "Knowledge scope", "Selected detail")
            drawer.get_by_role("group", name="Fields to share").get_by_label(
                "occupation", exact=True
            ).check()
            drawer.get_by_label("Detail to share").fill("Mara recognizes the seal.")
            drawer.get_by_role("button", name="Preview knowledge", exact=True).click()
            share = drawer.get_by_role("button", name="Share knowledge", exact=True)
            expect(share).to_be_visible()
            report["timings"]["reveal_interaction_seconds"] = (
                time.monotonic() - reveal_start
            )
            assert report["timings"]["reveal_interaction_seconds"] < 15
            reveal_sent = time.monotonic()
            share.click()
            toast(dm, "Knowledge shared.")
            a.bring_to_front()
            expect(
                a.get_by_text("Mara recognizes the seal.", exact=False).first
            ).to_be_visible(timeout=5000)
            report["timings"]["reveal_propagation_seconds"] = (
                time.monotonic() - reveal_sent
            )
            assert report["timings"]["reveal_propagation_seconds"] < 3
            a_payload = state_text(a)
            assert "Mara recognizes the seal." in a_payload
            assert "DM_ONLY_INNKEEPER_SECRET" not in a_payload
            assert "Mara recognizes the seal." not in state_text(b)
            report["checks"].append(
                "Partial grant reveal reaches only its recipient without the DM canary"
            )
            a_box.fill("A draft while checking knowledge")
            a.get_by_role(
                "button", name="Explore Mara, the innkeeper", exact=True
            ).click()
            detail = knowledge_drawer(a)
            expect(
                detail.get_by_text("Keeps the Lantern Inn", exact=True)
            ).to_be_visible()
            assert "DM_ONLY_INNKEEPER_SECRET" not in detail.inner_text()
            entity_id = next(
                (row["body"].get("reveals", [row["body"]])[0])["entity_id"]
                for row in events_of(a)
                if row["kind"] == "reveal"
            )
            # The standalone knowledge page is the same client app, so its
            # authority is the entity read it makes: 200 for A, 404 for B.
            entity_read = a.request.get(
                f"{API}/campaigns/{campaign_of(a)}/entities/{entity_id}"
            )
            assert entity_read.status == 200
            assert "Keeps the Lantern Inn" in entity_read.text()
            assert "DM_ONLY_INNKEEPER_SECRET" not in entity_read.text()
            assert (
                b.request.get(
                    f"{API}/campaigns/{campaign_of(b)}/entities/{entity_id}"
                ).status
                == 404
            )
            entity_url = f"{LOBBY}/campaigns/{campaign_of(a)}/entities/{entity_id}"
            for seat, shown in ((a, True), (b, False)):
                entity_page = seat.context.new_page()
                observe_page(entity_page, report)
                entity_page.goto(entity_url)
                if shown:
                    expect(
                        entity_page.get_by_role(
                            "heading", name="Mara, the innkeeper", exact=True
                        )
                    ).to_be_visible()
                    expect(
                        entity_page.get_by_text("Keeps the Lantern Inn", exact=True)
                    ).to_be_visible()
                else:
                    expect(
                        entity_page.get_by_text(
                            "This knowledge entry is not available to you.",
                            exact=True,
                        ).first
                    ).to_be_visible()
                    expect(entity_page.get_by_text("Mara", exact=False)).to_have_count(
                        0
                    )
                assert "DM_ONLY_INNKEEPER_SECRET" not in entity_page.content()
                entity_page.close()
            a.bring_to_front()
            shot(a, args.output / "partial-knowledge.png")
            close_dialog(detail)
            expect(a_box).to_have_value("A draft while checking knowledge")
            a_box.fill("")
            dm.bring_to_front()
            share_with.get_by_label("Bram", exact=True).check()
            choose(drawer, "Knowledge scope", "Full knowledge")
            drawer.get_by_role("button", name="Preview knowledge", exact=True).click()
            share.click()
            toast(dm, "Knowledge shared.")
            b.bring_to_front()
            b.get_by_role(
                "button", name="Explore Mara, the innkeeper", exact=True
            ).click()
            expect(
                knowledge_drawer(b).get_by_text("DM_ONLY_INNKEEPER_SECRET", exact=True)
            ).to_be_visible(timeout=5000)
            shot(b, args.output / "full-knowledge.png")
            close_dialog(knowledge_drawer(b))
            # B holds the full scope now; A's card and payload must stay partial.
            a.bring_to_front()
            expect(
                a.get_by_text("DM_ONLY_INNKEEPER_SECRET", exact=False)
            ).to_have_count(0)
            assert "DM_ONLY_INNKEEPER_SECRET" not in state_text(a)
            assert "DM_ONLY_INNKEEPER_SECRET" in state_text(b)
            dm.bring_to_front()
            drawer.get_by_role("button", name="Retract from Bram", exact=True).click()
            toast(dm, "Knowledge retracted.")

            def b_lost_the_secret():
                assert "DM_ONLY_INNKEEPER_SECRET" not in state_text(b)

            eventually(b_lost_the_secret)
            close_reveal(dm)
            report["checks"].append(
                "Field preview and knowledge drawer enforce partial A/full B scopes and preserve the session draft"
            )
            dm.bring_to_front()
            secret = f"A_SECRET_{stamp}: You recognize your mentor's seal."
            say(dm, secret, "Elowen and DM")
            a.bring_to_front()
            expect(a.get_by_text(secret, exact=True)).to_be_visible(timeout=5000)
            b.bring_to_front()
            expect(b.get_by_text(scene, exact=True)).to_be_visible(timeout=5000)
            expect(b.get_by_text(secret, exact=True)).to_have_count(0)
            assert secret in state_text(a)
            assert secret not in state_text(b), (
                "Player A secret leaked into B's response"
            )
            report["checks"].append(
                "Targeted message appears for A and is absent from B's page and payload"
            )
            a.bring_to_front()
            action = f"A_WHISPER_{stamp}: I quietly check the letter for a trap."
            say(a, action, "DM privately")
            expect(a.get_by_text(action, exact=True)).to_be_visible(timeout=5000)
            dm.bring_to_front()
            expect(dm.get_by_text(action, exact=True)).to_be_visible(timeout=5000)
            b.bring_to_front()
            b.reload()
            expect(feed(b).get_by_text(scene, exact=True)).to_be_visible()
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
            composer(dm).fill(reply)
            send_button(dm).click()
            expect(composer(dm)).to_have_value("")
            a.bring_to_front()
            expect(a.get_by_text(reply, exact=True)).to_be_visible(timeout=5000)
            expect(a.get_by_text("Resolved", exact=True)).to_be_visible()
            assert reply not in state_text(b)
            report["checks"].append(
                "DM replies privately to the sender and resolves the waiting action"
            )
            # The app offers a player neither control, so the refusals are
            # read from the API the app would call, with B's own cookie.
            b_campaign = campaign_of(b)
            b_session = current_session(b)["id"]
            denied = b.request.patch(
                f"{API}/campaigns/{b_campaign}/sessions/{b_session}",
                data={"status": "ended"},
            )
            assert denied.status == 403, (
                f"Player could end the session: {denied.status}"
            )
            assert current_session(b)["status"] == "active"
            denied = b.request.post(
                f"{API}/campaigns/{b_campaign}/sessions/{b_session}/events",
                data={
                    "kind": "narration",
                    "audience": "table",
                    "audience_pc_ids": [],
                    "body": {"text": "Unauthorized narration"},
                },
            )
            assert denied.status == 403, (
                f"Player could post DM narration: {denied.status}"
            )
            assert "Unauthorized narration" not in state_text(dm)
            report["checks"].append(
                "Backend rejects player attempts to end the session or narrate as DM"
            )

            # Rolls originate on the server and follow the same audience policy.
            a.bring_to_front()
            a_character = campaign_read(a, "/characters").json()[0]
            assert a_character["character_name"] == "Elowen"
            dice = open_dialog(a, "Roll dice", "Roll dice")
            choose(dice, "Sheet roll type", "Saving throws")
            dice.get_by_role("radio", name="Advantage", exact=True).click()
            expect(
                dice.get_by_role("radio", name="Advantage", exact=True)
            ).to_be_checked()
            dice.get_by_role("button", name="Strength save", exact=True).click()
            expect(
                feed(a).get_by_text("Strength save", exact=False).last
            ).to_be_attached()
            sheet_roll = next(
                row
                for row in events_of(a)
                if (row["body"] or {}).get("label") == "Strength save"
            )
            approved_bonus = approved_sheet(a, a_character["id"])[
                "saving_throw_bonuses"
            ]["strength"]
            assert sheet_roll["body"]["modifier"] == approved_bonus
            assert len(sheet_roll["body"]["rolls"]) == 2
            assert sheet_roll["body"]["kept"] == [max(sheet_roll["body"]["rolls"])]
            report["checks"].append(
                "Quick saving throws use the approved sheet bonus and advantage"
            )
            dice.get_by_role("radio", name="Normal", exact=True).click()
            dice.get_by_label("Dice formula", exact=True).fill("d20adv+3")
            roll_secret = f"A_ROLL_{stamp}"
            dice.get_by_label("Roll label", exact=True).fill(roll_secret)
            choose(dice, "Roll visibility", "DM and me")
            dice.get_by_role("button", name="Roll", exact=True).click()
            close_dialog(dice)
            expect(a.get_by_text(roll_secret, exact=False).first).to_be_visible(
                timeout=5000
            )
            dm.bring_to_front()
            expect(dm.get_by_text(roll_secret, exact=False).first).to_be_visible(
                timeout=5000
            )
            private_roll = next(
                row
                for row in events_of(dm)
                if (row["body"] or {}).get("label") == roll_secret
            )
            assert len(private_roll["body"]["rolls"]) == 2
            assert private_roll["body"]["modifier"] == 3
            b.bring_to_front()
            assert roll_secret not in state_text(b)
            expect(b.get_by_text(roll_secret, exact=False)).to_have_count(0)
            dice = open_dialog(b, "Roll dice", "Roll dice")
            dice.get_by_label("Dice formula", exact=True).fill("2d6+3")
            public_label = f"B_PUBLIC_ROLL_{stamp}"
            dice.get_by_label("Roll label", exact=True).fill(public_label)
            expect(
                dice.get_by_role("combobox", name="Roll visibility", exact=True)
            ).to_have_text("Everyone")
            dice.get_by_role("button", name="Roll", exact=True).click()
            close_dialog(dice)
            expect(b.get_by_text(public_label, exact=False).first).to_be_visible(
                timeout=5000
            )
            a.bring_to_front()
            expect(a.get_by_text(public_label, exact=False).first).to_be_visible(
                timeout=5000
            )
            rolled = next(
                row
                for row in events_of(a)
                if (row["body"] or {}).get("label") == public_label
            )
            assert 5 <= rolled["body"]["total"] <= 15
            assert rolled["body"]["total"] == sum(rolled["body"]["kept"]) + 3
            report["checks"].append(
                "Server dice supports advantage, modifiers, public rolls and private rolls"
            )

            # A transient polling failure must retain the unsent draft.
            def connection(page, state):
                return page.get_by_role(
                    "img", name=f"Session connection: {state}", exact=True
                )

            a.bring_to_front()
            a_box.fill("A draft I have not sent yet")
            delayed_polls = []

            def delay_poll(route):
                if route.request.method == "GET" and not delayed_polls:
                    response = fetch_loopback_route(route)
                    time.sleep(1)
                    delayed_polls.append(route.request.url)
                    route.fulfill(response=response)
                else:
                    route.continue_()

            a.route(EVENTS, delay_poll)
            # A locator expectation waits while Playwright services the delayed route.
            expect(a_box).to_have_value("A draft I have not sent yet")
            a.wait_for_timeout(3500)
            assert len(delayed_polls) == 1
            expect(connection(a, "Live")).to_be_visible()
            expect(a_box).to_have_value("A draft I have not sent yet")
            a.unroute(EVENTS, delay_poll)
            report["checks"].append(
                "A delayed successful poll preserves the unsent draft and returns to a live feed"
            )
            a.route(EVENTS, lambda route: route.abort())
            dropped = time.monotonic()
            expect(connection(a, "Reconnecting")).to_be_visible(timeout=6000)
            report["timings"]["reconnecting_shown_seconds"] = time.monotonic() - dropped
            expect(a_box).to_have_value("A draft I have not sent yet")
            shot(a, args.output / "player-reconnecting.png")
            a.unroute(EVENTS)
            expect(connection(a, "Live")).to_be_visible(timeout=6000)
            report["checks"].append("Polling recovers and preserves the unsent draft")
            retry_text = f"RETRY_{stamp}: I inspect the door."
            choose(a.get_by_role("region", name="Message"), "Send to", "Everyone")
            a_box.fill(retry_text)

            def lose_write_response(route):
                if route.request.method == "POST":
                    fetch_loopback_route(
                        route
                    )  # Commit normally, then lose the response.
                    route.abort()
                else:
                    route.continue_()

            a.route(EVENTS, lose_write_response)
            send_button(a).click()
            # The failure is an error toast; the draft stays in the composer.
            toast(a, "Connection interrupted. Nothing was lost; try again.")
            expect(a_box).to_have_value(retry_text)
            shot(a, args.output / "player-send-error.png")
            a.unroute(EVENTS, lose_write_response)
            send_button(a).click()
            expect(a_box).to_have_value("")
            assert (
                sum(
                    row["body"].get("text") == retry_text
                    for row in events_of(a)
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
            assert current_session(dm)["status"] == "paused"
            dm.get_by_role("button", name="Resume session", exact=True).click()
            expect(
                dm.get_by_role("button", name="Pause session", exact=True)
            ).to_be_visible()
            assert current_session(dm)["status"] == "active"
            mobile_scene = f"PHONE_DM_{stamp}: The lantern flickers."
            say(dm, mobile_scene, "Everyone")
            expect(dm.get_by_text(mobile_scene, exact=True)).to_be_visible()
            assert_fits(dm)
            shot(dm, args.output / "dm-phone-controls.png")
            dm.set_viewport_size({"width": 1280, "height": 844})
            report["checks"].append(
                "DM phone controls pause, resume and narrate without horizontal overflow"
            )
            for role, page in zip(["dm", "elowen", "bram"], pages):
                page.bring_to_front()
                assert_fits(page)
                shot(page, args.output / f"{role}.png")
            report["checks"].append(
                "DM desktop and player phone views have no horizontal overflow"
            )
            dm.bring_to_front()
            open_reveal(dm)
            drawer.get_by_role("button", name="Retract from Elowen", exact=True).click()

            def a_lost_the_clue():
                assert "Mara recognizes the seal." not in state_text(a)

            eventually(a_lost_the_clue)
            a.bring_to_front()
            expect(
                a.get_by_text("Knowledge retracted: Mara, the innkeeper.", exact=True)
            ).to_be_visible(timeout=5000)
            dm.bring_to_front()
            choose(drawer, "Knowledge scope", "Selected detail")
            expect(share_with.get_by_label("Elowen", exact=True)).to_be_enabled()
            share_with.get_by_label("Elowen", exact=True).check()
            share_with.get_by_label("Bram", exact=True).check()
            drawer.get_by_label("Detail to share").fill(
                "Mara opens the inn to the party."
            )
            drawer.get_by_role("button", name="Preview knowledge", exact=True).click()
            share.click()
            for player in (a, b):
                player.bring_to_front()
                expect(
                    player.get_by_text(
                        "Mara opens the inn to the party.", exact=False
                    ).first
                ).to_be_visible(timeout=5000)
            shared = [
                row
                for row in events_of(dm)
                if row["kind"] == "reveal"
                and "Mara opens the inn to the party." in json.dumps(row["body"])
            ]
            # One transaction: both grants were written before either shows.
            assert len(shared) >= 1 and all(
                "Mara opens the inn to the party." in state_text(player)
                for player in (a, b)
            )
            dm.bring_to_front()
            close_reveal(dm)
            report["checks"].append(
                "DM editor retracts old knowledge and atomically shares with both players"
            )
            # Private notes stay private until the author explicitly opts in.
            a.bring_to_front()
            tab(a, "Notes")
            a_notes = a.get_by_role("region", name="Campaign notes")
            note_title = f"A_NOTE_{stamp}"
            field(a_notes, "Note title").fill(note_title)
            field(a_notes, "Note text").fill("**A private thought** about the seal.")
            a_notes.get_by_role("button", name="Save note", exact=True).click()
            expect(
                a_notes.get_by_role("heading", name=note_title, exact=True)
            ).to_be_visible()
            assert note_title in notes_text(a, "character")
            for viewer in (dm, b):
                assert note_title not in notes_text(viewer, "character")
            a_notes.get_by_role("button", name=f"Edit {note_title}", exact=True).click()
            a_notes.get_by_label("Share this note with the DM").check()
            a_notes.get_by_role("button", name="Save note", exact=True).click()
            expect(
                a_notes.get_by_text("Shared with the DM", exact=True)
            ).to_be_visible()
            dm.bring_to_front()
            tab(dm, "Notes")
            dm_notes = dm.get_by_role("region", name="Campaign notes")
            shared_note = dm_notes.get_by_role("heading", name=note_title, exact=True)
            with defect(
                "A note shared with the DM appears in the DM's notes within six seconds"
            ):
                expect(shared_note).to_be_visible(timeout=6000)
            expect(shared_note).to_be_visible(timeout=20000)
            assert note_title in notes_text(dm, "character")
            assert note_title not in notes_text(b, "character")
            a.bring_to_front()
            a_notes.get_by_role("tab", name="Party notes", exact=True).click()
            party_title = f"PARTY_NOTE_{stamp}"
            field(a_notes, "Note title").fill(party_title)
            field(a_notes, "Note text").fill("The inn is open to our party.")
            held_write.clear()
            a.route(NOTES, hold_first_write)
            a_notes.get_by_role("button", name="Save note", exact=True).click()
            expect(field(a_notes, "Note title")).to_be_disabled()
            expect(field(a_notes, "Note text")).to_be_disabled()
            locked = len(report["failed_checks"])
            with defect(
                "An in-flight note save locks its fields and audience until completion"
            ):
                expect(
                    a_notes.get_by_role("tab", name="My notes", exact=True)
                ).to_be_disabled(timeout=1000)
            assert len(held_write) == 1
            held_write[0].continue_()
            a.unroute(NOTES, hold_first_write)
            expect(field(a_notes, "Note title")).to_be_enabled()
            if len(report["failed_checks"]) == locked:
                report["checks"].append(
                    "An in-flight note save locks its fields and audience until completion"
                )
            expect(
                a_notes.get_by_role("heading", name=party_title, exact=True)
            ).to_be_visible()
            for viewer in (dm, b):
                viewer.bring_to_front()
                if viewer is b:
                    tab(b, "Notes")
                panel = viewer.get_by_role("region", name="Campaign notes")
                panel.get_by_role("tab", name="Party notes", exact=True).click()
                expect(
                    panel.get_by_role("heading", name=party_title, exact=True)
                ).to_be_visible(timeout=6000)
            assert party_title in notes_text(b, "party")
            a.bring_to_front()
            shot(a, args.output / "party-notes.png")
            a_notes.get_by_role(
                "button", name=f"Delete {party_title}", exact=True
            ).click()
            confirm = a.get_by_role("alertdialog", name="Delete this note?")
            confirm.get_by_role("button", name="Delete note", exact=True).click()
            expect(
                a_notes.get_by_role("heading", name=party_title, exact=True)
            ).to_have_count(0)
            assert party_title not in notes_text(b, "party")
            a_notes.get_by_role("tab", name="My notes", exact=True).click()
            field(a_notes, "Note title").fill("An unsaved note")
            tab(a, "Story")
            tab(a, "Notes")
            expect(field(a_notes, "Note title")).to_have_value("An unsaved note")
            shot(a, args.output / "private-notes.png")
            for viewer in (a, b, dm):
                tab(viewer, "Story")
            a.bring_to_front()
            feed(a).get_by_role(
                "button", name="Pin Mara, the innkeeper to notes", exact=True
            ).last.click()
            toast(a, "Pinned to your notes.")
            pinned = a_notes.get_by_role("listitem").filter(
                has=a.get_by_role("heading", name="Mara, the innkeeper", exact=True)
            )
            expect(pinned.get_by_text("Pinned", exact=True)).to_be_visible()
            expect(
                pinned.get_by_text(
                    "Mara opens the inn to the party.", exact=False
                ).first
            ).to_be_visible()
            a_note_text = notes_text(a, "character")
            assert "Mara opens the inn to the party." in a_note_text
            assert "DM_ONLY_INNKEEPER_SECRET" not in a_note_text
            dm_note_text = notes_text(dm, "character")
            assert note_title in dm_note_text
            assert "Mara, the innkeeper" not in dm_note_text
            shot(a, args.output / "pinned-notes.png")
            pinned.get_by_role("button", name="Mara, the innkeeper", exact=True).click()
            expect(
                knowledge_drawer(a)
                .get_by_text("Mara opens the inn to the party.", exact=True)
                .first
            ).to_be_visible()
            close_dialog(knowledge_drawer(a))
            pinned.get_by_role("button", name="Source event", exact=True).click()
            expect(a_box).to_be_visible()
            expect(a.get_by_role("tab", name="Story", exact=True)).to_have_attribute(
                "aria-selected", "true"
            )
            report["checks"].append(
                "Pin to notes copies visible knowledge, stays private, and links back to its event"
            )
            report["checks"].append(
                "Notes editor enforces private default, DM opt-in, party sharing, deletion and draft retention"
            )
            a.bring_to_front()
            tab(a, "Journal")
            journal_region = a.get_by_role("region", name="Session journal")
            expect(
                journal_region.get_by_role("region", name="Learned")
                .get_by_text("Mara opens the inn to the party.", exact=False)
                .first
            ).to_be_visible()
            expect(
                journal_region.get_by_role("region", name="Rolls")
                .get_by_text(roll_secret, exact=False)
                .first
            ).to_be_visible()
            mine, party = journal_of(a, "mine"), journal_of(a, "party")
            assert mine["open_threads"] == []
            assert len(mine["learned"]) == 1
            assert party["learned"] == []
            shot(a, args.output / "my-journal.png")
            journal_region.get_by_role("region", name="Learned").get_by_role(
                "button", name="Explore Mara, the innkeeper", exact=True
            ).click()
            expect(
                knowledge_drawer(a)
                .get_by_text("Mara opens the inn to the party.", exact=True)
                .first
            ).to_be_visible()
            close_dialog(knowledge_drawer(a))
            party_view = journal_region.get_by_role("radio", name="Party", exact=True)
            party_view.click()
            expect(party_view).to_be_checked()
            expect(
                journal_region.get_by_text(public_label, exact=False).first
            ).to_be_visible()
            expect(journal_region.get_by_text(roll_secret, exact=False)).to_have_count(
                0
            )
            shot(a, args.output / "party-journal.png")
            b_journal = json.dumps([journal_of(b, "mine"), journal_of(b, "party")])
            assert public_label in b_journal
            assert roll_secret not in b_journal
            assert secret not in b_journal
            tab(a, "Story")
            report["checks"].append(
                "Journal uses latest visible knowledge, own rolls and resolved threads; party journal excludes private events"
            )
            dm.bring_to_front()
            dm.get_by_role("button", name="Pause session", exact=True).click()
            expect(
                dm.get_by_role("button", name="Resume session", exact=True)
            ).to_be_visible()
            campaign_id = campaign_of(dm)
            grants_url = f"{LOBBY}/campaigns/{campaign_id}/grants"
            matrix = dm.context.new_page()
            observe_page(matrix, report)
            matrix.goto(grants_url)
            expect(
                matrix.get_by_role(
                    "heading", name="Knowledge at your table", exact=True
                )
            ).to_be_visible()
            # The matrix is one client route for everyone, so a player's
            # refusal is the grants read itself plus the page's own notice.
            assert a.request.get(f"{API}/campaigns/{campaign_id}/grants").status == 403
            player_matrix = a.context.new_page()
            observe_page(player_matrix, report)
            player_matrix.goto(grants_url)
            expect(
                player_matrix.get_by_text(
                    "The grants table is for the DM.", exact=True
                ).first
            ).to_be_visible()
            expect(player_matrix.get_by_text("Mara", exact=False)).to_have_count(0)
            player_matrix.close()
            matrix.get_by_role("searchbox", name="Find knowledge", exact=True).fill(
                "Mara"
            )

            def edit_mara():
                matrix.get_by_role(
                    "button", name=re.compile(r"^Edit Mara, the innkeeper for Elowen")
                ).click()
                editor = matrix.get_by_role(
                    "dialog", name="Mara, the innkeeper · Elowen", exact=True
                )
                expect(editor).to_be_visible()
                return editor

            editor = edit_mara()
            expect(editor.get_by_label("Detail to share", exact=True)).to_have_value(
                "Mara opens the inn to the party."
            )
            editor.get_by_label("Detail to share", exact=True).fill(
                "Mara remembers our promise."
            )
            editor.get_by_role("button", name="Preview knowledge", exact=True).click()
            confirm = editor.get_by_role("button", name="Confirm knowledge", exact=True)
            expect(confirm).to_be_visible()
            shot(matrix, args.output / "grants-matrix.png")
            confirm.click()
            expect(editor).to_have_count(0)
            a.bring_to_front()
            expect(
                feed(a).get_by_text("Mara remembers our promise.", exact=False).first
            ).to_be_visible(timeout=5000)
            a_journal = journal_of(a, "mine")
            assert (
                a_journal["learned"][0]["entity"]["revealed_details"]["clue"]
                == "Mara remembers our promise."
            )
            assert "Mara opens the inn to the party." not in state_text(a)
            assert "Mara remembers our promise." not in state_text(b)
            matrix.bring_to_front()
            editor = edit_mara()
            choose(editor, "Knowledge scope", "Name only")
            editor.get_by_role("button", name="Preview knowledge", exact=True).click()
            editor.get_by_role("button", name="Confirm knowledge", exact=True).click()
            expect(editor).to_have_count(0)
            assert "Mara remembers our promise." not in state_text(a)
            editor = edit_mara()
            editor.get_by_role("button", name="Retract knowledge", exact=True).click()
            expect(editor).to_have_count(0)
            assert not [
                grant
                for grant in campaign_read(dm, "/grants").json()
                if grant["player_character_id"] == a_character["id"]
            ]
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
            characters = campaign_read(dm, "/characters").json()
            elowen_id = next(
                pc["id"] for pc in characters if pc["character_name"] == "Elowen"
            )
            batch_entities = [
                campaign_read(dm, "/entities", q=name).json()["items"][0]
                for name in ("Tessa", "Orrin")
            ]
            a.bring_to_front()
            story_tab = a.get_by_role("tab", name="Story", exact=True)
            story_tab.focus()
            a.evaluate("window.scrollTo(0, 0)")
            reading_position = a.evaluate("window.scrollY")
            # The same cookie and Origin the app's own fetch would send.
            batch = dm.request.post(
                f"{API}/campaigns/{campaign_id}/grants/bulk",
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
            expect(
                a.get_by_text("BATCH_Mapmaker Tessa", exact=False).first
            ).to_be_attached(timeout=5000)
            expect(
                a.get_by_text("BATCH_Ferryman Orrin", exact=False).first
            ).to_be_attached()
            grouped = [
                row
                for row in events_of(a)
                if row["body"]
                and "reveals" in row["body"]
                and "BATCH_" in json.dumps(row["body"])
            ]
            assert len(grouped) == 1 and len(grouped[0]["body"]["reveals"]) == 2
            assert "DM_ONLY_BATCH_SECRET" not in json.dumps(grouped)
            assert "BATCH_Mapmaker Tessa" not in state_text(b)
            assert abs(a.evaluate("window.scrollY") - reading_position) <= 2
            expect(story_tab).to_be_focused()
            report["checks"].append(
                "Incoming grouped knowledge preserves the reader's scroll position and keyboard focus"
            )
            shot(a, args.output / "grouped-reveal.png")
            feed(a).get_by_role(
                "button", name="Pin Mapmaker Tessa, Ferryman Orrin to notes", exact=True
            ).click()
            toast(a, "Pinned to your notes.")
            for clue in ("BATCH_Mapmaker Tessa", "BATCH_Ferryman Orrin"):
                expect(a_notes.get_by_text(clue, exact=False).first).to_be_visible()
            tab(a, "Story")
            dm.bring_to_front()
            open_reveal(dm)
            pick_knowledge(dm, "Tessa", "Mapmaker Tessa")
            drawer.get_by_role("button", name="Retract from Elowen", exact=True).click()

            def tessa_retracted():
                remaining = next(
                    row for row in events_of(a) if row["id"] == grouped[0]["id"]
                )
                assert len(remaining["body"]["reveals"]) == 1

            eventually(tessa_retracted)
            close_reveal(dm)
            a.bring_to_front()
            retracted_at = time.monotonic()
            still_shown = feed(a).get_by_text("BATCH_Mapmaker Tessa", exact=False)
            with defect(
                "A retracted item leaves the player's feed within five seconds"
            ):
                expect(still_shown).to_have_count(0, timeout=5000)
            expect(still_shown).to_have_count(0, timeout=30000)
            report["timings"]["retraction_hidden_seconds"] = (
                time.monotonic() - retracted_at
            )
            expect(
                feed(a).get_by_text("BATCH_Ferryman Orrin", exact=False).first
            ).to_be_visible()
            shot(a, args.output / "grouped-reveal-retracted.png")
            report["checks"].append(
                "Grouped reveal renders and pins both items; retracting one hides its projection while preserving the other"
            )
            # Combat: both PCs and a hidden monster. Each player sees only their
            # own turn, and the monster stays masked in pages and payloads.
            bram_id = next(
                pc["id"] for pc in characters if pc["character_name"] == "Bram"
            )
            monster = f"HIDDEN_MONSTER_{stamp}"
            dm.bring_to_front()
            dm.get_by_role("button", name="Initiative order", exact=True).click()
            rows = dm.get_by_role("list", name="Initiative entries")
            for label, character, score in (
                ("", "Bram", "12"),
                (monster, "", "15"),
                ("", "Elowen", "18"),
            ):
                dm.get_by_role("button", name="Add row", exact=True).click()
                row = rows.get_by_role("listitem").last
                if character:
                    choose(row, "Character", character)
                else:
                    row.get_by_label("Name", exact=True).fill(label)
                    row.get_by_label("Hidden", exact=True).check()
                row.get_by_label("Initiative", exact=True).fill(score)
            dm.get_by_role("button", name="Sort by initiative", exact=True).click()
            dm.get_by_role("button", name="Save order", exact=True).click()
            toast(dm, "Initiative order saved.")
            dm_turn = dm.get_by_role("region", name="Turn order")
            expect(dm_turn.get_by_text(monster, exact=True)).to_be_visible()
            shot(dm, args.output / "dm-initiative.png")

            def turn_banner(viewer):
                # The banner, and its screen-reader announcement while it shows.
                return viewer.get_by_text("Your turn", exact=True)

            def active_turn(viewer):
                return viewer.get_by_role("region", name="Turn order").locator(
                    "[aria-current=step]"
                )

            a.bring_to_front()
            expect(turn_banner(a).first).to_be_visible(timeout=5000)
            expect(active_turn(a)).to_contain_text("Elowen")
            b.bring_to_front()
            expect(active_turn(b)).to_contain_text("Elowen", timeout=5000)
            expect(turn_banner(b)).to_have_count(0)
            dm.bring_to_front()
            turn_controls = dm.get_by_role("group", name="Turn controls")
            turn_controls.get_by_role("button", name="Next turn", exact=True).click()
            expect(active_turn(dm)).to_contain_text(monster)
            for viewer in (a, b):
                viewer.bring_to_front()
                expect(active_turn(viewer)).to_contain_text("???", timeout=5000)
                expect(turn_banner(viewer)).to_have_count(0)
            dm.bring_to_front()
            turn_controls.get_by_role("button", name="Next turn", exact=True).click()
            expect(active_turn(dm)).to_contain_text("Bram")
            b.bring_to_front()
            expect(turn_banner(b).first).to_be_visible(timeout=5000)
            assert_fits(b)
            shot(b, args.output / "bram-your-turn.png")
            a.bring_to_front()
            expect(active_turn(a)).to_contain_text("Bram", timeout=5000)
            expect(turn_banner(a)).to_have_count(0)
            for viewer, own in ((a, elowen_id), (b, bram_id)):
                assert monster not in state_text(viewer)
                entries = session_read(viewer, "/initiative").json()["entries"]
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
            turn_controls.get_by_role(
                "button", name="End encounter", exact=True
            ).click()
            dm.get_by_role("alertdialog", name="End this encounter?").get_by_role(
                "button", name="End encounter", exact=True
            ).click()
            expect(dm_turn).to_have_count(0)
            for viewer in (a, b):
                viewer.bring_to_front()
                expect(viewer.get_by_role("region", name="Turn order")).to_have_count(
                    0, timeout=5000
                )
                expect(
                    feed(viewer).get_by_text("The encounter has ended.", exact=True)
                ).to_be_visible()
            dm.bring_to_front()
            dm.get_by_role("button", name="Initiative order", exact=True).click()
            report["checks"].append(
                "Initiative orders both PCs and a hidden monster; only the active player sees their turn banner, the monster stays masked in pages and payloads, and the DM ends the encounter"
            )
            # Handouts: one for the table and one for Elowen only.
            handouts = open_dialog(dm, "Send a handout", "Send a handout")
            public_title = f"Wanted poster {stamp}"
            private_title = f"ELOWEN_LETTER_{stamp}"
            private_text = f"Meet me at the mill at midnight, {stamp}."
            for title, text, recipient in (
                (public_title, "**Reward:** fifty gold for the smuggler.", None),
                (private_title, private_text, "Elowen"),
            ):
                field(handouts, "Handout title").fill(title)
                handouts.get_by_label("Handout text", exact=True).fill(text)
                if recipient:
                    handouts.get_by_label(
                        "Send to everyone at the table", exact=True
                    ).uncheck()
                    handouts.get_by_role("group", name="Send to").get_by_label(
                        recipient, exact=True
                    ).check()
                handouts.get_by_role("button", name="Send handout", exact=True).click()
                toast(dm, "Handout sent.")
                expect(field(handouts, "Handout title")).to_have_value("")
            close_dialog(handouts)
            a.bring_to_front()
            for title in (public_title, private_title):
                expect(
                    feed(a).get_by_role("heading", name=title, exact=True)
                ).to_be_visible(timeout=5000)
            expect(feed(a).get_by_text(private_text, exact=True)).to_be_visible()
            assert_fits(a)
            shot(a, args.output / "elowen-handouts.png")
            b.bring_to_front()
            expect(
                feed(b).get_by_role("heading", name=public_title, exact=True)
            ).to_be_visible(timeout=5000)
            expect(
                b.get_by_role("heading", name=private_title, exact=True)
            ).to_have_count(0)
            b_payload = state_text(b)
            assert public_title in b_payload
            assert private_title not in b_payload and private_text not in b_payload
            a.bring_to_front()
            a.get_by_role(
                "button", name=f"Pin {private_title} to my notes", exact=True
            ).click()
            toast(a, "Pinned to your notes.")
            pinned_handout = a_notes.get_by_role("listitem").filter(
                has=a.get_by_role("heading", name=private_title, exact=True)
            )
            expect(pinned_handout.get_by_text("Pinned", exact=True)).to_be_visible()
            expect(
                pinned_handout.get_by_text(private_text, exact=False).first
            ).to_be_visible()
            assert_fits(a)
            assert private_title in notes_text(a, "character")
            for viewer in (dm, b):
                assert private_title not in notes_text(viewer, "character")
            tab(a, "Story")
            report["checks"].append(
                "Handouts reach the table and one PC; Bram never receives Elowen's handout in page or payload, and Elowen pins it to private notes"
            )
            # Inventory: Give item turns the table handout into a party item.
            dm.bring_to_front()
            feed(dm).get_by_role(
                "button", name=f"Give item from {public_title}", exact=True
            ).click()
            give = dm.get_by_role("dialog", name="Give item", exact=True)
            expect(field(give, "Item name")).to_have_value(public_title)
            expect(
                give.get_by_role("combobox", name="Give to", exact=True)
            ).to_have_text("Party pool")
            give.get_by_role("button", name=f"Give {public_title}", exact=True).click()
            toast(dm, f"Gave {public_title}.")
            dm_pool = dm.get_by_role("region", name="Party pool")
            expect(dm_pool.get_by_role("heading", name=public_title)).to_be_visible()
            hidden_item = f"HIDDEN_ITEM_{stamp}"
            dm.get_by_role("region", name="Campaign inventory").get_by_role(
                "button", name="Give item", exact=True
            ).click()
            field(give, "Item name").fill(hidden_item)
            give.get_by_label("Hidden from party", exact=True).check()
            give.get_by_role("button", name=f"Give {hidden_item}", exact=True).click()
            toast(dm, f"Gave {hidden_item}.")
            expect(dm_pool.get_by_role("heading", name=hidden_item)).to_be_visible()
            shot(dm, args.output / "dm-inventory.png")
            item_ids = {
                item["name"]: item["id"]
                for item in campaign_read(dm, "/inventory").json()
            }
            public_id, hidden_id = item_ids[public_title], item_ids[hidden_item]
            for viewer in (a, b):
                viewer.bring_to_front()
                tab(viewer, "Inventory")
                expect(
                    viewer.get_by_role("region", name="Party pool").get_by_role(
                        "heading", name=public_title
                    )
                ).to_be_visible(timeout=6000)
                expect(viewer.get_by_text(hidden_item)).to_have_count(0)
                assert_fits(viewer)
                # Each view must carry the public item, so its absence checks
                # cannot pass on an empty or failed response.
                for payload, shown in (
                    (
                        campaign_read(viewer, "/inventory").text(),
                        (public_title, public_id),
                    ),
                    (
                        campaign_read(viewer, "/inventory/changes").text(),
                        (public_id,),
                    ),
                    (state_text(viewer), (public_title,)),
                ):
                    assert all(value in payload for value in shown), shown
                    assert hidden_item not in payload and hidden_id not in payload
            shot(b, args.output / "bram-inventory.png")
            for viewer in (a, b, dm):
                tab(viewer, "Story")
            report["checks"].append(
                "Give item turns a handout into a party-pool item both players see; a hidden party item never reaches player pages or payloads"
            )
            # Knowledge search: run the play embedder on demand, then search
            # as a player. DM-only and other-PC text must stay out of results.
            dm.bring_to_front()
            dm_canary = f"DM_ONLY_SEARCH_{stamp}"
            say(dm, f"{dm_canary}: Mara pays the smugglers.", "DM notes")
            expect(
                dm.get_by_text(f"{dm_canary}: Mara pays the smugglers.", exact=True)
            ).to_be_visible()
            choose(dm.get_by_role("region", name="Message"), "Send to", "Everyone")
            # Bram holds Ilsa's full knowledge only until the retraction below.
            ilsa_secret = "HARBOUR_LEDGER_SECRET"
            open_reveal(dm)
            pick_knowledge(dm, "Ilsa", "Harbourmaster Ilsa")
            share_with.get_by_label("Bram", exact=True).check()
            choose(drawer, "Knowledge scope", "Full knowledge")
            drawer.get_by_role("button", name="Preview knowledge", exact=True).click()
            share.click()

            def bram_holds_ilsa():
                assert ilsa_secret in state_text(b)

            eventually(bram_holds_ilsa)
            close_reveal(dm)
            embedded = dm.request.post(f"{ORIGIN}/__local/embed-play")
            assert embedded.status == 204, embedded.status

            def search(viewer, query):
                return campaign_read(viewer, "/knowledge/search", q=query).text()

            # Positive controls: the DM note and Elowen's handout are indexed.
            assert dm_canary in search(dm, dm_canary)
            assert private_text in search(a, private_text)
            b.bring_to_front()
            finder = b.get_by_role("region", name="Character knowledge search")

            def find(query):
                """Search through Bram's page and wait for this response to render."""
                finder.get_by_role(
                    "searchbox", name="Search what your character knows"
                ).fill(query)
                with b.expect_response(
                    lambda response: "/knowledge/search" in response.url
                ) as found:
                    finder.get_by_role(
                        "button", name="Search knowledge", exact=True
                    ).click()
                assert found.value.ok, found.value.status
                count = len(found.value.json())
                # Submitting clears the previous results, so this count is new.
                expect(
                    finder.get_by_text(
                        f"{count} result{'' if count == 1 else 's'} found."
                        if count
                        else "No knowledge matches your search.",
                        exact=True,
                    )
                ).to_be_visible()
                return found.value.text()

            hits = find("Mara")
            assert "Mara opens the inn to the party." in hits and dm_canary not in hits
            expect(
                finder.get_by_text("Mara opens the inn to the party.").first
            ).to_be_visible()
            assert_fits(b)
            shot(b, args.output / "bram-knowledge-search.png")
            assert dm_canary not in find(dm_canary)
            expect(b.get_by_text(dm_canary)).to_have_count(0)
            assert ilsa_secret in find(ilsa_secret)
            expect(finder.get_by_text(ilsa_secret, exact=False).first).to_be_visible()
            dm.bring_to_front()
            open_reveal(dm)
            expect(
                drawer.get_by_role("heading", name="Harbourmaster Ilsa", exact=True)
            ).to_be_visible()
            drawer.get_by_role("button", name="Retract from Bram", exact=True).click()

            def bram_lost_ilsa():
                assert ilsa_secret not in state_text(b)

            eventually(bram_lost_ilsa)
            close_reveal(dm)
            b.bring_to_front()
            expect(
                b.get_by_text("Knowledge retracted: Harbourmaster Ilsa.", exact=True)
            ).to_be_visible(timeout=5000)
            # No embedder run since the grant: retraction alone removes it.
            assert ilsa_secret not in find(ilsa_secret)
            with defect(
                "Retracted knowledge leaves the player's page as soon as the retraction notice shows"
            ):
                expect(b.get_by_text(ilsa_secret, exact=False)).to_have_count(0)
            expect(b.get_by_text(ilsa_secret, exact=False)).to_have_count(
                0, timeout=30000
            )
            for query in (private_text, private_title):
                assert query not in search(b, query)
            report["checks"].append(
                "Player knowledge search finds granted knowledge and a full reveal after the play embedder runs; retracting that grant removes it without re-embedding; DM-only and other-PC text stays out of results"
            )
            dm.bring_to_front()
            dm.get_by_role("button", name="End session", exact=True).click()
            dm.get_by_role("alertdialog", name="End this session?").get_by_role(
                "button", name="End session", exact=True
            ).click()
            expect(
                dm.get_by_text("This session has ended. Your story is saved here.")
            ).to_be_visible()
            assert current_session(dm)["status"] == "ended"
            expect(feed(dm).get_by_text(scene, exact=True)).to_be_visible()
            report["checks"].append(
                "DM can pause, resume and end; ended story remains readable"
            )

            # A second campaign exercises the real invitation-to-character
            # path with the same signed identities, without preassigned PCs.
            def campaign_card(page, name="The Newcomers"):
                return page.get_by_role("article", name=name, exact=True)

            def open_players(page):
                """Expand the owner's roster and invitation section."""
                card = campaign_card(page)
                toggle = card.get_by_role(
                    "button", name="Players and invitations", exact=True
                )
                if toggle.get_attribute("aria-expanded") != "true":
                    toggle.click()
                expect(toggle).to_have_attribute("aria-expanded", "true")
                return card

            dm.goto(LOBBY)
            dm.get_by_label("Campaign name", exact=True).fill("The Newcomers")
            dm.get_by_role("button", name="Create campaign", exact=True).click()
            expect(campaign_card(dm)).to_be_visible()
            invite_links = {}
            for email in ("a@example.test", "b@example.test"):
                newcomers = open_players(dm)
                newcomers.get_by_label("Player's email or @username", exact=True).fill(
                    email
                )
                newcomers.get_by_role(
                    "button", name="Create invitation link", exact=True
                ).click()
                private_link = newcomers.get_by_label(
                    "Private invitation link", exact=True
                )
                expect(private_link).to_be_visible()
                invite_links[email] = private_link.input_value()
                newcomers.get_by_role("button", name="Hide link", exact=True).click()
                expect(private_link).to_have_count(0)
            newcomers_id = next(
                row["id"]
                for row in read(dm, "/lobby").json()["campaigns"]
                if row["name"] == "The Newcomers"
            )
            for player, email in ((a, "a@example.test"), (b, "b@example.test")):
                # A fragment-only goto would not reload the landing page.
                player.goto("about:blank")
                player.goto(invite_links[email])
                player.get_by_role("link", name="Sign in to accept", exact=True).click()
                player.get_by_role(
                    "button", name="Accept invitation", exact=True
                ).click()
                toast(player, "You joined The Newcomers.")
                expect(player).to_have_url(LOBBY)
                expect(campaign_card(player)).to_be_visible()
            joined = read(dm, f"/campaigns/{newcomers_id}/members").json()
            assert sorted(
                row["email"] for row in joined if row["role"] == "player"
            ) == [
                "a@example.test",
                "b@example.test",
            ]
            new_a = campaign_card(a)
            new_a.get_by_label("Character name", exact=True).fill("Nyx")
            new_a.get_by_role(
                "button", name="Create your character", exact=True
            ).click()
            expect(a.get_by_role("heading", name="Nyx", exact=True)).to_be_visible()
            a.goto(LOBBY)
            expect(new_a.get_by_text("Your character: Nyx", exact=True)).to_be_visible()
            dm.reload()
            newcomers = open_players(dm)
            newcomers.get_by_role("listitem").filter(
                has_text="b@example.test"
            ).get_by_role("button", name="Manage", exact=True).click()
            manage = dm.get_by_role("dialog", name="b@example.test")
            manage.get_by_label("New character name", exact=True).fill("Wren")
            manage.get_by_role("button", name="Create and assign", exact=True).click()
            toast(dm, "Character assignment saved.")
            b.reload()
            expect(
                campaign_card(b).get_by_text("Your character: Wren", exact=True)
            ).to_be_visible()
            assert {
                row["email"]: row["character_name"]
                for row in read(dm, f"/campaigns/{newcomers_id}/members").json()
                if row["role"] == "player"
            } == {"a@example.test": "Nyx", "b@example.test": "Wren"}
            shot(a, args.output / "player-onboarding.png")
            shot(dm, args.output / "dm-onboarding.png")
            report["checks"].append(
                "Invited players accept into a new campaign; player creates Nyx and DM creates and assigns Wren"
            )
            for player, name, class_name in (
                (a, "Nyx", "ranger"),
                (b, "Wren", "fighter"),
            ):
                player.goto(f"{LOBBY}/sheets")
                sheet = player.get_by_role("region", name=name, exact=True)
                field(sheet, "Ancestry").fill("Human")
                field(sheet, "Class").fill(class_name)
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
                expect(sheet.get_by_text("Submitted", exact=True).first).to_be_visible()
                shot(player, args.output / f"{name.lower()}-submitted.png")
            dm.goto(f"{LOBBY}/sheets")
            for name in ("Nyx", "Wren"):
                sheet = dm.get_by_role("region", name=name, exact=True)
                sheet.get_by_role("button", name="Approve version", exact=True).click()
                expect(sheet.get_by_text("Approved", exact=True).first).to_be_visible()
            dm.goto(LOBBY)
            campaign_card(dm).get_by_role(
                "link", name="Open session", exact=True
            ).click()
            dm.get_by_role("button", name="Start session", exact=True).click()
            expect(
                dm.get_by_role("button", name="Pause session", exact=True)
            ).to_be_visible()
            for player in (a, b):
                player.goto(LOBBY)
                campaign_card(player).get_by_role(
                    "link", name="Open session", exact=True
                ).click()
            expect(a.get_by_text("Nyx", exact=True).first).to_be_visible()
            expect(b.get_by_text("Wren", exact=True).first).to_be_visible()
            dice = open_dialog(a, "Roll dice", "Roll dice")
            dice.get_by_role("button", name="Strength check", exact=True).click()
            close_dialog(dice)
            expect(
                feed(a).get_by_text("Strength check", exact=False).last
            ).to_be_visible()
            nyx = campaign_read(a, "/characters").json()[0]
            assert nyx["character_name"] == "Nyx"
            assert approved_sheet(a, nyx["id"])["max_hit_points"] == 11
            assert (
                next(
                    row["body"]["modifier"]
                    for row in events_of(a)
                    if (row["body"] or {}).get("label") == "Strength check"
                )
                == 2
            )
            shot(a, args.output / "new-player-session.png")
            shot(dm, args.output / "new-dm-session.png")
            report["checks"].append(
                "Both invited players submit sheets, DM approves them, and the new table enters a session with approved-sheet rolls"
            )
            assert not report["errors"], report["errors"]
        except Exception as error:
            report["passed"] = False
            report["failure"] = str(error) or repr(error)
            if args.join_links:
                raise RuntimeError(redact_join_artifact(report["failure"])) from None
            raise
        finally:
            seen = set()
            for miss in POINTER_MISSES:
                key = (miss["control"], miss["option"])
                if key not in seen:
                    seen.add(key)
                    report["failed_checks"].append(
                        {
                            "check": f"The pointer can choose {miss['option']!r} in the {miss['control']!r} selector",
                            "observed": miss["observed"],
                        }
                    )
            report["passed"] = bool(
                report.get("passed", True) and not report["failed_checks"]
            )
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
                        if not report.get("passed") and not args.join_links:
                            # What a locator saw when the rehearsal stopped.
                            (args.output / f"{role}-final-{index}.aria.yml").write_text(
                                f"# {page.url}\n"
                                + page.locator("body").aria_snapshot()
                                + "\n"
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
            if report["failed_checks"]:
                lines.extend(["", "## Failed checks", ""])
                lines.extend(
                    f"- {row['check']}: {row['observed']}"
                    for row in report["failed_checks"]
                )
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
            if report["failed_checks"]:
                raise SystemExit(
                    f"{len(report['failed_checks'])} rehearsal check(s) failed; inspect report.json"
                )


if __name__ == "__main__":
    main()
