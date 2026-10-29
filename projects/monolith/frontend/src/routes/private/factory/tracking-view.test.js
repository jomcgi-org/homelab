import { expect, test } from "vitest";
import { trackingSource } from "./tracking-view.js";

test.each([
  "javascript:alert(1)",
  "data:text/html,x",
  "https://user:password@example.test",
  undefined,
  "not a URL",
])("rejects unsafe source %s", (value) => {
  expect(trackingSource(value)).toBeNull();
});

test("retains safe untrusted HTTPS references", () => {
  expect(trackingSource("https://example.test/task/1")).toBe(
    "https://example.test/task/1",
  );
});
