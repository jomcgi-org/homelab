import { describe, it, expect } from "vitest";
import { reroute } from "./hooks.js";

function url(hostname, pathname) {
  return new URL(`https://${hostname}${pathname}`);
}

describe("reroute", () => {
  it("rewrites public.* paths under /public", () => {
    expect(reroute({ url: url("public.jomcgi.dev", "/slos") })).toBe(
      "/public/slos",
    );
  });

  it("rewrites private.* paths under /private", () => {
    expect(reroute({ url: url("private.jomcgi.dev", "/notes") })).toBe(
      "/private/notes",
    );
  });

  it("leaves /otel/* alone on subdomain hosts so browser spans reach the proxy", () => {
    expect(
      reroute({ url: url("public.jomcgi.dev", "/otel/v1/traces") }),
    ).toBeUndefined();
    expect(
      reroute({ url: url("private.jomcgi.dev", "/otel/v1/traces") }),
    ).toBeUndefined();
  });

  it("leaves already-prefixed paths alone", () => {
    expect(
      reroute({ url: url("public.jomcgi.dev", "/public/slos") }),
    ).toBeUndefined();
  });

  it("rewrites bare apex paths under /public", () => {
    expect(reroute({ url: url("jomcgi.dev", "/cv") })).toBe("/public/cv");
  });

  it("maps the apex root to /public/", () => {
    expect(reroute({ url: url("jomcgi.dev", "/") })).toBe("/public/");
  });

  it("keeps /private unreachable from the apex", () => {
    expect(reroute({ url: url("jomcgi.dev", "/private/notes") })).toBe(
      "/public/private/notes",
    );
  });

  it("leaves /otel/* alone on the apex so browser spans reach the proxy", () => {
    expect(
      reroute({ url: url("jomcgi.dev", "/otel/v1/traces") }),
    ).toBeUndefined();
  });
});
