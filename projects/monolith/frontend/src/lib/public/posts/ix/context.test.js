import { expect, test } from "vitest";
import { attentionBytes, kvCache, rowBytes } from "./data-context.js";

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
