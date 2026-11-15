"""Dispute seeded local facts through the real player UI and authority checks."""

from playwright.sync_api import expect

CHECK = "Both players can dispute their own facts; other-player and party facts stay protected"
TRANSCRIPT_CHECK = (
    "Disabled transcript capability is recorded without activating capture"
)
CANARIES = {
    "a": "FACT_A_SILVER_COMPASS: Elowen remembers a silver compass.",
    "b": "FACT_B_COPPER_KEY: Bram remembers a copper key.",
    "party": "FACT_PARTY_GLASS_COMET: The party saw a glass comet.",
}


def rehearse_facts(pages, output, report):
    from rehearse import API, LOBBY, assert_fits, campaign_read, read

    dm = pages["dm"]
    campaign_id = next(
        row["id"]
        for row in read(dm, "/lobby").json()["campaigns"]
        if row["name"] == "The Lantern at the Crossroads"
    )
    base = f"/campaigns/{campaign_id}"
    # Exercise the same deterministic embedding client and pgvector route as
    # the existing knowledge checks. This endpoint exists only in the fixture.
    embedded = dm.request.post("http://friends.localhost:8177/__local/embed-play")
    assert embedded.status == 204
    dm.goto(f"{LOBBY}{base}/session")
    identifiers = {}
    for role in ("a", "b"):
        page = pages[role]
        page.goto(f"{LOBBY}{base}/session")
        finder = page.get_by_role("region", name="Character knowledge search")
        finder.get_by_role("searchbox", name="Search what your character knows").fill(
            CANARIES[role]
        )
        with page.expect_response(
            lambda response: "/knowledge/search" in response.url
        ) as answered:
            finder.get_by_role("button", name="Search knowledge", exact=True).click()
        assert answered.value.ok
        rows = answered.value.json()
        own = next(row for row in rows if row.get("preview") == CANARIES[role])
        identifiers[role] = own["id"]
        assert own["status"] == "active" and own["can_dispute"] is True
        assert CANARIES["b" if role == "a" else "a"] not in answered.value.text()
        result = finder.get_by_role("listitem").filter(has_text=CANARIES[role])
        expect(
            result.get_by_role("button", name="Dispute fact", exact=True)
        ).to_be_visible()
        assert_fits(page)
        page.screenshot(path=str(output / f"{role}-fact-active.png"), full_page=True)
        result.get_by_role("button", name="Dispute fact", exact=True).click()
        expect(result).to_contain_text("Disputed. Excluded from campaign context.")
        expect(
            result.get_by_role("button", name="Dispute fact", exact=True)
        ).to_have_count(0)
        refreshed = campaign_read(page, "/knowledge/search", q=CANARIES[role], k=50)
        assert (
            next(row for row in refreshed.json() if row["id"] == own["id"])["status"]
            == "disputed"
        )
        assert CANARIES["b" if role == "a" else "a"] not in refreshed.text()
        page.screenshot(path=str(output / f"{role}-fact-disputed.png"), full_page=True)
    party = next(
        row
        for row in read(
            dm, f"{base}/knowledge/search", q=CANARIES["party"], k=50
        ).json()
        if row.get("preview") == CANARIES["party"]
    )
    for role, identifier in (
        ("a", identifiers["b"]),
        ("b", identifiers["a"]),
        ("a", party["id"]),
        ("dm", identifiers["a"]),
    ):
        assert (
            pages[role].request.post(f"{API}{base}/facts/{identifier}/dispute").status
            == 404
        )
    # Repeating an own dispute succeeds and cannot restore active status.
    repeated = pages["a"].request.post(f"{API}{base}/facts/{identifiers['a']}/dispute")
    assert repeated.status == 200 and repeated.json()["status"] == "disputed"
    report["checks"].append(CHECK)
    for role, page in pages.items():
        assert page.request.get(f"{API}{base}/transcript/consent").status == 404
        assert (
            page.get_by_role("button", name="Start transcript", exact=True).count() == 0
        )
    report["unavailable"].append(
        {
            "journey": "Transcripts",
            "reason": "The React UI has no capture controls and the local transcript-consent endpoint returns404 for all three seats. Capture remains disabled.",
        }
    )
    report["checks"].append(TRANSCRIPT_CHECK)
