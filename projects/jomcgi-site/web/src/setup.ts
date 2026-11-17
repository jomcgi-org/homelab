import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

beforeEach(() => {
  // Pre-render/hydration and registry checks also mount data-reading pages.
  // Tests opt into fixture responses; none should contact the network.
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Not available")));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
