// HOME-212: read one committed export, then render every trip URL into its
// own HTML entry. All paths and image types are allow-listed at publication.
import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";
const presets = ["thumb", "gallery", "preview", "display", "full"];
const slug = /^(?!\.{1,2}$)[^/\\\x00-\x1f]+$/;
const image = /^img_[A-Za-z0-9_-]{1,64}\.jpg$/;
const hash = (body) => createHash("sha256").update(body).digest("hex");
const escape = (text) =>
  String(text).replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

export function validateExport(manifest, bucket = "stars") {
  if (
    !manifest ||
    !Number.isFinite(Date.parse(manifest.asOf)) ||
    manifest.refreshSeconds !== 86400 ||
    !manifest.files?.index
  )
    throw new Error("Invalid trip export manifest");
  if (
    !/^trips\/v\/index-[a-f0-9]{64}\.json$/.test(manifest.files.index) ||
    !manifest.files.trips ||
    typeof manifest.files.trips !== "object" ||
    Array.isArray(manifest.files.trips)
  )
    throw new Error("Invalid trip JSON key");
  for (const [name, key] of Object.entries(manifest.files.trips))
    if (
      !slug.test(name) ||
      !/^trips\/v\/trip\/[A-Za-z0-9_%~-]+-[a-f0-9]{64}\.json$/.test(key)
    )
      throw new Error("Invalid trip JSON key");
  if (
    !Array.isArray(manifest.images) ||
    manifest.images.some((key) => !image.test(key)) ||
    new Set(manifest.images).size !== manifest.images.length
  )
    throw new Error("Invalid trip image keys");
  if (
    manifest.photoCount !== manifest.images.length ||
    manifest.photoAssetCount !== manifest.images.length * 5
  )
    throw new Error("Invalid trip photo count");
  if (
    !Number.isSafeInteger(manifest.photoBytes) ||
    !Number.isSafeInteger(manifest.photoMaxBytes) ||
    manifest.photoMaxBytes < 0 ||
    manifest.photoBytes < manifest.photoMaxBytes
  )
    throw new Error("Invalid trip photo sizes");
  const mode =
    manifest.photoAssetCount <= 10000 &&
    manifest.photoBytes <= 1024 ** 3 &&
    manifest.photoMaxBytes <= 25 * 1024 ** 2
      ? "snapshot"
      : manifest.photoMode === "private"
        ? "private"
        : "public";
  const base =
    mode === "snapshot"
      ? "/data/trips/photos"
      : mode === "private"
        ? null
        : bucket === "trips-public"
          ? "https://trips-data.jomcgi.dev/trips/photos"
          : "https://factory-data.jomcgi.dev/trips/photos";
  if (manifest.photoMode !== mode || manifest.photoBase !== base)
    throw new Error("Invalid trip photo destination");
}
async function parallel(items, run) {
  const queue = [...items];
  await Promise.all(
    Array.from({ length: Math.min(16, queue.length) }, async () => {
      while (queue.length) await run(queue.shift());
    }),
  );
}
export async function snapshotTrips({ bucket, files }, get, out) {
  const { body } = await get(bucket, files.export);
  const manifest = JSON.parse(body);
  validateExport(manifest, bucket);
  const root = join(out, "trips");
  await mkdir(join(root, "v"), { recursive: true });
  const latest = {
    asOf: manifest.asOf,
    generatedAt: new Date().toISOString(),
    refreshSeconds: manifest.refreshSeconds,
    photoBytes: manifest.photoBytes,
    photoMaxBytes: manifest.photoMaxBytes,
    photoCount: manifest.photoCount,
    photoAssetCount: manifest.photoAssetCount,
    photoMode: manifest.photoMode,
    photoBase: manifest.photoBase,
    files: { trips: Object.create(null) },
  };
  const payloads = new Map();
  await parallel(
    [[null, manifest.files.index], ...Object.entries(manifest.files.trips)],
    async ([name, key]) => {
      const { body } = await get(bucket, key);
      const digest = hash(body);
      if (!key.endsWith(`-${digest}.json`))
        throw new Error("Trip JSON checksum mismatch");
      const file = `${digest}.json`;
      await writeFile(join(root, "v", file), body);
      (name === null ? latest.files : latest.files.trips)[name ?? "index"] =
        `/data/trips/v/${file}`;
      payloads.set(name, JSON.parse(body));
    },
  );
  const index = payloads.get(null);
  if (
    !Array.isArray(index.trips) ||
    index.count !== index.trips.length ||
    new Set(index.trips.map((t) => t.slug)).size !== index.count
  )
    throw new Error("Invalid trip index");
  const trips = index.trips.map((t) => {
    const payload = payloads.get(t.slug);
    if (
      !slug.test(t.slug) ||
      payload?.trip?.slug !== t.slug ||
      !Array.isArray(payload.points)
    )
      throw new Error("Trip index/payload mismatch");
    return payload;
  });
  const referenced = [
    ...index.trips.map((t) => t.default_image),
    ...trips.flatMap((p) => [
      p.trip.default_image,
      ...(p.trip.highlights ?? []).map((h) => h.image),
      ...p.points.map((p) => p.image),
    ]),
  ].filter(Boolean);
  const images = new Set(manifest.images);
  if (referenced.some((key) => !images.has(key)))
    throw new Error("Trip references an unexported photo");
  if (manifest.photoMode === "snapshot") {
    await parallel(manifest.images, async (key) => {
      await mkdir(join(root, "photos", key), { recursive: true });
      for (const preset of presets) {
        const { body } = await get(
          bucket,
          `trips/photos/${key}/${preset}.webp`,
        );
        if (
          body.toString("ascii", 0, 4) !== "RIFF" ||
          body.toString("ascii", 8, 12) !== "WEBP"
        )
          throw new Error("Trip image is not WebP");
        await writeFile(join(root, "photos", key, `${preset}.webp`), body);
      }
    });
  }
  await writeFile(join(root, "latest.json"), JSON.stringify(latest));
  return { latest, index, trips };
}

export async function* tripPages(dist, snapshot, renderer) {
  const { tripEntries, renderTrip } =
    renderer ??
    (await import(pathToFileURL(join(dist, ".trips-renderer/render.mjs"))));
  const templates = Object.fromEntries(
    await Promise.all(
      ["trips", "trip", "trip-day", "trip-timeline"].map(async (name) => [
        name,
        await readFile(join(dist, `${name}.html`), "utf8"),
      ]),
    ),
  );
  const entries = tripEntries(snapshot.latest, snapshot.index, snapshot.trips);
  for (const { path, template, boot, title, description } of entries) {
    // Inert JSON escapes < so a trip title cannot terminate the script element.
    const data = JSON.stringify(boot).replaceAll("<", "\\u003c");
    let html = templates[template].replace(
      /<!--trips-root-->[\s\S]*?<!--\/trips-root-->/,
      () =>
        `<div id="root">${renderTrip(template, boot)}</div><script id="trips-data" type="application/json">${data}</script>`,
    );
    html = html.replace(
      /<title>[\s\S]*?<\/title>/,
      () => `<title>${escape(title)}</title>`,
    );
    if (template !== "trips" && description)
      html = html.replace(
        "</head>",
        () =>
          `<meta name="description" content="${escape(description)}" /></head>`,
      );
    // The hub trip routes have no OG/Twitter tags; retain their titles and
    // descriptions and give each new static URL its own canonical address.
    html = html.replace(
      "</head>",
      `<link rel="canonical" href="https://jomcgi.dev${escape(path)}" /></head>`,
    );
    yield { path, html };
  }
}

export async function renderTrips(dist, snapshot, renderer) {
  let pages = 0;
  for await (const { path, html } of tripPages(dist, snapshot, renderer)) {
    pages++;
    const file = join(dist, path.slice(1) + ".html");
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, html);
  }
  await rm(join(dist, ".trips-renderer"), { recursive: true, force: true });
  for (const name of ["trip", "trip-day", "trip-timeline"])
    await rm(join(dist, `${name}.html`));
  async function count(dir) {
    const children = await readdir(dir, { withFileTypes: true });
    return (
      await Promise.all(
        children.map((child) =>
          child.isDirectory() ? count(join(dir, child.name)) : 1,
        ),
      )
    ).reduce((a, b) => a + b, 0);
  }
  const countAssets = await count(dist);
  if (countAssets >= 20000)
    throw new Error(`Site exceeds Workers asset budget: ${countAssets}`);
  return `${pages} trip pages; ${snapshot.latest.photoCount} photos; ${snapshot.latest.photoAssetCount} WebP files; ${snapshot.latest.photoMode}; ${countAssets} site assets`;
}
