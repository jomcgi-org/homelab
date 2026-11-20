import assert from "node:assert/strict";
import { test } from "node:test";

const hostname = "factory-explore.jomcgi.dev";
const binding = { hostname, service: "factory-explore", id: "a".repeat(32) };
const endpoint =
  "https://api.cloudflare.com/client/v4/accounts/test/workers/domains";
process.env.CLOUDFLARE_ACCOUNT_ID = "test";
process.env.CLOUDFLARE_API_TOKEN = "test-token";

function run(t, responses) {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(options.headers.Authorization, "Bearer test-token");
    requests.push([options.method ?? "GET", String(url)]);
    assert.ok(responses.length, "unexpected extra request");
    return responses.shift();
  });
  return {
    requests,
    completed: import(`./withdraw.mjs?case=${encodeURIComponent(t.name)}`),
  };
}

test("withdraws only the explorer hostname bound to the explorer Worker", async (t) => {
  const result = [
    { ...binding, hostname: "another.example.org", id: "b".repeat(32) },
    { ...binding, service: "another-worker", id: "c".repeat(32) },
    binding,
  ];
  const { requests, completed } = run(t, [
    Response.json({ success: true, result }),
    Response.json({ success: true }),
  ]);
  await completed;
  assert.deepEqual(requests, [
    ["GET", endpoint + "?hostname=" + hostname],
    ["DELETE", endpoint + "/" + binding.id],
  ]);
});

for (const [name, responses, error] of [
  [
    "lookup HTTP failure",
    [new Response(null, { status: 403 })],
    /lookup returned 403/,
  ],
  ["lookup API failure", [Response.json({ success: false })], /lookup failed/],
  [
    "withdrawal failure",
    [
      Response.json({ success: true, result: [binding] }),
      new Response(null, { status: 503 }),
    ],
    /withdrawal returned 503/,
  ],
]) {
  test(name, async (t) => {
    await assert.rejects(run(t, responses).completed, error);
  });
}
