"""Chromium acceptance for synthetic shared controls, never a live service."""

import argparse
import functools
import http.server
import importlib.metadata
import importlib.util
import itertools
import json
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def helper(directory):
    path = Path(__file__).resolve().parent.parent / directory / "browser_check.py"
    spec = importlib.util.spec_from_file_location(directory.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Share the existing preview platform, colour compositing, real text resize and
# clipping checks rather than duplicating their definitions in another runner.
theme = helper("theme-preview")
factory = helper("factory-preview")
SAMPLES = ("light", "nested", "sibling", "dark")
INTERACTIVE = "a[href], button, summary, input, select, textarea, [role=tabpanel]"


def sample(page, name):
    return page.locator(f'[data-sample="{name}"]')


def semantics(page):
    records = []
    for name in SAMPLES:
        root = sample(page, name)
        region = root.get_by_role(
            "combobox", name="Sample region (required)", exact=True
        )
        expect(region).to_have_accessible_description(
            "Select a synthetic region Error: Choose another region"
        )
        expect(region).to_have_attribute("aria-invalid", "true")
        expect(region).to_have_attribute("required", "")
        title = root.get_by_role("textbox", name="Sample title", exact=True)
        expect(title).to_have_accessible_description("Enter a synthetic title")
        notes = root.get_by_role("textbox", name="Sample notes", exact=True)
        expect(notes).to_have_accessible_description("Describe the synthetic sample")
        unavailable = root.get_by_role("textbox", name="Unavailable sample", exact=True)
        expect(unavailable).to_be_disabled()
        associations = root.locator("input, select, textarea").evaluate_all("""nodes => nodes.map(node => ({
          id: node.id, labels: [...node.labels].map(label => label.htmlFor),
          descriptions: (node.getAttribute('aria-describedby') ?? '').split(' ').filter(Boolean)
            .map(id => ({id, text: document.getElementById(id)?.textContent})),
          disabled: node.disabled, required: node.required
        }))""")
        assert len(associations) == 4, associations
        for association in associations:
            assert association["labels"] == [association["id"]], association
            assert all(item["text"] for item in association["descriptions"]), (
                association
            )
        expect(
            root.get_by_role("button", name="Named action", exact=True)
        ).to_have_accessible_name("Named action")
        expect(
            root.get_by_role("navigation", name="Breadcrumb", exact=True)
        ).to_be_visible()
        expect(root.locator("nav [aria-current=page]")).to_have_text(
            "SyntheticReferenceWithAnUnbrokenNameThatMustWrapAtNarrowWidths"
        )
        expect(
            root.get_by_role(
                "heading",
                name="SyntheticReferenceWithAnUnbrokenNameThatMustWrapAtNarrowWidths",
                exact=True,
            )
        ).to_be_visible()
        expect(
            root.get_by_role("tablist", name="Sample panels", exact=True)
        ).to_be_visible()
        expect(
            root.get_by_role("tabpanel", name="Overview", exact=True)
        ).to_be_visible()
        button_types = root.locator("button").evaluate_all(
            "nodes => nodes.map(node => ({text: node.textContent.trim(), type: node.getAttribute('type')}))"
        )
        assert all(
            item["type"] in ("button", "submit", "reset") for item in button_types
        ), button_types
        records.append(
            {
                "sample": name,
                "associations": associations,
                "button_types": button_types,
                "role_queries": "passed",
            }
        )
    return records


def focus_record(page):
    record = page.evaluate(
        "() => {"
        + theme.COLOURS
        + """
      const node = document.activeElement, style = getComputedStyle(node);
      const ground = background(node.parentElement);
      return {sample: node.closest('[data-sample]').dataset.sample, tag: node.tagName,
        text: (node.textContent || node.name).trim().slice(0, 100),
        visible: node.matches(':focus-visible'), style: style.outlineStyle,
        width: parseFloat(style.outlineWidth), colour: style.outlineColor, ground,
        ratio: contrast(rgba(style.outlineColor), ground)};
    }"""
    )
    assert record["visible"] and record["style"] == "solid", record
    assert record["width"] >= 2 and record["ratio"] >= 3, record
    return record


def tab_order(page):
    # Explicit authored order, not the DOM's dynamically discovered focus order.
    stops = []
    for name in SAMPLES:
        root = sample(page, name)
        for selector in (
            'a[href="#home"]',
            'a[href="#library"]',
            'a[href="#current"]',
            '[data-action="header"]',
            'select[name="region"]',
            'input[name="title"]',
            'textarea[name="notes"]',
            '[data-action="default"]',
            '[data-action="submit"]',
            '[type="reset"]',
            '[data-action="named"]',
            '[data-action="field"]',
            '[data-action="external-submit"]',
            "summary",
            '[data-action="disclosure"]',
            '[role="tab"][aria-selected="true"]',
            '[role="tabpanel"]:not([hidden])',
            '[data-action="selection"]',
        ):
            stops.append(root.locator(selector))
    contract = sample(page, "contract")
    stops.extend([contract.get_by_role("tab"), contract.get_by_role("tabpanel")])
    page.evaluate("document.activeElement.blur()")
    records = []
    for index, stop in enumerate(stops):
        page.keyboard.press("Tab")
        expect(stop).to_be_focused()
        records.append({"index": index, **focus_record(page)})
    # Walk back over every stop. Check the first stop against a fresh forward
    # traversal above; Shift+Tab after it returns to browser chrome.
    for stop in reversed(stops[:-1]):
        page.keyboard.press("Shift+Tab")
        expect(stop).to_be_focused()
        focus_record(page)
    return {"forward": records, "reverse_count": len(stops) - 1}


def interactions(page):
    records = []
    for name in SAMPLES:
        root = sample(page, name)
        state = root.locator('[data-state="submissions"]')
        button = root.locator('[data-action="default"]')
        # Reset through the hydrated fixture for identical per-boundary results.
        before = int(state.inner_text().split(": ")[1])
        title = root.get_by_role("textbox", name="Sample title", exact=True)
        title.fill("Synthetic keyboard sample")
        title.press("Enter")
        expect(state).to_have_text(f"Submissions: {before + 1}")
        expect(root.locator('[data-state="submitter"]')).to_have_text(
            "Submitter: submit"
        )
        button.click()
        expect(state).to_have_text(f"Submissions: {before + 1}")
        clicks = int(button.inner_text().split(": ")[1])
        for key in ("Enter", "Space"):
            button.press(key)
            clicks += 1
            expect(button).to_have_text(f"Sample action: {clicks}")
            expect(state).to_have_text(f"Submissions: {before + 1}")
        disabled_records = []
        for disabled in root.locator(":disabled").all():
            button.focus()
            focused = disabled.evaluate(
                "node => {node.focus(); return document.activeElement === node;}"
            )
            assert not focused, "disabled control accepted focus"
            disabled.evaluate("node => node.click()")
            disabled.click(force=True)
            expect(button).to_have_text(f"Sample action: {clicks}")
            expect(state).to_have_text(f"Submissions: {before + 1}")
            disabled_records.append(
                disabled.evaluate(
                    "node => ({tag: node.tagName, disabled: node.disabled})"
                )
            )
        root.get_by_role("button", name="Reset sample", exact=True).click()
        expect(title).to_have_value("")
        expect(state).to_have_text(f"Submissions: {before + 1}")
        details = root.locator("details")
        summary = root.locator("summary")
        toggles = []
        for key in ("Enter", "Space", "click"):
            for opened in (True, False):
                if key == "click":
                    summary.click()
                else:
                    summary.press(key)
                expect(details).to_have_js_property("open", opened)
                expect(root.locator('[data-state="disclosure"]')).to_have_text(
                    f"Open: {str(opened).lower()}"
                )
                indicator = summary.locator('[aria-hidden="true"]').evaluate(
                    "node => getComputedStyle(node, '::after').display"
                )
                assert (indicator == "none") == opened, indicator
                toggles.append({"key": key, "open": opened, "indicator": indicator})
        tabs = root.get_by_role("tab")
        tabs.nth(0).focus()
        sequence = [
            ("ArrowLeft", 3),
            ("ArrowRight", 0),
            ("ArrowRight", 2),
            ("ArrowRight", 3),
            ("ArrowRight", 0),
            ("End", 3),
            ("Home", 0),
        ]
        selections = []
        for key, index in sequence:
            page.keyboard.press(key)
            expect(tabs.nth(index)).to_be_focused()
            expect(tabs.nth(index)).to_have_attribute("aria-selected", "true")
            assert root.locator('[role=tab][tabindex="0"]').count() == 1
            assert root.locator('[role=tab][aria-selected="true"]').count() == 1
            panel_id = tabs.nth(index).get_attribute("aria-controls")
            panel = page.locator(f'[id="{panel_id}"]')
            expect(panel).to_be_visible()
            expect(panel).to_have_attribute(
                "aria-labelledby", tabs.nth(index).get_attribute("id")
            )
            assert root.locator("[role=tabpanel]:not([hidden])").count() == 1
            selections.append({"key": key, "index": index, "focus": focus_record(page)})
        page.keyboard.press("Tab")
        expect(
            root.get_by_role("tabpanel", name="Overview", exact=True)
        ).to_be_focused()
        focus_record(page)
        page.keyboard.press("Shift+Tab")
        expect(tabs.nth(0)).to_be_focused()
        records.append(
            {
                "sample": name,
                "implicit_submitter": "submit",
                "button_keys": ["Enter", "Space"],
                "disabled": disabled_records,
                "disclosure": toggles,
                "tabs": selections,
            }
        )
    return records


def colours(page):
    records = page.evaluate(
        "() => {"
        + theme.COLOURS
        + """
      const records = [];
      const record = (node, kind, colour, ground, threshold) => records.push({
        sample: node.closest('[data-sample]').dataset.sample, kind,
        text: (node.textContent || node.name).trim().slice(0, 80), colour, ground,
        ratio: contrast(rgba(colour), ground), threshold});
      const elements = [...document.querySelectorAll('[data-sample] *')]
        .filter(node => node.closest('[data-sample]').dataset.sample !== 'contract' && node.checkVisibility());
      for (const node of elements) {
        const style = getComputedStyle(node);
        const hasText = [...node.childNodes].some(child => child.nodeType === Node.TEXT_NODE && child.textContent.trim());
        if (hasText || /INPUT|SELECT|TEXTAREA/.test(node.tagName))
          record(node, node.disabled ? 'disabled-text' : node.classList.contains('error') ? 'error-text' :
            node.getAttribute('aria-selected') === 'true' ? 'selected-text' : 'text', style.color, background(node), 4.5);
        if (/BUTTON|INPUT|SELECT|TEXTAREA|DETAILS/.test(node.tagName)) {
          for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
            const colour = style[`border${side}Color`];
            if (parseFloat(style[`border${side}Width`]) > 0 && rgba(colour)[3] > 0) {
              record(node, `boundary-${side}-outside`, colour, background(node.parentElement), 3);
              // Buttons define their shape against the adjacent exterior. As in
              // theme-preview, a filled button does not need an internal rule.
              if (node.tagName !== 'BUTTON') record(node, `boundary-${side}-inside`, colour, background(node), 3);
            }
          }
        }
      }
      return records;
    }"""
    )
    for name in SAMPLES:
        rows = [row for row in records if row["sample"] == name]
        for kind in (
            "text",
            "selected-text",
            "error-text",
            "disabled-text",
            "boundary-Bottom-outside",
        ):
            assert any(row["kind"] == kind for row in rows), (name, kind)
    assert len(records) > 300, "incomplete rendered contrast coverage"
    for record in records:
        assert record["ratio"] >= record["threshold"], record
    return records


def structural_states(page):
    records = []
    for name in SAMPLES:
        root = sample(page, name)
        cues = root.evaluate("""root => {
          const selected = root.querySelector('[aria-selected=true]');
          const unselected = root.querySelector('[role=tab]:not(:disabled)[aria-selected=false]');
          const selectedStyle = getComputedStyle(selected), unselectedStyle = getComputedStyle(unselected);
          const invalid = root.querySelector('[aria-invalid=true]');
          const error = invalid.getAttribute('aria-describedby').split(' ').map(id => document.getElementById(id))
            .find(node => node.classList.contains('error'));
          return {selected: {attribute: selected.getAttribute('aria-selected'),
              decoration: selectedStyle.textDecorationLine, weight: selectedStyle.fontWeight,
              other_weight: unselectedStyle.fontWeight},
            error: {attribute: invalid.getAttribute('aria-invalid'), text: error.textContent},
            disabled: [...root.querySelectorAll(':disabled')].map(node => ({
              tag: node.tagName, attribute: node.hasAttribute('disabled'),
              border: getComputedStyle(node).borderTopStyle}))};
        }""")
        assert cues["selected"]["attribute"] == "true"
        assert "underline" in cues["selected"]["decoration"]
        assert int(cues["selected"]["weight"]) > int(cues["selected"]["other_weight"])
        assert cues["error"]["attribute"] == "true" and cues["error"][
            "text"
        ].startswith("Error: ")
        assert len(cues["disabled"]) == 3
        assert all(
            row["attribute"] and row["border"] == "dashed" for row in cues["disabled"]
        )
        records.append({"sample": name, **cues})
    return records


def targets(page):
    records = page.locator(
        INTERACTIVE
    ).evaluate_all("""nodes => nodes.filter(node => node.checkVisibility())
      .map(node => {const rect = node.getBoundingClientRect(); return {
        sample: node.closest('[data-sample]').dataset.sample, tag: node.tagName,
        text: (node.textContent || node.name).trim().slice(0, 80), width: rect.width, height: rect.height};})""")
    assert len(records) >= 90, "interactive target coverage incomplete"
    for record in records:
        assert record["width"] >= 44 and record["height"] >= 44, record
    return records


def layout(page):
    record = theme.layout_checks(page)
    assert not record["issues"], record
    overlap = page.evaluate("""() => {
      const root = document.querySelector('.fixture'), boxes = [], issues = [];
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = walker.currentNode, node = text.parentElement;
        // Native select options paint in a browser-owned popup, not page text boxes.
        if (!text.textContent.trim() || !node.checkVisibility() || node.closest('option, textarea')) continue;
        const range = document.createRange(); range.selectNodeContents(text);
        for (const box of range.getClientRects()) if (box.width && box.height)
          boxes.push({node, box, text: text.textContent.trim()});
      }
      for (const node of root.querySelectorAll('a[href], button, summary, input, select, textarea')) {
        if (node.checkVisibility()) boxes.push({node, box: node.getBoundingClientRect(), text: node.tagName});
      }
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        // A control owns its label text. That intentional containment is not overlap.
        if (a.node.contains(b.node) || b.node.contains(a.node)) continue;
        const width = Math.min(a.box.right, b.box.right) - Math.max(a.box.left, b.box.left);
        const height = Math.min(a.box.bottom, b.box.bottom) - Math.max(a.box.top, b.box.top);
        if (width > 1 && height > 1) issues.push({a: a.text, b: b.text, width, height});
      }
      return {issues, checked_boxes: boxes.length};
    }""")
    assert not overlap["issues"], overlap
    return {**record, "overlap": overlap}


def isolation_and_motion(page):
    record = page.evaluate("""() => {
      const samples = ['light', 'dark', 'nested', 'sibling'];
      const roles = ['--ds-surface', '--ds-surface-raised', '--ds-ink', '--ds-ink-muted', '--ds-accent',
        '--ds-on-accent', '--ds-accent-ink', '--ds-line-strong', '--ds-focus', '--ds-focus-width', '--ds-err'];
      const values = Object.fromEntries(samples.map(name => {
        const root = document.querySelector(`[data-sample="${name}"]`);
        const nodes = [root, ...root.querySelectorAll('button, input, select, textarea, summary, nav, header, [role=tabpanel]')];
        return [name, nodes.map(node => Object.fromEntries(roles.map(role => [role, getComputedStyle(node).getPropertyValue(role).trim()])))];
      }));
      const motion = [...document.querySelectorAll('.fixture, .fixture *')].map(node => ({
        tag: node.tagName, transition: getComputedStyle(node).transitionDuration,
        animation: getComputedStyle(node).animationDuration}));
      const nested = document.querySelector('[data-sample=nested]');
      return {values, motion, sibling: nested.nextElementSibling.dataset.sample,
        reduced: matchMedia('(prefers-reduced-motion: reduce)').matches};
    }""")
    assert record["sibling"] == "sibling" and record["reduced"]
    values = record["values"]
    assert values["light"][0] != values["dark"][0]
    for name in SAMPLES:
        expected = values["dark" if name in ("dark", "nested") else "light"][0]
        assert all(row == expected for row in values[name]), (name, values[name])
    for motion in record["motion"]:
        assert all(
            float(duration.strip().removesuffix("s")) == 0
            for duration in (motion["transition"] + "," + motion["animation"]).split(
                ","
            )
        ), motion
    return record


def screenshot_boundaries(page, output, name):
    files = []
    for boundary in SAMPLES:
        path = output / f"{name}-{boundary}.png"
        sample(page, boundary).screenshot(path=str(path))
        files.append(path.name)
    return files


def check(root, output, expected_sha):
    output.mkdir(parents=True, exist_ok=True)
    evidence = {
        "commit": expected_sha,
        "playwright": importlib.metadata.version("playwright"),
        "cases": [],
        "issues": [],
    }
    server = None
    before = factory.digest_tree(root)
    try:
        assert (
            json.loads((root / "build.json").read_text())["commit"] == expected_sha
        ), "wrong artifact commit"
        html = (root / "index.html").read_text()
        assert (
            "<form" in html
            and 'role="tablist"' in html
            and 'data-sample="nested"' in html
        ), "missing SSR markup"
        evidence["ssr_artifact"] = "passed"
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            functools.partial(
                http.server.SimpleHTTPRequestHandler, directory=str(root)
            ),
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            evidence["chromium"] = browser.version
            static = browser.new_context(java_script_enabled=False)
            static_page = static.new_page()
            static_page.goto(base)
            assert static_page.locator("form").count() == 4
            assert static_page.locator('[role="tablist"]').count() == 5
            assert static_page.locator("#app[data-hydrated]").count() == 0
            evidence["served_ssr_without_scripts"] = "passed"
            static.close()
            for (width, height), scheme, scale in itertools.product(
                [(320, 740), (1440, 1000)], ["light", "dark"], [1, 2]
            ):
                name = f"{width}x{height}-{scheme}-{scale * 100}pct"
                case = {
                    "name": name,
                    "viewport": [width, height],
                    "scheme": scheme,
                    "text_scale": scale,
                    "issues": [],
                    "repeats": [],
                }
                evidence["cases"].append(case)
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    color_scheme=scheme,
                    reduced_motion="reduce",
                    service_workers="block",
                )
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text)
                        if message.type in ("warning", "error")
                        else None
                    ),
                )

                def guard(route):
                    if not route.request.url.startswith(base):
                        errors.append(f"external request: {route.request.url}")
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", guard)
                try:
                    for repeat in range(2):
                        page.goto(base, wait_until="networkidle")
                        page.locator('#app[data-hydrated="true"]').wait_for()
                        if scale == 2:
                            # Closed details need real computed type before resize.
                            page.locator("details").evaluate_all(
                                "nodes => nodes.forEach(node => node.open = true)"
                            )
                            resized = theme.resize_text(page)
                            assert resized["verified"] and resized["count"] > 100, (
                                resized
                            )
                            page.locator("details").evaluate_all(
                                "nodes => nodes.forEach(node => node.open = false)"
                            )
                            expect(
                                sample(page, "light").locator(
                                    '[data-state="disclosure"]'
                                )
                            ).to_have_text("Open: false")
                            case["text_resize"] = resized
                        result = {
                            "semantics": semantics(page),
                            "tab_order": tab_order(page),
                            "interaction": interactions(page),
                        }
                        case["repeats"].append(result)
                        if repeat == 0:
                            case["contrast"] = colours(page)
                            case["state_cues"] = structural_states(page)
                            case["targets"] = targets(page)
                            case["layout"] = layout(page)
                            case["themes_and_motion"] = isolation_and_motion(page)
                            # Also measure content hidden by the initial closed disclosures.
                            page.locator("details").evaluate_all(
                                "nodes => nodes.forEach(node => node.open = true)"
                            )
                            case["expanded_layout"] = layout(page)
                            case["expanded_contrast"] = colours(page)
                            page.locator("details").evaluate_all(
                                "nodes => nodes.forEach(node => node.open = false)"
                            )
                            case["screenshots"] = screenshot_boundaries(
                                page, output, name
                            )
                    assert case["repeats"][0] == case["repeats"][1], (
                        "repeated keyboard/semantics differed"
                    )
                    assert not errors, errors
                except Exception as error:
                    case["issues"].append(f"{type(error).__name__}: {error}")
                finally:
                    case["console_and_page_errors"] = errors
                    if errors:
                        case["issues"].append(f"console/page errors: {errors}")
                    page.screenshot(path=str(output / f"{name}.png"), full_page=True)
                    case["status"] = "failed" if case["issues"] else "passed"
                    print(f"{case['status']}: {name}", flush=True)
                    for issue in case["issues"]:
                        print(issue, flush=True)
                    context.close()
            browser.close()
        assert before == factory.digest_tree(root), "browser mutated built artifact"
        assert len(evidence["cases"]) == 8, "browser matrix incomplete"
        assert all(case["status"] == "passed" for case in evidence["cases"]), (
            "controls browser acceptance failed"
        )
    except Exception as error:
        evidence["issues"].append(f"{type(error).__name__}: {error}")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        evidence["status"] = "failed" if evidence["issues"] else "passed"
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        (output / "tested-files.json").write_text(json.dumps(before, indent=2) + "\n")
    assert evidence["status"] == "passed", evidence["issues"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.expected_sha)
