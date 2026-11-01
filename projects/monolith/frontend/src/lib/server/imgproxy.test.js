import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHmac } from "node:crypto";

const KEY = "00112233445566778899aabbccddeeff"; // gitleaks:allow
const SALT = "ffeeddccbbaa99887766554433221100"; // gitleaks:allow
const ROTATED = "1234567890abcdef1234567890abcdef"; // gitleaks:allow

// Independent oracle signs the concatenated byte buffer, never the helper.
function expectedUrl(path, key = KEY, salt = SALT) {
  const signature = createHmac("sha256", Buffer.from(key, "hex"))
    .update(
      Buffer.concat([Buffer.from(salt, "hex"), Buffer.from(path, "utf8")]),
    )
    .digest("base64url");
  return `/img/${signature}${path}`;
}

function credentials(key, salt) {
  vi.stubEnv("IMGPROXY_KEY", key);
  vi.stubEnv("IMGPROXY_SALT", salt);
}

let subjects;
let warn;

beforeEach(async () => {
  // Fresh module instances isolate the warning set without production hooks.
  vi.resetModules();
  credentials(KEY, SALT);
  warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  const { signedUrl } = await import("./imgproxy.js");
  const { signedImgUrl } = await import("./trips-img.js");
  const { signedGrimImgUrl } = await import("./grimoire-img.js");
  const { signedDocsImgUrl } = await import("./docs-img.js");
  subjects = [
    {
      label: "helper-img",
      prefix: "s3://fixture/",
      fallback: "full",
      presets: ["full", "display"],
      sign: (key, preset) =>
        signedUrl({
          prefix: "s3://fixture/",
          preset,
          presets: new Set(["full", "display"]),
          fallback: "full",
          key,
          label: "helper-img",
        }),
    },
    {
      label: "trips-img",
      prefix: "s3://monolith-trips/",
      fallback: "gallery",
      presets: ["thumb", "gallery", "preview", "display", "full"],
      sign: signedImgUrl,
    },
    {
      label: "grimoire-img",
      prefix: "s3://grimoire/",
      fallback: "display",
      presets: ["display", "full"],
      sign: signedGrimImgUrl,
    },
    {
      label: "docs-img",
      prefix: "s3://docs-assets/",
      fallback: "full",
      presets: ["full"],
      sign: signedDocsImgUrl,
    },
  ];
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("shared imgproxy helper and real wrappers", () => {
  it("matches independent signatures for defaults, invalid names and allowed presets", () => {
    for (const { sign, prefix, fallback, presets } of subjects) {
      for (const invalid of [undefined, "nope", "../full", "", null]) {
        expect(sign("fixture.jpg", invalid)).toBe(
          expectedUrl(`/${fallback}/plain/${prefix}fixture.jpg`),
        );
      }
      for (const preset of presets) {
        const url = sign("fixture.jpg", preset);
        expect(url).toBe(expectedUrl(`/${preset}/plain/${prefix}fixture.jpg`));
        expect(url.split("/")[2]).toMatch(/^[A-Za-z0-9_-]{43}$/);
      }
    }
    expect(warn).not.toHaveBeenCalled();
  });

  it("pins the literal fixed docs vector through the helper", async () => {
    const { signedUrl } = await import("./imgproxy.js");
    expect(
      signedUrl({
        prefix: "s3://docs-assets/",
        preset: "full",
        presets: new Set(["full"]),
        fallback: "full",
        key: "fixture.jpg",
        label: "docs-img",
      }),
    ).toBe(
      "/img/upygl4dnKMLkvX4Z3hIomrknexC9YOG2FZ5A1Wjaaio/full/plain/s3://docs-assets/fixture.jpg",
    );
  });

  it("preserves spaces, Unicode, repeated slashes and dot segments byte for byte", () => {
    const key = "folder with spaces/雪/café//../fixture.jpg";
    for (const { sign, prefix, fallback } of subjects) {
      const path = `/${fallback}/plain/${prefix}${key}`;
      expect(sign(key)).toBe(expectedUrl(path));
      credentials(undefined, undefined);
      expect(sign(key)).toBe(`/img/unsafe${path}`);
      credentials(KEY, SALT);
    }
  });

  it.each([
    [undefined, SALT],
    [KEY, undefined],
    [undefined, undefined],
    ["", SALT],
    [KEY, ""],
  ])("uses unsafe URLs with missing credentials (%s, %s)", (key, salt) => {
    credentials(key, salt);
    for (const { sign, prefix, fallback } of subjects) {
      expect(sign("fixture.jpg")).toBe(
        `/img/unsafe/${fallback}/plain/${prefix}fixture.jpg`,
      );
    }
  });

  it("reads key and salt rotations and missing-to-present transitions after import", () => {
    for (const { sign, prefix, fallback } of subjects) {
      const path = `/${fallback}/plain/${prefix}fixture.jpg`;
      credentials(KEY, SALT);
      expect(sign("fixture.jpg")).toBe(expectedUrl(path));
      credentials(ROTATED, SALT);
      expect(sign("fixture.jpg")).toBe(expectedUrl(path, ROTATED, SALT));
      credentials(ROTATED, ROTATED);
      expect(sign("fixture.jpg")).toBe(expectedUrl(path, ROTATED, ROTATED));
      credentials(undefined, ROTATED);
      expect(sign("fixture.jpg")).toBe(`/img/unsafe${path}`);
      credentials(KEY, SALT);
      expect(sign("fixture.jpg")).toBe(expectedUrl(path));
    }
  });

  it("warns once independently per label across repeated calls and credential changes", () => {
    for (const { sign } of subjects) {
      credentials(undefined, SALT);
      sign("fixture.jpg");
      sign("another.jpg");
      credentials(KEY, SALT);
      sign("fixture.jpg");
      credentials(KEY, undefined);
      sign("fixture.jpg");
      credentials(undefined, undefined);
      sign("fixture.jpg");
    }
    expect(warn.mock.calls).toEqual(
      subjects.map(({ label }) => [
        `[${label}] IMGPROXY_KEY/IMGPROXY_SALT unset, falling back to unsigned /img/unsafe/ URLs (local dev only).`,
      ]),
    );
    const output = JSON.stringify(warn.mock.calls);
    for (const secret of [KEY, SALT, ROTATED]) {
      expect(output).not.toContain(secret);
    }
  });
});
