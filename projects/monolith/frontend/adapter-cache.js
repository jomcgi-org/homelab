import adapter from "@sveltejs/adapter-node";

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
