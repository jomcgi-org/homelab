"""Capture synthetic friends sheet views against run.py's real local router."""

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from rehearse import use_loopback_requests

ROOT = Path(__file__).resolve().parents[4]
HOST = "http://friends.localhost:4177"


def capture(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "synthetic_only": True,
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "frontend_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD:projects/monolith/frontend"],
            cwd=ROOT,
            text=True,
        ).strip(),
        "captured_at": datetime.now(UTC).isoformat(),
        "method": "Playwright Chromium, real SvelteKit routes and Grimoire router; disposable run.py PostgreSQL/pgvector and signed demo seats",
        "screens": [],
        "interactions": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--host-resolver-rules=MAP friends.localhost 127.0.0.1"]
        )
        for role in ("dm", "a"):
            context = browser.new_context(reduced_motion="reduce")
            use_loopback_requests(context)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://friends.localhost:8177/__local/login/{role}")
            session_path = page.get_by_role(
                "link", name="Open session", exact=True
            ).get_attribute("href")
            base = session_path.removesuffix("/session")
            state_path = f"{HOST}{session_path}/state"
            if role == "dm":
                page.goto(f"{HOST}{session_path}")
                start = page.get_by_role("button", name="Start session", exact=True)
                if start.count():
                    start.click()
                expect(page.get_by_label("Set the scene", exact=True)).to_be_visible()
                entities = context.request.get(state_path + "?q=Human").json()["items"]
                entity_id = next(
                    entity["id"] for entity in entities if entity["name"] == "Human"
                )
            views = {
                "lobby-campaign": "/grimoire",
                "session": session_path,
                "character": "/grimoire/sheets",
                "notes": f"{base}/notes",
                "journal": f"{base}/journal",
                "entity": f"{base}/entities/{entity_id}",
                **({"grants": f"{base}/grants"} if role == "dm" else {}),
            }
            for width in (390, 1440):
                page.set_viewport_size(
                    {"width": width, "height": 1000 if width == 1440 else 844}
                )
                for scheme in ("light", "dark"):
                    page.goto(f"{HOST}/grimoire")
                    for _ in range(2):
                        if page.locator("html").get_attribute("data-theme") == scheme:
                            break
                        page.locator("button.scheme").click()
                    expect(page.locator("html")).to_have_attribute("data-theme", scheme)
                    for view, path in views.items():
                        response = page.goto(f"{HOST}{path}")
                        expect(page.locator("main.td.sheet h1")).to_be_visible()
                        if view == "lobby-campaign" and role == "dm":
                            # Campaign management is a lobby section, not an invented route.
                            page.locator(".lobby article details").first.evaluate(
                                "el => el.open = true"
                            )
                        page.evaluate(
                            "async () => { await document.fonts.load('16px \"Schibsted Grotesk\"'); await document.fonts.ready; }"
                        )
                        metrics = page.evaluate("""() => ({
                            viewport: innerWidth,
                            scroll_width: document.documentElement.scrollWidth,
                            scheme: document.documentElement.dataset.theme || 'light',
                            font: getComputedStyle(document.querySelector('main')).fontFamily,
                            heading_font: getComputedStyle(document.querySelector('main h1')).fontFamily,
                            font_loaded: document.fonts.check('16px "Schibsted Grotesk"'),
                            buttons: [...document.querySelectorAll('main button')].filter(b => b.getBoundingClientRect().height > 0).map(b => ({label: b.textContent.trim(), height: b.getBoundingClientRect().height, font: getComputedStyle(b).fontFamily, radius: getComputedStyle(b).borderRadius}))
                        })""")
                        assert response.status == 200, (view, response.status)
                        assert metrics["scroll_width"] <= width, (view, role, metrics)
                        assert metrics["scheme"] == scheme, metrics
                        assert (
                            metrics["font_loaded"]
                            and "Schibsted Grotesk" in metrics["font"]
                            and "Schibsted Grotesk" in metrics["heading_font"]
                        ), metrics
                        if width == 390:
                            assert all(
                                button["height"] >= 44 for button in metrics["buttons"]
                            ), (view, metrics)
                        assert all(
                            "Schibsted Grotesk" in button["font"]
                            and button["radius"] == "0px"
                            for button in metrics["buttons"]
                        ), (view, metrics)
                        assert not errors, errors
                        filename = f"{role}-{view}-{width}-{scheme}.png"
                        page.screenshot(path=str(output / filename), full_page=True)
                        report["screens"].append(
                            {
                                "file": filename,
                                "sha256": hashlib.sha256(
                                    (output / filename).read_bytes()
                                ).hexdigest(),
                                "route": path,
                                "role": "player" if role == "a" else "dm",
                                "status": response.status,
                                **metrics,
                            }
                        )
                    # Exercise the visible switch, persistence across a full load,
                    # and the original lobby/session navigation on each viewport.
                    page.goto(f"{HOST}{session_path}")
                    page.locator("button.scheme").click()
                    other = "light" if scheme == "dark" else "dark"
                    expect(page.locator("html")).to_have_attribute("data-theme", other)
                    assert page.evaluate("localStorage.getItem('td-theme')") == other
                    page.reload()
                    expect(page.locator("html")).to_have_attribute("data-theme", other)
                    page.get_by_role(
                        "link", name="← Your campaigns", exact=True
                    ).click()
                    page.get_by_role("link", name="Open session", exact=True).click()
                    expect(
                        page.get_by_role(
                            "heading", name="The Lantern at the Crossroads", exact=True
                        )
                    ).to_be_visible()
                    report["interactions"].append(
                        {
                            "role": role,
                            "width": width,
                            "from_scheme": scheme,
                            "switch_persistence_navigation": True,
                        }
                    )
            context.close()
        browser.close()
    report["passed"] = True
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Captured {len(report['screens'])} screens; zero horizontal overflow; fonts, 44px phone buttons, scheme persistence and navigation passed."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    capture(parser.parse_args().output)
