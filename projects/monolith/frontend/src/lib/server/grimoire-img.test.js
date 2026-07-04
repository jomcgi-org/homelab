import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { createHmac } from "node:crypto";
import { signedGrimImgUrl } from "./grimoire-img.js";

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

describe("grimoire imgproxy URL signer", () => {
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
    const path = "/display/plain/s3://grimoire/books/mm/raw/img/abc.jpg";
    const sig = expectedSig(path, KEY, SALT);
    expect(signedGrimImgUrl("books/mm/raw/img/abc.jpg", "display")).toBe(
      `/img/${sig}${path}`,
    );
    // base64url, no padding, URL-safe alphabet only.
    expect(sig).toMatch(/^[A-Za-z0-9_-]+$/);
    expect(sig).not.toContain("=");
  });

  it("puts the /img prefix before the signature and the unprefixed path after", () => {
    setEnv(KEY, SALT);
    const url = signedGrimImgUrl("books/mm/raw/img/abc.jpg", "full");
    expect(url).toMatch(
      /^\/img\/[A-Za-z0-9_-]+\/full\/plain\/s3:\/\/grimoire\/books\/mm\/raw\/img\/abc\.jpg$/,
    );
  });

  it("defaults to the display preset when omitted or unknown", () => {
    setEnv(KEY, SALT);
    const path = "/display/plain/s3://grimoire/books/mm/raw/img/k.jpg";
    expect(signedGrimImgUrl("books/mm/raw/img/k.jpg", "nope")).toBe(
      `/img/${expectedSig(path, KEY, SALT)}${path}`,
    );
    expect(signedGrimImgUrl("books/mm/raw/img/k.jpg")).toBe(
      `/img/${expectedSig(path, KEY, SALT)}${path}`,
    );
  });

  it("produces different signatures for different keys", () => {
    setEnv(KEY, SALT);
    const a = signedGrimImgUrl("books/mm/raw/img/k.jpg", "display");
    setEnv("aabbccddeeff00112233445566778899", SALT); // gitleaks:allow
    const b = signedGrimImgUrl("books/mm/raw/img/k.jpg", "display");
    expect(a).not.toBe(b);
  });

  it("falls back to an unsigned /unsafe/ URL when the secret is absent", () => {
    setEnv(undefined, undefined);
    expect(signedGrimImgUrl("books/mm/raw/img/abc.jpg", "full")).toBe(
      "/img/unsafe/full/plain/s3://grimoire/books/mm/raw/img/abc.jpg",
    );
    expect(signedGrimImgUrl("books/mm/raw/img/abc.jpg")).toBe(
      "/img/unsafe/display/plain/s3://grimoire/books/mm/raw/img/abc.jpg",
    );
  });
});
