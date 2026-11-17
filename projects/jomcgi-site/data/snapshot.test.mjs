// Tests for snapshot.mjs. Run: node snapshot.test.mjs
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { r2Getter, signGet, snapshot } from "./snapshot.mjs";

const SOURCES = JSON.parse(
  readFileSync(new URL("./sources.json", import.meta.url), "utf8"),
);

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
  const log = await snapshot(
    {
      sections: {
        stars: SOURCES.sections.stars,
        hikes: SOURCES.sections.hikes,
      },
    },
    get,
    out,
  );
  assert.equal(log.length, 4);

  const stars = JSON.parse(
    await readFile(join(out, "stars", "latest.json"), "utf8"),
  );
  assert.equal(stars.asOf, "2026-11-17T15:07:04.000Z");
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
  assert.deepEqual(Object.keys(hikes.files), ["walks"]);
  assert.deepEqual((await readdir(join(out, "hikes", "v"))).length, 1);
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
