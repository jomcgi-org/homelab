import { describe, it, expect } from "vitest";
import { browserTelemetryEnabled } from "./telemetry-gate.js";

describe("browserTelemetryEnabled", () => {
  it("keeps the exporter off the Access-gated private tier", () => {
    expect(browserTelemetryEnabled("private.jomcgi.dev")).toBe(false);
    expect(browserTelemetryEnabled("PRIVATE.jomcgi.dev")).toBe(false);
  });

  it("keeps the exporter on for the public tiers", () => {
    expect(browserTelemetryEnabled("jomcgi.dev")).toBe(true);
    expect(browserTelemetryEnabled("public.jomcgi.dev")).toBe(true);
    expect(browserTelemetryEnabled("friends.jomcgi.dev")).toBe(true);
  });

  it("keeps the exporter on for local previews", () => {
    expect(browserTelemetryEnabled("localhost")).toBe(true);
    expect(browserTelemetryEnabled("127.0.0.1")).toBe(true);
  });

  it("fails closed when the hostname is missing", () => {
    expect(browserTelemetryEnabled(undefined)).toBe(false);
    expect(browserTelemetryEnabled("")).toBe(false);
  });
});
