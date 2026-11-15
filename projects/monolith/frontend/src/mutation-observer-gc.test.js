import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { expect, it } from "vitest";

const require = createRequire(import.meta.url);

it("keeps a connected DOM observer alive through GC and releases it on disconnect", () => {
  // A fresh Node process exposes deterministic GC without changing the test
  // runner's globals. Use the same pinned DOM package as the page tests.
  const result = execFileSync(
    process.execPath,
    [
      "--expose-gc",
      "--input-type=module",
      "-e",
      `
      import assert from "node:assert/strict";
      import { setImmediate as nextTask } from "node:timers/promises";
      const { Window } = await import(process.argv[1]);
      const window = new Window();
      const root = window.document.documentElement;
      let calls = 0;
      let observer = new window.MutationObserver(() => { calls += 1; });
      observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
      root.dataset.theme = "dark";
      await window.happyDOM.waitUntilComplete();
      assert.equal(calls, 1, "initial observation");

      // WeakRefs are protected until the current job ends. Cross a task
      // boundary before collecting, as a busy full-suite run naturally does.
      await nextTask();
      globalThis.gc();
      root.dataset.theme = "light";
      await window.happyDOM.waitUntilComplete();
      assert.equal(calls, 2, "connected observer must survive GC");

      observer.disconnect();
      const released = new WeakRef(observer);
      observer = null;
      await nextTask();
      globalThis.gc();
      assert.equal(released.deref(), undefined, "disconnected observer can be collected");
      root.dataset.theme = "dark";
      await window.happyDOM.waitUntilComplete();
      assert.equal(calls, 2, "disconnected observer must not receive mutations");
      await window.happyDOM.close();
      console.log(JSON.stringify({ calls, released: true }));
      `,
      pathToFileURL(require.resolve("happy-dom")).href,
    ],
    { encoding: "utf8", timeout: 5000 },
  );
  expect(JSON.parse(result)).toEqual({ calls: 2, released: true });
});
