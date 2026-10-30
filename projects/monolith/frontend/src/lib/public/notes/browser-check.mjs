// Advisory browser evidence against the actual SvelteKit /app/notes route.
// Start the frontend dev server, install Playwright outside the checkout, then
// pass NOTES_PLAYWRIGHT_MODULE if it is not in the module search path.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { graphFixture, noteFixture } from "./graph-fixture.js";

const { chromium } = await import(
  process.env.NOTES_PLAYWRIGHT_MODULE || "playwright"
);
const origin = process.env.NOTES_PREVIEW_ORIGIN || "http://127.0.0.1:5173";
const output = process.argv[2];
assert(output, "Pass a transient evidence directory");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.NOTES_CHROMIUM_EXECUTABLE,
  args: ["--no-sandbox"],
});
const results = [];

try {
  for (const width of [390, 1440]) {
    const context = await browser.newContext({
      viewport: { width, height: width === 390 ? 844 : 1000 },
      colorScheme: "light",
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requests = [];
    await page.route("**/*", (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      if (url.pathname === "/app/notes/graph") {
        requests.push(url.pathname);
        return route.fulfill({ json: graphFixture });
      }
      if (url.pathname.startsWith("/app/notes/body/")) {
        requests.push(url.pathname);
        return route.fulfill({ json: noteFixture });
      }
      if (url.pathname.startsWith("/otel/")) return route.fulfill({ json: {} });
      return route.continue();
    });
    await page.addInitScript(() => {
      localStorage.setItem("td-theme", "light");
      window.notesPaints = [];
      for (const method of [
        "fill",
        "stroke",
        "fillRect",
        "strokeRect",
        "fillText",
        "arc",
      ]) {
        const original = CanvasRenderingContext2D.prototype[method];
        CanvasRenderingContext2D.prototype[method] = function (...args) {
          const t = this.getTransform();
          window.notesPaints.push({
            method,
            args,
            fill: this.fillStyle,
            stroke: this.strokeStyle,
            alpha: this.globalAlpha,
            transform: { a: t.a, d: t.d, e: t.e, f: t.f },
          });
          return original.apply(this, args);
        };
      }
    });
    await page.goto(`${origin}/app/notes?focus=sheet-note`);
    await page.locator(".panel-body pre").waitFor();
    await page.locator("canvas").waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() =>
      window.notesPaints.some((p) => p.method === "fillText"),
    );
    await page.waitForFunction(
      () => !document.querySelector("canvas").__transition,
    );
    await page.evaluate(() => {
      window.notesCanvas = document.querySelector("canvas");
    });
    const snapshot = () =>
      page.evaluate(() => {
        const canvas = document.querySelector("canvas");
        const stage = canvas.parentElement;
        const ctx = canvas.getContext("2d");
        const resolve = (value) => {
          const probe = document.createElement("span");
          probe.style.color = value;
          stage.append(probe);
          const color = getComputedStyle(probe).color;
          probe.remove();
          ctx.fillStyle = color;
          return ctx.fillStyle;
        };
        return {
          scheme: document.documentElement.dataset.theme,
          viewport: innerWidth,
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
          transform: {
            x: canvas.__zoom.x,
            y: canvas.__zoom.y,
            k: canvas.__zoom.k,
          },
          sameCanvas: canvas === window.notesCanvas,
          title: document.querySelector(".panel-title")?.textContent,
          ink: resolve("var(--ink)"),
          sheet: resolve("var(--sheet)"),
          selected: resolve("var(--accent-ink)"),
          atom: resolve("var(--warn)"),
          background: getComputedStyle(stage).backgroundColor,
          decoration: getComputedStyle(stage, "::before").backgroundImage,
          panelWidth: document.querySelector(".panel").getBoundingClientRect()
            .width,
          panelShadow: getComputedStyle(document.querySelector(".panel"))
            .boxShadow,
          paints: window.notesPaints,
        };
      });
    const day = await snapshot();
    assert.equal(day.decoration, "none");
    assert.equal(day.panelShadow, "none");
    if (width === 390)
      assert(day.panelWidth > 300, "phone panel must use the sheet width");
    assert(day.scrollWidth <= day.clientWidth, `${width}: day page overflow`);
    await page.screenshot({
      path: `${output}/notes-${width}-light.png`,
      fullPage: true,
    });
    await page.evaluate(() => {
      window.notesPaints = [];
    });
    await page.getByRole("button", { name: "Switch to night scheme" }).click();
    await page.waitForFunction(
      (previous) =>
        document.documentElement.dataset.theme === "dark" &&
        getComputedStyle(document.querySelector(".stage")).backgroundColor !==
          previous.background &&
        window.notesPaints.some(
          (p) => p.method === "fillText" && p.fill !== previous.ink,
        ),
      { background: day.background, ink: day.ink },
    );
    const night = await snapshot();
    assert.notEqual(night.ink, day.ink);
    assert.notEqual(night.background, day.background);
    assert.notEqual(night.atom, day.atom);
    assert(night.sameCanvas);
    assert.deepEqual(night.transform, day.transform);
    assert.equal(night.title, day.title);
    assert(
      night.scrollWidth <= night.clientWidth,
      `${width}: night page overflow`,
    );
    for (const [method, property, color] of [
      ["fillText", "fill", night.ink],
      ["fillRect", "fill", night.sheet],
      ["strokeRect", "stroke", night.selected],
      ["fill", "fill", night.atom],
      ["stroke", "stroke", night.ink],
    ]) {
      assert(
        night.paints.some((p) => p.method === method && p[property] === color),
        `${width}: ${method} must repaint using ${color}`,
      );
    }
    assert.equal(requests.filter((r) => r === "/app/notes/graph").length, 1);
    assert.equal(
      requests.filter((r) => r === "/app/notes/body/sheet-note").length,
      1,
    );
    await page.screenshot({
      path: `${output}/notes-${width}-dark.png`,
      fullPage: true,
    });
    await page.getByRole("button", { name: "close", exact: true }).click();
    assert.equal(await page.locator(".panel").count(), 0);
    await page.keyboard.press("/");
    assert.equal(
      await page
        .locator("#graph-search-input")
        .evaluate((el) => el === document.activeElement),
      true,
    );
    const focus = await page.locator("#graph-search-input").evaluate((el) => ({
      style: getComputedStyle(el).outlineStyle,
      width: getComputedStyle(el).outlineWidth,
    }));
    assert.notEqual(focus.style, "none");
    assert.notEqual(focus.width, "0px");
    await page.locator("#graph-search-input").fill("Scheme");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#graph-search-input").inputValue(), "");
    const filter = page.locator(".legend-row").first();
    await filter.focus();
    await page.keyboard.press("Space");
    assert.equal(await filter.getAttribute("aria-pressed"), "false");
    await page.keyboard.press("Space");
    assert.equal(await filter.getAttribute("aria-pressed"), "true");
    const canvas = page.locator("canvas");
    const bounds = await canvas.boundingBox();
    const beforeZoom = await canvas.evaluate((el) => el.__zoom.k);
    await page.mouse.move(
      bounds.x + bounds.width - 20,
      bounds.y + bounds.height - 20,
    );
    await page.mouse.wheel(0, -100);
    await page.waitForFunction(
      (k) => document.querySelector("canvas").__zoom.k !== k,
      beforeZoom,
    );
    const point = await page.evaluate(() => {
      const canvas = document.querySelector("canvas");
      const bounds = canvas.getBoundingClientRect();
      const arcs = window.notesPaints
        .filter((p) => p.method === "arc")
        .slice(-7);
      for (const arc of arcs) {
        const x = bounds.x + arc.args[0] * canvas.__zoom.k + canvas.__zoom.x;
        const y = bounds.y + arc.args[1] * canvas.__zoom.k + canvas.__zoom.y;
        if (document.elementFromPoint(x, y) === canvas) return { x, y };
      }
      return null;
    });
    assert(point, "at least one node remains pointer-accessible");
    await page.mouse.move(point.x, point.y);
    await page.waitForFunction(
      () =>
        document.querySelector(".graph-hover").textContent.trim() !==
        "hover a node",
    );
    const hovered = (await page.locator(".graph-hover").textContent()).trim();
    // Focus the toggle by keyboard without moving the pointer off the node.
    await page.locator(".scheme").focus();
    await page.evaluate(() => {
      window.notesPaints = [];
    });
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      ({ title, ink }) =>
        window.notesPaints.some(
          (p) =>
            p.method === "fillText" && p.args[0] === title && p.fill === ink,
        ),
      { title: hovered, ink: day.ink },
    );
    await page.mouse.click(point.x, point.y);
    await page.locator(".panel").waitFor();
    assert.equal(
      (await page.locator(".panel-title").textContent()).trim(),
      hovered,
    );
    await page.waitForFunction(
      () => !document.querySelector("canvas").__transition,
    );
    // With no explicit preference, an OS scheme change also repaints the
    // existing graph. Allow the attribute observer's frame to finish first.
    await page.evaluate(async () => {
      delete document.documentElement.dataset.theme;
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      window.notesPaints = [];
    });
    await page.emulateMedia({ colorScheme: "dark" });
    await page.waitForFunction(
      (ink) =>
        window.notesPaints.some(
          (p) => p.method === "fillText" && p.fill === ink,
        ),
      night.ink,
    );
    assert.equal(
      await canvas.evaluate((el) => el === window.notesCanvas),
      true,
    );
    assert.equal(errors.length, 0, errors.join("\n"));
    results.push({
      width,
      light: { ...day, paints: undefined },
      dark: { ...night, paints: undefined },
      requests,
      keyboardFocus: focus,
      errors,
    });
    await context.close();
  }
  await writeFile(`${output}/checks.json`, JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
} finally {
  await browser.close();
}
