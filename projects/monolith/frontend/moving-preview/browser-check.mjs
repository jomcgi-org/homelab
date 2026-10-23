import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile, readdir, mkdir, writeFile } from "node:fs/promises";
import { resolve, extname } from "node:path";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { movingState, clock } from "../test/moving-fixtures.js";

// node browser-check.mjs <playwright/index.mjs> <built-dir> <evidence-dir> <sha> [--baseline]
const [driver, artifact, destination, expectedSha, mode] =
  process.argv.slice(2);
const { chromium } = await import(pathToFileURL(resolve(driver)).href);
const output = resolve(destination);
const baseline = mode === "--baseline";
await mkdir(output, { recursive: true });
const stamp = JSON.parse(
  await readFile(resolve(artifact, "build.json"), "utf8"),
);
assert.equal(
  stamp.commit,
  expectedSha,
  "Built artifact must match the reviewed commit",
);
const assets = new Map();
const hashes = {};
async function collect(directory, prefix = "") {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const name = `${prefix}${entry.name}`;
    if (entry.isDirectory())
      await collect(resolve(directory, entry.name), `${name}/`);
    else {
      const bytes = await readFile(resolve(directory, entry.name));
      assets.set(`/${name}`, bytes);
      hashes[name] = createHash("sha256").update(bytes).digest("hex");
    }
  }
}
await collect(resolve(artifact));
const unexpected = [],
  requests = [],
  evidence = [];
let aborts = 0;
let failTask = true;
const mime = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".ttf": "font/ttf",
};
const fontStylesheets = new Set([
  "https://fonts.googleapis.com/css2?family=Hanken+Grotesk:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600;700&display=swap",
  "https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&display=swap",
]);
const frame = (type, data) => `data: ${JSON.stringify({ type, data })}\n\n`;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  const key = url.pathname === "/moving" ? "/index.html" : url.pathname;
  if (req.method === "GET" && assets.has(key)) {
    res.writeHead(200, {
      "content-type": mime[extname(key)] ?? "application/octet-stream",
    });
    res.end(assets.get(key));
    return;
  }
  let raw = "";
  for await (const chunk of req) raw += chunk;
  const body = raw ? JSON.parse(raw) : {};
  requests.push({ path: url.pathname, method: req.method, body });
  const json = (status, value) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(status === 204 ? undefined : JSON.stringify(value));
  };
  if (req.method === "POST" && url.pathname === "/api/moving/chat") {
    if (body.message.startsWith("Error")) {
      json(503, { detail: "Synthetic chat unavailable. Please try again." });
      return;
    }
    res.writeHead(200, { "content-type": "text/event-stream" });
    res.write(frame("token", { text: "Pack the labelled boxes. " }));
    if (body.message.startsWith("Abort")) {
      res.on("close", () => {
        aborts += 1;
      });
    } else {
      setTimeout(
        () =>
          res.end(
            frame("token", { text: "Check the collection date." }) +
              frame("done", {}),
          ),
        20,
      );
    }
    return;
  }
  if (
    req.method === "POST" &&
    /^\/api\/moving\/tasks\/task-pack\/(done|undone)$/.test(url.pathname)
  ) {
    const failed = failTask;
    failTask = false;
    setTimeout(() => json(failed ? 503 : 200, {}), 150);
    return;
  }
  if (
    req.method === "PATCH" &&
    url.pathname === "/api/moving/tasks/task-pack"
  ) {
    json(200, { ...movingState().tasks[0], ...body });
    return;
  }
  if (
    req.method === "DELETE" &&
    url.pathname === "/api/moving/tasks/task-pack"
  ) {
    json(204);
    return;
  }
  if (req.method === "POST" && url.pathname === "/api/moving/tasks") {
    json(201, {
      ...movingState().tasks[0],
      ...body,
      id: "synthetic-restored-task",
      done_at: null,
    });
    return;
  }
  unexpected.push(`${req.method} ${url.pathname}`);
  json(500, { detail: "Unexpected synthetic endpoint" });
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  for (const width of [390, 1440])
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme: scheme,
        reducedMotion: "reduce",
      });
      await context.route("**/*", (route) => {
        const url = new URL(route.request().url());
        // Real friends layout font declarations are intercepted without egress.
        if (fontStylesheets.has(url.href))
          return route.fulfill({ contentType: "text/css", body: "" });
        if (
          url.origin === base &&
          (assets.has(url.pathname) ||
            url.pathname === "/moving" ||
            url.pathname.startsWith("/api/moving/"))
        )
          return route.continue();
        unexpected.push(route.request().url());
        return route.abort("blockedbyclient");
      });
      await context.tracing.start({ screenshots: true, snapshots: true });
      const page = await context.newPage();
      page.setDefaultTimeout(8000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.clock.setFixedTime(new Date(clock));
      const inspect = () =>
        page.evaluate(() => {
          const styles = {};
          for (const selector of [
            "body",
            "main",
            ".c-today",
            ".cta-body",
            ".agenda-body",
            ".card-plot",
            ".plot",
            "dialog[open]",
            "dialog[open] .dlg-body",
            ".chat-messages",
          ]) {
            const el = document.querySelector(selector);
            if (!el) continue;
            const css = getComputedStyle(el),
              box = el.getBoundingClientRect();
            styles[selector] = {
              overflowX: css.overflowX,
              overflowY: css.overflowY,
              display: css.display,
              width: box.width,
              height: box.height,
              top: box.top,
              bottom: box.bottom,
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
              background: css.backgroundColor,
              color: css.color,
            };
          }
          return {
            viewport: { width: innerWidth, height: innerHeight },
            documentWidth: document.documentElement.scrollWidth,
            styles,
          };
        });
      const capture = async (name) => {
        await page.evaluate(() => document.fonts.ready);
        // Dynamic viewport units settle on the rendering frame after resize.
        // Record the same settled layout that the screenshot will paint.
        await page.evaluate(
          () =>
            new Promise((done) =>
              requestAnimationFrame(() => requestAnimationFrame(done)),
            ),
        );
        const layout = await inspect();
        if (!baseline) {
          assert.ok(
            layout.documentWidth <= layout.viewport.width + 1,
            `${name}: page overflow ${JSON.stringify(layout)}`,
          );
          assert.equal(
            layout.styles.body.overflowY,
            "auto",
            `${name}: body scrolling`,
          );
          assert.equal(
            layout.styles.main.overflowX,
            "visible",
            `${name}: page overflow contract`,
          );
          assert.equal(
            layout.styles.main.overflowY,
            "visible",
            `${name}: page scrolling contract`,
          );
          assert.equal(await page.locator(".md-nav").count(), 0);
          assert.equal(await page.locator("main.td.sheet").count(), 1);
        }
        const filename = `${name}-${scheme}-${width}.png`;
        await page.screenshot({
          path: `${output}/${filename}`,
          fullPage: true,
        });
        evidence.push({
          name,
          width,
          scheme,
          screenshot: filename,
          commit: stamp.commit,
          layout,
        });
        console.log(`Captured ${filename}`);
        return layout;
      };
      try {
        for (const [query, name] of [
          ["", "dashboard"],
          ["?mode=manage", "manage"],
          ["?fixture=forbidden", "forbidden"],
          ["?fixture=unavailable", "unavailable"],
          ["?fixture=empty", "empty"],
        ]) {
          await page.goto(`${base}/moving${query}`, {
            waitUntil: "networkidle",
          });
          await capture(name);
          if (!baseline) {
            const toggle = page.locator("button.scheme");
            await toggle.focus();
            assert.notEqual(
              await toggle.evaluate((el) => getComputedStyle(el).outlineStyle),
              "none",
            );
            const paint = await page
              .locator("main")
              .evaluate((el) => getComputedStyle(el).backgroundColor);
            await page.keyboard.press("Enter");
            await page.waitForFunction(
              (previous) =>
                getComputedStyle(document.querySelector("main"))
                  .backgroundColor !== previous,
              paint,
            );
            await page.keyboard.press("Enter");
            await page.waitForFunction(
              (previous) =>
                getComputedStyle(document.querySelector("main"))
                  .backgroundColor === previous,
              paint,
            );
            assert.equal(
              await page.evaluate(() => localStorage.getItem("td-theme")),
              scheme,
            );
          }
        }
        await page.goto(`${base}/moving`, { waitUntil: "networkidle" });
        if (width === 390) {
          for (const tab of ["Plan", "More", "Today"]) {
            await page
              .getByRole("navigation", { name: "Sections" })
              .getByRole("button", { name: new RegExp(`^${tab}`) })
              .click();
            await capture(`phone-${tab.toLowerCase()}`);
          }
          await page.getByRole("button", { name: "More", exact: true }).click();
        }
        const dock = page.getByRole("navigation", { name: "Details" });
        for (const [label, title] of [
          ["Tasks", "What's left"],
          ["To sell", "To sell"],
          ["Calendar", "Shared calendar"],
          ["Roles", "Roles"],
          ["Ask", "Ask"],
        ]) {
          const trigger = dock.getByRole("button", {
            name: new RegExp(`^${label}`),
          });
          await trigger.click();
          const dialog = page.locator("dialog[open]");
          assert.equal(await dialog.count(), 1);
          assert.equal(
            await dialog.evaluate((el) => el.contains(document.activeElement)),
            true,
          );
          if (!baseline) {
            const layout = await inspect();
            assert.equal(layout.styles["dialog[open]"].overflowY, "hidden");
            assert.equal(
              layout.styles["dialog[open] .dlg-body"].overflowY,
              label === "Ask" ? "hidden" : "auto",
            );
            const close = dialog.getByRole("button", {
              name: "Close",
              exact: true,
            });
            await page.keyboard.press("Tab");
            await close.focus();
            assert.notEqual(
              await close.evaluate((el) => getComputedStyle(el).outlineStyle),
              "none",
            );
          }
          await capture(`dialog-${label.toLowerCase().replaceAll(" ", "-")}`);
          await page.keyboard.press("Escape");
          await page.waitForFunction(
            () => !document.querySelector("dialog[open]"),
          );
          assert.equal(
            await trigger.evaluate((el) => document.activeElement === el),
            true,
            `${title} focus restoration`,
          );
        }
        if (baseline) {
          assert.deepEqual(errors, []);
          continue;
        }

        // Task optimistic state, pending protection, rollback, then retry.
        await dock.getByRole("button", { name: /^Tasks/ }).click();
        failTask = true;
        const task = page.locator("#task-task-pack");
        await task.check();
        assert.equal(await task.isDisabled(), true);
        await page.locator('dialog[open] [role="alert"]').waitFor();
        assert.equal(await task.isChecked(), false);
        await capture("task-error");
        await task.check();
        await page.waitForFunction(
          () => !document.querySelector("#task-task-pack").disabled,
        );
        assert.equal(await task.isChecked(), true);
        await page.keyboard.press("Escape");

        // Real streaming over the isolated HTTP server, including close abort.
        await dock.getByRole("button", { name: /^Ask/ }).click();
        const input = page.getByRole("textbox", { name: "Ask about the plan" });
        const send = async (message) => {
          await input.fill(message);
          await page.getByRole("button", { name: "Send", exact: true }).click();
        };
        await send("What is next?");
        await page.waitForFunction(() =>
          document
            .querySelector(".chat-status")
            .textContent.includes("complete"),
        );
        await capture("chat-complete");
        await send("Error please");
        await page.locator(".chat-error").waitFor();
        const chatRequests = requests.filter(
          (item) => item.path === "/api/moving/chat",
        );
        assert.equal(chatRequests.at(-1).body.history.length, 2);
        await capture("chat-error");
        const abortsBefore = aborts;
        await send("Abort this stream");
        await page.waitForFunction(
          () => document.querySelectorAll(".chat-message").length >= 5,
        );
        await page.getByRole("button", { name: "Close", exact: true }).click();
        await dock.getByRole("button", { name: /^Ask/ }).click();
        await page.waitForFunction(
          () => !document.querySelector("#moving-chat-message").disabled,
        );
        assert.ok(
          aborts > abortsBefore,
          "Dialog close must abort the HTTP stream",
        );
        await capture("chat-aborted");

        // A shorter mobile viewport catches clipped dialog controls even when
        // scrollWidth is fine. Resizing back must also restore desktop panels.
        await page.setViewportSize({ width: 390, height: 520 });
        await page.waitForFunction(() => document.querySelector(".app.phone"));
        const short = await capture("resized-chat");
        assert.ok(
          short.styles["dialog[open]"].top >= 0 &&
            short.styles["dialog[open]"].bottom <= 520,
        );
        await input.fill("Short viewport control");
        assert.equal(
          await page
            .getByRole("button", { name: "Send", exact: true })
            .isVisible(),
          true,
        );
        await page.keyboard.press("Escape");
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.waitForFunction(() => !document.querySelector(".app.phone"));
        assert.equal(await page.locator(".card-plot").isVisible(), true);
        const desktop = await inspect();
        assert.equal(desktop.styles[".agenda-body"].overflowY, "auto");
        assert.equal(
          await page
            .locator(".agenda-body")
            .evaluate((el) => getComputedStyle(el).contain),
          "size",
        );
        assert.equal(desktop.styles[".cta-body"].overflowY, "auto");
        assert.equal(desktop.styles[".card-plot"].overflowX, "auto");
        assert.equal(
          await page
            .locator(".row.slim .track")
            .evaluate((el) => el.getBoundingClientRect().height),
          34,
        );
        assert.equal(
          await page
            .locator(".plot")
            .evaluate((el) =>
              getComputedStyle(el).getPropertyValue("--pad-btm").trim(),
            ),
          "46px",
        );
        await page.setViewportSize({ width, height: 1000 });

        await page.goto(`${base}/moving?keep=synthetic`, {
          waitUntil: "networkidle",
        });
        await page.getByRole("button", { name: "All", exact: true }).click();
        await page.waitForURL(/scope=all/);
        assert.equal(new URL(page.url()).searchParams.get("keep"), "synthetic");
        await page.getByRole("button", { name: "Manage", exact: true }).click();
        await page.waitForURL(/mode=manage/);
        const row = page
          .locator(".tasks-manage .manage-row")
          .filter({ hasText: "Pack the kitchen" });
        await row.getByRole("button", { name: "Edit", exact: true }).click();
        await page
          .locator('.tasks-manage .edit-row input[name="title"]')
          .fill("Updated synthetic packing task with a long label");
        await capture("manage-edit");
        await page.getByRole("button", { name: "Save", exact: true }).click();
        const updated = page
          .locator(".tasks-manage .manage-row")
          .filter({ hasText: "Updated synthetic packing task" });
        await updated.waitFor();
        assert.ok(
          (
            await page.locator('label[for="task-task-pack"]').textContent()
          ).includes("Updated synthetic"),
        );
        await updated
          .getByRole("button", { name: "Delete", exact: true })
          .click();
        await page.getByRole("button", { name: "Undo", exact: true }).waitFor();
        await capture("manage-undo");
        await page.getByRole("button", { name: "Undo", exact: true }).click();
        await page
          .locator(".tasks-manage .manage-row")
          .filter({ hasText: "Updated synthetic packing task" })
          .waitFor();
        await page
          .getByRole("button", { name: "Back to plan", exact: true })
          .click();
        await page.waitForURL((url) => !url.searchParams.has("mode"));
        assert.equal(new URL(page.url()).searchParams.get("scope"), "all");
        assert.deepEqual(errors, []);
        assert.deepEqual(unexpected, []);
      } catch (error) {
        await page.screenshot({
          path: `${output}/failure-${scheme}-${width}.png`,
          fullPage: true,
        });
        evidence.push({
          error: error.stack,
          url: page.url(),
          layout: await inspect(),
        });
        throw error;
      } finally {
        await context.tracing.stop({
          path: `${output}/trace-${scheme}-${width}.zip`,
        });
        await context.close();
      }
    }
  assert.deepEqual(unexpected, []);
} finally {
  if (browser) await browser.close();
  server.closeAllConnections();
  await new Promise((done) => server.close(done));
  await writeFile(
    `${output}/checks.json`,
    JSON.stringify(
      { stamp, baseline, hashes, evidence, requests, aborts, unexpected },
      null,
      2,
    ) + "\n",
  );
}
console.log(`PASS: ${evidence.length} captures at ${stamp.commit}`);
