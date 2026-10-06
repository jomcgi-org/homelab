"""Read-only Chromium checks of the isolated, server-rendered theme fixture."""

import argparse
import functools
import hashlib
import http.server
import importlib.metadata
import itertools
import json
import threading
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


def digest_tree(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def resize_text(page):
    # Same real text-only resize as factory-preview: snapshot computed pixels,
    # then double every font and explicit line-height. Spacing stays unchanged.
    return page.evaluate("""async () => {
      const rows = [...document.querySelectorAll('.fixture, .fixture *')]
        .map(node => ({node, size: parseFloat(getComputedStyle(node).fontSize),
          line: getComputedStyle(node).lineHeight}));
      for (const {node, size, line} of rows) {
        node.style.setProperty('font-size', `${size * 2}px`, 'important');
        if (line !== 'normal') node.style.setProperty('line-height', `${parseFloat(line) * 2}px`, 'important');
      }
      await new Promise(requestAnimationFrame);
      const mismatches = rows.filter(({node, size}) =>
        Math.abs(parseFloat(getComputedStyle(node).fontSize) - size * 2) >= 0.1)
        .map(({node, size}) => ({tag: node.tagName, expected: size * 2,
          actual: getComputedStyle(node).fontSize}));
      return {count: rows.length, verified: mismatches.length === 0, mismatches};
    }""")


# Compute contrast from rendered longhands, never by parsing token source.
# Composite ancestor backgrounds so transparent children use their actual ground.
COLOURS = """
  const rgba = value => {
    const channels = value.match(/[\\d.]+/g)?.map(Number);
    if (!channels || channels.length < 3 || !/^rgba?\\(/.test(value))
      throw new Error(`Unsupported computed colour: ${value}`);
    return [...channels.slice(0, 3), channels[3] ?? 1];
  };
  const over = (front, back) => {
    const alpha = front[3] + back[3] * (1 - front[3]);
    return [...front.slice(0, 3).map((channel, i) =>
      (channel * front[3] + back[i] * back[3] * (1 - front[3])) / alpha), alpha];
  };
  const background = node => {
    const stack = [];
    for (let parent = node; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.backgroundImage !== 'none' || Number(style.opacity) !== 1)
        throw new Error('Contrast requires unambiguous solid backgrounds');
      stack.push(rgba(style.backgroundColor));
    }
    return stack.reverse().reduce((back, front) => over(front, back), [255, 255, 255, 1]);
  };
  const luminance = colour => colour.slice(0, 3).map(channel => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (front, back) => {
    const values = [luminance(over(front, back)), luminance(back)].sort((a, b) => b - a);
    return (values[0] + 0.05) / (values[1] + 0.05);
  };
"""


def contrast_checks(page):
    return page.evaluate(
        "() => {"
        + COLOURS
        + """
      const records = [];
      const record = (node, kind, colour, ground, threshold) => {
        const sample = node.closest('[data-sample]');
        records.push({sample: sample.dataset.sample, scheme: sample.dataset.dsTheme,
          kind, text: node.textContent.trim().slice(0, 80), colour, ground,
          ratio: contrast(rgba(colour), ground), threshold});
      };
      for (const node of document.querySelectorAll('[data-contrast-text], [data-data-display] p, [data-data-display] dt, [data-data-display] dd, [data-data-display] summary, [data-data-display] th, [data-data-display] td, [data-data-display] caption, [data-data-display] span:not(.exact-sr), [data-data-display] h3, [data-data-display] h4')) {
        if (!node.checkVisibility() || node.closest('.exact-sr')) continue;
        const style = getComputedStyle(node);
        const large = parseFloat(style.fontSize) >= 24 ||
          (parseFloat(style.fontSize) >= 18.6667 && parseFloat(style.fontWeight) >= 700);
        record(node, 'text', style.color, background(node), large ? 3 : 4.5);
      }
      for (const node of document.querySelectorAll('[data-contrast-marker]'))
        record(node, 'series', getComputedStyle(node).fill, background(node), 3);
      for (const node of document.querySelectorAll('[data-series-role] svg'))
        record(node, 'series', getComputedStyle(node).fill, background(node), 3);
      for (const node of document.querySelectorAll('[data-contrast-border]')) {
        const style = getComputedStyle(node);
        if (parseFloat(style.borderTopWidth) !== 1 || style.borderTopStyle !== 'solid' ||
            parseFloat(style.borderRadius) !== 0) throw new Error('Structure must be square and 1px');
        record(node, 'border-outside', style.borderTopColor, background(node.parentElement), 3);
        if (node.tagName !== 'BUTTON') record(node, 'border-inside', style.borderTopColor, background(node), 3);
      }
      return records;
    }"""
    )


def root_boundary_checks(page):
    # Append the actual rendered contract values to replay a late contract
    # stylesheet. Root boundaries must retain every role, not just colour-scheme.
    return page.evaluate("""() => {
      const root = document.documentElement;
      const light = getComputedStyle(document.querySelector('[data-sample="light"]'));
      const roles = [...light].filter(name => name.startsWith('--ds-'));
      const defaults = getComputedStyle(root);
      const style = document.createElement('style');
      style.textContent = ':root{' + roles.map(role => [role, defaults.getPropertyValue(role)])
        .filter(([, value]) => value.trim()).map(([role, value]) => `${role}:${value};`).join('') + '}';
      document.head.append(style);
      const records = [];
      try {
        for (const scheme of ['light', 'dark']) {
          const expected = getComputedStyle(document.querySelector(`[data-sample="${scheme}"]`));
          root.dataset.dsTheme = `technical-drawing-${scheme}`;
          const actual = getComputedStyle(root);
          const mismatches = roles.filter(role => actual.getPropertyValue(role).trim() !== expected.getPropertyValue(role).trim());
          if (mismatches.length) throw new Error(`Root boundary lost roles: ${scheme}: ${mismatches}`);
          records.push({scheme, checked_roles: roles.length, late_contract: 'passed'});
        }
      } finally {
        root.removeAttribute('data-ds-theme');
        style.remove();
      }
      if (roles.length !== 33) throw new Error(`Root role coverage incomplete: ${roles.length}`);
      return records;
    }""")


def layout_checks(page):
    return page.evaluate("""() => {
      const issues = [];
      const root = document.documentElement;
      if (root.scrollWidth > root.clientWidth) issues.push(`page overflow: ${root.scrollWidth} > ${root.clientWidth}`);
      const walker = document.createTreeWalker(document.querySelector('.fixture'), NodeFilter.SHOW_TEXT);
      let count = 0;
      const boxes = [];
      while (walker.nextNode()) {
        const text = walker.currentNode;
        if (!text.textContent.trim() || !text.parentElement.checkVisibility() || text.parentElement.closest('.exact-sr')) continue;
        count++;
        const textStyle = getComputedStyle(text.parentElement);
        if (parseFloat(textStyle.fontSize) < 16) issues.push(`tiny meaningful text: ${text.textContent.trim()}`);
        if (textStyle.textOverflow === 'ellipsis' || textStyle.webkitLineClamp !== 'none')
          issues.push(`truncated text: ${text.textContent.trim()}`);
        const range = document.createRange();
        range.selectNodeContents(text);
        for (const box of range.getClientRects()) {
          if (!box.width || !box.height) continue;
          boxes.push({box, text: text.textContent.trim()});
          if (box.left < -1 || box.right > root.clientWidth + 1)
            issues.push(`text outside viewport: ${text.textContent.trim()}`);
          for (let node = text.parentElement; node; node = node.parentElement) {
            const style = getComputedStyle(node), rect = node.getBoundingClientRect();
            const clipsX = /hidden|clip|scroll|auto/.test(style.overflowX);
            const clipsY = /hidden|clip|scroll|auto/.test(style.overflowY);
            if ((clipsX && (box.left < rect.left - 1 || box.right > rect.right + 1)) ||
                (clipsY && (box.top < rect.top - 1 || box.bottom > rect.bottom + 1)))
              issues.push(`clipped text: ${text.textContent.trim()}`);
          }
        }
      }
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const a = boxes[i].box, b = boxes[j].box;
          if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 &&
              Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1)
            issues.push(`overlapping text: ${boxes[i].text} / ${boxes[j].text}`);
        }
      }
      return {issues, visible_text_nodes: count, checked_text_rectangles: boxes.length, scroll_width: root.scrollWidth, client_width: root.clientWidth};
    }""")


def keyboard_checks(page):
    controls = page.locator("a[href], button, summary, input, select, textarea, [tabindex]")
    expected = controls.count()
    assert expected > 6, f"data disclosures are missing, found {expected} controls"
    records = []
    # Start from the document and use Tab exclusively, including offscreen controls.
    page.evaluate("document.activeElement.blur()")
    for index in range(expected):
        page.keyboard.press("Tab")
        assert controls.nth(index).evaluate(
            "node => node === document.activeElement"
        ), f"keyboard skipped control {index}"
        record = page.evaluate(
            "() => {"
            + COLOURS
            + """
          const node = document.activeElement, style = getComputedStyle(node);
          const ground = background(node.parentElement);
          return {text: node.textContent.trim(), sample: node.closest('[data-sample]').dataset.sample,
            visible: node.matches(':focus-visible'), style: style.outlineStyle,
            width: parseFloat(style.outlineWidth), offset: parseFloat(style.outlineOffset),
            expected_width: parseFloat(style.getPropertyValue('--ds-focus-width')),
            colour: style.outlineColor, ground, ratio: contrast(rgba(style.outlineColor), ground),
            height: node.getBoundingClientRect().height, tag: node.tagName};
        }"""
        )
        assert record["visible"] and record["style"] == "solid", record
        assert record["width"] == record["expected_width"] and record["width"] >= 2, (
            record
        )
        assert record["offset"] >= record["width"], record
        assert record["ratio"] >= 3, record
        if record["tag"] == "SUMMARY":
            assert record["height"] >= 44, record
            page.keyboard.press("Enter")
            assert controls.nth(index).evaluate("node => node.parentElement.open"), record
        records.append(record)
    button = page.locator('[data-sample="dark"] > .raised button')
    button.focus()
    page.keyboard.press("Enter")
    assert button.inner_text() == "Sample action: 1", (
        "hydrated button did not respond"
    )
    return records


def data_display_checks(page):
    return page.evaluate("""() => {
      const expectedRoles = ['gpu', 'host-ram', 'page-cache', 'nvme', 'hot-expert-set'];
      const expectedMarkers = ['circle', 'square', 'triangle', 'diamond', 'cross'];
      const expectedLabels = ['GPU', 'Host RAM', 'Page cache', 'NVMe', 'Hot expert set'];
      const displays = [...document.querySelectorAll('[data-data-display]')];
      if (displays.length !== 3) throw new Error('Every boundary must render data primitives');
      const records = [];
      const ordered = nodes => {
        const rects = nodes.map(node => node.getBoundingClientRect());
        for (let i = 1; i < rects.length; i++) {
          const a = rects[i - 1], b = rects[i];
          if (b.top < a.top - 1 || (Math.abs(b.top - a.top) < 1 && b.left < a.right - 1))
            throw new Error(`Visual reading order changed: ${nodes[i].textContent}`);
        }
      };
      for (const display of displays) {
        const boundary = display.closest('[data-sample]');
        const legend = [...display.querySelectorAll('[data-series-role]')];
        legend.forEach((node, i) => {
          if (node.dataset.seriesRole !== expectedRoles[i] || node.dataset.marker !== expectedMarkers[i] ||
              node.textContent.trim() !== `${expectedLabels[i]} (${expectedMarkers[i]})`)
            throw new Error('Series identity, marker or order changed');
          const reference = boundary.querySelector(`.series > [data-series="${i + 1}"] svg`);
          if (getComputedStyle(reference).fill !== getComputedStyle(node.querySelector('svg')).fill)
            throw new Error(`Series role colour changed: ${expectedRoles[i]}`);
        });
        if (legend.length !== 5) throw new Error('Incomplete series legend');
        ordered(legend);
        const metrics = [...display.querySelectorAll('.metrics .metric')];
        if (metrics.length !== 12) throw new Error(`Missing measurement edge fixtures: ${metrics.length}`);
        ordered(metrics);
        for (const dl of display.querySelectorAll('dl')) ordered([...dl.querySelectorAll('dt, dd')]);
        const table = display.querySelector('[data-fallback-table]');
        if (!table.closest('details').open || !table.checkVisibility() || !table.querySelector('caption') ||
            table.querySelectorAll('thead th[scope="col"]').length !== 2 ||
            table.querySelectorAll('tbody th[scope="row"]').length !== metrics.length)
          throw new Error('Chart fallback table is not usable or incomplete');
        ordered([...table.querySelectorAll('tbody tr')]);
        for (const [i, metric] of metrics.entries()) {
          const row = table.querySelectorAll('tbody tr')[i];
          if (row.querySelector('th').textContent !== metric.querySelector('.label').textContent)
            throw new Error('Fallback table reading order differs from metrics');
          const exact = metric.querySelector('.exact-sr');
          if (row.querySelector('td').textContent !== (exact?.textContent ?? 'Unavailable'))
            throw new Error('Fallback table lost exact measurement or missing state');
          if (exact && (exact.closest('[aria-hidden="true"]') || metric.querySelector('details data').textContent !== exact.textContent))
            throw new Error('Exact value is not accessible without hover');
        }
        if (display.querySelector('[aria-live], [role="status"], [title]')) throw new Error('Ordinary data must not announce or require hover');
        for (const state of ['loading', 'empty', 'error', 'unavailable']) {
          const block = display.querySelector(`[data-state-case="${state}"]`);
          if (block.querySelector('figure').dataset.state !== state || !block.textContent.includes(`Chart data: ${state}`))
            throw new Error(`Missing state: ${state}`);
          if (block.querySelector('.chart, .measurement')) throw new Error('State displays stale data');
        }
        records.push({sample: boundary.dataset.sample, metrics: metrics.length, series: legend.length,
          fallback_rows: table.querySelectorAll('tbody tr').length, reading_order: 'passed'});
      }
      return records;
    }""")


def check(root, output, expected_sha):
    output.mkdir(parents=True, exist_ok=True)
    before = digest_tree(root)
    assert json.loads((root / "build.json").read_text())["commit"] == expected_sha, (
        "artifact is not from the requested commit"
    )
    assert (
        'data-ds-theme="technical-drawing-light"' in (root / "index.html").read_text()
    ), "static artifact has no server-rendered light boundary"
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root)),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    evidence = {
        "commit": expected_sha,
        "playwright": importlib.metadata.version("playwright"),
        "cases": [],
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            evidence["chromium"] = browser.version
            for (width, height), scheme, scale in itertools.product(
                [(320, 740), (1440, 1000)], ["light", "dark"], [1, 2]
            ):
                name = f"{width}x{height}-{scheme}-{scale * 100}pct"
                record = {
                    "name": name,
                    "width": width,
                    "height": height,
                    "browser_scheme": scheme,
                    "text_scale": scale,
                    "issues": [],
                }
                evidence["cases"].append(record)
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    color_scheme=scheme,
                    reduced_motion="reduce",
                    service_workers="block",
                )
                page = context.new_page()
                page.set_default_timeout(10000)
                errors = []
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                page.on(
                    "console",
                    lambda message, errors=errors: (
                        errors.append(message.text)
                        if message.type in ("warning", "error")
                        else None
                    ),
                )

                def guard(route, errors=errors):
                    if not route.request.url.startswith(base):
                        errors.append(f"external request: {route.request.url}")
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", guard)
                try:
                    page.goto(base, wait_until="networkidle")
                    page.locator('#app[data-hydrated="true"]').wait_for()
                    assert (
                        page.locator(
                            '[data-sample="light"] > [data-sample="nested"]'
                        ).count()
                        == 1
                    )
                    assert (
                        page.locator('[data-ds-theme="technical-drawing-dark"]').count()
                        == 2
                    )
                    record["root_boundaries"] = root_boundary_checks(page)
                    if scale == 2:
                        record["text_resize"] = resize_text(page)
                        assert record["text_resize"]["verified"], record["text_resize"]
                    record["contrast"] = contrast_checks(page)
                    assert len(record["contrast"]) >= 100, (
                        "contrast coverage is incomplete"
                    )
                    for colour in record["contrast"]:
                        assert colour["ratio"] >= colour["threshold"], colour
                    record["layout"] = layout_checks(page)
                    assert not record["layout"]["issues"], record["layout"]
                    record["focus"] = keyboard_checks(page)
                    record["data_display"] = data_display_checks(page)
                    record["expanded_contrast"] = contrast_checks(page)
                    for colour in record["expanded_contrast"]:
                        assert colour["ratio"] >= colour["threshold"], colour
                    after = layout_checks(page)
                    record["expanded_layout"] = after
                    assert not after["issues"], after
                    assert not errors, errors
                except (AssertionError, PlaywrightError) as error:
                    record["issues"].append(str(error))
                finally:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(output / f"{name}.png"), full_page=True)
                    record["status"] = "failed" if record["issues"] else "passed"
                    context.close()
                    print(f"{record['status']}: {name}", flush=True)
                    for issue in record["issues"]:
                        print(issue, flush=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        (output / "tested-files.json").write_text(json.dumps(before, indent=2) + "\n")
    assert before == digest_tree(root), "browser check changed the artifact"
    assert len(evidence["cases"]) == 8, "browser matrix incomplete"
    assert all(case["status"] == "passed" for case in evidence["cases"]), (
        "theme browser checks failed; see result.json and screenshots"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    check(args.artifact.resolve(), args.output.resolve(), args.expected_sha)
