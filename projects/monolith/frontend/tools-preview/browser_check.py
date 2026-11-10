"""Serial, synthetic real-page evidence. No authentication or live acceptance."""

import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path in {
            "/review",
            "/chat",
            "/notes",
            "/qwen",
            "/grimoire",
        }:
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, *_args):
        pass


def check_reflow(page, width):
    result = page.evaluate("""() => {
      const bad = [];
      for (const el of document.querySelectorAll('button,input:not([type="hidden"]),textarea,select,.card-header,.card-meta,.actions,.composer,.controls,.identity-fields,.abilities,.decision-actions,.panel,.chat-panel,.msg')) {
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        if (r.left < -1 || r.right > innerWidth + 1) bad.push({tag: el.tagName, class: el.className, left: r.left, right: r.right});
      }
      const clipped = [...document.querySelectorAll('.body-scroll,.chat-log,.log,.panel-body,.note-drawer,.sheet-card')].filter(el => el.scrollWidth > el.clientWidth + 1).map(el => el.className);
      return {viewport: innerWidth, document: document.documentElement.scrollWidth, bad, clipped};
    }""")
    assert result["document"] <= width, result
    assert not result["bad"], result
    assert not result["clipped"], result
    return result


def submit_form(page, selector, expected_action, expected_fields):
    # Native form submission is allowed to reach the local fixture host. The
    # response is synthetic HTML; no server action or production loader runs.
    requests = []

    def receive(route):
        request = route.request
        if request.method == "POST":
            requests.append({"url": request.url, "body": request.post_data})
            route.fulfill(
                status=200,
                content_type="text/html",
                body="<p>Fixture form received</p>",
            )
        else:
            route.continue_()

    page.route("**/*", receive)
    form = page.locator(selector)
    form.locator('button[type="submit"]').last.click()
    expect(page.get_by_text("Fixture form received")).to_be_visible()
    assert requests and expected_action in requests[-1]["url"], requests
    fields = parse_qs(requests[-1]["body"])
    for key, value in expected_fields.items():
        assert fields.get(key) == [value], fields
    page.unroute("**/*", receive)
    return requests[-1]


def exercise(page, name, base, width):
    results = []
    if name == "review":
        page.get_by_role("button", name="Keep (y)").click()
        expect(page.get_by_text("Second synthetic gap", exact=True)).to_be_visible()
        calls = page.evaluate("window.fixtureCalls")
        assert calls[0]["body"]["path"] == "/api/knowledge/gaps/fixture-gap/verify", (
            calls
        )
        page.get_by_role("tab", name="Notes", exact=True).click()
        page.goto(base + "/review?empty")
        expect(page.get_by_text("Queue empty", exact=False)).to_be_visible()
        results += [
            "Review action payload and optimistic advance",
            "Tab control",
            "Empty queue",
        ]
    elif name == "chat":
        page.locator(".chat-input-bar input").fill("Synthetic browser question")
        page.get_by_role("button", name="Send message").click()
        expect(page.locator(".chat-text").last).to_contain_text(
            "Synthetic streamed answer"
        )
        node = page.locator('.graph-hit-areas [role="button"]').first
        expect(node).to_be_visible()
        node.focus()
        node.press("Enter")
        expect(page.get_by_role("complementary", name="Note details")).to_be_visible()
        check_reflow(page, width)
        page.get_by_role("button", name="Close note").click()
        expect(page.get_by_role("complementary", name="Note details")).to_have_count(0)
        results += [
            "Streamed answer and graph event",
            "Keyboard graph selection and note detail response",
        ]
    elif name == "notes":
        page.locator("#graph-search-input").fill("Synthetic")
        page.keyboard.press("Escape")
        expect(page.locator("#graph-search-input")).to_have_value("")
        cluster = page.locator(".legend-row").first
        cluster.click()
        expect(cluster).to_have_attribute("aria-pressed", "false")
        cluster.click()
        expect(cluster).to_have_attribute("aria-pressed", "true")
        results += ["Note detail and review history", "Search/escape", "Cluster mask"]
    elif name == "qwen":
        before = page.evaluate(
            "window.fixtureCalls.filter(c => c.path.endsWith('/stats')).length"
        )
        page.wait_for_function(
            "n => window.fixtureCalls.filter(c => c.path.endsWith('/stats')).length > n",
            arg=before,
            timeout=6500,
        )
        page.locator(".composer textarea").fill("Synthetic browser prompt")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator(".log")).to_contain_text("Synthetic streamed Qwen answer")
        page.get_by_role("button", name="+ New chat", exact=True).click()
        expect(page.locator(".log")).to_contain_text("Ask something")
        page.locator(".sessions .pick").last.click()
        expect(page.locator(".log")).to_contain_text("Synthetic streamed Qwen answer")
        page.locator(".sessions .drop").first.click()
        page.reload()
        expect(page.locator(".log")).to_contain_text("Synthetic streamed Qwen answer")
        results += [
            "Five-second stats polling",
            "SSE reasoning/answer",
            "Create/select/delete/persist session",
        ]
    elif name == "grimoire":
        page.locator('input[name="ancestry"]').fill("Synthetic elf")
        request = submit_form(
            page,
            'form[action="?/save"]',
            "?/save",
            {
                "ancestry": "Synthetic elf",
                "version_id": "fixture-version",
                "strength": "12",
            },
        )
        results.append({"player_save": request})
        page.goto(base + "/grimoire")
        results.append(
            {
                "player_submit": submit_form(
                    page,
                    'form[action="?/submit"]',
                    "?/submit",
                    {"version_id": "fixture-version"},
                )
            }
        )
        page.goto(base + "/grimoire?dm")
        expect(page.locator('form[action="?/save"]')).to_have_count(0)
        page.locator('textarea[name="comment"]').fill("Synthetic approval")
        results.append(
            {
                "dm_decide": submit_form(
                    page,
                    'form[action="?/decide"]',
                    "?/decide",
                    {"decision": "approve", "comment": "Synthetic approval"},
                )
            }
        )
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    assert len(args.expected_sha) == 40
    assert (
        json.loads((args.artifact / "build.json").read_text())["head"]
        == args.expected_sha
    )
    args.output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(args.artifact.resolve()))
    )
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    cases = []
    failures = []
    command = (
        "python3 projects/monolith/frontend/tools-preview/browser_check.py --artifact projects/monolith/frontend/tools-fixture-preview --output tools-preview-evidence --expected-sha "
        + args.expected_sha
    )
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                for name in ["review", "chat", "notes", "qwen", "grimoire"]:
                    for width in [390, 1440]:
                        for scheme in ["light", "dark"]:
                            case = {
                                "page": name,
                                "route": "/" + name,
                                "width": width,
                                "scheme": scheme,
                                "head": args.expected_sha,
                            }
                            cases.append(case)
                            context = browser.new_context(
                                viewport={"width": width, "height": 900},
                                color_scheme=scheme,
                                reduced_motion="reduce",
                            )
                            context.add_init_script(
                                "localStorage.setItem('td-theme', '" + scheme + "');"
                            )
                            page = context.new_page()
                            errors = []
                            page.on(
                                "pageerror", lambda error: errors.append(str(error))
                            )
                            try:
                                page.goto(base + "/" + name)
                                expect(page.locator(".td").first).to_be_visible()
                                expect(page.locator(".scheme")).to_have_count(1)
                                toggle = page.locator(".scheme")
                                toggle.click()
                                other = "dark" if scheme == "light" else "light"
                                expect(page.locator("html")).to_have_attribute(
                                    "data-theme", other
                                )
                                assert (
                                    page.evaluate("localStorage.getItem('td-theme')")
                                    == other
                                )
                                toggle.click()
                                expect(page.locator("html")).to_have_attribute(
                                    "data-theme", scheme
                                )
                                if name == "chat":
                                    page.locator(".chat-input-bar input").fill(
                                        "Synthetic opening question"
                                    )
                                    page.get_by_role(
                                        "button", name="Send message"
                                    ).click()
                                    expect(
                                        page.locator(".chat-text").last
                                    ).to_contain_text("Synthetic streamed answer")
                                    expect(
                                        page.locator(
                                            '.graph-hit-areas [role="button"]'
                                        ).first
                                    ).to_be_visible()
                                    # The hit area exists before pencil/ink/text
                                    # reveal. Capture the painted node too.
                                    expect(
                                        page.locator(".graph-nodes text").first
                                    ).to_have_css("opacity", "1")
                                if name == "notes":
                                    canvas = page.locator("canvas")
                                    box = canvas.bounding_box()
                                    canvas.click(
                                        position={
                                            "x": box["width"] / 2,
                                            "y": box["height"] / 2,
                                        }
                                    )
                                    expect(page.locator(".panel-body")).to_contain_text(
                                        "Fixture paragraph"
                                    )
                                case["reflow"] = check_reflow(page, width)
                                filename = f"{name}-{width}-{scheme}.png"
                                page.screenshot(
                                    path=str(args.output / filename),
                                    full_page=True,
                                    animations="disabled",
                                )
                                case["screenshot"] = filename
                                case["sha256"] = hashlib.sha256(
                                    (args.output / filename).read_bytes()
                                ).hexdigest()
                                # Show the lower part of the longest nested scroller too.
                                scroller = page.locator(
                                    {
                                        "review": ".body-scroll",
                                        "chat": ".chat-log",
                                        "notes": ".panel-body",
                                        "qwen": ".log",
                                        "grimoire": ".sheet-card",
                                    }[name]
                                ).first
                                if scroller.count():
                                    scroller.evaluate(
                                        "el => { el.scrollTop = el.scrollHeight; }"
                                    )
                                    scroller.screenshot(
                                        path=str(args.output / f"nested-{filename}"),
                                        animations="disabled",
                                    )
                                if name == "notes":
                                    page.get_by_role(
                                        "button", name="close", exact=True
                                    ).click()
                                case["interactions"] = [
                                    "Persisted scheme toggle",
                                    *exercise(page, name, base, width),
                                ]
                                # Error fixtures prove recovery UI, not live outage handling.
                                if name in {"chat", "qwen"}:
                                    page.goto(base + "/" + name + "?networkError&empty")
                                    selector = (
                                        ".chat-input-bar input"
                                        if name == "chat"
                                        else ".composer textarea"
                                    )
                                    page.locator(selector).fill(
                                        "Synthetic error request"
                                    )
                                    page.get_by_role(
                                        "button",
                                        name="Send message"
                                        if name == "chat"
                                        else "Send",
                                        exact=True,
                                    ).click()
                                    expect(
                                        page.locator(
                                            ".chat-log" if name == "chat" else ".log"
                                        )
                                    ).to_contain_text("Synthetic network unavailable")
                                if name == "grimoire":
                                    page.goto(base + "/grimoire?empty")
                                    expect(
                                        page.get_by_text("No campaigns yet")
                                    ).to_be_visible()
                                    page.goto(base + "/grimoire?unavailable")
                                    expect(
                                        page.get_by_text("Sheets are unavailable")
                                    ).to_be_visible()
                                assert not errors, errors
                                case["passed"] = True
                            except Exception as error:
                                case["passed"] = False
                                case["error"] = str(error)
                                case["page_errors"] = errors
                                failures.append(case)
                                page.screenshot(
                                    path=str(
                                        args.output
                                        / f"failure-{name}-{width}-{scheme}.png"
                                    ),
                                    full_page=True,
                                )
                            finally:
                                context.close()
                            print(json.dumps(case), flush=True)
            finally:
                browser.close()
    finally:
        server.shutdown()
        result = {
            "head": args.expected_sha,
            "synthetic": True,
            "command": command,
            "cases": cases,
            "failures": len(failures),
            "memory": Path("/proc/meminfo").read_text()
            if Path("/proc/meminfo").exists()
            else "unavailable",
        }
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        (args.output / "MATRIX.md").write_text(
            "# Synthetic private tools\n\nHead: `"
            + args.expected_sha
            + "`\n\nNo live authentication or production readiness is demonstrated. Root/private layouts and root-error CSS are mounted; routes are checked against current hooks.\n\nCommand: `"
            + command
            + "`\n\n| Page | Width | Scheme | Screenshot | Passed |\n|---|---:|---|---|---|\n"
            + "".join(
                f"| {case['route']} | {case['width']} | {case['scheme']} | [{case.get('screenshot', 'missing')}]({case.get('screenshot', '')}) | {case.get('passed', False)} |\n"
                for case in cases
            )
        )
        subprocess.run(["du", "-sh", str(args.output)], check=False)
    assert len(cases) == 20 and not failures, (
        f"{len(failures)} of {len(cases)} cases failed"
    )


if __name__ == "__main__":
    main()
