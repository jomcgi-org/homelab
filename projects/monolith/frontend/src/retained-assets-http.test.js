import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Exercise the deployed entry and its generated Polka/sirv/Kit chain. Release A
// lives only in the archive; this packaged release B has different local files.
const script = String.raw`
  import { createServer, request } from "node:http";
  import { once } from "node:events";
  import { readFileSync, readdirSync, existsSync } from "node:fs";
  import { createHash } from "node:crypto";
  import { gzipSync, brotliCompressSync } from "node:zlib";
  const failure = process.argv[1] === "fail";
  const local = readdirSync("./dist/client/_app/immutable/entry").find(name => name.startsWith("start.") && name.endsWith(".js"));
  const startPath = "entry/" + local;
  const current = readFileSync("./dist/client/_app/immutable/" + startPath);
  const archive = new Map([
    ["chunks/AAAAAAAA.js", { type: "text/javascript", body: Buffer.from('export const release = "A";') }],
    ["assets/page.AAAAAAAA.css", { type: "text/css", body: Buffer.from("body { color: teal; }") }],
    ["workers/maplibre-gl-worker-AAAAAAAA.js", { type: "text/javascript", body: Buffer.from("self.onmessage = () => {};") }],
    ["assets/photo.AAAAAAAA.webp", { type: "image/webp", body: Buffer.from([82,73,70,70,0,1,2,3,87,69,66,80]) }],
    [startPath, { type: "text/javascript", body: current }],
  ]);
  const hash = bytes => '"' + createHash("sha256").update(bytes).digest("hex") + '"';
  const calls = [];
  const policy = { "cache-control": "public, max-age=31536000, immutable", vary: "Accept-Encoding" };
  const backend = createServer((req, res) => {
    calls.push({ path: req.url, method: req.method, headers: req.headers });
    if (failure) return res.writeHead(503).end();
    const url = new URL(req.url, "http://backend");
    const key = url.pathname.slice("/internal/public-assets/".length);
    const item = archive.get(key);
    if (key === "chunks/BBBBBBBB.js") return res.writeHead(302, { location: "http://untrusted.invalid/asset" }).end();
    if (!item) return res.writeHead(404).end();
    const encoding = url.searchParams.get("encoding");
    if (item.type === "image/webp" && encoding !== "identity") return res.writeHead(406).end();
    const body = encoding === "br" ? brotliCompressSync(item.body) : encoding === "gzip" ? gzipSync(item.body) : item.body;
    const etag = hash(body);
    if (req.headers["if-none-match"] === etag) return res.writeHead(304, { ...policy, etag }).end();
    res.writeHead(200, { ...policy, "content-type": item.type, "content-length": body.length, etag, ...(encoding !== "identity" ? { "content-encoding": encoding } : {}) });
    res.end(req.method === "HEAD" ? undefined : body);
  });
  backend.listen(0, "127.0.0.1"); await once(backend, "listening");
  process.env.PORT = "0";
  process.env.HOST = "127.0.0.1";
  process.env.PUBLIC_SNAPSHOT_PREWARM = "false";
  process.env.PRIVATE_DASHBOARD_PREWARM = "false";
  process.env.PUBLIC_ASSET_ARCHIVE = "true";
  process.env.API_BASE = "http://127.0.0.1:" + backend.address().port;
  const log = console.log; console.log = () => {};
  let server;
  try { ({ server } = await import("./dist/cache-server.js")); }
  catch (error) {
    backend.closeAllConnections(); await new Promise(resolve => backend.close(resolve));
    console.log = log;
    if (!failure) throw error;
    console.log(JSON.stringify({ startupFailed: true, calls, error: error.message }));
    process.exit(0);
  }
  if (failure) throw new Error("Failed readback started frontend");
  const http = server.server;
  if (!http.listening) await once(http, "listening");
  async function get(path, headers = {}, method = "GET") {
    return new Promise((resolve, reject) => {
      const req = request({ hostname: "127.0.0.1", port: http.address().port, path, method, headers: { host: "jomcgi.dev", ...headers } }, res => {
        const chunks = [];
        res.on("data", chunk => chunks.push(chunk)); res.on("error", reject);
        res.on("end", () => resolve({ path, method, status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString("base64") }));
      }); req.on("error", reject); req.end();
    });
  }
  const results = [];
  try {
    for (const encoding of ["identity", "br", "gzip"]) {
      const headers = { "accept-encoding": encoding };
      const response = await get("/_app/immutable/chunks/AAAAAAAA.js", headers);
      results.push({ ...response, kind: encoding, expected: (encoding === "br" ? brotliCompressSync(archive.get("chunks/AAAAAAAA.js").body) : encoding === "gzip" ? gzipSync(archive.get("chunks/AAAAAAAA.js").body) : archive.get("chunks/AAAAAAAA.js").body).toString("base64") });
      results.push({ ...await get(response.path, headers, "HEAD"), kind: "head" });
      results.push({ ...await get(response.path, { ...headers, "if-none-match": response.headers.etag }), kind: "conditional" });
    }
    for (const [path, item] of archive) {
      const before = calls.length;
      results.push({ ...await get("/_app/immutable/" + path), kind: path === startPath ? "local" : "old", expected: item.body.toString("base64"), calls: calls.length - before });
    }
    for (const header of ["cookie", "authorization", "cf-access-jwt-assertion", "x-auth-email"]) results.push({ ...await get("/_app/immutable/chunks/AAAAAAAA.js", { [header]: "identity-test" }), kind: "identity" });
    for (const host of ["private.jomcgi.dev", "friends.jomcgi.dev"]) results.push({ ...await get("/_app/immutable/chunks/AAAAAAAA.js", { host }), kind: "identity" });
    results.push({ ...await get("/_app/immutable/chunks/CCCCCCCC.js"), kind: "missing" });
    results.push({ ...await get("/_app/immutable/chunks/BBBBBBBB.js"), kind: "redirect" });
    for (const path of ["chunks/%2e%2e/private.js", "chunks/AAAAAAAA.js.map", "nodes/26.AAAAAAAA.js.functions.json", "../version.json"]) {
      const before = calls.length;
      results.push({ ...await get("/_app/immutable/" + path), kind: "invalid", calls: calls.length - before });
    }
  } finally {
    http.closeAllConnections(); await new Promise(resolve => http.close(resolve));
    backend.closeAllConnections(); await new Promise(resolve => backend.close(resolve));
  }
  console.log = log;
  console.log(JSON.stringify({ results, calls, oldAbsent: !existsSync("./dist/client/_app/immutable/chunks/AAAAAAAA.js"), startupPath: startPath }));
`;

const observe = (mode = "ok") =>
  JSON.parse(
    execFileSync(
      process.execPath,
      ["--input-type=module", "-e", script, mode],
      {
        cwd: fileURLToPath(new URL("..", import.meta.url)),
        encoding: "utf8",
        timeout: 30_000,
      },
    ),
  );
const observations = observe();

describe("packaged release A assets on release B", () => {
  it("reads the current release before serving and keeps its sirv fast path", () => {
    expect(observations.oldAbsent).toBe(true);
    expect(observations.calls[0].path).toBe(
      `/internal/public-assets/${observations.startupPath}?encoding=identity`,
    );
    const local = observations.results.find(({ kind }) => kind === "local");
    expect(local.status).toBe(200);
    expect(local.body).toBe(local.expected);
    expect(local.calls).toBe(0);
  });
  it.each(
    observations.results.filter(({ kind }) =>
      ["old", "identity", "br", "gzip"].includes(kind),
    ),
  )("preserves $kind $path", (result) => {
    expect(result.status).toBe(200);
    if (result.expected) expect(result.body).toBe(result.expected);
    if (result.kind === "identity" && !result.expected) {
      expect(result.headers["cache-control"]).toBe("no-store");
      expect(result.headers["cloudflare-cdn-cache-control"]).toBe("no-store");
      expect(result.headers.etag).toBeUndefined();
    } else {
      expect(result.headers["cache-control"]).toContain("immutable");
      expect(result.headers.etag).toMatch(/^"[a-f0-9]{64}"$/);
    }
  });
  it.each(
    observations.results.filter(({ kind }) =>
      ["head", "conditional"].includes(kind),
    ),
  )("preserves $kind metadata", (result) => {
    expect(result.status).toBe(result.kind === "head" ? 200 : 304);
    expect(result.body).toBe("");
    expect(result.headers.etag).toMatch(/^"[a-f0-9]{64}"$/);
    if (result.kind === "head")
      expect(Number(result.headers["content-length"])).toBeGreaterThan(0);
  });
  it.each(
    observations.results.filter(({ kind }) =>
      ["missing", "redirect", "invalid"].includes(kind),
    ),
  )("keeps $kind responses uncached", (result) => {
    expect(result.status).toBe(result.kind === "redirect" ? 503 : 404);
    expect(result.headers["cache-control"]).toBe("no-store");
    expect(result.headers.etag).toBeUndefined();
    if (result.kind === "invalid") expect(result.calls).toBe(0);
  });
  it("never sends browser identities or arbitrary destinations to the reader", () => {
    for (const { path, headers } of observations.calls) {
      expect(path).toMatch(
        /^\/internal\/public-assets\/(?:entry|chunks|assets|workers)\//,
      );
      for (const name of [
        "cookie",
        "authorization",
        "cf-access-jwt-assertion",
        "x-auth-email",
      ])
        expect(headers[name]).toBeUndefined();
    }
  });
  it("fails before listening when the configured public reader is unavailable", () => {
    const result = observe("fail");
    expect(result.startupFailed).toBe(true);
    expect(result.calls).toHaveLength(1);
  });
});
