import { describe, it, expect } from "vitest";
import { PRESETS } from "./images.js";

describe("trips image presets", () => {
  it("exposes the named presets imgproxy is locked to", () => {
    // Must match IMGPROXY_PRESETS in the monolith-public chart values.
    for (const preset of ["thumb", "gallery", "preview", "display", "full"]) {
      expect(PRESETS.has(preset)).toBe(true);
    }
    expect(PRESETS.size).toBe(5);
  });
});
