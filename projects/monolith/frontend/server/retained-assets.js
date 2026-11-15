import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const PREFIX = "/_app/immutable/";
const CACHE = "public, max-age=31536000, immutable";
const MAX_BYTES = 16 * 1024 * 1024;
const TYPES = {
  js: "text/javascript",
  css: "text/css",
  woff: "font/woff",
  woff2: "font/woff2",
  ttf: "font/ttf",
  otf: "font/otf",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  avif: "image/avif",
  gif: "image/gif",
  svg: "image/svg+xml",
  ico: "image/x-icon",
};

// Match the backend reader's closed immutable namespace. In particular, URL
// decoding or URL normalization must never turn a rejected path into an object.
export function retainedAssetPath(target = "") {
  const pathname = target.split("?", 1)[0];
  if (!pathname.startsWith(PREFIX)) return null;
  const relative = pathname.slice(PREFIX.length);
  if (relative.length > 240) return null;
  const match =
    /^(assets|chunks|entry|nodes|workers)\/([A-Za-z0-9_.-]+)\.([a-z0-9]+)$/.exec(
      relative,
    );
  if (!match || !Object.hasOwn(TYPES, match[3]) || match[2].includes(".."))
    return null;
  if (!/^(?:[A-Za-z0-9_.-]+[.-])?[A-Za-z0-9_-]{8}$/.test(match[2])) return null;
  return { relative, type: TYPES[match[3]] };
}

export function acceptedEncodings(value = "") {
  const weights = new Map();
  for (const item of value.toLowerCase().split(",")) {
    const [name, ...params] = item.trim().split(";");
    if (!name) continue;
    const q = params.find((param) => param.trim().startsWith("q="));
    const text = q?.trim().slice(2);
    const weight =
      text === undefined
        ? 1
        : /^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/.test(text)
          ? Number(text)
          : 0;
    // Duplicate contradictory values are handled conservatively.
    weights.set(name, Math.min(weights.get(name) ?? 1, weight));
  }
  return ["br", "gzip", "identity"]
    .map((encoding, preference) => ({
      encoding,
      preference,
      weight:
        weights.get(encoding) ??
        (encoding === "identity"
          ? weights.get("*") === 0
            ? 0
            : 1
          : (weights.get("*") ?? 0)),
    }))
    .filter(({ weight }) => weight > 0)
    .sort((a, b) => b.weight - a.weight || a.preference - b.preference)
    .map(({ encoding }) => encoding);
}

function localAssets(root) {
  const paths = new Set();
  try {
    for (const directory of readdirSync(root, { withFileTypes: true })) {
      if (!directory.isDirectory()) continue;
      for (const file of readdirSync(join(root, directory.name), {
        withFileTypes: true,
      })) {
        const relative = `${directory.name}/${file.name}`;
        if (file.isFile() && retainedAssetPath(PREFIX + relative))
          paths.add(relative);
      }
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  return paths;
}

function readAsset(url, method, etag, signal, maxBytes) {
  return new Promise((resolve, reject) => {
    const request = (url.protocol === "https:" ? httpsRequest : httpRequest)(
      url,
      {
        method,
        signal,
        headers: etag ? { "if-none-match": etag } : {},
      },
      (response) => {
        const chunks = [];
        let size = 0;
        const length = response.headers["content-length"];
        if (
          length !== undefined &&
          (!/^\d+$/.test(length) || Number(length) > maxBytes)
        ) {
          response.destroy(new Error("Invalid retained asset length"));
        }
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > maxBytes)
            response.destroy(new Error("Retained asset too large"));
          else chunks.push(chunk);
        });
        response.on("error", reject);
        response.on("end", () =>
          resolve({
            status: response.statusCode,
            headers: response.headers,
            body: Buffer.concat(chunks),
          }),
        );
      },
    );
    request.on("error", reject);
    request.end();
  });
}

function failure(response, status) {
  response.writeHead(status, {
    "cache-control": "no-store",
    "cloudflare-cdn-cache-control": "no-store",
    "content-type": "text/plain; charset=utf-8",
    "x-content-type-options": "nosniff",
  });
  response.end(status === 404 ? "Not found" : "Asset unavailable");
}

function hasPublicAssetPolicy(headers) {
  return (
    headers["cache-control"] === CACHE &&
    !headers["set-cookie"] &&
    ["cdn-cache-control", "cloudflare-cdn-cache-control"].every(
      (name) => !headers[name] || headers[name] === CACHE,
    ) &&
    (headers.vary || "").toLowerCase() === "accept-encoding"
  );
}

export async function verifyRetainedAssetRead({
  assetRoot,
  apiBase,
  timeoutMs = 5000,
  maxBytes = MAX_BYTES,
}) {
  const relative = [...localAssets(assetRoot)].find((path) =>
    /^entry\/start\./.test(path),
  );
  if (!relative)
    throw new Error("Public asset archive: current start asset is missing");
  const expected = readFileSync(join(assetRoot, relative));
  if (expected.length > maxBytes)
    throw new Error("Public asset archive: current start asset exceeds limit");
  const url = new URL(
    `/internal/public-assets/${relative}?encoding=identity`,
    apiBase,
  );
  const signal = AbortSignal.timeout(timeoutMs);
  const response = await readAsset(url, "GET", undefined, signal, maxBytes);
  const hash = `"${createHash("sha256").update(expected).digest("hex")}"`;
  if (
    response.status !== 200 ||
    !hasPublicAssetPolicy(response.headers) ||
    (response.headers["content-encoding"] || "identity") !== "identity" ||
    (response.headers["content-type"] || "").split(";")[0] !==
      "text/javascript" ||
    response.headers.etag !== hash ||
    Number(response.headers["content-length"]) !== expected.length ||
    !response.body.equals(expected)
  )
    throw new Error("Public asset archive: current release readback failed");
}

export function createRetainedAssets({
  enabled = false,
  assetRoot,
  apiBase,
  timeoutMs = 5000,
  maxBytes = MAX_BYTES,
  maxConcurrent = 4,
  maxQueued = 128,
  flushTimeoutMs = 120_000,
}) {
  if (!enabled) return (_request, _response, next) => next();
  // A deployment-owned origin only. No credentials, redirects, user input or
  // alternate route can change the storage reader's destination.
  const base = new URL(apiBase);
  if (
    !["http:", "https:"].includes(base.protocol) ||
    base.username ||
    base.password ||
    base.pathname !== "/" ||
    base.search ||
    base.hash
  )
    throw new Error(
      "API_BASE must be a plain HTTP(S) origin for retained assets",
    );
  const local = localAssets(assetRoot);
  let active = 0;
  const waiting = [];
  function acquire(signal) {
    if (active < maxConcurrent) {
      active++;
      return Promise.resolve();
    }
    if (waiting.length >= maxQueued)
      return Promise.reject(new Error("Archive busy"));
    return new Promise((resolve, reject) => {
      const entry = { resolve, signal, cancelled };
      function cancelled() {
        const index = waiting.indexOf(entry);
        if (index !== -1) waiting.splice(index, 1);
        reject(new Error("Archive request cancelled"));
      }
      signal.addEventListener("abort", cancelled, { once: true });
      waiting.push(entry);
    });
  }
  function release() {
    active--;
    const next = waiting.shift();
    if (next) {
      active++;
      next.signal.removeEventListener("abort", next.cancelled);
      next.resolve();
    }
  }
  return async (request, response, next) => {
    const asset = retainedAssetPath(request.url);
    if (
      !asset ||
      !["GET", "HEAD"].includes(request.method) ||
      local.has(asset.relative)
    )
      return next();
    const encodings = acceptedEncodings(request.headers["accept-encoding"]);
    if (!encodings.length) return failure(response, 406);
    let acquired = false;
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), timeoutMs);
    const disconnected = () => {
      if (!response.writableFinished) abort.abort();
    };
    response.on("close", disconnected);
    try {
      await acquire(abort.signal);
      acquired = true;
      for (const encoding of encodings) {
        const url = new URL(`/internal/public-assets/${asset.relative}`, base);
        url.searchParams.set("encoding", encoding);
        const upstream = await readAsset(
          url,
          request.method,
          request.headers["if-none-match"],
          abort.signal,
          maxBytes,
        );
        if (upstream.status === 406) continue;
        if (upstream.status === 404) return failure(response, 404);
        if (![200, 304].includes(upstream.status))
          throw new Error("Retained asset reader failed");
        const headers = upstream.headers;
        if (
          !hasPublicAssetPolicy(headers) ||
          !/^"[a-f0-9]{64}"$/.test(headers.etag || "")
        )
          throw new Error("Invalid retained asset headers");
        if (upstream.status === 304) {
          const conditional = request.headers["if-none-match"] || "";
          const matches =
            conditional === "*" ||
            conditional
              .split(",")
              .some((tag) =>
                [headers.etag, `W/${headers.etag}`].includes(tag.trim()),
              );
          if (!matches || upstream.body.length)
            throw new Error("Unexpected retained asset 304");
          response.writeHead(304, {
            etag: headers.etag,
            vary: "Accept-Encoding",
            "cache-control": CACHE,
            "cloudflare-cdn-cache-control": CACHE,
          });
          return response.end();
        }
        if (
          (headers["content-type"] || "").split(";")[0] !== asset.type ||
          (headers["content-encoding"] || "identity") !== encoding ||
          !/^\d+$/.test(headers["content-length"] || "")
        )
          throw new Error("Invalid retained asset representation");
        if (
          request.method === "GET" &&
          (upstream.body.length !== Number(headers["content-length"]) ||
            `"${createHash("sha256").update(upstream.body).digest("hex")}"` !==
              headers.etag)
        )
          throw new Error("Retained asset integrity failed");
        response.writeHead(200, {
          "content-type": asset.type,
          "content-length": headers["content-length"],
          ...(encoding !== "identity" ? { "content-encoding": encoding } : {}),
          etag: headers.etag,
          vary: "Accept-Encoding",
          "cache-control": CACHE,
          "cloudflare-cdn-cache-control": CACHE,
          "x-content-type-options": "nosniff",
        });
        return response.end(
          request.method === "HEAD" ? undefined : upstream.body,
        );
      }
      failure(response, 406);
    } catch {
      if (!response.destroyed) failure(response, 503);
    } finally {
      clearTimeout(timeout);
      response.off("close", disconnected);
      // Keep the bounded slot while Node is flushing a buffered representation
      // to a slow client, as well as while the upstream read is in progress.
      if (!response.writableFinished && !response.destroyed) {
        await new Promise((resolve) => {
          // This is separate from the reader deadline: a valid large asset can
          // take longer to download on mobile. A client that stops reading must
          // still release its bounded buffer and slot eventually.
          const flushTimeout = setTimeout(
            () => response.destroy(),
            flushTimeoutMs,
          );
          const done = () => {
            clearTimeout(flushTimeout);
            response.off("finish", done);
            response.off("close", done);
            resolve();
          };
          response.once("finish", done);
          response.once("close", done);
        });
      }
      if (acquired) release();
    }
  };
}

const configuration = {
  enabled: process.env.PUBLIC_ASSET_ARCHIVE === "true",
  apiBase: process.env.API_BASE,
  assetRoot: fileURLToPath(
    new URL("./client/_app/immutable/", import.meta.url),
  ),
};
export const retainedAssets = createRetainedAssets(configuration);
// A failed reader rollout or public read-only credential must not serve new
// HTML. This runs before adapter-node constructs/listens on its HTTP server.
if (configuration.enabled) await verifyRetainedAssetRead(configuration);
