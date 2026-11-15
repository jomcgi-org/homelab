"""Keep two real player editors safe across concurrent writes and decisions."""

import time

from playwright.sync_api import expect
from rehearsal_report import expect_network_diagnostic
from rehearse_join_links import observe_page

CHECKS = (
    "Concurrent sheet edits merge without loss and stale saves are rejected",
    "Remote submission preserves unsaved sheet edits for copying and recovery",
)


def rehearse_sheet_conflicts(pages, output, report, width):
    from rehearse import API, LOBBY, ORIGIN, fetch_loopback_route, read, shot, toast

    dm, a, b = (pages[role] for role in ("dm", "a", "b"))
    campaign = next(
        row
        for row in read(dm, "/lobby").json()["campaigns"]
        if row["name"] == "The Lantern at the Crossroads"
    )
    cid = campaign["id"]
    character = next(
        row
        for row in read(dm, f"/campaigns/{cid}/characters").json()
        if row["character_name"] == "Elowen"
    )
    history_path = f"/campaigns/{cid}/characters/{character['id']}/sheets"
    second = a.context.new_page()
    second.set_viewport_size({"width": width, "height": 844})
    observe_page(second, report, "a")

    def editor(page):
        return page.get_by_role("region", name="Elowen", exact=True)

    def open_editor(page):
        page.goto(f"{LOBBY}/sheets")
        expect(editor(page)).to_be_visible()

    def latest():
        return read(a, history_path).json()["versions"][0]

    def number(page, name, value):
        control = editor(page).get_by_role("spinbutton", name=name, exact=True)
        control.fill(str(value))
        # NumberInput commits a finished number on blur. The submission case
        # below deliberately leaves one number focused to test raw input too.
        control.press("Tab")
        expect(control).to_have_value(str(value), timeout=1000)

    def value(page, name, expected):
        expect(
            editor(page).get_by_role("spinbutton", name=name, exact=True)
        ).to_have_value(str(expected), timeout=6000)

    def save(page):
        editor(page).get_by_role("button", name="Save draft", exact=True).click()
        toast(page, "Draft saved.")

    def run(index, action):
        started = time.monotonic()
        try:
            action()
            report["checks"].append(CHECKS[index])
        except Exception as error:
            report["failed_checks"].append(
                {"check": CHECKS[index], "observed": str(error) or repr(error)}
            )
            for role, page in (("a", a), ("a-other-tab", second), ("dm", dm)):
                try:
                    shot(page, output / f"sheet-conflict-{index}-{role}-failed.png")
                except Exception as capture_error:
                    report.setdefault("capture_errors", []).append(str(capture_error))
            report["checks"].ready()
        finally:
            report["timings"][f"sheet_conflict_{index}_seconds"] = (
                time.monotonic() - started
            )

    def concurrent_edits():
        open_editor(a)
        number(a, "Level", 3)
        editor(a).get_by_role("button", name="Start new draft", exact=True).click()
        toast(a, "Draft saved.")
        open_editor(second)
        draft = latest()
        assert draft["status"] == "draft"
        number(a, "DEX", 17)
        number(second, "INT", 18)
        save(second)
        a.bring_to_front()
        value(a, "INT", 18)
        value(a, "DEX", 17)
        save(a)
        merged = latest()
        assert merged["sheet"]["ability_scores"]["dexterity"] == 17
        assert merged["sheet"]["ability_scores"]["intelligence"] == 18
        second.bring_to_front()
        value(second, "DEX", 17)

        # Freeze one real PATCH after the UI chose its revision. Another tab
        # changes the server before that PATCH reaches its row lock.
        number(a, "DEX", 19)
        number(second, "INT", 20)
        stale_base = latest()
        patch_url = f"{API}{history_path}/{draft['id']}"
        held = []

        def hold(route):
            if route.request.method == "PATCH":
                route.request.rehearsal_injected = True
                held.append(route)
            else:
                route.continue_()

        a.route(patch_url, hold)
        try:
            editor(a).get_by_role("button", name="Save draft", exact=True).click()
            a.wait_for_timeout(150)
            assert len(held) == 1
            assert (
                held[0].request.post_data_json["expected_updated_at"]
                == stale_base["updated_at"]
            )
            save(second)
            remote = latest()
            assert remote["updated_at"] != stale_base["updated_at"]
            assert remote["sheet"]["ability_scores"]["dexterity"] == 17
            assert remote["sheet"]["ability_scores"]["intelligence"] == 20
            expect_network_diagnostic(report, "a", "PATCH", patch_url, status=409)
            response = fetch_loopback_route(held[0])
            assert response.status == 409
            assert response.json()["detail"] == (
                "This sheet changed since you opened it. Review the latest version before saving."
            )
            held.pop().fulfill(response=response)
        finally:
            for route in held:
                route.abort()
            a.unroute(patch_url, hold)
        toast(
            a,
            "This sheet changed since you opened it. Review the latest version before saving.",
        )
        assert latest() == remote, "The stale PATCH changed the winning version"
        a.bring_to_front()
        value(a, "DEX", 19)
        value(a, "INT", 20)
        shot(a, output / "sheet-stale-save-preserved.png")
        save(a)
        assert latest()["sheet"]["ability_scores"]["dexterity"] == 19
        assert latest()["sheet"]["ability_scores"]["intelligence"] == 20

        # An overlapping change requires the player's explicit choice.
        second.bring_to_front()
        value(second, "DEX", 19)
        number(a, "DEX", 18)
        number(second, "DEX", 17)
        save(second)
        a.bring_to_front()
        conflict = editor(a).get_by_role("region", name="Resolve sheet changes")
        expect(conflict).to_be_visible(timeout=6000)
        expect(
            editor(a).get_by_role("button", name="Save draft", exact=True)
        ).to_be_disabled()
        expect(
            editor(a).get_by_role("button", name="Submit to DM", exact=True)
        ).to_be_disabled()
        value(a, "DEX", 18)
        shot(a, output / "sheet-overlapping-changes.png")
        conflict.get_by_role(
            "button", name="Keep my conflicting edits", exact=True
        ).click()
        save(a)
        assert latest()["sheet"]["ability_scores"]["dexterity"] == 18
        assert latest()["sheet"]["ability_scores"]["intelligence"] == 20
        assert b.request.get(f"{API}{history_path}").status == 404
        shot(a, output / "sheet-conflicts-resolved.png")

    def remote_submission():
        for page in (a, second, dm):
            open_editor(page)
        original = latest()
        assert original["status"] == "draft"
        # Keep this number focused: a remote status change must preserve even
        # the raw number the player has not yet committed by blurring it.
        editor(a).get_by_role("spinbutton", name="Level", exact=True).fill("4")
        editor(second).get_by_role("button", name="Submit to DM", exact=True).click()
        toast(second, "Submitted to the DM.")
        a.bring_to_front()
        recovery = editor(a).get_by_role("region", name="Unsaved sheet changes")
        expect(recovery).to_be_visible(timeout=6000)
        snapshot = recovery.get_by_role("textbox", name="Your unsaved sheet changes")
        expect(snapshot).not_to_be_editable()
        assert "Level: 4" in snapshot.input_value()
        expect(editor(a).get_by_role("spinbutton")).to_have_count(0)
        for label in ("Save draft", "Submit to DM"):
            expect(
                editor(a).get_by_role("button", name=label, exact=True)
            ).to_have_count(0)
        a.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=ORIGIN
        )
        recovery.get_by_role(
            "button", name="Copy my unsaved changes", exact=True
        ).click()
        toast(a, "Unsaved sheet changes copied.")
        assert a.evaluate("navigator.clipboard.readText()") == snapshot.input_value()
        shot(a, output / "sheet-submitted-unsaved-copy.png")
        submitted = latest()
        assert (
            submitted["status"] == "submitted"
            and submitted["sheet"] == original["sheet"]
        )
        version_url = f"{API}{history_path}/{submitted['id']}"
        refused = a.request.patch(
            version_url,
            data={
                **submitted["sheet"],
                "level": 5,
                "expected_updated_at": submitted["updated_at"],
            },
        )
        assert refused.status == 409
        assert a.request.post(version_url + "/submit", data={}).status == 409
        assert latest() == submitted, "A submitted version was rewritten"

        dm.bring_to_front()
        review = editor(dm)
        expect(
            review.get_by_role("button", name="Return to player", exact=True)
        ).to_be_visible(timeout=6000)
        review.get_by_role("textbox", name="Decision comment").fill(
            "Keep your level 4 edit and resubmit."
        )
        review.get_by_role("button", name="Return to player", exact=True).click()
        toast(dm, "Sheet returned to the player.")
        a.bring_to_front()
        value(a, "Level", 4)
        editor(a).get_by_role("button", name="Start new draft", exact=True).click()
        toast(a, "Draft saved.")
        replacement = latest()
        assert replacement["id"] != original["id"]
        assert replacement["sheet"]["level"] == 4
        assert replacement["sheet"]["ability_scores"]["intelligence"] == 20
        history = read(a, history_path).json()["versions"]
        old = next(row for row in history if row["id"] == original["id"])
        assert old["status"] == "returned" and old["sheet"] == original["sheet"]
        editor(a).get_by_role("button", name="Submit to DM", exact=True).click()
        toast(a, "Submitted to the DM.")
        dm.bring_to_front()
        expect(
            review.get_by_role("button", name="Approve version", exact=True)
        ).to_be_visible(timeout=6000)
        review.get_by_role("button", name="Approve version", exact=True).click()
        toast(dm, "Sheet approved.")
        a.bring_to_front()
        expect(editor(a).get_by_text("Approved", exact=True)).to_be_visible(
            timeout=6000
        )
        assert latest()["status"] == "approved"
        assert b.request.get(f"{API}{history_path}").status == 404
        shot(a, output / "sheet-recovered-and-approved.png")

    try:
        run(0, concurrent_edits)
        run(1, remote_submission)
    finally:
        second.close()
