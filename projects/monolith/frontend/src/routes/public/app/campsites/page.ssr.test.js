import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { snapshot } from "../../../../../campsites-preview/fixtures.js";

vi.mock("maplibre-gl", () => {
  throw new Error("SSR must not import the WebGL runtime");
});

describe("campsites server shell", () => {
  it("renders the existing title, description and collapsed controls without WebGL", async () => {
    expect(typeof window).toBe("undefined");
    expect(typeof document).toBe("undefined");
    const { body, head } = await render(Page, {
      props: { data: { snapshot } },
    });
    expect(body).toContain("td sheet campsites-page");
    expect(body).toContain(
      "BC Parks campsites, open sites and clear-sky weather",
    );
    expect(body).toContain("All nights");
    expect(body).toContain("Ranked parks");
    expect(body).toContain(
      "Green means the site is open and the sky is forecast clear.",
    );
    expect(body).toContain('aria-expanded="false"');
    expect(head).toContain("A full-screen map of BC Parks campsites");
    expect(body).not.toContain("maplibregl-canvas");
  });
});
