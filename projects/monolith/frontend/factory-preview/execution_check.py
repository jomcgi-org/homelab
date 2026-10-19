"""Actual private routes with synthetic GETs and a controllable EventSource."""

from urllib.parse import urlencode

from playwright.sync_api import Error as PlaywrightError


def geometry(page):
    return page.evaluate("""() => {
      const main = document.querySelector('main');
      const rect = main.getBoundingClientRect();
      const box = selector => {
        const el = document.querySelector(selector);
        if (!el) return null;
        return {height: el.clientHeight, scrollHeight: el.scrollHeight,
          overflowY: getComputedStyle(el).overflowY};
      };
      return {width: innerWidth, viewportHeight: innerHeight,
        pageOverflow: Math.max(0, document.documentElement.scrollWidth - innerWidth),
        bodyOverflow: getComputedStyle(document.body).overflow,
        mainTop: rect.top, mainBottom: rect.bottom,
        mainHeight: rect.height, sheet: getComputedStyle(main).getPropertyValue('--sheet').trim(),
        chromePosition: getComputedStyle(document.querySelector('.chrome')).position,
        legacyLoaded: getComputedStyle(document.documentElement).getPropertyValue('--cream').trim(),
        inbox: box('.inbox-body'), transcript: box('.turns'),
        publicSiteLinks: document.querySelectorAll('.site-index, a[href="/engineering"], a[href="/docs"], a[href="/cv"], a[href^="/app/"], a[href="/ember/firecracker"]').length,
        inboxCounts: [...document.querySelectorAll('.inbox-group-count')].map(e => ({
          size: parseFloat(getComputedStyle(e).fontSize),
          after: getComputedStyle(e, '::after').content})),
        clippedCrumbs: [...document.querySelectorAll('.trail')].filter(e => e.checkVisibility()).flatMap(trail => {
          const bounds = trail.getBoundingClientRect();
          return [...trail.querySelectorAll('.crumb')].filter(crumb => {
            const rect = crumb.getBoundingClientRect();
            return rect.left < bounds.left - 1 || rect.right > bounds.right + 1;
          }).map(crumb => crumb.textContent.trim());
        }),
        navVisible: [...document.querySelectorAll('.md-nav, .back-to-dashboard')].some(e => e.checkVisibility()),
        maintenanceBanner: !!document.querySelector('.maintenance-banner')};
    }""")


def assert_geometry(page, view):
    measured = geometry(page)
    assert measured["pageOverflow"] == 0, f"horizontal page overflow: {measured}"
    assert not measured["navVisible"], f"legacy navigation overlaps sheet: {measured}"
    assert not measured["clippedCrumbs"], f"clipped breadcrumb: {measured}"
    assert not measured["maintenanceBanner"], (
        "private route inherited public maintenance chrome"
    )
    assert measured["legacyLoaded"], "production legacy stylesheet was not loaded"
    assert measured["publicSiteLinks"] == 0, "public-tier links cannot resolve on the private host"
    for count in measured["inboxCounts"]:
        assert count["size"] >= 11.5 and count["after"] in ("none", "normal"), count
    if view == "execution":
        assert measured["bodyOverflow"] == "hidden", (
            f"sheet stole viewport scrolling: {measured}"
        )
        assert abs(measured["mainBottom"] - measured["viewportHeight"]) <= 1, measured
        assert measured["mainTop"] == 0, measured
        assert measured["chromePosition"] == "static", (
            "floating chrome covers console controls"
        )
        assert measured["inbox"] and measured["inbox"]["overflowY"] == "auto", measured
        if measured["transcript"]:
            assert measured["transcript"]["overflowY"] == "auto", measured
    else:
        assert measured["bodyOverflow"] == "auto", (
            "drain must retain document scrolling"
        )
    return measured


def assert_color_roles(page):
    measured = page.evaluate("""() => {
      const main = document.querySelector('main');
      const probe = document.createElement('span');
      main.append(probe);
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 1;
      const ctx = canvas.getContext('2d');
      const rgba = role => {
        probe.style.backgroundColor = `var(${role})`;
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = getComputedStyle(probe).backgroundColor;
        ctx.fillRect(0, 0, 1, 1);
        return [...ctx.getImageData(0, 0, 1, 1).data];
      };
      const luminance = rgb => rgb.slice(0, 3).map(c => c / 255)
        .map(c => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
        .reduce((sum, c, i) => sum + c * [.2126, .7152, .0722][i], 0);
      const fg = luminance(rgba('--ok')), bg = luminance(rgba('--ok-soft'));
      const sheet = rgba('--sheet'), scrim = rgba('--scrim-bg');
      const alpha = scrim[3] / 255;
      const blended = sheet.slice(0, 3).map((c, i) => scrim[i] * alpha + c * (1 - alpha));
      probe.remove();
      return {scheme: getComputedStyle(main).colorScheme, scrim,
        successContrast: (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05),
        sheetLuminance: luminance(sheet), dimmedLuminance: luminance(blended)};
    }""")
    assert measured["successContrast"] >= 4.5, measured
    assert measured["dimmedLuminance"] < measured["sheetLuminance"], measured
    return measured


def check_execution(browser, base, output, evidence):
    for width in (390, 1440):
        for scheme in ("light", "dark"):
            for view in ("execution", "drain"):
                name = f"{view}-{width}-{scheme}"
                record = {
                    "name": name,
                    "view": view,
                    "width": width,
                    "scheme": scheme,
                    "issues": [],
                    "images": [],
                    "measurements": {},
                }
                evidence["cases"].append(record)
                context = browser.new_context(
                    viewport={"width": width, "height": 900},
                    color_scheme=scheme,
                    reduced_motion="reduce",
                    service_workers="block",
                )
                page = context.new_page()
                page.set_default_timeout(5000)
                page.clock.install(time="2026-10-03T12:00:00Z")
                forbidden, errors = [], []

                def guard(route):
                    if not route.request.url.startswith(base):
                        forbidden.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", guard)
                page.on("pageerror", lambda error: errors.append(str(error)))
                context.tracing.start(screenshots=True, snapshots=True, sources=False)

                def shot(state):
                    record["measurements"][state] = assert_geometry(page, view)
                    image = f"{name}-{state}.png"
                    page.screenshot(path=str(output / image), full_page=view == "drain")
                    record["images"].append(image)

                try:
                    page.goto(
                        base
                        + "?"
                        + urlencode({"view": view, "selection": "?keep=synthetic"})
                    )
                    page.wait_for_function("window.__factoryFixtureReady")
                    page.evaluate("document.fonts.ready")
                    record["colors"] = {"system": assert_color_roles(page)}
                    assert record["colors"]["system"]["scheme"] == scheme
                    # An explicit toggle must win even against the other OS scheme.
                    for override in ("light", "dark"):
                        page.evaluate("theme => document.documentElement.dataset.theme = theme", override)
                        record["colors"][override] = assert_color_roles(page)
                        assert record["colors"][override]["scheme"] == override
                    page.evaluate("document.documentElement.removeAttribute('data-theme')")
                    if view == "drain":
                        page.get_by_text(
                            "Synthetic checkpoint 0", exact=False
                        ).wait_for()
                        shot("active")
                        page.get_by_role(
                            "button", name="synthetic-drain", exact=True
                        ).focus()
                        page.keyboard.press("Enter")
                        page.locator(".job-detail").wait_for()
                        page.evaluate("window.__executionFixture.advance()")
                        page.clock.run_for(5100)
                        page.get_by_text(
                            "Synthetic checkpoint 1", exact=False
                        ).wait_for()
                        shot("updated-detail")
                        # Operational buttons are inspected, never activated.
                        for label in ("run a cycle now", "cancel cycle"):
                            control = page.get_by_role("button", name=label, exact=True)
                            assert control.count() == 1, (
                                f"missing drain control: {label}"
                            )
                            control.focus()
                            assert control.evaluate(
                                "e => e === document.activeElement && getComputedStyle(e).outlineStyle !== 'none'"
                            )
                        page.get_by_role(
                            "button", name="synthetic-completed", exact=True
                        ).focus()
                        page.keyboard.press("Enter")
                        requeue = page.get_by_role("button", name="requeue", exact=True)
                        requeue.wait_for()
                        requeue.focus()
                        assert requeue.evaluate(
                            "e => e === document.activeElement && getComputedStyle(e).outlineStyle !== 'none'"
                        )
                        shot("completed-detail")
                        record["checks"] = [
                            "active item",
                            "keyboard detail",
                            "subsequent polling",
                            "document scrolling",
                            "controls focus",
                        ]
                    else:
                        page.wait_for_function(
                            "window.__executionFixture.stats.active === 1"
                        )
                        shot("opening")
                        if width == 390:
                            page.get_by_role(
                                "button", name="Synthetic running task", exact=False
                            ).click()
                        page.locator(".runview").wait_for()
                        shot("run")
                        page.locator(".srow").first.click()
                        page.get_by_text(
                            "Synthetic transcript update 0", exact=False
                        ).wait_for()
                        page.evaluate("window.__executionFixture.advance()")
                        page.clock.run_for(2100)
                        page.get_by_text(
                            "Synthetic transcript update 1", exact=False
                        ).wait_for()
                        # A streamed VM update is painted even without another page load.
                        page.wait_for_function(
                            "document.querySelector('.transcript-head').textContent.includes('asleep')"
                        )
                        scroller = page.locator(".turns")
                        assert scroller.evaluate(
                            "e => e.scrollHeight > e.clientHeight"
                        ), "fixture did not exercise inner scrolling"
                        scroller.evaluate("e => e.scrollTop = 0")
                        assert page.evaluate("scrollY === 0"), (
                            "transcript scrolled the document"
                        )
                        shot("updated-transcript")
                        # The repo-bearing session mounts SessionWalkthrough inside
                        # Turns, distinct from the full Walkthrough tab below.
                        page.locator(".turns .walk-summary").click()
                        points = page.locator(".turns .walk .point")
                        points.nth(1).wait_for()
                        assert points.count() == 2
                        points.nth(1).click()
                        selected = page.locator('.turns .walk .point[aria-current="true"]')
                        assert "selection.js" in selected.inner_text()
                        marker = selected.evaluate("""e => {
                          const s = getComputedStyle(e);
                          return {width: s.borderLeftWidth, style: s.borderLeftStyle,
                            edge: s.borderLeftColor, text: s.color};
                        }""")
                        assert marker["width"] == "2px" and marker["style"] == "solid", marker
                        assert marker["edge"] == marker["text"], marker
                        assert points.first.get_attribute("aria-current") == "false"
                        record["selectionMarker"] = marker
                        if width == 390:
                            page.locator(".turns .walk-back").click()
                        shot("walkthrough-selection")
                        if width == 390:
                            page.get_by_role(
                                "button", name="Session actions", exact=True
                            ).click()
                            page.get_by_role(
                                "menuitem", name="Walkthrough", exact=True
                            ).click()
                        else:
                            page.get_by_role(
                                "button", name="Walkthrough", exact=True
                            ).click()
                        page.get_by_text("Fixture walkthrough for synthetic.js", exact=True).wait_for()
                        shot("walkthrough")
                        if width == 390:
                            page.get_by_role(
                                "button", name="Session actions", exact=True
                            ).click()
                            page.get_by_role(
                                "menuitem", name="Conversation", exact=True
                            ).click()
                        else:
                            page.get_by_role(
                                "button", name="Conversation", exact=True
                            ).click()
                        # Repeated disconnects must arm one fallback; reconnect stops it.
                        page.evaluate(
                            "window.__executionFixture.disconnect(); window.__executionFixture.disconnect()"
                        )
                        page.clock.run_for(2100)
                        page.evaluate("window.__executionFixture.reconnect()")
                        calls = page.evaluate(
                            "window.__executionFixture.stats.calls['/factory/execution/vms']"
                        )
                        page.clock.run_for(2100)
                        assert (
                            page.evaluate(
                                "window.__executionFixture.stats.calls['/factory/execution/vms']"
                            )
                            == calls
                        )
                        assert (
                            page.evaluate("window.__executionFixture.stats.created")
                            == 1
                        )
                        assert (
                            page.evaluate("window.__executionFixture.stats.maxActive")
                            == 1
                        )
                        page.locator(".top-search").focus()
                        page.keyboard.press("Enter")
                        dialog = page.get_by_role(
                            "dialog", name="Jump to a session or run"
                        )
                        dialog.wait_for()
                        assert page.get_by_role(
                            "combobox", name="Filter sessions and runs"
                        ).evaluate("e => e === document.activeElement")
                        shot("jump")
                        page.keyboard.press("Escape")
                        dialog.wait_for(state="hidden")
                        page.get_by_role("button", name="New", exact=True).focus()
                        page.keyboard.press("Enter")
                        page.locator(".new-panel").wait_for()
                        assert page.locator(".new-panel textarea").evaluate(
                            "e => e === document.activeElement"
                        )
                        shot("new-panel")
                        page.keyboard.press("Escape")
                        page.locator(".new-panel").wait_for(state="hidden")
                        # Resize exercises the production visualViewport subscription.
                        if width == 390:
                            page.set_viewport_size({"width": 390, "height": 620})
                            page.wait_for_function(
                                "() => Math.abs(document.querySelector('main').getBoundingClientRect().bottom - innerHeight) <= 1"
                            )
                            shot("viewport")
                            page.set_viewport_size({"width": 390, "height": 900})
                            page.get_by_role(
                                "button", name="Back to agent sessions", exact=True
                            ).click()
                            page.locator(".inbox").wait_for()
                            shot("inbox-return")
                        assert (
                            "keep%3Dsynthetic" in page.url
                            or "keep=synthetic" in page.url
                        )
                        # Same-document unmount, then remount: no leaked streams or polls.
                        page.evaluate(
                            "document.querySelector('#preview-scope').hidden = false"
                        )
                        page.evaluate(
                            "document.querySelector('#preview-scope').insertAdjacentHTML('beforeend', '<a href=\"/factory/execution/drain\" id=\"fixture-drain-nav\">drain</a>')"
                        )
                        page.locator("#fixture-drain-nav").click()
                        page.locator(".drain-page").wait_for()
                        assert (
                            page.evaluate("window.__executionFixture.stats.active") == 0
                        )
                        before = page.evaluate(
                            "window.__executionFixture.stats.calls['/factory/execution/data']"
                        )
                        page.clock.run_for(16000)
                        assert (
                            page.evaluate(
                                "window.__executionFixture.stats.calls['/factory/execution/data']"
                            )
                            == before
                        )
                        page.locator(".top a.back").click()
                        page.locator(".execution-sheet").wait_for()
                        assert (
                            page.evaluate("window.__executionFixture.stats.active") == 1
                        )
                        assert (
                            page.evaluate("window.__executionFixture.stats.maxActive")
                            == 1
                        )
                        record["checks"] = [
                            "run detail",
                            "session query",
                            "transcript polling",
                            "VM stream update",
                            "walkthrough",
                            "repo-bearing session selection marker",
                            "fallback reconnect",
                            "keyboard overlays",
                            "viewport resize",
                            "mobile back",
                            "unmount/remount",
                        ]
                    record["network"] = page.evaluate("window.__executionFixture.stats")
                    assert not record["network"]["unexpected"], record["network"][
                        "unexpected"
                    ]
                    assert not forbidden, forbidden
                    assert not errors, errors
                except (AssertionError, PlaywrightError) as error:
                    record["issues"].append(str(error))
                    page.screenshot(
                        path=str(output / f"{name}-failure.png"), full_page=True
                    )
                finally:
                    record["status"] = "failed" if record["issues"] else "passed"
                    context.tracing.stop(
                        path=str(output / f"{name}-trace.zip")
                        if record["issues"]
                        else None
                    )
                    context.close()
                    print(f"{record['status']}: {name}", flush=True)
                    for issue in record["issues"]:
                        print(f"  {issue}", flush=True)
