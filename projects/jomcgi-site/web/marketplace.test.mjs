import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const expected = {
  name: "jomcgi",
  owner: {
    name: "Joe McGinley",
    email: "joe@jomcgi.dev",
    url: "https://jomcgi.dev",
  },
  description: "Plugins from jomcgi.dev.",
  plugins: [
    {
      name: "scotland",
      source: {
        source: "git-subdir",
        url: "https://github.com/jomcgi-org/homelab.git",
        path: "projects/scotland-connector/plugin",
        ref: "main",
      },
      description:
        "Plan and check Scottish walks and stargazing with forecast verdicts from https://scotland-mcp.jomcgi.dev/mcp.",
      category: "travel",
    },
  ],
};

assert.equal(
  readFileSync(join(process.argv[2], "scotland/marketplace.json"), "utf8"),
  JSON.stringify(expected, null, 2) + "\n",
);
