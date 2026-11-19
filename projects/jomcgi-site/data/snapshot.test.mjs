// Tests for snapshot.mjs. Run: node snapshot.test.mjs
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { r2Getter, signGet, snapshot } from "./snapshot.mjs";

const SOURCES = JSON.parse(
  readFileSync(new URL("./sources.json", import.meta.url), "utf8"),
);

test("publishes a small summary and stamps HTML with the upstream forecast timestamp", async () => {
  const root = await mkdtemp(join(tmpdir(), "stars-page-"));
  const out = join(root, "data");
  await mkdir(out);
  const page = join(root, "stars.html");
  await writeFile(
    page,
    "<main><!--stars-summary-->old<!--/stars-summary--></main>",
  );
  const body = Buffer.from(
    JSON.stringify({
      count: 8,
      fetched_at: "2026-11-17T12:00:00Z",
      sites: Array.from({ length: 8 }, (_, i) => ({
        name: `Site <${i}>`,
        lat: 55,
        lon: -4,
        clear_dark_hours: i + 1,
        best_hours: [{ secretInlineMarker: "full-data-stays-out-of-html" }],
      })),
    }),
  );
  const sources = {
    sections: { stars: { bucket: "stars", files: { sites: "sites.json" } } },
  };
  await snapshot(
    sources,
    async () => ({ body, lastModified: "Tue, 17 Nov 2026 15:07:03 GMT" }),
    out,
  );
  const html = await readFile(page, "utf8");
  assert.match(html, /8 dark-sky sites/);
  assert.match(html, /2026-11-17T12:00:00.000Z/);
  assert.equal((html.match(/<li>/g) ?? []).length, 6);
  assert.match(html, /Site &lt;0&gt;/);
  assert.doesNotMatch(html, /full-data-stays-out-of-html|Site &lt;6&gt;/);
  const latest = JSON.parse(
    await readFile(join(out, "stars/latest.json"), "utf8"),
  );
  assert.equal(latest.asOf, "2026-11-17T15:07:03.000Z");
  assert.match(
    latest.files.summary,
    /^\/data\/stars\/v\/summary-[a-f0-9]{12}\.json$/,
  );
  const summaryBody = await readFile(join(root, latest.files.summary), "utf8");
  const summary = JSON.parse(summaryBody);
  assert.equal(summary.asOf, "2026-11-17T12:00:00.000Z");
  assert.equal(summary.snapshotAsOf, latest.generatedAt);
  assert.ok(Number.isFinite(Date.parse(latest.generatedAt)));
  assert.equal(summary.rows.length, 6);
  assert.ok(summaryBody.length < 2048);
  assert.doesNotMatch(summaryBody, /full-data-stays-out-of-html/);
  const embedded = JSON.parse(
    html.match(/id="stars-summary-data">([\s\S]*?)<\/script>/)[1],
  );
  assert.deepEqual(embedded, summary);
  for (const broken of ["<main>broken build</main>", ""]) {
    await writeFile(page, broken);
    await assert.rejects(
      snapshot(sources, async () => ({ body }), out),
      /missing its summary markers/,
    );
  }
});

test("sources name the stars and hikes objects on the account's R2 endpoint", () => {
  assert.match(
    SOURCES.endpoint,
    /^https:\/\/[0-9a-f]{32}\.r2\.cloudflarestorage\.com$/,
  );
  assert.deepEqual(SOURCES.sections.stars.files, {
    sites: "sites.json",
    historyMap: "history-map.json",
    map: "map.json",
  });
  assert.deepEqual(SOURCES.sections.hikes.files, { walks: "walks.json" });
});

test("signGet matches AWS SigV4 for a fixed request", () => {
  // Same canonical request and key derivation as the AWS SigV4 spec; region
  // "auto" and service "s3" as R2 uses them.
  const headers = signGet(
    "https://abc.r2.cloudflarestorage.com/stars/sites.json",
    { accessKeyId: "AKID", secretAccessKey: "SECRET" },
    new Date("2026-11-17T12:00:00Z"),
  );
  assert.equal(headers["x-amz-date"], "20261117T120000Z");
  assert.equal(
    headers["x-amz-content-sha256"],
    createHash("sha256").update("").digest("hex"),
  );
  assert.match(
    headers.Authorization,
    /^AWS4-HMAC-SHA256 Credential=AKID\/20261117\/auto\/s3\/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$/,
  );
  // Known answer, computed by an independent SigV4 implementation (Python
  // hmac/hashlib) from the same inputs.
  assert.ok(
    headers.Authorization.endsWith(
      "Signature=9736c8622ac774838ea618508ceb4df1345f0ab86f9c2006048e409a1e7b2df2",
    ),
  );
  // Deterministic: the same inputs give the same signature.
  const again = signGet(
    "https://abc.r2.cloudflarestorage.com/stars/sites.json",
    { accessKeyId: "AKID", secretAccessKey: "SECRET" },
    new Date("2026-11-17T12:00:00Z"),
  );
  assert.equal(headers.Authorization, again.Authorization);
});

test("writes content-named files and a latest.json index per section", async () => {
  const objects = {
    "stars/sites.json": ['{"sites":1}', "Tue, 17 Nov 2026 15:07:03 GMT"],
    "stars/history-map.json": ['{"h":1}', "Tue, 17 Nov 2026 15:07:03 GMT"],
    "stars/map.json": ['{"m":1}', "Tue, 17 Nov 2026 15:07:04 GMT"],
    "stars/walks.json": ['{"walks":[]}', "Tue, 17 Nov 2026 16:20:41 GMT"],
  };
  const get = async (bucket, key) => {
    const [body, lastModified] = objects[`${bucket}/${key}`];
    return { body: Buffer.from(body), lastModified };
  };
  const out = await mkdtemp(join(tmpdir(), "snapshot-"));
  const sources = {
    sections: {
      stars: SOURCES.sections.stars,
      hikes: SOURCES.sections.hikes,
    },
  };
  const log = await snapshot(
    sources,
    get,
    out,
    new Date("2026-11-17T16:30:00Z"),
  );
  assert.equal(log.length, 4);

  const stars = JSON.parse(
    await readFile(join(out, "stars", "latest.json"), "utf8"),
  );
  assert.equal(stars.asOf, "2026-11-17T15:07:04.000Z");
  assert.equal(stars.generatedAt, "2026-11-17T16:30:00.000Z");
  const hash = createHash("sha256")
    .update('{"sites":1}')
    .digest("hex")
    .slice(0, 12);
  assert.equal(stars.files.sites, `/data/stars/v/sites-${hash}.json`);
  assert.equal(
    await readFile(join(out, "stars", "v", `sites-${hash}.json`), "utf8"),
    '{"sites":1}',
  );
  assert.deepEqual(stars.sources.walks, undefined);

  const hikes = JSON.parse(
    await readFile(join(out, "hikes", "latest.json"), "utf8"),
  );
  assert.equal(hikes.asOf, "2026-11-17T16:20:41.000Z");
  assert.equal(hikes.generatedAt, stars.generatedAt);
  assert.deepEqual(Object.keys(hikes.files), ["walks"]);
  assert.deepEqual((await readdir(join(out, "hikes", "v"))).length, 1);
  // A successful site refresh advances its own clock, even when the forecast
  // producer has not published another object. Keep the source age and hash.
  await snapshot(sources, get, out, new Date("2026-11-17T17:00:00Z"));
  const refreshed = JSON.parse(
    await readFile(join(out, "hikes", "latest.json"), "utf8"),
  );
  assert.equal(refreshed.generatedAt, "2026-11-17T17:00:00.000Z");
  assert.equal(refreshed.asOf, hikes.asOf);
  assert.deepEqual(refreshed.files, hikes.files);
});

test("a failed object read fails the snapshot", async () => {
  const fetchImpl = async () => new Response("denied", { status: 403 });
  const get = r2Getter(
    SOURCES.endpoint,
    { accessKeyId: "a", secretAccessKey: "b" },
    fetchImpl,
  );
  const out = await mkdtemp(join(tmpdir(), "snapshot-"));
  await assert.rejects(
    snapshot(SOURCES, get, out),
    /GET stars\/sites.json: 403/,
  );
});

test("a disabled section is skipped without reading its objects", async () => {
  // Trips stays disabled until the hub export commits trips/export.json
  // (HOME-452); until then every deploy would fail on the missing object.
  assert.equal(SOURCES.sections.trips.enabled, false);
  const read = [];
  const get = async (bucket, key) => {
    read.push(`${bucket}/${key}`);
    throw new Error(`unexpected GET ${bucket}/${key}`);
  };
  const out = await mkdtemp(join(tmpdir(), "snapshot-"));
  const log = await snapshot(
    { sections: { trips: SOURCES.sections.trips } },
    get,
    out,
  );
  assert.deepEqual(read, []);
  assert.deepEqual(log, ["trips: disabled, not snapshotted"]);
});

test("r2Getter signs each request and returns the body and Last-Modified", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push([url, init.headers.Authorization]);
    return new Response("{}", {
      status: 200,
      headers: { "last-modified": "Tue, 17 Nov 2026 15:07:03 GMT" },
    });
  };
  const get = r2Getter(
    SOURCES.endpoint,
    { accessKeyId: "a", secretAccessKey: "b" },
    fetchImpl,
  );
  const { body, lastModified } = await get("stars", "sites.json");
  assert.equal(body.toString(), "{}");
  assert.equal(lastModified, "Tue, 17 Nov 2026 15:07:03 GMT");
  assert.equal(seen[0][0], `${SOURCES.endpoint}/stars/sites.json`);
  assert.match(seen[0][1], /^AWS4-HMAC-SHA256 Credential=a\//);
});

test("public manifest copies verified objects and fails on a missing object", async () => {
  const url = "https://factory-data.jomcgi.dev/manifest.json";
  const sources = { sections: {}, httpsManifests: { factory: url } };
  const object = '{"kind":"story"}';
  const path = `objects/${createHash("sha256").update(object).digest("hex")}.json`;
  const manifest = JSON.stringify({
    generatedAt: "2026-11-18T09:00:00Z",
    version: 1,
    story: path,
    goals: {},
    issues: {},
    sessions: {},
  });
  const out = await mkdtemp(join(tmpdir(), "snapshot-"));
  let missing = false;
  const fetchImpl = async (location, options) => {
    assert.equal(options.credentials, "omit");
    if (location === url) return new Response(manifest);
    assert.equal(location, new URL(path, url).href);
    return new Response(object, { status: missing ? 404 : 200 });
  };
  await snapshot(sources, null, out, undefined, fetchImpl);
  assert.equal(await readFile(join(out, "factory", path), "utf8"), object);
  assert.equal(
    await readFile(join(out, "factory", "manifest.json"), "utf8"),
    manifest,
  );
  missing = true;
  await assert.rejects(
    snapshot(sources, null, out, undefined, fetchImpl),
    /404/,
  );
});

test("rejects a manifest without generatedAt before reading any object", async () => {
  const url = "https://factory-data.jomcgi.dev/manifest.json";
  const manifest = {
    version: 1,
    story: `objects/${"a".repeat(64)}.json`,
    goals: {},
    issues: {},
    sessions: {},
  };
  await assert.rejects(
    snapshot(
      { sections: {}, httpsManifests: { factory: url } },
      null,
      await mkdtemp(join(tmpdir(), "snapshot-")),
      undefined,
      async (location) => {
        assert.equal(location, url);
        return Response.json(manifest);
      },
    ),
    /Invalid storyboard manifest/,
  );
});
