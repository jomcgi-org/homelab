// Runs the real deploy-time R2 snapshot against the built site, offline.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { cp, mkdtemp, readFile, access } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { snapshotTrips, renderTrips, validateExport } from "../data/trips.mjs";
import { measure, BUDGET_BYTES } from "./budget.mjs";

const source = resolve(process.argv[2] ?? "dist");
const image = "img_aaaaaaaaaaaa.jpg";
const trip = {
  slug: "2025-trip",
  title: "A trip <script> & journey",
  short_title: "Journey",
  subtitle: "Along the road",
  tz: "America/Vancouver",
  default_image: image,
  days: { 1: { label: "To the mountains" } },
  highlights: [],
  stats: {},
};
const index = { count: 1, trips: [trip] };
const payload = {
  trip,
  points: [1, 2].map((day) => ({
    id: String(day),
    taken_at: `2025-07-0${day}T12:00:00Z`,
    lat: 58,
    lng: -125,
    image,
    tags: [],
    elevation: day * 100,
  })),
};
const objects = new Map();
const files = {};
for (const [name, value] of [
  ["index", index],
  [trip.slug, payload],
]) {
  const body = Buffer.from(JSON.stringify(value));
  const hash = createHash("sha256").update(body).digest("hex");
  const key = `trips/v/${name}-${hash}.json`;
  objects.set(key, body);
  files[name] = key;
}
const manifest = {
  asOf: "2026-11-17T00:00:00Z",
  refreshSeconds: 86400,
  photoMode: "snapshot",
  photoBase: "/data/trips/photos",
  photoCount: 1,
  photoAssetCount: 5,
  photoBytes: 110,
  photoMaxBytes: 22,
  images: [image],
  files,
};
objects.set("trips/export.json", Buffer.from(JSON.stringify(manifest)));
const webp = Buffer.from(
  "RIFF\x0e\x00\x00\x00WEBPVP8 \x02\x00\x00\x00xx",
  "binary",
);
for (const preset of ["thumb", "gallery", "preview", "display", "full"])
  objects.set(`trips/photos/${image}/${preset}.webp`, webp);
const config = { bucket: "stars", files: { export: "trips/export.json" } };
const get = async (bucket, key) => {
  assert.equal(bucket, "stars");
  assert.ok(objects.has(key), `unexpected object read: ${key}`);
  assert.ok(!key.endsWith(".jpg"));
  return { body: objects.get(key) };
};

test("export -> snapshot -> individual HTML entries, hydration data, redirects, and JS budget", async () => {
  const dist = join(await mkdtemp(join(tmpdir(), "trips-built-")), "dist");
  await cp(source, dist, { recursive: true });
  const saved = await snapshotTrips(config, get, join(dist, "data"));
  const report = await renderTrips(dist, saved);
  assert.match(report, /5 trip pages; 1 photos; 5 WebP files; snapshot/);
  for (const entry of [
    "trips.html",
    "trips/2025-trip.html",
    "trips/2025-trip/timeline.html",
    "trips/2025-trip/day/1.html",
    "trips/2025-trip/day/2.html",
  ]) {
    const html = await readFile(join(dist, entry), "utf8");
    assert.match(html, /<h1>/);
    assert.match(html, /Data as of/);
    assert.match(html, /2026-11-17/);
    assert.match(html, /trips\/2025-trip/);
    assert.ok(!html.includes("<script> & journey"));
    assert.ok(!html.includes("<!--ssr-->"));
    assert.ok(!html.includes("/img/"));
    const boot = JSON.parse(
      html.match(
        /<script id="trips-data" type="application\/json">([\s\S]*?)<\/script>/,
      )[1],
    );
    assert.equal(boot.asOf, manifest.asOf);
    assert.equal(boot.photoBase, "/data/trips/photos");
    assert.ok(boot.days.every((day) => !("points" in day)));
    const scripts = [...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map(
      (m) => m[1],
    );
    assert.equal(scripts.length, 1);
    assert.ok(!html.includes("trips-map"));
    assert.ok(!html.includes("maplibre"));
  }
  for (const { page, bytes } of measure(dist))
    assert.ok(bytes <= BUDGET_BYTES, page);
  const redirects = await readFile(join(dist, "_redirects"), "utf8");
  assert.match(redirects, /\/app\/trips \/trips 301/);
  assert.match(redirects, /\/app\/trips\/\* \/trips\/:splat 301/);
  await assert.rejects(access(join(dist, ".trips-renderer")));
  await assert.rejects(access(join(dist, "trip-day.html")));
  assert.deepEqual(
    await readFile(join(dist, "data/trips/photos", image, "full.webp")),
    webp,
  );
});

test("rejects originals, bad destinations, path traversal and inconsistent counts", () => {
  for (const change of [
    { images: ["../original.jpg"] },
    { files: { index: "../../index.html" } },
    { photoBase: "https://evil.invalid" },
    { photoCount: 3 },
    { refreshSeconds: 0 },
    { photoMode: "public" },
  ])
    assert.throws(() => validateExport({ ...manifest, ...change }));
});

test("a corrupted JSON export fails before publication", async () => {
  const out = await mkdtemp(join(tmpdir(), "trips-corrupt-"));
  await assert.rejects(
    snapshotTrips(
      config,
      async (bucket, key) =>
        key === files.index ? { body: Buffer.from("{}") } : get(bucket, key),
      out,
    ),
    /checksum/,
  );
});

test("public mode skips image asset downloads at 2,001 unique photos", async () => {
  const images = Array.from(
    { length: 2001 },
    (_, i) => `img_${i.toString(16).padStart(12, "0")}.jpg`,
  );
  images[0] = image;
  const large = {
    ...manifest,
    images,
    photoCount: 2001,
    photoAssetCount: 10005,
    photoMode: "public",
    photoBase: "https://factory-data.jomcgi.dev/trips/photos",
  };
  const reads = [];
  const out = await mkdtemp(join(tmpdir(), "trips-public-"));
  const result = await snapshotTrips(
    config,
    async (bucket, key) => {
      reads.push(key);
      return key === "trips/export.json"
        ? { body: Buffer.from(JSON.stringify(large)) }
        : get(bucket, key);
    },
    out,
  );
  assert.equal(result.latest.photoCount, 2001);
  assert.ok(reads.every((key) => key.endsWith(".json")));
});

test("oversized small galleries use the public bucket too", async () => {
  for (const sizes of [
    { photoBytes: 1024 ** 3 + 1, photoMaxBytes: 1024 ** 2 },
    { photoBytes: 26 * 1024 ** 2, photoMaxBytes: 26 * 1024 ** 2 },
  ]) {
    const large = {
      ...manifest,
      ...sizes,
      photoMode: "public",
      photoBase: "https://factory-data.jomcgi.dev/trips/photos",
    };
    const out = await mkdtemp(join(tmpdir(), "trips-large-"));
    const result = await snapshotTrips(
      config,
      async (bucket, key) => {
        assert.ok(key.endsWith(".json"));
        return key === "trips/export.json"
          ? { body: Buffer.from(JSON.stringify(large)) }
          : get(bucket, key);
      },
      out,
    );
    assert.equal(result.latest.photoMode, "public");
  }
});
