import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { snapshot } from "./snapshot.mjs";
import { publishHikesSummary } from "./hikes-page.mjs";

const fetched = "2026-11-17T12:00:00Z";
const walk = {
  name: 'Ben <script>alert("x")</script>',
  url: "https://www.walkhighlands.co.uk/ben.shtml",
  duration_h: 5,
  distance_km: 10,
  ascent_m: 800,
  forecast_fetched_at: fetched,
  summary: "licensed prose must not appear",
  hours: [[1794902400, 12, 0, 20, 30, null, null]],
};
async function page(walks, generatedAt = fetched) {
  const dir = await mkdtemp(join(tmpdir(), "hikes-page-"));
  const out = join(dir, "data");
  const file = join(dir, "hikes.html");
  await writeFile(
    file,
    '<main><div id="hikes-published"><!--hikes-summary:start-->placeholder<!--hikes-summary:end--></div></main>',
  );
  await snapshot(
    {
      sections: { hikes: { bucket: "stars", files: { walks: "walks.json" } } },
    },
    async () => ({
      lastModified: new Date(fetched).toUTCString(),
      body: Buffer.from(
        JSON.stringify({ schema_version: 2, fetched_at: fetched, walks }),
      ),
    }),
    out,
    new Date(generatedAt),
  );
  return {
    file,
    render: async (time) => {
      await publishHikesSummary(out, new Date(time));
      return readFile(file, "utf8");
    },
  };
}
test("deploy snapshot fills the no-JS summary, escapes facts and omits prose/hours", async () => {
  const { file, render } = await page([
    walk,
    { ...walk, name: "Zero duration", duration_h: 0 },
    { ...walk, name: "Unsafe URL", url: "javascript:alert(1)" },
  ]);
  let html = await render("2026-11-17T12:30:00Z");
  assert.match(html, /1 Scottish walks/);
  assert.match(html, /<time datetime="2026-11-17T12:00:00.000Z">/);
  assert.match(
    html,
    /Ben &lt;script&gt;alert\(&quot;x&quot;\)&lt;\/script&gt;/,
  );
  assert.match(html, /10 km · 800 m ascent · 5 h/);
  assert.doesNotMatch(
    html,
    /<script|licensed prose|1794902400|Zero duration|Unsafe URL|unavailable|Stale/,
  );
  assert.doesNotMatch(await render("2026-11-17T13:29:59.999Z"), /Stale/);
  html = await render("2026-11-17T13:30:00Z");
  assert.match(html, /Stale site snapshot · published data retained/);
  assert.doesNotMatch(html, /Stale forecast/);
  html = await render("2026-11-17T18:30:00Z");
  assert.match(html, /Stale forecast · retained weather windows/);
  assert.match(html, /Ben &lt;script/);
  await writeFile(file, "missing slot");
  await assert.rejects(render(fetched), /must have one published summary slot/);
});
test("new publication keeps partial fetch failures visible in the no-JS summary", async () => {
  const { render } = await page(
    [
      ["Current", fetched],
      ["Retained", "2026-11-17T10:00:00Z"],
      ["Old", "2026-11-15T12:00:00Z"],
      ["Unknown", null],
    ].map(([name, at]) => ({ ...walk, name, forecast_fetched_at: at })),
    "2026-11-17T12:30:00Z",
  );
  const html = await render("2026-11-17T12:30:00Z");
  for (const pattern of [
    /Latest forecast fetch: <time datetime="2026-11-17T12:00:00.000Z"/,
    /Oldest walk forecast: <time datetime="2026-11-15T12:00:00.000Z"/,
    /1 walks have unknown forecast age/,
    /Old<\/a>[^]*?Stale forecast/,
    /Retained<\/a>[^]*?Earlier forecast retained/,
    /Unknown<\/a>[^]*?Forecast age unknown/,
    /Site snapshot: <time datetime="2026-11-17T12:30:00.000Z"/,
  ])
    assert.match(html, pattern);
  assert.doesNotMatch(html, /Stale site snapshot|unavailable/);
});
