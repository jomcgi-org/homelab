"""Hidden recovery with real signed identities and disposable campaign data."""

from playwright.sync_api import expect
from rehearsal_report import expect_network_diagnostic
from rehearse_join_links import observe_page

CHECKS = (
    "Dirty sheets stay hidden through clear and swap and recover only after renewed access",
    "Sheet recovery survives expiry without crossing an account boundary",
    "Membership removal denies old sheet reads and writes until fresh admission",
    "Explicit sign-out clears sheet recovery and revokes the local signed session",
)


def rehearse_sheet_recovery(pages, output, report, width):
    from rehearse import API, LOBBY, ORIGIN, assert_fits, read, shot, toast, use_loopback_requests

    dm = pages["dm"]
    context = pages["a"].context.browser.new_context(
        viewport={"width": width, "height": 844}
    )
    context.add_cookies(pages["a"].context.cookies(ORIGIN))
    use_loopback_requests(context)
    player = context.new_page()
    switch = context.new_page()
    observe_page(player, report, "a")
    observe_page(switch, report, "a")
    # These writes use normal authorization against this matrix cell's throwaway DB.
    response = dm.request.post(API + "/campaigns", data={"name": f"Sheet recovery {width}"})
    assert response.ok, response.text()
    cid = response.json()["id"]
    base = f"/campaigns/{cid}"
    private = f"PRIVATE_SHEET_RECOVERY_{width}"

    def join():
        invitation = dm.request.post(
            API + base + "/invitations", data={"email": "a@example.test"}
        )
        assert invitation.ok, invitation.text()
        accepted = player.request.post(API + f"/invitations/{invitation.json()['id']}/accept")
        assert accepted.ok, accepted.text()
        return next(row["id"] for row in read(dm, base + "/members").json()
                    if row["email"] == "a@example.test")

    member = join()
    characters = []
    for name in (private, f"Replacement {width}"):
        response = dm.request.post(API + base + "/characters", data={"character_name": name})
        assert response.ok, response.text()
        characters.append(response.json())
    original, replacement = characters
    history_path = base + f"/characters/{original['id']}/sheets"

    def assign(character_id):
        response = dm.request.put(API + base + f"/members/{member}/character",
                                  data={"player_character_id": character_id})
        assert response.ok, response.text()

    assign(original["id"])
    options = read(player, history_path + "/options").json()
    sheet = {
        "schema_version": 1, "ancestry": options["ancestries"][0]["name"],
        "class_name": options["classes"][0]["name"], "level": 3,
        "ability_scores": {name: 10 for name in (
            "strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"
        )},
    }
    draft = player.request.post(API + history_path + "/drafts", data=sheet)
    assert draft.ok, draft.text()
    version = draft.json()
    version_url = API + history_path + "/" + version["id"]
    viewer_id = read(player, "/lobby").json()["user"]["id"]
    editor = player.get_by_role("region", name=private, exact=True)

    def open_sheet():
        player.goto(LOBBY + "/sheets")
        expect(editor.get_by_role("spinbutton", name="Level", exact=True)).to_be_visible()

    def dirty(value=4):
        control = editor.get_by_role("spinbutton", name="Level", exact=True)
        control.fill(str(value))  # Deliberately leave NumberInput focused.

    def hidden():
        expect(player.get_by_text(private, exact=True)).to_have_count(0, timeout=6000)
        expect(player.get_by_role("region", name="Recovered sheet changes")).to_have_count(0)
        expect(player.get_by_role("region", name="Unsaved sheet changes")).to_have_count(0)
        assert private not in player.locator("body").inner_text()

    def denied():
        assert player.request.get(API + history_path).status in (401, 403, 404)
        assert player.request.patch(version_url, data={
            **sheet, "level": 9, "expected_updated_at": version["updated_at"]
        }).status in (401, 403, 404)
        assert player.request.post(API + history_path + "/drafts", data=sheet).status in (401, 403, 404)
        assert player.request.post(version_url + "/submit").status in (401, 403, 404)

    # Polling may observe the real revocation before the lobby does. Record
    # only exact, observed refusal diagnostics, leaving every assertion intact.
    def observe_refusal(route):
        from rehearse import fetch_loopback_route
        response = fetch_loopback_route(route)
        if response.status in (401, 403, 404):
            expect_network_diagnostic(report, "a", route.request.method,
                                      route.request.url, status=response.status)
        route.fulfill(response=response)

    player.route(API + "/**", observe_refusal)

    def restored():
        expect(editor.get_by_role("spinbutton", name="Level", exact=True)).to_have_value("4", timeout=6000)
        expect(editor.get_by_role("region", name="Recovered sheet changes")).to_be_visible()
        response = read(player, history_path)
        assert response.headers["x-grimoire-viewer-id"] == viewer_id
        assert response.json()["versions"][0] == version

    def run(index, action):
        report["checks"].ready()
        try:
            action()
            assert_fits(player)
            shot(player, output / f"sheet-recovery-{index}.png")
            report["checks"].append(CHECKS[index])
        except Exception as error:
            report["failed_checks"].append({"check": CHECKS[index], "observed": str(error)})
            shot(player, output / f"sheet-recovery-{index}-failed.png")
            raise

    def clear_and_swap():
        open_sheet()
        dirty()
        assign(None)
        hidden()
        denied()
        assign(original["id"])
        restored()
        assign(replacement["id"])
        hidden()
        denied()
        new_editor = player.get_by_role("region", name=replacement["character_name"], exact=True)
        expect(new_editor.get_by_role("spinbutton", name="Level", exact=True)).to_have_value("1", timeout=6000)
        assert read(dm, base + f"/characters/{replacement['id']}/sheets").json()["versions"] == []
        assign(original["id"])
        restored()

    def account_and_expiry():
        switch.goto(ORIGIN + "/__local/login/b")
        player.bring_to_front()
        hidden()
        denied()
        switch.goto(ORIGIN + "/__local/login/a")
        open_sheet()
        restored()
        # Session expiry clears cookies, without invoking the explicit sign-out UI.
        context.clear_cookies()
        player.reload()
        hidden()
        denied()
        switch.goto(ORIGIN + "/__local/login/a")
        open_sheet()
        restored()

    def membership():
        nonlocal member
        removed = dm.request.delete(API + base + f"/members/{member}")
        assert removed.ok, removed.text()
        hidden()
        denied()
        member = join()
        assign(original["id"])
        open_sheet()
        restored()
        editor.get_by_role("button", name="Save draft", exact=True).click()
        toast(player, "Draft saved.")
        latest = read(player, history_path).json()["versions"][0]
        assert latest["sheet"]["level"] == 4
        assert latest["id"] == version["id"]

    def sign_out():
        dirty(5)
        player.get_by_role("link", name="Your campaigns", exact=True).click()
        player.get_by_role("link", name="Sign out", exact=True).click()
        expect(player).to_have_url(ORIGIN + "/__local")
        denied()
        switch.goto(ORIGIN + "/__local/login/a")
        open_sheet()
        expect(editor.get_by_role("spinbutton", name="Level", exact=True)).to_have_value("4")
        expect(editor.get_by_role("region", name="Recovered sheet changes")).to_have_count(0)

    try:
        run(0, clear_and_swap)
        run(1, account_and_expiry)
        run(2, membership)
        run(3, sign_out)
    finally:
        context.close()
