import { describe, it, expect } from "vitest";
import { reroute } from "./hooks.js";

function url(hostname, pathname) {
  return new URL(`https://${hostname}${pathname}`);
}

describe("reroute", () => {
  it("does not map the retired public hostname", () => {
    expect(reroute({ url: url("public.jomcgi.dev", "/slos") })).toBeUndefined();
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

  it.each([
    ["private.jomcgi.dev", "/private"],
    ["friends.jomcgi.dev", "/friends"],
    ["jomcgi.dev", "/public"],
    ["localhost", "/public"],
    ["127.0.0.1", "/public"],
  ])("routes the retired telemetry path normally on %s", (host, prefix) => {
    for (const path of ["/otel/v1/traces", "/ordinary-missing-path"]) {
      expect(reroute({ url: url(host, path) })).toBe(`${prefix}${path}`);
      expect(reroute({ url: url(host, `${prefix}${path}`) })).toBeUndefined();
    }
  });

  it("leaves already-prefixed paths alone", () => {
    expect(reroute({ url: url("jomcgi.dev", "/public/slos") })).toBeUndefined();
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

  it("no longer special-cases the retired notes chat BFF paths", () => {
    // The /chat/* browser paths have no route any more (the notes chat is
    // retired); they fall through to the ordinary apex/public prefixing.
    expect(reroute({ url: url("jomcgi.dev", "/chat/session") })).toBe(
      "/public/chat/session",
    );
    expect(reroute({ url: url("jomcgi.dev", "/chat/share") })).toBe(
      "/public/chat/share",
    );
  });
});

describe("factory private routes", () => {
  it.each([
    ["/factory", "/private/factory"],
    ["/factory/execution?session=42&mode=voice", "/private/factory/execution"],
    ["/agents/factory?task=t-1", "/private/factory"],
    [
      "/agents/escalations/decisions/3",
      "/private/factory/escalations/decisions/3",
    ],
    ["/agents?run=t-1&compose=1", "/private/factory/execution"],
    [
      "/agents/session/42/messages",
      "/private/factory/execution/session/42/messages",
    ],
    ["/private/agents/vms/stream", "/private/factory/execution/vms/stream"],
    ["/private/agents/factory?task=t-1", "/private/factory"],
  ])("resolves %s without changing query parameters", (path, expected) => {
    const target = url("private.jomcgi.dev", path);
    const search = target.search;
    expect(reroute({ url: target })).toBe(expected);
    expect(target.search).toBe(search);
  });

  it.each(["jomcgi.dev", "public.jomcgi.dev", "friends.jomcgi.dev"])(
    "does not expose private compatibility routes on %s",
    (host) => {
      expect(
        reroute({ url: url(host, "/agents/factory") }) ?? "",
      ).not.toContain("/private/");
      expect(
        reroute({ url: url(host, "/private/agents/session/42") }),
      ).not.toBe("/private/factory/execution/session/42");
    },
  );

  it("does not alias an unrelated path prefix", () => {
    expect(reroute({ url: url("private.jomcgi.dev", "/agents-other") })).toBe(
      "/private/agents-other",
    );
  });
});

describe("local public previews", () => {
  it.each(["localhost", "127.0.0.1"])("keeps %s in the public tree", (host) => {
    expect(reroute({ url: url(host, "/") })).toBe("/public/");
    expect(reroute({ url: url(host, "/private/notes") })).toBe(
      "/public/private/notes",
    );
  });
});
