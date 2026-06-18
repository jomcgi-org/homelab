import { describe, it, expect } from "vitest";
import { cspDirectives } from "./csp.js";

describe("cspDirectives", () => {
  it("keeps script-src strict: no 'unsafe-inline' and no 'unsafe-eval'", () => {
    // The whole point of the CSP (ADR 005 layer 8): injected script and inline
    // event handlers must not execute, so untrusted public-chat output is inert.
    expect(cspDirectives["script-src"]).not.toContain("unsafe-inline");
    expect(cspDirectives["script-src"]).not.toContain("unsafe-eval");
    expect(cspDirectives["script-src"]).toContain("self");
  });

  it("allows the Turnstile script and challenge frame", () => {
    expect(cspDirectives["script-src"]).toContain(
      "https://challenges.cloudflare.com",
    );
    expect(cspDirectives["frame-src"]).toContain(
      "https://challenges.cloudflare.com",
    );
  });

  it("allows the OpenFreeMap basemap (maps) over connect and img", () => {
    expect(cspDirectives["connect-src"]).toContain(
      "https://tiles.openfreemap.org",
    );
    expect(cspDirectives["img-src"]).toContain("https://tiles.openfreemap.org");
  });

  it("allows MapLibre web workers (blob:)", () => {
    expect(cspDirectives["worker-src"]).toContain("blob:");
  });

  it("allows the same-origin OTEL passthrough and API calls via connect 'self'", () => {
    // /otel/v1/traces and every /api call are same-origin.
    expect(cspDirectives["connect-src"]).toContain("self");
  });

  it("allows Google Fonts (stylesheet + font files)", () => {
    expect(cspDirectives["style-src-elem"]).toContain(
      "https://fonts.googleapis.com",
    );
    expect(cspDirectives["font-src"]).toContain("https://fonts.gstatic.com");
  });

  it("keeps inline style attributes working without forcing a style nonce", () => {
    // SvelteKit only adds a style nonce when style-src has a value other than
    // 'unsafe-inline'; a nonce would make CSP3 ignore 'unsafe-inline' and break
    // every Svelte `style:` directive (maps + graph). So style-src must stay
    // exactly ['unsafe-inline'] and the attribute context must allow inline.
    expect(cspDirectives["style-src"]).toEqual(["unsafe-inline"]);
    expect(cspDirectives["style-src-attr"]).toContain("unsafe-inline");
  });
});
