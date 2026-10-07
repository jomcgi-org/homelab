// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { mount, tick, unmount } from "svelte";
import Context from "./Context.svelte";
import {
  attentionBytes,
  kvCache,
  rowBytes,
  sliderToTokens,
  tokensToSlider,
} from "./data-context.js";

test("row and layer sizes follow the engine's formulas", () => {
  expect([rowBytes(8), rowBytes(6), rowBytes(0)]).toEqual([272, 208, 1024]);
  // k8v6: 2 heads x (272 + 208) + fp32 indexer keys and block keys.
  expect(attentionBytes(4096, "k8v6") / 4096).toBe(1600);
  expect(attentionBytes(4096, "fp32") / 4096).toBe(4736);
});

test("a full 256k context: about 5.45 GB of KV cache, ~2,000 records", () => {
  const kv = kvCache(262_144);
  expect((kv.bytes / 1e9).toFixed(2)).toBe("5.45");
  expect(kv.records).toBe(1969);
});

test("the slider spans 2k to 256k tokens on a log scale", () => {
  expect(sliderToTokens(0)).toBe(2048);
  expect(sliderToTokens(1000)).toBe(262_144);
  expect(tokensToSlider(32_768)).toBe(571);
});

test("the figure renders and the KV cache follows the slider", async () => {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Context, { target });
  await tick();
  const range = target.querySelector('input[type="range"]');
  const set = async (v) => {
    range.value = String(v);
    range.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    return target.querySelector(".allocated").textContent;
  };
  expect(await set(1000)).toBe("5.45 GB");
  expect(await set(0)).toBe("0.04 GB");
  unmount(component);
  target.remove();
});
