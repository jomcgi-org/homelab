// @vitest-environment happy-dom
import { describe, expect, it } from "vitest";
import { reroute } from "./hooks.js";

describe("public screenshot browser navigation", () => {
  it("resolves unprefixed links and proxy paths from the internal browser origin", () => {
    window.happyDOM.setURL(
      "http://monolith-public-frontend.monolith-public.svc.cluster.local:3000/app/hikes",
    );
    for (const href of [
      "/app/stars",
      "/app/stars/history-map",
      "/app/trips?view=map",
    ]) {
      const link = document.createElement("a");
      link.href = href;
      const target = new URL(link.href);
      expect(reroute({ url: target })).toBe(`/public${target.pathname}`);
    }
    expect(
      reroute({ url: new URL("/private/factory", window.location.href) }),
    ).toBe("/public/private/factory");
    expect(
      reroute({ url: new URL("/friends/moving", window.location.href) }),
    ).toBe("/public/friends/moving");
  });
});
