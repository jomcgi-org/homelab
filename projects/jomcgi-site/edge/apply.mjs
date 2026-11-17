import { readFile } from "node:fs/promises";
import https from "node:https";
import { isDeepStrictEqual } from "node:util";
import { snapshotStore } from "./state.mjs";

const phase = "http_request_cache_settings";
const identity = (rule) => JSON.stringify([rule.description, rule.expression]);

// Rule IDs, refs, versions and timestamps are not cache policy.
function settings(rule) {
  const { id, version, last_updated, ref, ...rest } = rule;
  return rest;
}

function matches(live, desired) {
  return isDeepStrictEqual(live.map(settings), desired);
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

function validateSnapshot(saved, accountId) {
  if (
    !saved ||
    typeof saved !== "object" ||
    saved.schema !== 1 ||
    saved.accountId !== accountId ||
    !/^[a-f0-9]{32}$/.test(saved.zoneId) ||
    typeof saved.active !== "boolean"
  )
    throw new Error("Invalid or foreign edge snapshot; refusing to write");
  validateFile(saved.original);
  validateFile(saved.applied);
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

export async function applyCacheSettings({
  file,
  imported,
  accountId,
  client,
  state,
  mode = "apply",
}) {
  if (!["apply", "restore"].includes(mode))
    throw new Error("Mode must be apply or restore");
  validateFile(file);
  validateFile(imported);
  if (
    !imported.rules.length ||
    !isDeepStrictEqual(
      file.rules.slice(0, imported.rules.length),
      imported.rules,
    )
  ) {
    throw new Error(
      "The desired file must retain the imported rules unchanged and in order",
    );
  }
  if (!accountId) throw new Error("CLOUDFLARE_ACCOUNT_ID is required");
  let saved = await state.read();
  if (saved) validateSnapshot(saved, accountId);
  if (mode === "restore" && !saved) {
    throw new Error(
      "Cannot restore without the durable prior-entrypoint snapshot",
    );
  }

  // Resolve with the deploy token; a denied zone lookup fails before any PUT.
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
  if (saved && saved.zoneId !== zones[0].id) {
    throw new Error(
      "Snapshot zone differs from the live zone; refusing to write",
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

  const live = await readRules();
  if (mode === "restore") {
    if (!saved.active) return;
    if (!matches(live, saved.original.rules)) {
      if (!matches(live, saved.applied.rules)) {
        throw new Error(
          "Refusing to restore: live rules differ from the saved original and last applied policies",
        );
      }
      await client(endpoint, "PUT", JSON.stringify(saved.original));
      if (!matches(await readRules(), saved.original.rules)) {
        throw new Error(
          "Cache-settings verification failed: live rules differ from the snapshot",
        );
      }
    }
    await state.write({ ...saved, active: false });
    return;
  }

  const desired = new Set(file.rules.map(identity));
  const unknown = live.filter((rule) => !desired.has(identity(rule)));
  if (unknown.length) {
    throw new Error(
      `Refusing to delete live rules absent from cache-settings.json: ${unknown.map((rule) => `${JSON.stringify(rule.description)} (${rule.expression})`).join(", ")}. Import them before applying.`,
    );
  }

  for (const [index, rule] of imported.rules.entries()) {
    if (!live[index] || !isDeepStrictEqual(settings(live[index]), rule)) {
      throw new Error(
        `Refusing to change imported rule ${JSON.stringify(rule.description)}: live settings or order differ from the HOME-192 import`,
      );
    }
  }
  // Persist the ACTUAL original policy before the first write, including a
  // pre-existing data rule. Repeated applies retain that original snapshot.
  saved = {
    schema: 1,
    accountId,
    zoneId: zones[0].id,
    active: true,
    original: saved?.active ? saved.original : { rules: live.map(settings) },
    applied: file,
  };
  // Refuse an original policy we could not safely read back and restore.
  validateSnapshot(saved, accountId);
  await state.write(saved);

  // Never copy IDs or versions from a GET into the PUT. Each apply sends the
  // same complete body, including the imported Monolith rule.
  await client(endpoint, "PUT", JSON.stringify(file));
  if (!matches(await readRules(), file.rules)) {
    throw new Error(
      "Cache-settings verification failed: live rules differ from the target",
    );
  }
}

export async function reconcileCacheSettings({ intentPath, ...options }) {
  let intent;
  try {
    intent = (await readFile(intentPath, "utf8")).trim();
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (intent === "true") return;
  if (intent !== undefined && intent !== "false") {
    throw new Error("Invalid edge intent; refusing to restore");
  }
  const saved = await options.state.read();
  if (!saved) return; // Initial sync has not written anything yet.
  validateSnapshot(saved, options.accountId);
  if (saved.active) await applyCacheSettings({ ...options, mode: "restore" });
}

export async function main(args = process.argv.slice(2)) {
  if (
    args.length > 1 ||
    (args.length === 1 && !["apply", "restore", "reconcile"].includes(args[0]))
  ) {
    throw new Error("Usage: apply [apply|restore|reconcile]");
  }
  const file = JSON.parse(
    await readFile(new URL("./cache-settings.json", import.meta.url), "utf8"),
  );
  const imported = JSON.parse(
    await readFile(
      new URL("./imported/2026-11-17.json", import.meta.url),
      "utf8",
    ),
  );
  const options = {
    file,
    imported,
    mode: args[0] ?? "apply",
    accountId: process.env.CLOUDFLARE_ACCOUNT_ID,
    client: cloudflareClient(process.env.CLOUDFLARE_API_TOKEN),
    state: snapshotStore("/var/lib/jomcgi-site-edge"),
  };
  if (options.mode === "reconcile") {
    await reconcileCacheSettings({
      ...options,
      intentPath: "/var/run/edge-intent/edgeApply",
    });
  } else {
    await applyCacheSettings(options);
  }
  console.log(
    `Completed jomcgi.dev cache-settings step (${args[0] ?? "apply"})`,
  );
}
