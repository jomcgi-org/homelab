// Tests for fence.mjs. Run: node fence.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { isCurrent, liveMessage, releaseMessage } from "./fence.mjs";

const reply = (status, body) => async () =>
  new Response(JSON.stringify(body), { status });
const ARGS = { token: "t", account: "acct", worker: "jomcgi-site-dev" };

test("the live message is the newest deployment's workers/message", async () => {
  const fetchImpl = async (url, init) => {
    assert.equal(
      url,
      "https://api.cloudflare.com/client/v4/accounts/acct/workers/scripts/jomcgi-site-dev/deployments",
    );
    assert.equal(init.headers.Authorization, "Bearer t");
    return new Response(
      JSON.stringify({
        result: {
          deployments: [
            { annotations: { "workers/message": "jomcgi-site chart 0.8.0" } },
            { annotations: { "workers/message": "jomcgi-site chart 0.7.7" } },
          ],
        },
      }),
    );
  };
  assert.equal(await liveMessage(ARGS, fetchImpl), "jomcgi-site chart 0.8.0");
});

test("no Worker or no deployment yet is null; a deployment without a message is empty", async () => {
  assert.equal(await liveMessage(ARGS, reply(404, {})), null);
  assert.equal(
    await liveMessage(ARGS, reply(200, { result: { deployments: [] } })),
    null,
  );
  assert.equal(
    await liveMessage(
      ARGS,
      reply(200, { result: { deployments: [{ annotations: {} }] } }),
    ),
    "",
  );
});

test("an API error fails the check", async () => {
  await assert.rejects(
    liveMessage(ARGS, reply(403, {})),
    /deployments of jomcgi-site-dev: 403/,
  );
});

test("a release may deploy over its own label or a first deploy, not over another", () => {
  assert.equal(releaseMessage("0.8.0"), "jomcgi-site chart 0.8.0");
  assert.ok(isCurrent(null, "0.8.0"));
  assert.ok(isCurrent("jomcgi-site chart 0.8.0", "0.8.0"));
  assert.ok(!isCurrent("jomcgi-site chart 0.8.1", "0.8.0"));
  assert.ok(!isCurrent("jomcgi-site chart 0.7.9", "0.8.0"));
  assert.ok(!isCurrent("", "0.8.0"));
});
