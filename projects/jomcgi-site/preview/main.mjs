// Entry point of the PR preview uploader (README "PR previews").
// It listens on loopback only, so the one way in is
// `kubectl port-forward pod/jomcgi-site-preview-0`, which RBAC grants to
// the factory session identities (platform-factory/kube-access).
//
//   POST /previews?pr=<N>&sha=<40 hex>   body: gzip tar of web/dist
//   -> 200 {"url", "sha"} or 4xx/5xx {"error"}; preview.sh writes the comment
//
// Env: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, ACCESS_CLIENT_ID and
// ACCESS_CLIENT_SECRET (the factory service token). It reads GitHub without
// a credential. WRANGLER_BIN,
// WRANGLER_CONFIG, PORT and WORK_ROOT have image defaults.
import { createServer } from "node:http";
import { publicGitHub } from "./github.mjs";
import { LIMITS } from "./archive.mjs";
import {
  Refused,
  handlePreview,
  workersSubdomain,
  wranglerUpload,
} from "./uploader.mjs";

const need = (name) => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} must be set`);
  return value;
};

const cloudflare = {
  token: need("CLOUDFLARE_API_TOKEN"),
  account: need("CLOUDFLARE_ACCOUNT_ID"),
};
const access = {
  clientId: need("ACCESS_CLIENT_ID"),
  clientSecret: need("ACCESS_CLIENT_SECRET"),
};
const deps = {
  github: publicGitHub({ repository: "jomcgi-org/homelab" }),
  subdomain: () => workersSubdomain(cloudflare),
  upload: wranglerUpload({
    wrangler:
      process.env.WRANGLER_BIN ?? "/app/projects/jomcgi-site/wrangler/wrangler",
    // main's config: the image is built from main, never from the PR.
    configSource: process.env.WRANGLER_CONFIG ?? "/app/site/wrangler.jsonc",
    workRoot: process.env.WORK_ROOT ?? "/tmp",
    // The rules_js launcher needs the image environment; the service token
    // is not wrangler's business.
    env: Object.fromEntries(
      Object.entries(process.env).filter(
        ([name]) => !name.startsWith("ACCESS_CLIENT_"),
      ),
    ),
  }),
  access,
};

// Compressed bodies above this are refused before they are read in full.
const MAX_BODY = LIMITS.totalBytes;
let busy = false;

function reply(response, status, value) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(`${JSON.stringify(value)}\n`);
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY)
      throw new Refused(413, `the archive is larger than ${MAX_BODY} bytes`);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", "http://localhost");
  if (request.method !== "POST" || url.pathname !== "/previews") {
    reply(response, 404, { error: "POST /previews?pr=<N>&sha=<sha>" });
    return;
  }
  if (busy) {
    reply(response, 429, {
      error: "another preview is uploading; retry in a minute",
    });
    request.resume();
    return;
  }
  busy = true;
  const pr = url.searchParams.get("pr");
  const sha = url.searchParams.get("sha");
  try {
    const result = await handlePreview(
      { pr, sha, body: await readBody(request) },
      deps,
    );
    console.log(`preview PR #${pr} ${sha}: ${result.url}`);
    reply(response, 200, result);
  } catch (error) {
    const status = error instanceof Refused ? error.status : 500;
    const message =
      error instanceof Refused
        ? error.message
        : "internal error; see the uploader log";
    console.error(`preview PR #${pr} ${sha}: ${status} ${error.message}`);
    reply(response, status, { error: message });
  } finally {
    busy = false;
  }
}).listen(Number(process.env.PORT ?? 8080), "127.0.0.1", () => {
  console.log("preview uploader listening on 127.0.0.1");
});
