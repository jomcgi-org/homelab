import { createHash } from "node:crypto";
import { EventEmitter, once } from "node:events";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer, request } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gzipSync, brotliCompressSync } from "node:zlib";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { installRetainedAssetHandler } from "../adapter-cache.js";
import { protectResponseCache } from "../server/http-cache-policy.js";
import {
  acceptedEncodings,
  createRetainedAssets,
  retainedAssetPath,
  verifyRetainedAssetRead,
} from "../server/retained-assets.js";

const data = Buffer.from('export const release = "A";');
const policy = {
  "cache-control": "public, max-age=31536000, immutable",
  vary: "Accept-Encoding",
};
const hash = (bytes) => `"${createHash("sha256").update(bytes).digest("hex")}"`;
const variants = {
  identity: data,
  gzip: gzipSync(data),
  br: brotliCompressSync(data),
};
let root,
  backend,
  frontend,
  apiBase,
  mode = "ok",
  calls = [],
  pending = [];

const listen = (server) =>
  new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const close = (server) =>
  new Promise((resolve) => {
    server.closeAllConnections();
    server.close(resolve);
  });
function get(
  server,
  path = "/_app/immutable/chunks/AAAAAAAA.js",
  headers = {},
  method = "GET",
) {
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: "127.0.0.1",
        port: server.address().port,
        path,
        method,
        headers: { host: "jomcgi.dev", ...headers },
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("error", reject);
        res.on("end", () =>
          resolve({
            status: res.statusCode,
            headers: res.headers,
            body: Buffer.concat(chunks),
          }),
        );
      },
    );
    req.on("error", reject);
    req.end();
  });
}

beforeAll(async () => {
  root = mkdtempSync(join(tmpdir(), "retained-assets-"));
  mkdirSync(join(root, "entry"));
  writeFileSync(join(root, "entry/start.BBBBBBBB.js"), data);
  backend = createServer((req, res) => {
    calls.push({ url: req.url, method: req.method, headers: req.headers });
    const encoding = new URL(req.url, "http://backend").searchParams.get(
      "encoding",
    );
    const body = variants[encoding];
    if (mode === "hold") {
      pending.push(() =>
        res
          .writeHead(200, {
            ...policy,
            "content-type": "text/javascript",
            "content-length": body.length,
            etag: hash(body),
          })
          .end(body),
      );
      return;
    }
    if (mode === "slow") return;
    if (mode === "missing")
      return res.writeHead(404).end("secret backend detail");
    if (mode === "error")
      return res.writeHead(503).end("secret backend detail");
    if (mode === "redirect")
      return res.writeHead(302, { location: apiBase + "/arbitrary" }).end();
    if (mode === "identity" && encoding !== "identity")
      return res.writeHead(406).end();
    if (mode === "oversize") return res.writeHead(200).end(Buffer.alloc(1025));
    const etag = hash(body);
    if (req.headers["if-none-match"] === etag || mode === "bad304")
      return res.writeHead(304, { ...policy, etag }).end();
    res.writeHead(200, {
      ...policy,
      ...(mode === "private" ? { "cache-control": "private, no-store" } : {}),
      ...(mode === "vary" ? { vary: "Cookie" } : {}),
      "content-type": mode === "html" ? "text/html" : "text/javascript",
      "content-length": body.length,
      etag: mode === "corrupt" ? hash(Buffer.from("corrupt")) : etag,
      ...(encoding !== "identity" ? { "content-encoding": encoding } : {}),
      ...(mode === "cookie" ? { "set-cookie": "session=bad" } : {}),
    });
    res.end(req.method === "HEAD" ? undefined : body);
  });
  await listen(backend);
  apiBase = `http://127.0.0.1:${backend.address().port}`;
  const middleware = createRetainedAssets({
    enabled: true,
    assetRoot: root,
    apiBase,
    maxBytes: 1024,
    timeoutMs: 60,
    maxConcurrent: 1,
    maxQueued: 0,
  });
  frontend = createServer((req, res) => {
    protectResponseCache(req, res);
    middleware(req, res, () => res.writeHead(418).end("local handler"));
  });
  await listen(frontend);
});
beforeEach(() => {
  mode = "ok";
  calls = [];
  pending = [];
});
afterAll(async () => {
  await close(frontend);
  await close(backend);
  rmSync(root, { recursive: true });
});

describe("retained asset boundary", () => {
  it.each([
    "chunks/Abcd_123.js",
    "entry/start.Abcd_123.js",
    "nodes/26.Abcd_123.js",
    "assets/page.Abcd_123.webp",
    "workers/maplibre-gl-worker-Abcd_123.js",
  ])("admits emitted hashed path %s", (path) =>
    expect(retainedAssetPath("/_app/immutable/" + path)?.relative).toBe(path),
  );

  it.each([
    "../server.js",
    "chunks/%2e%2e/private.js",
    "chunks/A%41AAAAAA.js",
    "chunks/../../secret.js",
    "chunks/AAAAAAAA.js.map",
    "chunks/AAAAAAAA.js.br",
    "entry/start.js",
    "version.json",
    "nodes/26.AAAAAAAA.js.functions.json",
    "chunks/nested/AAAAAAAA.js",
    "chunks\\AAAAAAAA.js",
    "chunks/AAAAAAAA.html",
    "chunks/AAAAAAAA.js/",
    "chunks/AAAAAAAA.js#fragment",
    "chunks/AAAAAAAA.constructor",
  ])("rejects unarchived path %s", async (path) => {
    expect(retainedAssetPath("/_app/immutable/" + path)).toBeNull();
    expect((await get(frontend, "/_app/immutable/" + path)).status).toBe(418);
    expect(calls).toHaveLength(0);
  });

  it("honors encoding weights, exclusions and identity defaults", () => {
    expect(acceptedEncodings("br, gzip")).toEqual(["br", "gzip", "identity"]);
    expect(acceptedEncodings("br;q=0, gzip;q=0.5, identity;q=0.1")).toEqual([
      "gzip",
      "identity",
    ]);
    expect(acceptedEncodings("br;q=0.1")).toEqual(["identity", "br"]);
    expect(acceptedEncodings("*;q=0")).toEqual([]);
    expect(acceptedEncodings("*;q=0, gzip")).toEqual(["gzip"]);
    expect(acceptedEncodings("br;q=1, br;q=0")).toEqual(["identity"]);
  });

  it.each(["identity", "br", "gzip"])(
    "preserves exact %s bytes and HEAD metadata",
    async (encoding) => {
      const headers = { "accept-encoding": encoding };
      const result = await get(frontend, undefined, headers);
      expect(result.status).toBe(200);
      expect(result.body.equals(variants[encoding])).toBe(true);
      expect(result.headers.etag).toBe(hash(variants[encoding]));
      expect(result.headers["content-encoding"]).toBe(
        encoding === "identity" ? undefined : encoding,
      );
      expect(result.headers.vary).toBe("Accept-Encoding");
      expect(result.headers["cache-control"]).toContain("immutable");
      const head = await get(frontend, undefined, headers, "HEAD");
      expect(head.body.length).toBe(0);
      expect(head.headers["content-length"]).toBe(
        String(variants[encoding].length),
      );
      const conditional = await get(frontend, undefined, {
        ...headers,
        "if-none-match": result.headers.etag,
      });
      expect(conditional.status).toBe(304);
      expect(conditional.body.length).toBe(0);
      expect(conditional.headers.etag).toBe(result.headers.etag);
    },
  );

  it("falls back only from unavailable representations", async () => {
    mode = "identity";
    const result = await get(frontend, undefined, {
      "accept-encoding": "br, gzip",
    });
    expect(result.body.equals(data)).toBe(true);
    expect(calls.map(({ url }) => url.split("=")[1])).toEqual([
      "br",
      "gzip",
      "identity",
    ]);
    calls = [];
    expect(
      (
        await get(frontend, undefined, {
          "accept-encoding": "br, identity;q=0",
        })
      ).status,
    ).toBe(406);
    expect(calls).toHaveLength(1);
  });

  it.each([
    "cookie",
    "authorization",
    "cf-access-jwt-assertion",
    "x-auth-email",
  ])(
    "keeps %s responses no-store and never forwards identity",
    async (header) => {
      const result = await get(frontend, undefined, {
        [header]: "private-test",
        range: "bytes=0-1",
      });
      expect(result.status).toBe(200);
      expect(result.headers["cache-control"]).toBe("no-store");
      expect(result.headers["cloudflare-cdn-cache-control"]).toBe("no-store");
      expect(result.headers.etag).toBeUndefined();
      expect(calls[0].headers[header]).toBeUndefined();
      expect(calls[0].headers.range).toBeUndefined();
    },
  );

  it.each([
    "missing",
    "error",
    "redirect",
    "oversize",
    "corrupt",
    "html",
    "cookie",
    "bad304",
    "private",
    "vary",
    "slow",
  ])("fails closed for %s", async (failureMode) => {
    mode = failureMode;
    const result = await get(frontend);
    expect(result.status).toBe(mode === "missing" ? 404 : 503);
    expect(result.headers["cache-control"]).toBe("no-store");
    expect(result.headers.etag).toBeUndefined();
    expect(result.body.toString()).not.toContain("secret");
    expect(calls).toHaveLength(1);
  });

  it("bounds concurrent archive misses", async () => {
    mode = "slow";
    const first = get(frontend);
    await new Promise((resolve) => backend.once("request", resolve));
    expect((await get(frontend)).status).toBe(503);
    expect(calls).toHaveLength(1);
    expect((await first).status).toBe(503);
  });

  it("queues an ordinary burst until the preceding read completes", async () => {
    mode = "hold";
    const middleware = createRetainedAssets({
      enabled: true,
      assetRoot: root,
      apiBase,
      maxConcurrent: 1,
      maxQueued: 2,
      timeoutMs: 1000,
    });
    const queued = createServer((req, res) =>
      middleware(req, res, () => res.end()),
    );
    await listen(queued);
    try {
      const firstRead = once(backend, "request");
      const first = get(queued);
      await firstRead;
      const second = get(queued);
      // The second frontend request arrives while the first upstream is held.
      await once(queued, "request");
      expect(calls).toHaveLength(1);
      const nextRead = once(backend, "request");
      pending.shift()();
      expect((await first).status).toBe(200);
      await nextRead;
      expect(calls).toHaveLength(2);
      pending.shift()();
      expect((await second).status).toBe(200);
    } finally {
      await close(queued);
    }
  });

  it("releases a stalled downstream buffer after its flush deadline", async () => {
    const middleware = createRetainedAssets({
      enabled: true,
      assetRoot: root,
      apiBase,
      maxConcurrent: 1,
      maxQueued: 0,
      flushTimeoutMs: 20,
    });
    class Sink extends EventEmitter {
      constructor(stalled) {
        super();
        this.stalled = stalled;
      }
      writeHead(status) {
        this.status = status;
        return this;
      }
      end() {
        if (!this.stalled) {
          this.writableFinished = true;
          this.emit("finish");
        }
        return this;
      }
      destroy() {
        this.destroyed = true;
        this.emit("close");
      }
    }
    const request = {
      url: "/_app/immutable/chunks/AAAAAAAA.js",
      method: "GET",
      headers: {},
    };
    const stalled = new Sink(true);
    await middleware(request, stalled, () => {
      throw new Error("Unexpected local route");
    });
    expect(stalled.status).toBe(200);
    expect(stalled.destroyed).toBe(true);
    const next = new Sink(false);
    await middleware(request, next, () => {
      throw new Error("Unexpected local route");
    });
    expect(next.status).toBe(200);
    expect(calls).toHaveLength(2);
  });

  it("keeps local assets on the existing handler and ignores methods/query destinations", async () => {
    expect(
      (await get(frontend, "/_app/immutable/entry/start.BBBBBBBB.js")).status,
    ).toBe(418);
    expect((await get(frontend, undefined, {}, "POST")).status).toBe(418);
    expect(calls).toHaveLength(0);
    await get(
      frontend,
      "/_app/immutable/chunks/AAAAAAAA.js?url=https://other.example/private",
    );
    expect(calls[0].url).toBe(
      "/internal/public-assets/chunks/AAAAAAAA.js?encoding=identity",
    );
  });

  it("leaves private/default-disabled deployments inert", () => {
    let next = 0;
    createRetainedAssets({})(null, null, () => next++);
    expect(next).toBe(1);
  });

  it.each([
    "file:///etc",
    "https://user:secret@host",
    "https://host/api",
    "https://host/?url=x",
  ])("rejects API_BASE %s", (apiBase) => {
    expect(() =>
      createRetainedAssets({ enabled: true, assetRoot: root, apiBase }),
    ).toThrow();
  });

  it("requires exact current-release readback before startup", async () => {
    await verifyRetainedAssetRead({ assetRoot: root, apiBase });
    expect(calls[0].url).toBe(
      "/internal/public-assets/entry/start.BBBBBBBB.js?encoding=identity",
    );
    for (mode of [
      "missing",
      "error",
      "redirect",
      "corrupt",
      "html",
      "cookie",
      "oversize",
      "slow",
    ]) {
      await expect(
        verifyRetainedAssetRead({
          assetRoot: root,
          apiBase,
          timeoutMs: 30,
          maxBytes: 1024,
        }),
      ).rejects.toThrow();
    }
  });

  it("inserts one middleware chain and fails on adapter drift", () => {
    const source = "const server = polka({ server: httpServer }).use(handler);";
    expect(installRetainedAssetHandler(source)).toContain(
      ".use(retainedAssets, handler)",
    );
    expect(() => installRetainedAssetHandler("new adapter")).toThrow(
      /must be reviewed/,
    );
    expect(() => installRetainedAssetHandler(source + source)).toThrow(
      /must be reviewed/,
    );
  });
});
