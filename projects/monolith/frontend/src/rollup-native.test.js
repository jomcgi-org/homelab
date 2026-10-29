import { createRequire } from "node:module";
import { expect, it } from "vitest";

const workspaceRequire = createRequire(import.meta.url);
const viteRequire = createRequire(
  workspaceRequire.resolve("vite/package.json"),
);

it("keeps the Bazel workspace native parser aligned with Vite's Rollup", () => {
  // Bazel omits Rollup's optional native dependency from its isolated package
  // tree. The patched loader then uses this explicitly linked workspace copy.
  // Different versions can silently misinterpret the serialized AST.
  const rollup = viteRequire("rollup/package.json");
  const native = workspaceRequire("@rollup/rollup-linux-x64-gnu/package.json");
  expect(native.version).toBe(rollup.version);
});
