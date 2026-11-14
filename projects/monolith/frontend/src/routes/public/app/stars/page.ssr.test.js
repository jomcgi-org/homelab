import { describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import {
  data,
  now,
  twilightOnlySite,
} from "../../../../../test/stars/fixtures.js";
import { ssr } from "./+page.js";

describe("/app/stars SSR", () => {
  it("opts into server rendering", () => {
    expect(ssr).toBe(true);
  });

  it("renders the H1 and description paragraph server-side", async () => {
    const { html } = await render(Page, {
      props: { data: { snapshot: { sites: [], count: 0 } } },
    });

    expect(html).toContain("Dark-sky stargazing map, Scotland viewing windows");
    expect(html.replace(/\s+/g, " ")).toContain(
      "Scotland's dark-sky sites ranked by forecast clear, dark hours.",
    );
  });

  it("renders actual snapshot rows without a browser fetch", async () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    try {
      const { html } = await render(Page, {
        props: { data: { ...data, renderedAt: Date.parse(now) } },
      });
      expect(html).toContain("Dark-sky forecast snapshot");
      expect(html).toContain("Fixture site 1");
      expect(html).toContain("9 dark-sky sites");
      expect(html.match(/<li class="snapshot-row/g)).toHaveLength(6);
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      fetch.mockRestore();
    }
  });

  it("uses the serialized clock even when a cached response is rendered later", async () => {
    const clock = vi
      .spyOn(Date, "now")
      .mockReturnValue(Date.parse(now) + 86400000);
    try {
      const { html } = await render(Page, {
        props: { data: { ...data, renderedAt: Date.parse(now) } },
      });
      expect(html.replace(/\s+/g, " ")).toContain("updated 0m ago");
    } finally {
      clock.mockRestore();
    }
  });

  it.each(["astronomical", "twilight", "none"])(
    "uses the map's %s darkness predicate for initial snapshot rows",
    async (darkness) => {
      const { html } = await render(Page, {
        props: {
          data: {
            ...data,
            renderedAt: Date.parse(now),
            snapshot: {
              ...data.snapshot,
              darkness,
              sites: [twilightOnlySite, ...data.snapshot.sites],
            },
          },
        },
      });
      expect(html.includes(twilightOnlySite.name)).toBe(
        darkness === "twilight",
      );
      expect(html).toContain("Fixture site 1");
      expect(html.match(/<li class="snapshot-row/g)).toHaveLength(6);
    },
  );

  it("identifies unnamed forecast cells by their real coordinates", async () => {
    const { html } = await render(Page, {
      props: {
        data: {
          ...data,
          renderedAt: Date.parse(now),
          snapshot: {
            ...data.snapshot,
            sites: [{ ...data.snapshot.sites[0], name: null }],
          },
        },
      },
    });
    expect(html).toContain("56.350°, -4.700°");
    expect(html).toContain("8 clear dark hours");
  });
});
