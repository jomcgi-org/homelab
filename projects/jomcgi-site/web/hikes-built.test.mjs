// Test the artifact after the same data finalization that run.sh deploys.
// Usage (after pnpm build): node hikes-built.test.mjs dist
import assert from "node:assert/strict";
import { cp, mkdtemp, readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { Window } from "happy-dom";
import { snapshot } from "../data/snapshot.mjs";
import { publishHikesSummary } from "../data/hikes-page.mjs";
import { BUDGET_BYTES, initialScripts, measure } from "./budget.mjs";

const dist = resolve(process.argv[2] ?? "dist");
test("built hikes page has its metadata and defers the map and forecast", async () => {
  const html = await readFile(join(dist, "hikes.html"), "utf8");
  const window = new Window({
    settings: { enableJavaScriptEvaluation: false },
  });
  const document = window.document;
  document.write(html);
  const title = "Hike planner, Scotland walks by weather window";
  const description =
    "A map of Scottish hill walks coloured by effort and filtered by viable weather windows from the met.no forecast.";
  const url = "https://jomcgi.dev/hikes";
  assert.equal(document.title, title);
  assert.equal(document.querySelector('link[rel="canonical"]').href, url);
  for (const [key, value] of Object.entries({
    description,
    "og:title": title,
    "twitter:title": title,
    "og:description": description,
    "twitter:description": description,
    "og:url": url,
    "og:type": "website",
    "og:site_name": "Joe McGinley",
    "twitter:card": "summary",
  })) {
    const meta = document.querySelector(
      'meta[property="' + key + '"], meta[name="' + key + '"]',
    );
    assert.equal(meta?.content, value, key);
  }
  assert.equal(document.querySelectorAll("h1").length, 1);
  assert.ok(document.querySelector("#hikes-published"));
  assert.equal(document.querySelectorAll("[style]").length, 0);
  assert.match(document.body.textContent, /Explore walks/);
  assert.match(document.body.textContent, /MET Norway.*CC BY 4.0.*Copernicus/);
  const { files, inline } = initialScripts(html);
  assert.deepEqual(inline, []);
  assert.ok(files.every((file) => !/map-|planner-|worker/.test(file)));
  assert.doesNotMatch(html, /href="[^"]*map-[^"]*\.css/);
  assert.ok(
    measure(dist).find((page) => page.page === "hikes.html").bytes <=
      BUDGET_BYTES,
  );
  await window.happyDOM.close();
});

test("deployed HTML shows the snapshot timestamp and walk facts without JavaScript", async () => {
  const dir = await mkdtemp(join(tmpdir(), "hikes-built-"));
  await cp(dist, dir, { recursive: true });
  const out = join(dir, "data");
  await snapshot(
    {
      sections: { hikes: { bucket: "stars", files: { walks: "walks.json" } } },
    },
    async () => ({
      lastModified: "Tue, 17 Nov 2026 12:00:00 GMT",
      body: Buffer.from(
        JSON.stringify({
          schema_version: 2,
          fetched_at: "2026-11-15T12:00:00Z",
          walks: [
            {
              id: "ben",
              name: "Ben Vorlich from Ardlui",
              url: "https://www.walkhighlands.co.uk/lochlomond/ben-vorlich-ardlui.shtml",
              duration_h: 5.5,
              forecast_fetched_at: "2026-11-15T12:00:00Z",
              distance_km: 11,
              ascent_m: 920,
              hours: [],
              summary: "Excluded prose",
            },
          ],
        }),
      ),
    }),
    out,
    new Date("2026-11-17T12:00:00Z"),
  );
  await publishHikesSummary(out, new Date("2026-11-17T14:00:00Z"));
  const window = new Window({
    settings: { enableJavaScriptEvaluation: false },
  });
  window.document.write(await readFile(join(dir, "hikes.html"), "utf8"));
  const summary = window.document.querySelector("#hikes-published");
  assert.equal(
    summary.querySelector("time").dateTime,
    "2026-11-17T12:00:00.000Z",
  );
  assert.match(
    summary.textContent,
    /1 Scottish walks.*Ben Vorlich from Ardlui.*11 km.*920 m ascent.*5.5 h/s,
  );
  assert.match(
    summary.textContent,
    /Stale forecast · retained weather windows/,
  );
  assert.match(
    summary.textContent,
    /Stale site snapshot · published data retained/,
  );
  assert.doesNotMatch(summary.textContent, /Excluded prose|unavailable/);
  assert.match(
    await readFile(join(dir, "_redirects"), "utf8"),
    /\/app\/hikes \/hikes 301\n\/app\/hikes\/\* \/hikes 301/,
  );
  await window.happyDOM.close();
});
