import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { expect, it } from "vitest";

const require = createRequire(import.meta.url);

it("handles canceled animation promises while preserving their rejection for consumers", () => {
  // Svelte and Astryx cancel animations through events without consuming
  // `finished`. The cancel algorithm marks that rejection handled:
  // https://drafts.csswg.org/web-animations-1/#cancel-an-animation
  const result = execFileSync(
    process.execPath,
    [
      "--unhandled-rejections=strict",
      "--input-type=module",
      "-e",
      `
      import assert from "node:assert/strict";
      import { setImmediate as nextTask } from "node:timers/promises";
      const { Window } = await import(process.argv[1]);
      const window = new Window();
      const element = window.document.createElement("div");
      const animation = element.animate([{ opacity: 0 }, { opacity: 1 }], 60_000);
      let canceled = 0;
      animation.oncancel = () => { canceled += 1; };
      const finished = animation.finished;
      animation.cancel();
      // Let unhandled rejections surface before attaching a consumer.
      await nextTask();
      await assert.rejects(finished, { name: "AbortError" });
      assert.equal(animation.playState, "idle");
      assert.equal(animation.currentTime, null);

      animation.play();
      const restarted = animation.finished;
      assert.notEqual(restarted, finished);
      animation.cancel();
      await nextTask();
      await assert.rejects(restarted, { name: "AbortError" });
      assert.equal(canceled, 2);

      animation.play();
      const completed = animation.finished;
      animation.finish();
      assert.equal(await completed, animation);
      assert.equal(animation.playState, "finished");
      await window.happyDOM.close();
      console.log(JSON.stringify({ canceled, completed: true }));
      `,
      pathToFileURL(require.resolve("happy-dom")).href,
    ],
    { encoding: "utf8", timeout: 5000 },
  );
  expect(JSON.parse(result)).toEqual({ canceled: 2, completed: true });
});
