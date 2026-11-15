import adapter from "@sveltejs/adapter-node";
import { readFileSync, writeFileSync } from "node:fs";

export function installRetainedAssetHandler(source) {
  const anchor = "const server = polka({ server: httpServer }).use(handler);";
  if (source.split(anchor).length !== 2)
    throw new Error(
      "adapter-node entry changed: retained asset middleware must be reviewed",
    );
  return (
    'import { retainedAssets } from "./retained-assets.js";\n' +
    source.replace(
      anchor,
      "const server = polka({ server: httpServer }).use(retainedAssets, handler);",
    )
  );
}

// Keep adapter-node's startup, timeout and shutdown handling. The production
// entry adds an HTTP response boundary around its handler, including responses
// emitted before SvelteKit calls handle (normalization redirects and errors).
export default function cacheSafeAdapter(options = {}) {
  const node = adapter(options);
  const out = options.out || "build";
  return {
    ...node,
    async adapt(builder) {
      await node.adapt(builder);
      // Insert into Polka's one request chain before it starts listening. A
      // second async request listener would race Kit's reserved-prefix 404.
      const entry = `${out}/index.js`;
      writeFileSync(
        entry,
        installRetainedAssetHandler(readFileSync(entry, "utf8")),
      );
      builder.copy("server/retained-assets.js", `${out}/retained-assets.js`);
      builder.copy("server/cache-server.js", `${out}/cache-server.js`);
      builder.copy(
        "server/http-cache-policy.js",
        `${out}/http-cache-policy.js`,
      );
      builder.copy(
        "server/response-cache-policy.js",
        `${out}/response-cache-policy.js`,
      );
    },
  };
}
