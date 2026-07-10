import { spawn } from "node:child_process";
import { join } from "node:path";
import { startMock } from "./mock-server.mjs";

// APP_ENTRY is execroot-relative under Bazel (bazel-out/.../bin/...), but the
// js_run_binary action runs with cwd under <execroot>/bazel-out/..., so a bare
// relative path would double the prefix. Resolve to absolute via the execroot
// (cwd cut at /bazel-out/) when running under Bazel; use as-is locally.
const _appEntry = process.env.APP_ENTRY;
const APP_ENTRY =
  _appEntry && process.cwd().includes("/bazel-out/")
    ? join(process.cwd().split("/bazel-out/")[0], _appEntry)
    : _appEntry;
const APP_PORT = Number(process.env.APP_PORT || 3000);
const MOCK_PORT = Number(process.env.MOCK_PORT || 8099);

async function waitFor(url, ms = 30000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    try {
      // Any HTTP response (even a 404) means the listener is up and accepting
      // requests, which is all readiness needs. The public tier only serves
      // routes under /public (the apex reroute is hostname-gated and does not
      // fire on 127.0.0.1), so a bare /cv legitimately 404s; do not require .ok.
      // Short per-attempt timeout satisfies the repo's fetch-no-timeout semgrep
      // rule and turns a hung connection into a retried poll.
      await fetch(url, { signal: AbortSignal.timeout(2000) });
      return;
    } catch {}
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`timeout waiting for ${url}`);
}

export async function serve() {
  const mock = await startMock(MOCK_PORT);
  // process.execPath, not "node": under Bazel the node runtime comes from the
  // hermetic toolchain (in runfiles), and the apko exec image has no system
  // node on PATH, so a bare "node" would not be found.
  const app = spawn(process.execPath, [APP_ENTRY], {
    env: {
      ...process.env,
      PORT: String(APP_PORT),
      API_BASE: `http://127.0.0.1:${MOCK_PORT}`,
      ORIGIN: `http://127.0.0.1:${APP_PORT}`,
      // Dummy site key so TurnstileGate mounts the widget instead of
      // rendering its "Chat is unavailable" fallback (no env var = no
      // widget). The real Cloudflare script + admission POST are
      // intercepted at the Playwright layer in capture.mjs, so this value
      // never has to resolve to anything real.
      TURNSTILE_SITE_KEY: "1x00000000000000000000AA",
    },
    stdio: "inherit",
  });
  await waitFor(`http://127.0.0.1:${APP_PORT}/cv`);
  return {
    base: `http://127.0.0.1:${APP_PORT}`,
    async stop() {
      app.kill("SIGTERM");
      mock.close();
    },
  };
}
