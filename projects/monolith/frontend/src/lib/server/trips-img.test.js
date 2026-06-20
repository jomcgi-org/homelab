import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { createHmac } from "node:crypto";
import { signedImgUrl, signedFullUrl } from "./trips-img.js";

// Hex-encoded fake key/salt (the real ones are random bytes from 1Password).
const KEY = "00112233445566778899aabbccddeeff"; // gitleaks:allow
const SALT = "ffeeddccbbaa99887766554433221100"; // gitleaks:allow

// Independent reference implementation of the imgproxy signature so the test
// pins the exact bytes, not just the shape: HMAC-SHA256(key=hex(KEY)) over
// hex(SALT) ++ utf8(path), base64url, no padding.
function expectedSig(path, key, salt) {
  const h = createHmac("sha256", Buffer.from(key, "hex"));
  h.update(Buffer.from(salt, "hex"));
  h.update(path);
  return h.digest("base64url");
}

function setEnv(key, salt) {
  if (key === undefined) delete process.env.IMGPROXY_KEY;
  else process.env.IMGPROXY_KEY = key;
  if (salt === undefined) delete process.env.IMGPROXY_SALT;
  else process.env.IMGPROXY_SALT = salt;
}

describe("trips imgproxy URL signer", () => {
  let savedKey;
  let savedSalt;

  beforeEach(() => {
    savedKey = process.env.IMGPROXY_KEY;
    savedSalt = process.env.IMGPROXY_SALT;
  });
  afterEach(() => {
    setEnv(savedKey, savedSalt);
  });

  it("signs a known path to a stable signature", () => {
    setEnv(KEY, SALT);
    const path = "/display/plain/s3://monolith-trips/img_x.jpg";
    const sig = expectedSig(path, KEY, SALT);
    expect(signedImgUrl("img_x.jpg", "display")).toBe(`/img/${sig}${path}`);
    // base64url, no padding, URL-safe alphabet only.
    expect(sig).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(sig).not.toContain("=");
  });

  it("puts the /img prefix before the signature and the unprefixed path after", () => {
    setEnv(KEY, SALT);
    const url = signedImgUrl("img_abc.jpg", "gallery");
    expect(url).toMatch(
      /^\/img\/[A-Za-z0-9_-]+\/gallery\/plain\/s3:\/\/monolith-trips\/img_abc\.jpg$/,
    );
  });

  it("falls back to the gallery preset for an unknown name", () => {
    setEnv(KEY, SALT);
    const path = "/gallery/plain/s3://monolith-trips/k";
    expect(signedImgUrl("k", "nope")).toBe(
      `/img/${expectedSig(path, KEY, SALT)}${path}`,
    );
    expect(signedImgUrl("k")).toBe(
      `/img/${expectedSig(path, KEY, SALT)}${path}`,
    );
  });

  it("signedFullUrl uses the full preset", () => {
    setEnv(KEY, SALT);
    const path = "/full/plain/s3://monolith-trips/img_abc.jpg";
    expect(signedFullUrl("img_abc.jpg")).toBe(
      `/img/${expectedSig(path, KEY, SALT)}${path}`,
    );
  });

  it("produces different signatures for different keys", () => {
    setEnv(KEY, SALT);
    const a = signedImgUrl("k", "display");
    setEnv("aabbccddeeff00112233445566778899", SALT); // gitleaks:allow
    const b = signedImgUrl("k", "display");
    expect(a).not.toBe(b);
  });

  it("falls back to an unsigned /unsafe/ URL when the secret is absent", () => {
    setEnv(undefined, undefined);
    expect(signedImgUrl("img_abc.jpg", "thumb")).toBe(
      "/img/unsafe/thumb/plain/s3://monolith-trips/img_abc.jpg",
    );
    expect(signedFullUrl("img_abc.jpg")).toBe(
      "/img/unsafe/full/plain/s3://monolith-trips/img_abc.jpg",
    );
  });
});
