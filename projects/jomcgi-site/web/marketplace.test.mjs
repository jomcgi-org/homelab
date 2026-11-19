import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
const catalog = JSON.parse(
  readFileSync(join(process.argv[2], "scotland/marketplace.json"), "utf8"),
);
assert.equal(catalog.name, "jomcgi");
const plugin = catalog.plugins.find((entry) => entry.name === "scotland");
assert.deepEqual(plugin.source, {
  source: "git-subdir",
  url: "https://github.com/jomcgi-org/homelab.git",
  path: "projects/scotland-connector/plugin",
  ref: "main",
});
assert.ok(plugin.description.includes("https://scotland-mcp.jomcgi.dev/mcp"));
