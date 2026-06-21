import { spawn } from "node:child_process";
import { startMock } from "./mock-server.mjs";

const APP_ENTRY = process.env.APP_ENTRY;
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
  const app = spawn("node", [APP_ENTRY], {
    env: {
      ...process.env,
      PORT: String(APP_PORT),
      API_BASE: `http://127.0.0.1:${MOCK_PORT}`,
      ORIGIN: `http://127.0.0.1:${APP_PORT}`,
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
