import { readFile } from "node:fs/promises";
import https from "node:https";
import { isDeepStrictEqual } from "node:util";

const phase = "http_request_cache_settings";
const identity = (rule) => JSON.stringify([rule.description, rule.expression]);

// Only Cloudflare's bookkeeping is excluded. All settings and rule order count.
function settings(rule) {
  const { id, version, last_updated, ref, ...rest } = rule;
  return rest;
}

function validateFile(file) {
  if (
    !file ||
    Object.keys(file).join() !== "rules" ||
    !Array.isArray(file.rules) ||
    file.rules.some(
      (rule) =>
        !rule ||
        typeof rule.description !== "string" ||
        !rule.description ||
        typeof rule.expression !== "string" ||
        !rule.expression ||
        rule.action !== "set_cache_settings" ||
        typeof rule.enabled !== "boolean" ||
        !rule.action_parameters ||
        !isDeepStrictEqual(settings(rule), rule),
    )
  ) {
    throw new Error(
      "Invalid cache-settings file: expected rules without API metadata",
    );
  }
  if (new Set(file.rules.map(identity)).size !== file.rules.length) {
    throw new Error(
      "Duplicate description and expression in cache-settings file",
    );
  }
}

export function cloudflareClient(token, request = https.request) {
  if (!token) throw new Error("CLOUDFLARE_API_TOKEN is required");
  return (path, method = "GET", body) =>
    new Promise((resolve, reject) => {
      const req = request(
        new URL(path, "https://api.cloudflare.com/client/v4/"),
        {
          method,
          // The token is IP-filtered to the cluster's IPv4 egress address.
          family: 4,
          signal: AbortSignal.timeout(30_000),
          headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
          },
        },
        (res) => {
          let text = "";
          res.setEncoding("utf8");
          res.on("data", (chunk) => {
            text += chunk;
          });
          res.on("error", () =>
            reject(new Error(`Cloudflare ${method}: response interrupted`)),
          );
          res.on("end", () => {
            // Do not log API responses or request objects: they may contain secrets.
            if (res.statusCode < 200 || res.statusCode >= 300) {
              reject(new Error(`Cloudflare ${method}: HTTP ${res.statusCode}`));
              return;
            }
            let data;
            try {
              data = JSON.parse(text);
            } catch {
              reject(new Error(`Cloudflare ${method}: invalid JSON response`));
              return;
            }
            if (data?.success !== true || data.result == null) {
              reject(
                new Error(`Cloudflare ${method}: unsuccessful API response`),
              );
              return;
            }
            resolve(data.result);
          });
        },
      );
      req.on("error", () =>
        reject(new Error(`Cloudflare ${method}: request failed or timed out`)),
      );
      req.end(body);
    });
}

export async function applyCacheSettings({ file, accountId, client }) {
  validateFile(file);
  if (!accountId) throw new Error("CLOUDFLARE_ACCOUNT_ID is required");

  // Reuse the deploy token's Zone:Read access, without another secret field.
  const query = new URLSearchParams({
    name: "jomcgi.dev",
    "account.id": accountId,
  });
  const zones = await client(`zones?${query}`);
  if (
    !Array.isArray(zones) ||
    zones.length !== 1 ||
    zones[0].name !== "jomcgi.dev" ||
    zones[0].account?.id !== accountId ||
    !/^[a-f0-9]{32}$/.test(zones[0].id)
  ) {
    throw new Error(
      "Expected exactly one jomcgi.dev zone in the configured account",
    );
  }
  const endpoint = `zones/${zones[0].id}/rulesets/phases/${phase}/entrypoint`;
  async function readRules() {
    const live = await client(endpoint);
    if (
      live?.phase !== phase ||
      live.kind !== "zone" ||
      !Array.isArray(live.rules) ||
      live.rules.some(
        (rule) =>
          !rule ||
          typeof rule.description !== "string" ||
          typeof rule.expression !== "string",
      )
    ) {
      throw new Error("Invalid cache-settings entrypoint response");
    }
    return live.rules;
  }

  const desired = new Set(file.rules.map(identity));
  const live = await readRules();
  const unknown = live.filter((rule) => !desired.has(identity(rule)));
  if (unknown.length) {
    throw new Error(
      `Refusing to delete live rules absent from cache-settings.json: ${unknown.map((rule) => `${JSON.stringify(rule.description)} (${rule.expression})`).join(", ")}. Import them before applying.`,
    );
  }

  // Never copy IDs or versions from a GET into the PUT. Each apply sends the
  // same complete body, including the imported Monolith rule.
  await client(endpoint, "PUT", JSON.stringify(file));
  if (!isDeepStrictEqual((await readRules()).map(settings), file.rules)) {
    throw new Error(
      "Cache-settings verification failed: live rules differ from cache-settings.json",
    );
  }
}

export async function main() {
  const file = JSON.parse(
    await readFile(new URL("./cache-settings.json", import.meta.url), "utf8"),
  );
  await applyCacheSettings({
    file,
    accountId: process.env.CLOUDFLARE_ACCOUNT_ID,
    client: cloudflareClient(process.env.CLOUDFLARE_API_TOKEN),
  });
  console.log("Verified jomcgi.dev cache-settings entrypoint");
}
