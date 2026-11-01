import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { signedDocsImgUrl } from "./docs-img.js";

const KEY = "00112233445566778899aabbccddeeff"; // gitleaks:allow
const SALT = "ffeeddccbbaa99887766554433221100"; // gitleaks:allow
const FIXTURE_URL =
  "/img/upygl4dnKMLkvX4Z3hIomrknexC9YOG2FZ5A1Wjaaio/full/plain/s3://docs-assets/fixture.jpg";

beforeEach(() => {
  vi.stubEnv("IMGPROXY_KEY", KEY);
  vi.stubEnv("IMGPROXY_SALT", SALT);
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("real docs imgproxy wrapper", () => {
  it("matches the fixed signature without mocking the signer", () => {
    expect(signedDocsImgUrl("fixture.jpg")).toBe(FIXTURE_URL);
  });

  it("always uses full even when callers pass extra preset arguments", () => {
    for (const preset of ["display", "gallery", "nope", undefined, null]) {
      expect(signedDocsImgUrl("fixture.jpg", preset)).toBe(FIXTURE_URL);
    }
  });

  it.each(["IMGPROXY_KEY", "IMGPROXY_SALT"])(
    "falls back when %s is missing and recovers without re-import",
    (name) => {
      vi.stubEnv(name, undefined);
      expect(signedDocsImgUrl("folder/雪 image.jpg", "display")).toBe(
        "/img/unsafe/full/plain/s3://docs-assets/folder/雪 image.jpg",
      );
      vi.stubEnv(name, name === "IMGPROXY_KEY" ? KEY : SALT);
      expect(signedDocsImgUrl("fixture.jpg")).toBe(FIXTURE_URL);
    },
  );
});
