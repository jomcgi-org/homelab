// Copies each section's source objects from R2 into the site's data
// directory before a deploy (HOME-209). For each section it writes
//   <out>/<section>/v/<name>-<sha256:12>.json   immutable, named by content
//   <out>/<section>/latest.json                 small index of those files
// The Worker serves them as static assets at /data/..., on the site's own
// origin, so pages need no CORS and requests cost nothing.
// main.mjs runs it; this module only exports, so tests import it safely.
import { createHash, createHmac } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { validate } from "../../factory-storyboard/schema/validate.ts";
import { join } from "node:path";

import { snapshotTrips, renderTrips } from "./trips.mjs";
import { makeSummary, stampSummary } from "./stars-summary.mjs";

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const hmac = (key, data) => createHmac("sha256", key).update(data).digest();

/** AWS SigV4 headers for an unsigned-payload-free GET on R2's S3 API. */
export function signGet(
  url,
  { accessKeyId, secretAccessKey },
  now = new Date(),
) {
  const { host, pathname } = new URL(url);
  const amzDate = now
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}/, "");
  const day = amzDate.slice(0, 8);
  const payload = sha256("");
  const canonical = [
    "GET",
    pathname,
    "",
    `host:${host}`,
    `x-amz-content-sha256:${payload}`,
    `x-amz-date:${amzDate}`,
    "",
    "host;x-amz-content-sha256;x-amz-date",
    payload,
  ].join("\n");
  const scope = `${day}/auto/s3/aws4_request`;
  const toSign = ["AWS4-HMAC-SHA256", amzDate, scope, sha256(canonical)].join(
    "\n",
  );
  let key = `AWS4${secretAccessKey}`;
  for (const part of [day, "auto", "s3", "aws4_request"]) key = hmac(key, part);
  const signature = createHmac("sha256", key).update(toSign).digest("hex");
  return {
    "x-amz-date": amzDate,
    "x-amz-content-sha256": payload,
    Authorization: `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=${signature}`,
  };
}

/** A getter for R2 objects; `fetch` is injectable for tests. */
export function r2Getter(endpoint, credentials, fetchImpl = fetch) {
  return async (bucket, key) => {
    const url = `${endpoint}/${bucket}/${key.split("/").map(encodeURIComponent).join("/")}`;
    const response = await fetchImpl(url, {
      headers: signGet(url, credentials),
    });
    if (!response.ok)
      throw new Error(`GET ${bucket}/${key}: ${response.status}`);
    return {
      body: Buffer.from(await response.arrayBuffer()),
      lastModified: response.headers.get("last-modified"),
    };
  };
}

/** Writes every section's files and latest.json under `out`. Returns the log. */
export async function snapshot(
  sources,
  get,
  out,
  now = new Date(),
  fetchImpl = fetch,
) {
  const log = [];
  for (const [section, config] of Object.entries(sources.sections)) {
    // A disabled section has no source objects yet (trips, HOME-452).
    if (config.enabled === false) {
      log.push(`${section}: disabled, not snapshotted`);
      continue;
    }
    const { bucket } = config;
    let files = config.files;
    if (section === "stars" && config.manifest) {
      const index = JSON.parse((await get(bucket, config.manifest)).body);
      files = Object.fromEntries(
        Object.keys(config.files).map((name) => {
          const key = index.files?.[name];
          const stem = name === "historyMap" ? "history-map" : name;
          if (
            typeof key !== "string" ||
            !new RegExp(`^stars/${stem}-[0-9a-f]{64}\\.json$`).test(key)
          )
            throw new Error(`Invalid Stars manifest key for ${name}`);
          return [name, key];
        }),
      );
    }
    if (config.format === "trips") {
      const data = await snapshotTrips(config, get, out);
      log.push(await renderTrips(join(out, ".."), data));
      continue;
    }
    // A successful snapshot can contain unchanged source objects. Keep its
    // publication cadence separate from the age of those objects.
    const index = {
      asOf: null,
      generatedAt: now.toISOString(),
      files: {},
      sources: {},
    };
    let starsSites;
    await mkdir(join(out, section, "v"), { recursive: true });
    let resolved = files;
    if (section === "hikes" && config.manifest) {
      const { body } = await get(bucket, config.manifest);
      const { walks } = JSON.parse(body);
      if (!/^walks-[0-9a-f]{64}\.json$/.test(walks))
        throw new Error("Invalid hikes manifest");
      resolved = { walks };
    }
    for (const [name, key] of Object.entries(resolved)) {
      const { body, lastModified } = await get(bucket, key);
      if (config.manifest && !key.endsWith(`-${sha256(body)}.json`))
        throw new Error(`Digest mismatch for ${key}`);
      if (section === "stars" && name === "sites")
        starsSites = JSON.parse(body);
      const file = `${name}-${sha256(body).slice(0, 12)}.json`;
      await writeFile(join(out, section, "v", file), body);
      const modified = lastModified
        ? new Date(lastModified).toISOString()
        : null;
      index.files[name] = `/data/${section}/v/${file}`;
      index.sources[name] = { bucket, key, lastModified: modified };
      if (modified && (!index.asOf || modified > index.asOf))
        index.asOf = modified;
      log.push(
        `${section}/${name}: ${bucket}/${key} -> v/${file} (${body.length} bytes)`,
      );
    }
    // A tiny content-named summary lets an idle page refresh without reading
    // the full forecast or loading the map. Stamp this same object into HTML.
    const summary = starsSites
      ? makeSummary(starsSites, Date.now(), index.generatedAt)
      : null;
    if (summary) {
      const body = Buffer.from(JSON.stringify(summary));
      const file = `summary-${sha256(body).slice(0, 12)}.json`;
      await writeFile(join(out, section, "v", file), body);
      index.files.summary = `/data/${section}/v/${file}`;
    }
    await writeFile(
      join(out, section, "latest.json"),
      `${JSON.stringify(index, null, 2)}\n`,
    );
    if (summary) {
      // Every refresh updates the no-JS page along with its data. An isolated
      // snapshot may have no page; a deployed page must have the build markers.
      const page = join(out, "..", "stars.html");
      let html;
      try {
        html = await readFile(page, "utf8");
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
      // An empty page is still a page: it must have the markers too.
      if (html !== undefined)
        await writeFile(page, stampSummary(html, summary));
    }
  }
  for (const [section, source] of Object.entries(
    sources.httpsManifests ?? {},
  )) {
    if (source.enabled === false) continue;
    const url = typeof source === "string" ? source : source.url;
    if (new URL(url).protocol !== "https:") throw new Error("HTTPS required");
    const read = async (location) => {
      const response = await fetchImpl(location, {
        redirect: "error",
        credentials: "omit",
      });
      if (!response.ok) throw new Error(`GET ${location}: ${response.status}`);
      return Buffer.from(await response.arrayBuffer());
    };
    const body = await read(url);
    const manifest = JSON.parse(body);
    if (!validate("manifest", manifest))
      throw new Error("Invalid storyboard manifest");
    const paths = new Set([manifest.story]);
    for (const refs of [manifest.goals, manifest.issues, manifest.sessions]) {
      if (!refs || typeof refs !== "object" || Array.isArray(refs))
        throw new Error("Invalid manifest index");
      for (const path of Object.values(refs)) paths.add(path);
    }
    await mkdir(join(out, section, "objects"), { recursive: true });
    for (const path of paths) {
      if (
        typeof path !== "string" ||
        !/^objects\/[a-f0-9]{64}\.json$/.test(path)
      )
        throw new Error("Invalid object path");
      const object = await read(new URL(path, url).href);
      if (path !== `objects/${sha256(object)}.json`)
        throw new Error("Object digest mismatch");
      await writeFile(join(out, section, path), object);
    }
    await writeFile(join(out, section, "manifest.json"), body);
    log.push(`${section}: copied ${paths.size} objects`);
  }
  return log;
}
