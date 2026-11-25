import assert from "node:assert/strict";
import { test } from "node:test";

const hostname = "factory-explore.jomcgi.dev";
const binding = { hostname, service: "factory-explore", id: "a".repeat(32) };
const endpoint =
  "https://api.cloudflare.com/client/v4/accounts/test/workers/domains";
const lookup = ["GET", endpoint + "?hostname=" + hostname];
process.env.CLOUDFLARE_ACCOUNT_ID = "test";
process.env.CLOUDFLARE_API_TOKEN = "test-token";

function run(t, responses) {
  const requests = [];
  const logs = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(options.headers.Authorization, "Bearer test-token");
    requests.push([options.method ?? "GET", String(url)]);
    assert.ok(responses.length, "unexpected extra request");
    return responses.shift();
  });
  t.mock.method(console, "error", (...args) => logs.push(args.join(" ")));
  return {
    requests,
    logs,
    completed: import(`./withdraw.mjs?case=${encodeURIComponent(t.name)}`),
  };
}

for (const id of ["a".repeat(32), "023e105f-4ecd-4b62-96e3-1c1f3ba5fbe0"]) {
  test(`withdraws a domain with a ${id.length}-character ID`, async (t) => {
    const { requests, completed } = run(t, [
      Response.json({ success: true, result: [{ ...binding, id }] }),
      Response.json({ success: true }),
    ]);
    await completed;
    assert.deepEqual(requests, [lookup, ["DELETE", `${endpoint}/${id}`]]);
  });
}

test("withdraws only the explorer hostname bound to the explorer Worker", async (t) => {
  const result = [
    { ...binding, hostname: "another.example.org", id: "b".repeat(32) },
    { ...binding, service: "another-worker", id: "c".repeat(32) },
    { ...binding, service: "another-worker", id: "../../zones" },
    binding,
  ];
  const { requests, completed } = run(t, [
    Response.json({ success: true, result }),
    Response.json({ success: true }),
  ]);
  await completed;
  assert.deepEqual(requests, [lookup, ["DELETE", `${endpoint}/${binding.id}`]]);
});

test("a 404 on withdrawal means the domain is already gone", async (t) => {
  const { requests, completed } = run(t, [
    Response.json({ success: true, result: [binding] }),
    new Response(null, { status: 404 }),
  ]);
  await completed;
  assert.equal(requests.length, 2);
});

for (const id of [
  "aaaaaaaa/../zones",
  "aaaaaaaa?x=1",
  "..",
  "aaaa aaaa aaaa",
  "a".repeat(7),
  "a".repeat(65),
  undefined,
  42,
]) {
  test(`refuses domain ID ${JSON.stringify(id)}`, async (t) => {
    const { requests, logs, completed } = run(t, [
      Response.json({ success: true, result: [{ ...binding, id }] }),
    ]);
    await assert.rejects(completed, /Invalid domain ID/);
    assert.deepEqual(requests, [lookup]);
    assert.equal(logs.length, 1);
    assert.match(logs[0], /^withdraw: domain ID shape (length=\d+|type=)/);
    if (typeof id === "string") assert.ok(!logs[0].includes(id));
  });
}

test("logs the ID shape, not the ID or token, when withdrawal fails", async (t) => {
  const id = "023e105f-4ecd-4b62-96e3-1c1f3ba5fbe0";
  const { logs, completed } = run(t, [
    Response.json({ success: true, result: [{ ...binding, id }] }),
    new Response("secret body", { status: 503 }),
  ]);
  await assert.rejects(completed, /withdrawal returned 503/);
  assert.deepEqual(logs, [
    "withdraw: domain ID shape length=36 classes=hex-lower,digit,hyphen",
  ]);
});

for (const [name, responses, error] of [
  [
    "lookup HTTP failure",
    [new Response(null, { status: 403 })],
    /lookup returned 403/,
  ],
  ["lookup API failure", [Response.json({ success: false })], /lookup failed/],
]) {
  test(name, async (t) => {
    await assert.rejects(run(t, responses).completed, error);
  });
}
