// Start the real dev server, then publish readiness only after its client
// dependency batch is committed. /@vite/client alone does not load app code.
import { rename, writeFile } from "node:fs/promises";
import { parseArgs } from "node:util";
import { createLogger, createServer } from "vite";

const { values } = parseArgs({
  options: {
    "cache-dir": { type: "string" },
    "ready-file": { type: "string" },
    port: { type: "string", default: "4177" },
  },
});
if (!values["cache-dir"] || !values["ready-file"]) {
  throw new Error("--cache-dir and --ready-file are required");
}
const errors = [];
const logger = createLogger();
const logError = logger.error.bind(logger);
logger.error = (message, options) => {
  errors.push(message);
  logError(message, options);
};
const server = await createServer({
  customLogger: logger,
  cacheDir: values["cache-dir"],
  server: { host: "127.0.0.1", port: Number(values.port), strictPort: true },
});
try {
  await server.listen();
  // Use the running server's optimizer, after Svelte's buildStart hook has
  // invalidated any stale metadata. A separate `vite optimize` skips that hook.
  await server.transformRequest("/src/hooks.client.js");
  await server.waitForRequestsIdle();
  const optimizer = server.environments.client.depsOptimizer;
  if (!optimizer) throw new Error("Client dependency optimizer is unavailable");
  await optimizer.scanProcessing;
  await Promise.all(
    optimizer.metadata.depInfoList.map((dep) => dep.processing),
  );
  const { optimized, discovered } = optimizer.metadata;
  if (
    errors.length ||
    !Object.keys(optimized).length ||
    Object.keys(discovered).length
  ) {
    throw new Error("Client dependency preparation did not complete");
  }
  await writeFile(
    `${values["ready-file"]}.tmp`,
    JSON.stringify({
      url: server.resolvedUrls.local[0],
      optimized: Object.keys(optimized),
    }),
  );
  await rename(`${values["ready-file"]}.tmp`, values["ready-file"]);
  console.log("Grimoire frontend dependencies ready");
} catch (error) {
  await server.close();
  throw error;
}
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, async () => {
    await server.close();
    process.exit(0);
  });
}
