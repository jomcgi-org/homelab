"""Bounded gallery acceptance, hosted by the existing theme browser CI step.

The original theme/data-display checks run unchanged before this matrix.
Every case renders both explicit schemes, the nested boundary and the unmarked
contract region. Both compositions are present in each full-page screenshot.
"""

import hashlib
import itertools
import shutil
from datetime import datetime, timezone

WIDTHS = (320, 390, 1440)
SCHEMES = ("light", "dark")
SCALES = (1, 2)
COMPOSITIONS = ("dashboard", "document")
CAPTURES = ("composition", "chart", "tabs", "disclosure-open", "error-field")
EXPECTED_CASES = len(WIDTHS) * len(SCHEMES) * len(SCALES) * len(COMPOSITIONS)
INTERACTIVE = "a[href],button,input,select,textarea,summary,[tabindex]"


def preflight(root):
    page = root / "gallery.html"
    assert page.is_file() and page.stat().st_size, "gallery page missing from artifact"
    assert 'data-gallery-boundary="light"' in page.read_text(), "gallery SSR missing"


def validate(evidence, output):
    cases = evidence["gallery_cases"]
    assert cases, "zero gallery cases ran"
    expected = {
        f"{composition}-{width}-{scheme}-{scale * 100}pct"
        for composition, width, scheme, scale in itertools.product(
            COMPOSITIONS, WIDTHS, SCHEMES, SCALES
        )
    }
    assert len(cases) == EXPECTED_CASES and {c["name"] for c in cases} == expected, (
        "gallery matrix incomplete"
    )
    for case in cases:
        required = {
            f"gallery/{case['name']}-{boundary}-{suffix}.png"
            for boundary in SCHEMES
            for suffix in CAPTURES
        }
        required.add(f"gallery/{case['name']}-full.png")
        assert required.issubset(case.get("screenshots", [])), (
            f"expected screenshot list incomplete: {case['name']}"
        )
        for name in case["screenshots"]:
            path = output / name
            assert path.is_file() and path.stat().st_size, (
                f"missing or empty screenshot: {name}"
            )
            assert (
                evidence["screenshots"].get(name)
                == hashlib.sha256(path.read_bytes()).hexdigest()
            ), f"screenshot digest mismatch: {name}"
        if case["status"] == "failed":
            assert (output / case["trace"]).is_file(), (
                "failed gallery case has no trace"
            )
    assert all(case["status"] == "passed" for case in cases), (
        "gallery checks failed; see result.json"
    )
    repeat = next(c for c in cases if c["name"] == "dashboard-320-light-200pct")
    assert repeat.get("repeat_digest_identical"), (
        "same-session determinism evidence missing"
    )


def settle(page):
    return page.evaluate("""async () => {
      await document.fonts.ready;
      await new Promise(requestAnimationFrame);
      const geometry = () => [...document.querySelectorAll('.gallery *')]
        .map(n => {const r = n.getBoundingClientRect(); return [r.x,r.y,r.width,r.height];});
      let before = JSON.stringify(geometry()), stable = 0;
      for (let frame=0;frame<30;frame++) {
        await new Promise(requestAnimationFrame);
        const after=JSON.stringify(geometry());
        stable=before===after?stable+1:0;
        if (stable>=2) return {stable_frames:stable, time: Date.now(),
          loaded: [...document.fonts].map(f => ({family:f.family,status:f.status}))};
        before=after;
      }
      throw new Error('gallery layout not settled after 30 frames');
    }""")


def isolation(page):
    return page.evaluate("""() => {
      const root = getComputedStyle(document.documentElement);
      const unmarked = getComputedStyle(document.querySelector('[data-gallery-boundary=unmarked]'));
      const roles = [...root].filter(k => k.startsWith('--ds-'));
      if (roles.length < 25) throw new Error('contract roles missing');
      for (const role of roles)
        if (root.getPropertyValue(role) !== unmarked.getPropertyValue(role))
          throw new Error(`theme leaked into unmarked region: ${role}`);
      if (unmarked.getPropertyValue('--ds-surface').trim() !== '#f3ede1' ||
          unmarked.getPropertyValue('--ds-radius').trim() !== '8px' ||
          !unmarked.getPropertyValue('--ds-font-body').includes('Hanken Grotesk'))
        throw new Error('unmarked contract defaults changed');
      const boundary = name => getComputedStyle(document.querySelector(`[data-gallery-boundary=${name}]`));
      const light = boundary('light'), dark = boundary('dark'), nested = boundary('nested'), sibling = boundary('sibling');
      const themed = [...dark].filter(k => k.startsWith('--ds-'));
      for (const role of themed) {
        if (nested.getPropertyValue(role) !== dark.getPropertyValue(role)) throw new Error(`nested scheme: ${role}`);
        if (sibling.getPropertyValue(role) !== light.getPropertyValue(role)) throw new Error(`sibling scheme: ${role}`);
      }
      if (light.colorScheme !== 'light' || dark.colorScheme !== 'dark' || nested.colorScheme !== 'dark')
        throw new Error('explicit colour scheme lost');
      if (!document.fonts.check('16px "Schibsted Grotesk"') ||
          ![...document.fonts].some(f => f.family === 'Schibsted Grotesk' && f.status === 'loaded'))
        throw new Error('bundled Schibsted Grotesk did not load');
      for (const name of ['light','dark','nested'])
        if (!boundary(name).fontFamily.startsWith('"Schibsted Grotesk"')) throw new Error(`font family: ${name}`);
      return {contract_roles:roles.length, nested_roles:themed.length, font:'Schibsted Grotesk', unmarked:'contract defaults'};
    }""")


def structure(page):
    return page.evaluate("""() => {
      const ordered = nodes => {
        const rows = nodes.filter(n => n.checkVisibility()).map(n => ({n,r:n.getBoundingClientRect()}));
        for (let i=1;i<rows.length;i++) {
          const a=rows[i-1].r,b=rows[i].r;
          if (b.top<a.top-1 || (Math.abs(b.top-a.top)<1 && b.left<a.right-1))
            throw new Error(`DOM/visual order: ${rows[i].n.textContent.slice(0,80)}`);
        }
        return rows.length;
      };
      const records=[];
      for (const name of ['light','dark']) {
        const root=document.querySelector(`[data-gallery-boundary=${name}]`);
        const sections=[...root.children].filter(n=>n.hasAttribute('data-gallery-section'));
        const expected=['dashboard','document','controls','navigation','rows','status','metrics','chart'];
        if (JSON.stringify(sections.map(n=>n.dataset.gallerySection))!==JSON.stringify(expected))
          throw new Error(`missing compositions: ${name}`);
        ordered(sections);
        for (const selector of ['.metrics','.stack','dl','tbody','[role=tablist]','nav ol'])
          for (const group of root.querySelectorAll(selector)) ordered([...group.children]);
        const roles=['gpu','host-ram','page-cache','nvme','hot-expert-set'];
        const shapes=['circle','square','triangle','diamond','cross'];
        const labels=['GPU','Host RAM','Page cache','NVMe','Hot expert set'];
        for (const legend of root.querySelectorAll('ul[aria-label="Synthetic memory tier markers"]')) {
          const entries=[...legend.children];
          if (entries.length!==5) throw new Error('incomplete legend');
          entries.forEach((n,i)=>{
            if (n.dataset.seriesRole!==roles[i] || n.dataset.marker!==shapes[i] ||
                n.textContent.trim()!==`Synthetic ${labels[i]} (${shapes[i]})`)
              throw new Error('series meaning requires labels and distinct shapes');
            const svg=n.querySelector('svg');
            const signature=svg.innerHTML;
            if (!signature.includes(i===0?'circle':i===1?'rect':'path')) throw new Error('marker geometry missing');
            if (entries.some((other,j)=>j!==i && other.querySelector('svg').innerHTML===signature))
              throw new Error('marker shapes not distinct');
          });
          ordered(entries);
        }
        const cues={'ok':'✓','warn':'△','err':'×','unknown':'?','pending':'◷'};
        for (const [kind,cue] of Object.entries(cues)) {
          const nodes=[...root.querySelectorAll(`[data-kind=${kind}]`)];
          if (!nodes.length || nodes.some(n=>n.querySelector('.cue')?.textContent!==cue ||
              !n.textContent.includes('Synthetic ') || n.textContent.trim()===cue))
            throw new Error(`status meaning missing: ${kind}`);
        }
        for (const figure of root.querySelectorAll('figure')) {
          const resolve=attribute=>(figure.getAttribute(attribute)||'').split(' ').map(id=>document.getElementById(id)?.textContent);
          if (resolve('aria-labelledby')[0]!=='Synthetic memory allocation' ||
              !resolve('aria-describedby')[0]?.includes('MiB') || !resolve('aria-describedby')[1])
            throw new Error('chart name, units or description missing');
          if (!figure.querySelector('details .fallback')) throw new Error('chart fallback missing');
        }
        for (const table of root.querySelectorAll('[data-gallery-fallback]')) {
          const rows=[...table.querySelectorAll('tbody tr')];
          if (rows.length!==5 || !table.querySelector('caption')?.textContent.includes('MiB'))
            throw new Error('exact chart fallback incomplete');
          rows.forEach((row,i)=>{
            if (row.querySelector('th').textContent!==`Synthetic ${labels[i]}` ||
                row.querySelector('td').textContent!==`${[80,64,48,32,16][i]} MiB`)
              throw new Error('exact fallback value or reading order changed');
          });
        }
        records.push({boundary:name, sections:ordered(sections), status_kinds:5, series_shapes:5});
      }
      const targets=[...document.querySelectorAll('a[href],button,input,select,textarea,summary')]
        .filter(n=>n.checkVisibility());
      for (const node of targets) {
        const r=node.getBoundingClientRect();
        if (r.width<44 || r.height<44) throw new Error(`target below 44x44: ${node.textContent||node.name}: ${r.width}x${r.height}`);
        if (r.left<0 || r.right>document.documentElement.clientWidth+1) throw new Error('control outside viewport');
      }
      return {boundaries:records, touch_targets:targets.length};
    }""")


def contrast(page, colours):
    records = page.evaluate(
        "() => {"
        + colours
        + """
      const records=[];
      const record=(node,kind,colour,ground,threshold)=>records.push({
        boundary:node.closest('[data-gallery-boundary]')?.dataset.galleryBoundary||'contract',
        kind,text:(node.textContent||node.name||'').trim().slice(0,100),colour,ground,
        ratio:contrast(rgba(colour),ground),threshold});
      for (const node of document.querySelectorAll('.gallery *')) {
        if (!node.checkVisibility() || node.closest('.exact-sr') || node.closest('svg')) continue;
        const s=getComputedStyle(node);
        const text=[...node.childNodes].some(n=>n.nodeType===Node.TEXT_NODE && n.textContent.trim());
        const large=parseFloat(s.fontSize)>=24 || (parseFloat(s.fontSize)>=18.6667 && parseFloat(s.fontWeight)>=700);
        if (text || /INPUT|SELECT|TEXTAREA/.test(node.tagName)) record(node,'text',s.color,background(node),large?3:4.5);
        // Decorative panel/table hairlines do not identify an interactive boundary.
        if (/BUTTON|INPUT|SELECT|TEXTAREA|DETAILS/.test(node.tagName))
          for (const side of ['Top','Right','Bottom','Left']) {
            const colour=s[`border${side}Color`];
            if (parseFloat(s[`border${side}Width`])>0 && rgba(colour)[3]>0)
              record(node,`border-${side}`,colour,background(node.parentElement),3);
          }
        if (node.classList.contains('cue')) record(node,'status-glyph',s.color,background(node),3);
      }
      for (const node of document.querySelectorAll('[data-series-role] svg, [data-gallery-series] g[fill]'))
        if (node.checkVisibility()) record(node,'series-marker',getComputedStyle(node).fill,background(node),3);
      return records;
    }"""
    )
    assert len(records) > 100, "gallery contrast coverage incomplete"
    for row in records:
        assert row["ratio"] >= row["threshold"], row
    for boundary in ("light", "dark"):
        for kind in ("text", "border-Bottom", "status-glyph", "series-marker"):
            assert any(
                r["boundary"] == boundary and r["kind"] == kind for r in records
            ), (boundary, kind)
    return records


def focus(page, colours):
    record = page.evaluate(
        "() => {"
        + colours
        + """
      const node=document.activeElement,s=getComputedStyle(node),ground=background(node.parentElement);
      const r=node.getBoundingClientRect();
      return {name:(node.textContent||node.name).trim().slice(0,100),visible:node.matches(':focus-visible'),
        style:s.outlineStyle,width:parseFloat(s.outlineWidth),offset:parseFloat(s.outlineOffset),
        contrast:contrast(rgba(s.outlineColor),ground),left:r.left,right:r.right};
    }"""
    )
    assert record["visible"] and record["style"] == "solid", record
    assert record["width"] >= 2 and record["offset"] >= 2 and record["contrast"] >= 3, (
        record
    )
    return record


def native_text(page):
    """Native control values have no DOM text ranges; measure their font too."""
    records = page.evaluate("""() => {
      const context=document.createElement('canvas').getContext('2d');
      return [...document.querySelectorAll('.gallery input,.gallery select')]
        .filter(n=>n.checkVisibility()).map(node=>{
          const style=getComputedStyle(node), rect=node.getBoundingClientRect();
          const text=node.tagName==='SELECT'?node.selectedOptions[0]?.textContent:node.value;
          context.font=`${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
          const spacing=parseFloat(style.letterSpacing)||0;
          const width=context.measureText(text||'').width+Math.max(0,(text||'').length-1)*spacing;
          // Reserve one font em for the browser's native dropdown arrow.
          const available=rect.width-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)
            -parseFloat(style.borderLeftWidth)-parseFloat(style.borderRightWidth)
            -(node.tagName==='SELECT'?parseFloat(style.fontSize):0);
          return {name:node.name,text,width,available};
        });
    }""")
    assert len(records) == 12, "native field coverage incomplete"
    for record in records:
        assert record["width"] <= record["available"], (
            f"clipped native control value: {record}"
        )
    return records


def native_text_regression(page):
    """Prove the previously clipped selected option now fails the checker."""
    select = page.locator('select[name="region"]').first
    original = select.evaluate("n=>n.selectedOptions[0].textContent")
    select.evaluate("n=>n.selectedOptions[0].textContent='Invented north'")
    try:
        native_text(page)
    except AssertionError as error:
        assert "clipped native control value" in str(error), str(error)
        return {"rejected_clipped_option": "Invented north", "failure": str(error)}
    else:
        raise AssertionError("native-value regression was not rejected")
    finally:
        select.evaluate("(n,text)=>n.selectedOptions[0].textContent=text", original)


def keyboard(page, colours):
    from playwright.sync_api import expect

    # Native DOM order is independently compared with visual order in structure().
    stops = page.locator(INTERACTIVE).evaluate_all("""nodes => nodes.filter(n=>
      n.checkVisibility() && !n.disabled && n.tabIndex>=0).map((n,i)=>{
        n.dataset.galleryStop=String(i);return i;
      })""")
    assert len(stops) > 40, "gallery keyboard coverage missing"
    page.locator("body").click(position={"x": 1, "y": 1})
    page.evaluate("document.activeElement.blur(); window.scrollTo(0,0)")
    records = []
    for index in stops:
        page.keyboard.press("Tab")
        expect(page.locator(f'[data-gallery-stop="{index}"]')).to_be_focused()
        records.append(focus(page, colours))
    for index in reversed(stops[:-1]):
        page.keyboard.press("Shift+Tab")
        expect(page.locator(f'[data-gallery-stop="{index}"]')).to_be_focused()
        focus(page, colours)
    return records


def interactions(page, colours):
    from playwright.sync_api import expect

    records = []
    for name in ("light", "dark"):
        root = page.locator(f'[data-gallery-boundary="{name}"]')
        for nav in root.get_by_role("navigation", name="Breadcrumb", exact=True).all():
            expect(nav.locator('[aria-current="page"]')).to_have_count(1)
            expect(
                nav.get_by_role("link", name="Synthetic gallery", exact=True)
            ).to_have_attribute("href", f"#{name}-dashboard")
        for form in root.locator("form").all():
            region = form.get_by_role(
                "combobox", name="Synthetic region (required)", exact=True
            )
            expect(region).to_have_accessible_description("Choose an invented region")
            region.select_option("south")
            expect(region).to_have_value("south")
            field = form.get_by_role(
                "textbox", name="Synthetic reference (required)", exact=True
            )
            expect(field).to_have_accessible_description(
                "Enter a local sample reference Error: Reference must contain a sheet label"
            )
            expect(field).to_have_attribute("aria-invalid", "true")
            assert field.get_attribute("aria-describedby"), (
                "error has no description association"
            )
            field.fill("sheet-014")
            expect(field).to_have_value("sheet-014")
            native_text(page)
            before = int(
                form.locator('[data-gallery-state="submissions"]')
                .inner_text()
                .split(": ")[1]
            )
            form.get_by_role("button", name="Save synthetic sheet", exact=True).press(
                "Enter"
            )
            expect(form.locator('[data-gallery-state="submissions"]')).to_have_text(
                f"Submissions: {before + 1}"
            )
            form.get_by_role("button", name="Reset fields", exact=True).click()
            expect(field).to_have_value("")
            expect(region).to_have_value("north")
            expect(
                form.get_by_role("textbox", name="Unavailable destination", exact=True)
            ).to_be_disabled()
        for node in root.locator(":disabled").all():
            expect(node).to_be_disabled()
            before = root.inner_text()
            assert not node.evaluate(
                "n => {n.focus();return n===document.activeElement}"
            ), "disabled control took focus"
            node.evaluate("n=>n.click()")
            assert before == root.inner_text(), "disabled control changed state"
        for tablist in root.get_by_role("tablist").all():
            tabs = tablist.get_by_role("tab")
            for key, index in (
                ("ArrowRight", 1),
                ("ArrowLeft", 0),
                ("End", 1),
                ("Home", 0),
            ):
                if key == "ArrowRight":
                    tabs.nth(0).press("Tab")
                    tabs.nth(0).focus()
                page.keyboard.press(key)
                expect(tabs.nth(index)).to_be_focused()
                expect(tabs.nth(index)).to_have_attribute("aria-selected", "true")
                expect(tablist.locator('[aria-selected="true"]')).to_have_count(1)
                panel = page.locator(
                    f'[id="{tabs.nth(index).get_attribute("aria-controls")}"]'
                )
                expect(panel).to_be_visible()
                expect(panel).to_have_accessible_name(tabs.nth(index).inner_text())
                focus(page, colours)
        toggle = root.locator(f'[data-gallery-action="{name}-disclosure"]')
        controlled = root.locator('[data-gallery-section="controls"] details')
        if controlled.evaluate("n=>n.open"):
            toggle.press("Enter")
            expect(controlled).to_have_js_property("open", False)
        for value in ("true", "false"):
            toggle.press("Enter")
            expect(root.locator('[data-gallery-state="disclosure"]')).to_have_text(
                f"Notes open: {value}"
            )
            assert (
                root.locator('[data-gallery-section="controls"] details').evaluate(
                    "n=>String(n.open)"
                )
                == value
            )
        records.append(
            {
                "boundary": name,
                "forms": 2,
                "tablists": 2,
                "repeated_toggles": 2,
                "disabled": "inert",
            }
        )
    # Open each native disclosure with real keyboard activation, close and reopen.
    for summary in page.locator("summary").all():
        details = summary.locator("xpath=..")
        if details.evaluate("n=>n.open"):
            summary.press("Enter")
            expect(details).to_have_js_property("open", False)
        for opened in (True, False, True):
            summary.press("Enter")
            expect(details).to_have_js_property("open", opened)
    return records


def motion(page):
    result = page.evaluate("""() => {
      const durations=[];
      for (const node of document.querySelectorAll('.gallery,.gallery *'))
        for (const pseudo of [null,'::before','::after']) {
          const s=getComputedStyle(node,pseudo);
          for (const property of ['animationDuration','transitionDuration'])
            if (s[property].split(',').some(v=>parseFloat(v)>0.00001)) durations.push({tag:node.tagName,pseudo,property,value:s[property]});
        }
      return {animations:document.getAnimations().length,durations};
    }""")
    assert result["animations"] == 0 and not result["durations"], result
    return result


def run(browser, base, output, evidence, resize_text, layout_checks, colours):
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect

    directory = output / "gallery"
    directory.mkdir(exist_ok=True)
    for width, scheme, scale in itertools.product(WIDTHS, SCHEMES, SCALES):
        # Both compositions share one page. Page-wide assertions cover both;
        # retain a separately named result and composition capture for each.
        composition = "dashboard"
        name = f"{composition}-{width}-{scheme}-{scale * 100}pct"
        document_name = f"document-{width}-{scheme}-{scale * 100}pct"
        selected = [
            (boundary, suffix, selector)
            for boundary in ("light", "dark")
            for suffix, selector in (
                ("composition", f'[data-gallery-section="{composition}"]'),
                ("chart", '[data-gallery-chart-state="ready"] figure'),
                ("tabs", '[data-gallery-section="navigation"] .tabs'),
                ("disclosure-open", '[data-gallery-section="document"] details[open]'),
                (
                    "error-field",
                    '[data-gallery-section="document"] input[aria-invalid="true"] >> xpath=..',
                ),
            )
        ]
        record = {
            "name": name,
            "composition": composition,
            "width": width,
            "browser_scheme": scheme,
            "text_scale": scale,
            "shared_page_cases": [name, document_name],
            "issues": [],
            "screenshots": [
                f"gallery/{name}-{boundary}-{suffix}.png"
                for boundary, suffix, _ in selected
            ]
            + [f"gallery/{name}-full.png"],
        }
        evidence["gallery_cases"].append(record)
        context = browser.new_context(
            viewport={"width": width, "height": 1000},
            color_scheme=scheme,
            reduced_motion="reduce",
            service_workers="block",
        )
        # Each failed case retains its full-page image and API trace. Avoid a
        # DOM snapshot and filmstrip for every keyboard event on this long page.
        context.tracing.start(screenshots=False, snapshots=False, sources=True)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
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
                errors.append(
                    f"external request (network fonts forbidden): {route.request.url}"
                )
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        try:
            page.clock.set_fixed_time(datetime(2026, 1, 14, tzinfo=timezone.utc))
            page.goto(base + "gallery.html", wait_until="networkidle")
            page.locator('#app[data-hydrated="true"]').wait_for()
            record["isolation"] = isolation(page)
            if scale == 2:
                record["text_resize"] = resize_text(page, ".gallery")
                assert record["text_resize"]["verified"], record["text_resize"]
            record["settled"] = settle(page)
            record["collapsed_layout"] = layout_checks(page, ".gallery")
            assert not record["collapsed_layout"]["issues"], record["collapsed_layout"]
            record["structure"] = structure(page)
            record["native_text"] = native_text(page)
            if width == 320 and scale == 2 and scheme == "light":
                record["native_value_regression"] = native_text_regression(page)
            record["contrast"] = contrast(page, colours)
            record["keyboard"] = keyboard(page, colours)
            record["interactions"] = interactions(page, colours)
            record["reduced_motion"] = motion(page)
            page.emulate_media(reduced_motion="no-preference")
            # Repeat interactions with ordinary motion, then restore capture conditions.
            record["ordinary_interactions"] = interactions(page, colours)
            for toggle in page.locator('[data-gallery-action$="-disclosure"]').all():
                before = (
                    toggle.locator(
                        "xpath=ancestor::section[@data-gallery-section='controls']"
                    )
                    .locator("details")
                    .evaluate("n=>n.open")
                )
                toggle.press("Enter")
                toggle.press("Enter")
                assert (
                    toggle.locator(
                        "xpath=ancestor::section[@data-gallery-section='controls']"
                    )
                    .locator("details")
                    .evaluate("n=>n.open")
                    == before
                )
            record["ordinary_motion"] = motion(page)
            page.emulate_media(reduced_motion="reduce")
            record["expanded_layout"] = layout_checks(page, ".gallery")
            assert not record["expanded_layout"]["issues"], record["expanded_layout"]
            record["expanded_structure"] = structure(page)
            record["expanded_native_text"] = native_text(page)
            record["expanded_contrast"] = contrast(page, colours)
            assert not errors, errors
            for boundary, suffix, selector in selected:
                target = (
                    page.locator(f'[data-gallery-boundary="{boundary}"]')
                    .locator(selector)
                    .first
                )
                expect(target).to_be_visible()
                settle(page)
                path = f"gallery/{name}-{boundary}-{suffix}.png"
                target.screenshot(
                    path=str(output / path), animations="disabled", timeout=60000
                )
            for boundary in SCHEMES:
                target = page.locator(
                    f'[data-gallery-boundary="{boundary}"] [data-gallery-section="document"]'
                )
                expect(target).to_be_visible()
                settle(page)
                target.screenshot(
                    path=str(
                        output / f"gallery/{document_name}-{boundary}-composition.png"
                    ),
                    animations="disabled",
                    timeout=60000,
                )
        except (AssertionError, PlaywrightError) as error:
            record["issues"].append(str(error))
        finally:
            try:
                page.evaluate("window.scrollTo(0,0);document.activeElement.blur()")
                settle(page)
                path = f"gallery/{name}-full.png"
                page.screenshot(
                    path=str(output / path),
                    full_page=True,
                    animations="disabled",
                    timeout=60000,
                )
                if (
                    composition == "dashboard"
                    and width == 320
                    and scheme == "light"
                    and scale == 2
                ):
                    repeat = f"gallery/{name}-repeat.png"
                    record["screenshots"].append(repeat)
                    page.screenshot(
                        path=str(output / repeat),
                        full_page=True,
                        animations="disabled",
                        timeout=60000,
                    )
                    record["repeat_digest_identical"] = (
                        hashlib.sha256((output / path).read_bytes()).digest()
                        == hashlib.sha256((output / repeat).read_bytes()).digest()
                    )
                    assert record["repeat_digest_identical"], (
                        "same-session screenshot digests differ"
                    )
            except (AssertionError, PlaywrightError) as error:
                record["issues"].append(f"capture failed: {error}")
            record["status"] = "failed" if record["issues"] else "passed"
            if record["issues"]:
                record["failure_screenshot"] = f"gallery/{name}-full.png"
                record["trace"] = f"gallery/{name}-failure.zip"
                context.tracing.stop(path=str(output / record["trace"]))
            else:
                context.tracing.stop()
            context.close()
            document_record = {
                **record,
                "name": document_name,
                "composition": "document",
                "screenshots": [
                    path.replace(name, document_name) for path in record["screenshots"]
                ],
            }
            for source, destination in zip(
                record["screenshots"], document_record["screenshots"], strict=True
            ):
                if source.endswith("-composition.png"):
                    continue  # Captured the distinct document section above.
                if (output / source).is_file():
                    shutil.copyfile(output / source, output / destination)
            if record["issues"]:
                document_record["failure_screenshot"] = (
                    f"gallery/{document_name}-full.png"
                )
            evidence["gallery_cases"].append(document_record)
            print(f"{record['status']}: gallery {name}", flush=True)
            print(f"{document_record['status']}: gallery {document_name}", flush=True)
            for issue in record["issues"]:
                print(issue, flush=True)
