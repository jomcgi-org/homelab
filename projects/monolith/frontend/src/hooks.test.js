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

  it("rewrites the friends moving path under /friends", () => {
    expect(reroute({ url: url("friends.jomcgi.dev", "/moving") })).toBe(
      "/friends/moving",
    );
  });

  it("does not resolve the apex moving path to the friends tree", () => {
    expect(reroute({ url: url("jomcgi.dev", "/moving") })).toBe(
      "/public/moving",
    );
  });

  it("does not resolve the private moving path to the friends tree", () => {
    expect(reroute({ url: url("private.jomcgi.dev", "/moving") })).toBe(
      "/private/moving",
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

  it("maps the chat BFF paths to /public/chat/* on any host", () => {
    expect(reroute({ url: url("jomcgi.dev", "/chat/session") })).toBe(
      "/public/chat/session",
    );
    expect(reroute({ url: url("jomcgi.dev", "/chat/message") })).toBe(
      "/public/chat/message",
    );
    expect(reroute({ url: url("jomcgi.dev", "/chat/share") })).toBe(
      "/public/chat/share",
    );
    expect(reroute({ url: url("jomcgi.dev", "/chat/fork") })).toBe(
      "/public/chat/fork",
    );
    // Host-independent: the same browser path resolves under /public on the
    // public subdomain too.
    expect(reroute({ url: url("public.jomcgi.dev", "/chat/share") })).toBe(
      "/public/chat/share",
    );
  });
});
