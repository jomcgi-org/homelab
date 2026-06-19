import { describe, it, expect } from "vitest";
import { imgUrl, fullUrl } from "./images.js";

describe("trips image helpers", () => {
  it("builds a preset-resized imgproxy URL from an object key", () => {
    expect(imgUrl("img_abc.jpg", "thumb")).toBe(
      "https://img.jomcgi.dev/unsafe/rs:fit:300:300/q:85/plain/s3://monolith-trips/img_abc.jpg",
    );
    expect(imgUrl("img_abc.jpg", "gallery")).toBe(
      "https://img.jomcgi.dev/unsafe/rs:fit:600:600/q:88/plain/s3://monolith-trips/img_abc.jpg",
    );
  });

  it("exposes all four presets", () => {
    for (const preset of ["thumb", "display", "preview", "gallery"]) {
      const url = imgUrl("k", preset);
      expect(url).toContain("/unsafe/");
      expect(url).toContain("/plain/s3://monolith-trips/k");
    }
  });

  it("builds a full-resolution URL with no resize preset", () => {
    expect(fullUrl("img_abc.jpg")).toBe(
      "https://img.jomcgi.dev/unsafe/plain/s3://monolith-trips/img_abc.jpg",
    );
  });
});
