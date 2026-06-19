import { describe, it, expect } from "vitest";
import { imgUrl, fullUrl } from "./images.js";

describe("trips image helpers", () => {
  it("builds a named-preset imgproxy URL from an object key", () => {
    expect(imgUrl("img_abc.jpg", "thumb")).toBe(
      "https://img.jomcgi.dev/unsafe/thumb/plain/s3://monolith-trips/img_abc.jpg",
    );
    expect(imgUrl("img_abc.jpg", "gallery")).toBe(
      "https://img.jomcgi.dev/unsafe/gallery/plain/s3://monolith-trips/img_abc.jpg",
    );
  });

  it("exposes all named presets", () => {
    for (const preset of ["thumb", "gallery", "preview", "display", "full"]) {
      const url = imgUrl("k", preset);
      expect(url).toBe(
        `https://img.jomcgi.dev/unsafe/${preset}/plain/s3://monolith-trips/k`,
      );
    }
  });

  it("falls back to the gallery preset for an unknown name", () => {
    expect(imgUrl("k", "nope")).toBe(
      "https://img.jomcgi.dev/unsafe/gallery/plain/s3://monolith-trips/k",
    );
    expect(imgUrl("k")).toBe(
      "https://img.jomcgi.dev/unsafe/gallery/plain/s3://monolith-trips/k",
    );
  });

  it("builds a full-resolution URL via the full preset", () => {
    expect(fullUrl("img_abc.jpg")).toBe(
      "https://img.jomcgi.dev/unsafe/full/plain/s3://monolith-trips/img_abc.jpg",
    );
  });
});
