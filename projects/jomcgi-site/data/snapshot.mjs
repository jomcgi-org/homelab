// Copies each section's source objects from R2 into the site's data
// directory before a deploy (HOME-209). For each section it writes
//   <out>/<section>/v/<name>-<sha256:12>.json   immutable, named by content
//   <out>/<section>/latest.json                 small index of those files
// The Worker serves them as static assets at /data/..., on the site's own
// origin, so pages need no CORS and requests cost nothing.
// main.mjs runs it; this module only exports, so tests import it safely.
import { createHash, createHmac } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

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
export async function snapshot(sources, get, out) {
  const log = [];
  for (const [section, { bucket, files }] of Object.entries(sources.sections)) {
    const index = { asOf: null, files: {}, sources: {} };
    await mkdir(join(out, section, "v"), { recursive: true });
    for (const [name, key] of Object.entries(files)) {
      const { body, lastModified } = await get(bucket, key);
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
    await writeFile(
      join(out, section, "latest.json"),
      `${JSON.stringify(index, null, 2)}\n`,
    );
  }
  return log;
}
