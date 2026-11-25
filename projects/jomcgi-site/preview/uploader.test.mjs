// Tests for uploader.mjs and github.mjs. Run: node --test uploader.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { tarGz } from "./tar-fixture.mjs";
import { CACHE_MS, GitHubError, publicGitHub } from "./github.mjs";
import { Refused, anonymousDenied, handlePreview } from "./uploader.mjs";

const SHA = "a".repeat(40);
const OTHER = "b".repeat(40);
const ALIAS = "https://pr-7-jomcgi-site-dev.example.workers.dev";
const archive = tarGz([{ path: "index.html", data: "<!doctype html>" }]);

const pull = (overrides = {}) => ({
  state: "open",
  user: { login: "jomcgi-factory[bot]" },
  head: { sha: SHA, repo: { full_name: "jomcgi-org/homelab" } },
  base: { repo: { full_name: "jomcgi-org/homelab" } },
  ...overrides,
});

const accessRedirect = () =>
  new Response(null, {
    status: 302,
    headers: {
      location: "https://jomcgi.cloudflareaccess.com/cdn-cgi/access/login/x",
    },
  });

const TOKEN = { clientId: "id", clientSecret: "secret" };

// The alias host: Access redirects anonymous requests and admits the token.
const gated = (_url, init) =>
  init.headers?.["CF-Access-Client-Id"] === "id"
    ? new Response("ok")
    : accessRedirect();

// A fake world: GitHub answers in order (a pull, or an Error to throw), the
// alias host's answers, uploads.
function world({ pulls = [pull()], gate = gated, uploaded = ALIAS } = {}) {
  const calls = { uploads: [], gate: [], pulls: 0 };
  const queue = [...pulls];
  return {
    calls,
    deps: {
      github: {
        pull: async () => {
          calls.pulls += 1;
          const next = queue.length > 1 ? queue.shift() : queue[0];
          if (next instanceof Error) throw next;
          return next;
        },
      },
      subdomain: async () => "example",
      upload: async (request) => {
        calls.uploads.push(request);
        return uploaded;
      },
      fetchImpl: async (url, init) => {
        calls.gate.push({ url, headers: init.headers ?? {} });
        return typeof gate === "function" ? gate(url, init) : gate;
      },
      access: TOKEN,
    },
  };
}

const rejects = async (promise, status, pattern) =>
  assert.rejects(promise, (error) => {
    assert.ok(error instanceof Refused, error);
    assert.equal(error.status, status);
    assert.match(error.message, pattern);
    return true;
  });

test("a trusted PR's head is uploaded as pr-<N> and its URL returned", async () => {
  const { deps, calls } = world();
  const result = await handlePreview(
    { pr: "7", sha: SHA, body: archive },
    deps,
  );
  assert.deepEqual(result, { url: `${ALIAS}/`, sha: SHA });
  assert.equal(calls.uploads.length, 1);
  assert.equal(calls.uploads[0].pr, "7");
  assert.ok(calls.uploads[0].files.has("index.html"));
  // The PR was checked before and after the upload.
  assert.equal(calls.pulls, 2);
  // Anonymous before and after the upload, then the service token.
  assert.deepEqual(
    calls.gate.map((c) => [c.url, c.headers["CF-Access-Client-Id"] ?? null]),
    [
      [`${ALIAS}/`, null],
      [`${ALIAS}/`, null],
      [`${ALIAS}/`, "id"],
    ],
  );
});

test("no service token means no upload", async () => {
  const { deps, calls } = world();
  delete deps.access;
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
    503,
    /no Access service token/,
  );
  assert.equal(calls.uploads.length, 0);
});

test("a GitHub refusal fails closed, before or after the upload", async () => {
  const limited = new GitHubError(
    503,
    "GitHub refused the PR lookup (HTTP 403, anonymous rate limit)",
  );
  const before = world({ pulls: [limited] });
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, before.deps),
    503,
    /rate limit/,
  );
  assert.equal(before.calls.uploads.length, 0);
  const after = world({ pulls: [pull(), limited] });
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, after.deps),
    503,
    /rate limit/,
  );
  assert.equal(after.calls.uploads.length, 1);
});

test("a PR that changes author or closes during the upload returns no URL", async () => {
  for (const [late, status, pattern] of [
    [pull({ state: "closed" }), 409, /not open/],
    [pull({ user: { login: "someone" } }), 403, /not trusted/],
  ]) {
    const { deps } = world({ pulls: [pull(), late] });
    await rejects(
      handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
      status,
      pattern,
    );
  }
});

test("Joe's own PRs are trusted too", async () => {
  const { deps } = world({ pulls: [pull({ user: { login: "jomcgi" } })] });
  await handlePreview({ pr: "7", sha: SHA, body: archive }, deps);
});

test("a SHA that is not the PR head is refused before any upload", async () => {
  const { deps, calls } = world();
  await rejects(
    handlePreview({ pr: "7", sha: OTHER, body: archive }, deps),
    409,
    /not the head/,
  );
  assert.equal(calls.uploads.length, 0);
});

test("malformed pr and sha are refused", async () => {
  const { deps } = world();
  await rejects(
    handlePreview({ pr: "7; rm", sha: SHA, body: archive }, deps),
    400,
    /pr/,
  );
  await rejects(
    handlePreview({ pr: "7", sha: "abc", body: archive }, deps),
    400,
    /full commit/,
  );
  await rejects(
    handlePreview({ pr: null, sha: SHA, body: archive }, deps),
    400,
    /pr/,
  );
});

test("untrusted authors, forks and closed PRs are refused", async () => {
  for (const [overrides, status, pattern] of [
    [{ user: { login: "renovate[bot]" } }, 403, /not trusted/],
    [{ user: { login: "someone" } }, 403, /not trusted/],
    [
      { head: { sha: SHA, repo: { full_name: "someone/homelab" } } },
      403,
      /fork/,
    ],
    [{ head: { sha: SHA, repo: null } }, 403, /fork/],
    [{ state: "closed" }, 409, /not open/],
  ]) {
    const { deps, calls } = world({ pulls: [pull(overrides)] });
    await rejects(
      handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
      status,
      pattern,
    );
    assert.equal(calls.uploads.length, 0);
  }
});

test("a Worker script in the archive is refused before any upload", async () => {
  const { deps, calls } = world();
  const bad = tarGz([
    { path: "index.html", data: "x" },
    { path: "_worker.js", data: "export default {}" },
  ]);
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: bad }, deps),
    422,
    /Worker code/,
  );
  assert.equal(calls.uploads.length, 0);
});

test("an alias host that Access does not protect gets no upload", async () => {
  for (const gate of [
    () => new Response("site", { status: 200 }),
    () => new Response("", { status: 404 }),
    () =>
      new Response(null, {
        status: 302,
        headers: { location: "https://evil.example/login" },
      }),
    () => {
      throw new Error("network");
    },
  ]) {
    const { deps, calls } = world({ gate });
    await rejects(
      handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
      503,
      /does not protect/,
    );
    assert.equal(calls.uploads.length, 0);
  }
});

test("a preview that answers anonymously after upload returns no URL", async () => {
  let n = 0;
  const { deps, calls } = world({
    gate: () =>
      n++ === 0 ? accessRedirect() : new Response("site", { status: 200 }),
  });
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
    503,
    /withheld/,
  );
  assert.equal(calls.uploads.length, 1);
});

test("the service token must open the preview", async () => {
  const gate = (_url, init) =>
    init.headers?.["CF-Access-Client-Id"]
      ? new Response("no", { status: 403 })
      : accessRedirect();
  const { deps } = world({ gate });
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
    502,
    /service token/,
  );
});

test("an unexpected alias URL from wrangler is refused", async () => {
  const { deps } = world({ uploaded: null });
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
    502,
    /previews may be off/,
  );
});

test("a PR that moves during the upload returns no URL", async () => {
  const { deps } = world({
    pulls: [
      pull(),
      pull({ head: { sha: OTHER, repo: { full_name: "jomcgi-org/homelab" } } }),
    ],
  });
  await rejects(
    handlePreview({ pr: "7", sha: SHA, body: archive }, deps),
    409,
    /moved/,
  );
});

test("anonymousDenied accepts only Access redirects and 401/403", async () => {
  const at = (response) =>
    anonymousDenied("https://x.example/", async () => response);
  assert.equal(await at(accessRedirect()), true);
  assert.equal(await at(new Response(null, { status: 403 })), true);
  assert.equal(await at(new Response(null, { status: 401 })), true);
  assert.equal(await at(new Response("page")), false);
  assert.equal(
    await at(
      new Response(null, {
        status: 302,
        headers: { location: "http://a.cloudflareaccess.com/" },
      }),
    ),
    false,
  );
  assert.equal(
    await at(
      new Response(null, {
        status: 302,
        headers: { location: "https://cloudflareaccess.com.evil/" },
      }),
    ),
    false,
  );
});

test("publicGitHub sends no credential and revalidates with the ETag", async () => {
  const requests = [];
  let answer;
  const fetchImpl = async (url, init) => {
    requests.push({ url, headers: init.headers });
    return answer();
  };
  let clock = 0;
  const client = publicGitHub({
    repository: "jomcgi-org/homelab",
    fetchImpl,
    now: () => clock,
  });
  answer = () =>
    new Response(JSON.stringify(pull()), { headers: { etag: '"v1"' } });
  assert.deepEqual(await client.pull("7"), pull());
  assert.equal(
    requests[0].url,
    "https://api.github.com/repos/jomcgi-org/homelab/pulls/7",
  );
  assert.equal(requests[0].headers.Authorization, undefined);
  assert.equal(requests[0].headers["If-None-Match"], undefined);
  // A 304 does not count against the anonymous limit; the kept body is used.
  answer = () => new Response(null, { status: 304 });
  assert.deepEqual(await client.pull("7"), pull());
  assert.equal(requests[1].headers["If-None-Match"], '"v1"');
  // A changed PR comes back whole with a new ETag.
  const moved = pull({
    head: { sha: OTHER, repo: { full_name: "jomcgi-org/homelab" } },
  });
  answer = () =>
    new Response(JSON.stringify(moved), { headers: { etag: '"v2"' } });
  assert.deepEqual(await client.pull("7"), moved);
  // Past the cache time the ETag is dropped and the next read is plain.
  clock += CACHE_MS + 1;
  answer = () => new Response(JSON.stringify(pull()));
  await client.pull("7");
  assert.equal(requests[3].headers["If-None-Match"], undefined);
});

test("publicGitHub fails closed on 403, 429, 404, 5xx and no answer", async () => {
  for (const [response, status, pattern] of [
    [
      () =>
        new Response("{}", {
          status: 403,
          headers: { "x-ratelimit-reset": "1800000000" },
        }),
      503,
      /HTTP 403, anonymous rate limit until 2027-01-15T08:00:00\.000Z.*no preview URL/,
    ],
    [
      () => new Response("{}", { status: 429 }),
      503,
      /HTTP 429.*no preview URL/,
    ],
    [() => new Response("{}", { status: 404 }), 404, /does not exist/],
    [() => new Response("{}", { status: 502 }), 502, /HTTP 502/],
    [
      () => {
        throw new Error("offline");
      },
      503,
      /did not answer/,
    ],
  ]) {
    const client = publicGitHub({
      repository: "jomcgi-org/homelab",
      fetchImpl: async () => response(),
    });
    await assert.rejects(client.pull("7"), (error) => {
      assert.ok(error instanceof GitHubError, error);
      assert.equal(error.status, status);
      assert.match(error.message, pattern);
      return true;
    });
  }
});

test("wranglerUpload writes the files beside main's config and reads the alias URL", async () => {
  const { mkdtemp, writeFile, chmod, readFile, readdir } =
    await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { wranglerUpload } = await import("./uploader.mjs");
  const root = await mkdtemp(
    join(process.env.TEST_TMPDIR ?? tmpdir(), "preview-test-"),
  );
  const config = join(root, "main-wrangler.jsonc");
  await writeFile(config, '{ "name": "jomcgi-site" }');
  const record = join(root, "record.json");
  // The fake wrangler records its arguments, config and files, then writes
  // the output record the real one writes.
  const fake = join(root, "wrangler");
  const script = join(root, "wrangler.cjs");
  await writeFile(
    script,
    `const fs = require("fs");
const path = require("path");
const config = process.argv[process.argv.indexOf("--config") + 1];
fs.writeFileSync(${JSON.stringify(record)}, JSON.stringify({
  args: process.argv.slice(2),
  config: fs.readFileSync(config, "utf8"),
  index: fs.readFileSync(path.join(path.dirname(config), "web/dist/index.html"), "utf8"),
  nested: fs.readFileSync(path.join(path.dirname(config), "web/dist/assets/a.js"), "utf8"),
}));
fs.writeFileSync(process.env.WRANGLER_OUTPUT_FILE_PATH,
  JSON.stringify({ type: "wrangler-session" }) + "\\n" +
  JSON.stringify({ type: "version-upload", preview_alias_url: ${JSON.stringify(ALIAS)} }) + "\\n");
`,
  );
  // Bazel's test PATH may have no node, so call this one.
  await writeFile(
    fake,
    `#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(script)} "$@"\n`,
  );
  await chmod(fake, 0o755);
  const upload = wranglerUpload({
    wrangler: fake,
    configSource: config,
    workRoot: root,
    env: { PATH: process.env.PATH },
  });
  const files = new Map([
    ["index.html", Buffer.from("<p>hi")],
    ["assets/a.js", Buffer.from("1")],
  ]);
  assert.equal(await upload({ files, pr: "7", sha: SHA }), ALIAS);
  const seen = JSON.parse(await readFile(record, "utf8"));
  assert.equal(seen.config, '{ "name": "jomcgi-site" }');
  assert.equal(seen.index, "<p>hi");
  assert.equal(seen.nested, "1");
  for (const pair of [
    ["--env", "dev"],
    ["--preview-alias", "pr-7"],
  ]) {
    assert.equal(seen.args[seen.args.indexOf(pair[0]) + 1], pair[1]);
  }
  assert.deepEqual(seen.args.slice(0, 2), ["versions", "upload"]);
  // The work directory is removed afterwards.
  assert.deepEqual((await readdir(root)).sort(), [
    "main-wrangler.jsonc",
    "record.json",
    "wrangler",
    "wrangler.cjs",
  ]);
});
