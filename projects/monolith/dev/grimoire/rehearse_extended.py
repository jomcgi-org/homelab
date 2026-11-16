"""Additional three-seat journeys against the disposable local table."""

import re
import time
from contextlib import contextmanager

from playwright.sync_api import expect
from rehearsal_report import declare_refused_poll

ORIGIN = "http://friends.localhost:8177"
API = f"{ORIGIN}/api/grimoire"
CAMPAIGN = "The Lantern at the Crossroads"
REFRESH_MS = 6000

CHECKS = (
    "Feedback preserves drafts through Escape and browser Back, submits once, and records votes",
    "Returned sheets can be edited, resubmitted and approved across open tabs without reloads",
    "Disadvantage rolls use the lower die plus modifier and respect public and private audiences",
    "Read-aloud controls persist and the synthetic speech queue respects each seat's audience",
    "Character reassignment changes access and retains sheet history",
    "Changing the signed-in demo account clears the previous player's private note",
)

# This adapter observes the application's real selection and queue. It replaces
# the device voice engine only; feed data still comes from the real API.
SPEECH_ADAPTER = """(() => {
  window.__grimoireSpeech = [];
  const synth = {
    getVoices: () => [{name: 'Local rehearsal voice', lang: 'en-GB', default: true}],
    addEventListener() {}, removeEventListener() {}, cancel() {},
    speak(utterance) {
      window.__grimoireSpeech.push(utterance.text);
      queueMicrotask(() => utterance.onend?.());
    },
  };
  Object.defineProperty(window, 'speechSynthesis', {value: synth, configurable: true});
  Object.defineProperty(window, 'SpeechSynthesisUtterance', {
    value: class {constructor(text) {this.text = text;}}, configurable: true,
  });
})();"""


def rehearse_extended(pages, output, report, width):
    """Run after the core scenario; every mutation uses a signed local seat."""
    from rehearse import (
        assert_fits,
        choose,
        eventually,
        fetch_loopback_route,
        field,
        read,
        say,
        toast,
    )
    from rehearse_join_links import observe_page

    dm, a, b = (pages[role] for role in ("dm", "a", "b"))
    for page in pages.values():
        page.set_viewport_size({"width": width, "height": 900})
    lobby = read(dm, "/lobby").json()
    campaign = next(row for row in lobby["campaigns"] if row["name"] == CAMPAIGN)
    cid = campaign["id"]
    base = f"/campaigns/{cid}"
    characters = {
        row["character_name"]: row["id"]
        for row in read(dm, f"{base}/characters").json()
    }

    def goto(page, path):
        page.goto(f"{ORIGIN}/grimoire{path}")
        expect(page.get_by_role("button", name="Feedback", exact=True)).to_be_visible()

    def capture(stage, roles=("dm", "a", "b")):
        for role in roles:
            page = pages[role]
            assert_fits(page)
            name = f"extended-{width}-{stage}-{role}.png"
            page.screenshot(path=str(output / name), full_page=True)
            report.setdefault("screenshots", []).append(
                {"file": name, "seat": role, "width": width, "url": page.url}
            )

    def run(index, key, action):
        started = time.monotonic()
        failed = False
        try:
            action()
            report["checks"].append(CHECKS[index])
        except Exception as error:
            failed = True
            report["failed_checks"].append(
                {"check": CHECKS[index], "observed": str(error) or repr(error)}
            )
            for role, page in pages.items():
                try:
                    page.screenshot(
                        path=str(output / f"extended-{width}-{key}-failed-{role}.png"),
                        full_page=True,
                    )
                    (
                        output / f"extended-{width}-{key}-failed-{role}.aria.yml"
                    ).write_text(page.locator("body").aria_snapshot())
                except Exception as capture_error:
                    report.setdefault("capture_errors", []).append(str(capture_error))
        finally:
            report["timings"][f"extended_{key}_seconds"] = time.monotonic() - started
            if failed:
                report["checks"].ready()

    def refresh(key, action):
        started = time.monotonic()
        action()
        elapsed = time.monotonic() - started
        report["timings"][f"extended_{key}_refresh_seconds"] = elapsed
        assert elapsed <= REFRESH_MS / 1000, f"{key} refresh took {elapsed:.3f}s"

    def feedback():
        goto(a, "")
        message = f"Local {width}px rehearsal: clearer sheet return notifications."
        a.get_by_role("button", name="Feedback", exact=True).click()
        dialog = a.get_by_role("dialog", name="Feedback", exact=True)
        text = dialog.get_by_role("textbox", name="What should change?")
        expect(text).to_be_focused()
        text.fill(message)
        a.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(a.get_by_role("button", name="Feedback", exact=True)).to_be_focused()
        a.get_by_role("button", name="Feedback", exact=True).click()
        expect(text).to_have_value(message)
        dialog.get_by_role("link", name="See what's already suggested").click()
        expect(a.get_by_role("heading", name="Roadmap", exact=True)).to_be_visible()
        a.go_back()
        a.get_by_role("button", name="Feedback", exact=True).click()
        expect(text).to_have_value(message)
        capture("feedback-draft", ("a",))
        dialog.get_by_role("button", name="Send", exact=True).click()
        toast(a, "Thanks. Sent.")
        rows = read(a, "/feedback/roadmap").json()
        assert sum(row["text"] == message for row in rows) == 1
        for page in (a, b, dm):
            goto(page, "/roadmap")
            expect(page.get_by_text(message, exact=True)).to_be_visible()
        # This fixture starts with no feedback. An author's suggestion has no
        # vote control; another signed seat can add and withdraw one vote.
        assert a.get_by_role("button", name=re.compile(r"^Me too")).count() == 0
        b.get_by_role("button", name="Me too (0)", exact=True).click()
        expect(
            b.get_by_role("button", name="Me too (1)", exact=True)
        ).to_have_attribute("aria-pressed", "true")
        b.get_by_role("button", name="Me too (1)", exact=True).click()
        expect(
            b.get_by_role("button", name="Me too (0)", exact=True)
        ).to_have_attribute("aria-pressed", "false")
        for row in read(b, "/feedback/roadmap").json():
            assert not {"author", "author_email", "page", "campaign_id"} & row.keys()
        capture("roadmap")

    run(0, "feedback", feedback)

    def sheets():
        for page in (a, b, dm):
            goto(page, "/sheets")
            expect(
                page.get_by_role("heading", name="Character sheets", exact=True)
            ).to_be_visible()
        own = a.get_by_role("region", name="Elowen", exact=True)
        review = dm.get_by_role("region", name="Elowen", exact=True)
        history_path = f"{base}/characters/{characters['Elowen']}/sheets"
        next_version = read(a, history_path).json()["versions"][0]["version"] + 1
        own.get_by_role("spinbutton", name="Level", exact=True).fill("2")
        own.get_by_role("button", name="Start new draft", exact=True).click()
        toast(a, "Draft saved.")
        own.get_by_role("button", name="Submit to DM", exact=True).click()
        toast(a, "Submitted to the DM.")
        expect(
            own.get_by_text(
                f"Version {next_version} is waiting for the DM.", exact=True
            )
        ).to_be_visible()
        dm.bring_to_front()
        refresh(
            "sheet_submission",
            lambda: expect(
                review.get_by_role("button", name="Return to player", exact=True)
            ).to_be_visible(timeout=REFRESH_MS),
        )
        review.get_by_role("button", name="Return to player", exact=True).click()
        expect(
            review.get_by_text("Say what the player should change.", exact=True)
        ).to_be_visible()
        comment = "Please set DEX to 16 before resubmitting."
        review.get_by_role("textbox", name="Decision comment").fill(comment)
        review.get_by_role("button", name="Return to player", exact=True).click()
        toast(dm, "Sheet returned to the player.")
        a.bring_to_front()
        refresh(
            "sheet_return",
            lambda: expect(
                own.get_by_role("button", name="Start new draft", exact=True)
            ).to_be_visible(timeout=REFRESH_MS),
        )
        expect(own.get_by_role("blockquote")).to_contain_text(comment)
        capture("sheet-returned", ("a", "dm"))
        own.get_by_role("spinbutton", name="DEX", exact=True).fill("16")
        own.get_by_role("button", name="Start new draft", exact=True).click()
        toast(a, "Draft saved.")
        own.get_by_role("button", name="Submit to DM", exact=True).click()
        toast(a, "Submitted to the DM.")
        dm.bring_to_front()
        refresh(
            "sheet_resubmission",
            lambda: expect(
                review.get_by_role("button", name="Approve version", exact=True)
            ).to_be_visible(timeout=REFRESH_MS),
        )
        review.get_by_role("button", name="Approve version", exact=True).click()
        toast(dm, "Sheet approved.")
        a.bring_to_front()
        refresh(
            "sheet_approval",
            lambda: expect(own.get_by_text("Approved", exact=True)).to_be_visible(
                timeout=REFRESH_MS
            ),
        )
        latest = read(a, history_path).json()["versions"][0]
        assert latest["status"] == "approved"
        assert latest["sheet"]["ability_scores"]["dexterity"] == 16
        capture("sheet-approved")

    run(1, "sheet_round_trip", sheets)

    def rolls():
        for page in (dm, a, b):
            goto(page, f"{base}/session")
        if dm.get_by_role("button", name="Start session", exact=True).count():
            dm.get_by_role("button", name="Start session", exact=True).click()
        expect(
            dm.get_by_role("button", name="Pause session", exact=True)
        ).to_be_visible()
        expect(a.get_by_role("button", name="Roll dice", exact=True)).to_be_visible(
            timeout=REFRESH_MS
        )
        session_id = read(dm, f"{base}/sessions").json()[0]["id"]
        events_path = f"{base}/sessions/{session_id}/events"
        a.get_by_role("button", name="Roll dice", exact=True).click()
        tray = a.get_by_role("dialog", name="Roll dice", exact=True)
        tray.get_by_role("radio", name="Disadvantage", exact=True).click()
        tray.get_by_role("textbox", name="Dice formula", exact=True).fill("d20+3")
        for visibility, label in (("Everyone", "public"), ("DM and me", "private")):
            marker = f"Local {width}px disadvantage {label}"
            choose(tray, "Roll visibility", visibility)
            tray.get_by_role("textbox", name="Roll label", exact=True).fill(marker)
            tray.get_by_role("button", name="Roll", exact=True).click()

            def find_roll():
                matches = [
                    row
                    for row in read(a, events_path, after=0, limit=500).json()
                    if (row.get("body") or {}).get("label") == marker
                ]
                assert len(matches) == 1, matches
                return matches[0]

            event = eventually(find_roll)
            body = event["body"]
            assert len(body["rolls"]) == 2
            assert body["total"] == min(body["rolls"]) + 3
            assert any(row["id"] == event["id"] for row in read(dm, events_path).json())
            visible_to_bram = any(
                row["id"] == event["id"] for row in read(b, events_path).json()
            )
            assert visible_to_bram == (label == "public")
        a.keyboard.press("Escape")
        capture("disadvantage")

    run(2, "disadvantage", rolls)

    def read_aloud():
        for page in (dm, a, b):
            page.context.add_init_script(SPEECH_ADAPTER)
            goto(page, f"{base}/session")
            page.get_by_role("button", name="Read aloud", exact=True).click()
            toggle = page.get_by_role("checkbox", name="Read DM narration", exact=True)
            original = toggle.is_checked()
            toggle.click()
            page.reload()
            page.get_by_role("button", name="Read aloud", exact=True).click()
            toggle = page.get_by_role("checkbox", name="Read DM narration", exact=True)
            assert toggle.is_checked() != original
            # History is marked handled and must not be spoken on reload.
            assert page.evaluate("window.__grimoireSpeech") == []
            desired = page in (dm, a)
            if toggle.is_checked() != desired:
                toggle.click()
            private = page.get_by_role(
                "checkbox", name="Read my reveals and private narration", exact=True
            )
            if page == dm:
                assert private.count() == 0
            elif private.is_checked() != (page == a):
                private.click()
        public = f"Synthetic speech table canary {width}"
        private = f"Synthetic speech Elowen private canary {width}"
        dm_only = f"Synthetic speech DM private canary {width}"
        say(dm, public, "Everyone")
        for page in (dm, a):
            page.wait_for_function(
                "text => window.__grimoireSpeech.includes(text)",
                arg=public,
                timeout=REFRESH_MS,
            )
        assert b.evaluate("window.__grimoireSpeech") == []
        say(dm, private, "Elowen and DM")
        a.wait_for_function(
            "text => window.__grimoireSpeech.includes(text)",
            arg=private,
            timeout=REFRESH_MS,
        )
        say(dm, dm_only, "DM notes")
        # The DM receives both private messages but its room speaker is table-only.
        expect(dm.get_by_text(dm_only, exact=True)).to_be_visible(timeout=REFRESH_MS)
        barrier = f"Synthetic speech delivery barrier {width}"
        say(dm, barrier, "Everyone")
        for page in (dm, a, b):
            expect(page.get_by_text(barrier, exact=True)).to_be_visible(
                timeout=REFRESH_MS
            )
        capture("synthetic-read-aloud")
        assert private not in dm.evaluate("window.__grimoireSpeech")
        for page in (dm, a, b):
            assert dm_only not in page.evaluate("window.__grimoireSpeech")
        # Check after Bram has caught up and rendered the barrier. Inspecting
        # an empty queue before his next poll could miss disabled public speech.
        assert b.evaluate("window.__grimoireSpeech") == []
        report.setdefault("unavailable", []).append(
            {
                "journey": "Audible read-aloud",
                "reason": "A deterministic local speech engine verifies selection and queue behavior; real device audio is not available in this headless rehearsal.",
            }
        )

    run(3, "read_aloud", read_aloud)

    def assignments():
        for page in (dm, a, b):
            goto(page, f"{base}/session")
        canaries = {
            "a": f"Elowen former character private canary {width}",
            "b": f"Bram former character private canary {width}",
        }
        say(dm, canaries["a"], "Elowen and DM")
        say(dm, canaries["b"], "Bram and DM")
        for role in ("a", "b"):
            expect(pages[role].get_by_text(canaries[role], exact=True)).to_be_visible(
                timeout=REFRESH_MS
            )
        goto(dm, "")
        article = dm.get_by_role("article", name=CAMPAIGN, exact=True)
        article.get_by_role(
            "button", name="Players and invitations", exact=True
        ).click()

        def manage(email):
            article.get_by_role("listitem").filter(has_text=email).get_by_role(
                "button", name="Manage", exact=True
            ).click()
            return dm.get_by_role("dialog", name=email, exact=True)

        def histories():
            return {
                name: read(dm, f"{base}/characters/{identifier}/sheets").json()[
                    "versions"
                ]
                for name, identifier in characters.items()
            }

        before = histories()
        lobby_answers = {}

        @contextmanager
        def observe_lobbies():
            # The viewer fence reloads as soon as it consumes this response.
            # Chrome can discard response.body() before a later waiter reads
            # it. Capture the real backend answer before delivering it, with
            # the original seat cookie and request, instead of inventing data.
            def observe(route):
                response = fetch_loopback_route(route)
                if response.ok:
                    lobby_answers[route.request] = response.json()
                route.fulfill(response=response)

            for page in (a, b):
                page.route(f"{API}/lobby", observe)
            try:
                yield
            finally:
                for page in (a, b):
                    page.unroute(f"{API}/lobby", observe)

        def confirms(response, character_id):
            return (
                response.url == f"{API}/lobby"
                and response.status == 200
                and any(
                    row["id"] == cid and row["player_character_id"] == character_id
                    for row in lobby_answers.get(response.request, {}).get(
                        "campaigns", []
                    )
                )
            )

        # An open session page polls each held character's sheets every 5 s.
        # Until it reloads, a just-lost character's poll gets 404: declare it.
        revoked = {"a": set(), "b": set()}

        def sheets_url(name):
            return f"{API}{base}/characters/{characters[name]}/sheets"

        def refused_poll(role):
            def declare(response):
                declare_refused_poll(
                    report,
                    role,
                    response.request.method,
                    response.url,
                    response.status,
                    {sheets_url(name) for name in revoked[role]},
                )

            return declare

        @contextmanager
        def declare_refused_polls():
            listeners = {role: refused_poll(role) for role in revoked}
            for role, listener in listeners.items():
                pages[role].on("response", listener)
            try:
                yield
            finally:
                for role, listener in listeners.items():
                    pages[role].remove_listener("response", listener)

        with observe_lobbies(), declare_refused_polls():
            # Keep both player pages mounted while their existing character access
            # changes. An API-only check would miss a previous character's cached
            # private feed. Start both listeners before either mutation.
            with (
                a.expect_event("load", timeout=20000),
                b.expect_event("load", timeout=20000),
            ):
                with (
                    a.expect_response(
                        lambda response: confirms(response, characters["Bram"]),
                        timeout=20000,
                    ),
                    b.expect_response(
                        lambda response: confirms(response, None), timeout=20000
                    ),
                ):
                    revoked["b"] = {"Bram"}
                    manage("b@example.test").get_by_role(
                        "button", name="Clear assignment", exact=True
                    ).click()
                    toast(dm, "Character assignment saved.")
                    revoked["a"] = {"Elowen"}
                    dialog = manage("a@example.test")
                    choose(dialog, "Unassigned character", "Bram")
                    dialog.get_by_role("button", name="Assign", exact=True).click()
                    toast(dm, "Character assignment saved.")
                assert read(b, f"{base}/characters").json() == []
                assert [row["id"] for row in read(a, f"{base}/characters").json()] == [
                    characters["Bram"]
                ]
                for role, old_character in (("a", "Elowen"), ("b", "Bram")):
                    page = pages[role]
                    expect(
                        page.get_by_text(canaries[role], exact=True)
                    ).not_to_be_visible(timeout=REFRESH_MS)
                    assert (
                        page.request.get(
                            f"{API}{base}/characters/{characters[old_character]}/sheets"
                        ).status
                        == 404
                    )
                current_session = read(a, f"{base}/sessions").json()[0]["id"]
                current_events = read(
                    a, f"{base}/sessions/{current_session}/events"
                ).text()
                assert canaries["a"] not in current_events
                assert canaries["b"] in current_events
                capture("assignment-swapped")
            # Restoring a character changes the viewer boundary again. Let each
            # open page confirm its restored access and finish its own reload
            # before the next journey navigates it elsewhere. Elowen's player
            # loses Bram first; Bram's player held nothing until restored.
            revoked.update(a={"Bram"}, b=set())
            with (
                a.expect_event("load", timeout=20000),
                b.expect_event("load", timeout=20000),
                a.expect_response(
                    lambda response: confirms(response, characters["Elowen"]),
                    timeout=20000,
                ),
                b.expect_response(
                    lambda response: confirms(response, characters["Bram"]),
                    timeout=20000,
                ),
            ):
                for email, name in (
                    ("a@example.test", "Elowen"),
                    ("b@example.test", "Bram"),
                ):
                    dialog = manage(email)
                    choose(dialog, "Unassigned character", name)
                    dialog.get_by_role("button", name="Assign", exact=True).click()
                    toast(dm, "Character assignment saved.")
            assert histories() == before
            for page in (a, b):
                goto(page, "")
            capture("assignment-restored")

    run(4, "assignments", assignments)

    def identity_switch():
        canary = f"ELOWEN_PRIVATE_IDENTITY_SWITCH_CANARY_{width}"
        title = f"Elowen private rehearsal note {width}"
        goto(a, f"{base}/notes")
        notes = a.get_by_role("region", name="Campaign notes", exact=True)
        field(notes, "Note title").fill(title)
        field(notes, "Note text").fill(canary)
        choose(notes, "DM sharing", "Private")
        notes.get_by_role("button", name="Add note", exact=True).click()
        toast(a, "Note added.")
        expect(a.get_by_role("paragraph").filter(has_text=canary)).to_be_visible()
        note = next(
            row for row in read(a, f"{base}/notes").json() if row["title"] == title
        )
        switch = a.context.new_page()
        observe_page(switch, report, "a")
        try:
            with a.expect_response(
                lambda response: (
                    response.url.endswith("/api/grimoire/lobby")
                    and response.status == 200
                    and response.json().get("user", {}).get("email") == "b@example.test"
                ),
                timeout=20000,
            ):
                switch.goto(f"{ORIGIN}/__local/login/b")
                a.bring_to_front()
            started = time.monotonic()
            # Also reject the old value in the card's hidden edit textarea.
            expect(a.get_by_text(canary, exact=True)).to_have_count(
                0, timeout=REFRESH_MS
            )
            report["timings"]["extended_identity_scrub_seconds"] = (
                time.monotonic() - started
            )
            assert a.request.get(f"{API}{base}/notes/{note['id']}").status == 404
            capture("identity-switched", ("a",))
        finally:
            switch.goto(f"{ORIGIN}/__local/login/a")
            switch.close()
            goto(a, "")

    run(5, "identity", identity_switch)
