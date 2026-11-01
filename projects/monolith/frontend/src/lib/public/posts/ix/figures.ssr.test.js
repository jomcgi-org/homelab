// Every post figure is static: it renders on the server, with no controls,
// and states its numbers without being clicked.
import { expect, test } from "vitest";
import { render } from "svelte/server";
import { figures } from "./index.js";

const html = Object.fromEntries(
  await Promise.all(
    Object.entries(figures).map(async ([name, Figure]) => [
      name,
      (await render(Figure)).body,
    ]),
  ),
);

test("no figure has a control", () => {
  for (const [name, body] of Object.entries(html)) {
    expect(body, name).toContain("<figure");
    expect(body, name).not.toMatch(/<(button|input|select)\b/);
  }
});

test("each figure states its key numbers on the server", () => {
  const expects = {
    fit: ["134.26 GB", "VRAM 24 GB", "+ RAM 88 GB", "PLE tables"],
    residency: [
      "About 110 µs",
      "About 70 µs",
      "560 µs",
      "illustrative",
      "one-token decode",
    ],
    context: ["2,048 tokens", "262,144 tokens", "5.45 GB", "1,969 records"],
    "copy-queue": [
      "All at once",
      "8 at a time",
      "waits 41 ms",
      "9.40 s",
      "450 records, 1.2 GB",
    ],
    "draft-pays": [
      "break-even",
      "92–95% kept",
      "32–36 tok/s",
      "64–73 tok/s",
      "110–125 ms",
      "prose −2 to −9%",
      "76%",
      "83%",
    ],
    precision: [
      "--dense fp8",
      "KL 0.10–0.13, 86–88% top-1",
      "1,660",
      "decode stays FP32",
    ],
    "prefix-race": ["13.1 s", "0.95 s", "0.64 s", "1.1 GB"],
    concurrency: ["Total, 8 streams", "Each request"],
    published: [
      "74.4 tok/s",
      "179.4–192.7 tok/s",
      "45.4 tok/s",
      "21,878-token prompt",
      "not all of the gain is MTP",
    ],
  };
  expect(Object.keys(expects).sort()).toEqual(Object.keys(html).sort());
  for (const [name, wanted] of Object.entries(expects))
    for (const text of wanted) expect(html[name], name).toContain(text);
});
