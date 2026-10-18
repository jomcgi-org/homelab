// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";
import { flushSync, mount, unmount } from "svelte";
import Page from "./+page.svelte";
const NOW = "2026-10-03T12:00:00.000Z";

vi.mock("$lib/public/components", async () => ({
  Seo: (await import("../../../../lib/public/components/Seo.svelte")).default,
  SchemeToggle: (
    await import("../../../../lib/public/components/SchemeToggle.svelte")
  ).default,
}));

let mounted;
afterEach(() => {
  if (mounted) unmount(mounted);
  mounted = null;
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const payloads = {
  "/slop/factory/data/activity": {
    now: {},
    daily: [],
    local_daily: [],
    spend_daily: [{ day: "2026-09-07", spend_usd: 12.75 }],
    totals_7d: { ember: {}, local: {}, combined: { spend_usd: 12.75 } },
  },
  "/slop/factory/merges": { daily: [], week: [], totals: {} },
  "/slop/factory/facts": {
    daily: [],
    totals: { verified: 1, unverified: 2, disputed: 0 },
    contradictions: 0,
  },
  "/slop/factory/goals": { goals: [], declared_at: null, stale: true },
  "/slop/factory/data/board": {
    state: "enabled",
    policy: { generation: 7 },
    active: [{ issue_number: 5980 }],
    queued: [],
    recent: [],
  },
};

function response(path, ok = true) {
  return {
    ok,
    headers: { get: () => null },
    json: async () => payloads[path],
  };
}

describe("factory overview loader", () => {
  it("loads live inputs through same-origin proxies", async () => {
    const fetch = vi.fn((path) => Promise.resolve(response(path)));
    const result = await load({ fetch, setHeaders: vi.fn() });

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/slop/factory/data/activity",
      "/slop/factory/merges",
      "/slop/factory/facts",
      "/slop/factory/goals",
      "/slop/factory/data/board",
    ]);
    expect(result.activity).toEqual(payloads["/slop/factory/data/activity"]);
    expect(result.merges).toEqual(payloads["/slop/factory/merges"]);
    expect(result.facts).toEqual(payloads["/slop/factory/facts"]);
    expect(result.goals).toEqual(payloads["/slop/factory/goals"]);
    expect(result.board).toEqual(payloads["/slop/factory/data/board"]);
  });

  it.each([
    ["/slop/factory/data/activity", "activity"],
    ["/slop/factory/merges", "merges"],
    ["/slop/factory/facts", "facts"],
    ["/slop/factory/goals", "goals"],
    ["/slop/factory/data/board", "board"],
  ])("keeps rendering when %s returns 503", async (failedPath, section) => {
    const fetch = vi.fn((path) =>
      Promise.resolve(response(path, path !== failedPath)),
    );

    const result = await load({ fetch, setHeaders: vi.fn() });

    expect(result.unavailable[section]).toBe(true);
    expect(result.title).toBe("Factory");
    if (section === "activity") {
      expect(result.activity).toMatchObject({
        spend_daily: [],
        totals_7d: { combined: {} },
      });
    }
    if (section === "board") {
      expect(result.board).toMatchObject({ active: [], recent: [] });
    }
  });
});

describe("subscription quota display", () => {
  async function mountOverview() {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW));
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        disconnect() {}
      },
    );
    const ago = (minutes) =>
      new Date(Date.parse(NOW) - minutes * 60000).toISOString();
    const providers = ["codex", "claude"].map((provider) => ({
      provider,
      source: provider === "codex" ? "accounts" : "provider",
      configured_accounts: 2,
      inventory_complete: provider === "codex",
      pacing:
        provider === "codex"
          ? {
              complete: true,
              reported_accounts: 2,
              state_counts: { use_more: 2 },
              evaluated_at: NOW,
              oldest_observed_at: ago(2),
              valid_until: ago(-120),
              budget_window_seconds: 604800,
              budget_remaining_percent: 58,
              budget_per_day_percent: 16.9,
              short_window_seconds: 18000,
              short_next_reset_at: ago(-120),
              short_last_reset_at: ago(-120),
            }
          : null,
      windows: [
        {
          window_seconds: 604800,
          reported_accounts: 2,
          used_percent: 42,
          remaining_seconds: 302400,
          complete: provider === "codex",
          next_reset_at: ago(-4320),
          last_reset_at: ago(-5760),
          oldest_observed_at: ago(provider === "codex" ? 2 : 120),
        },
      ],
    }));
    const history = providers.map(({ provider }) => ({
      provider,
      window_seconds: 604800,
      points: [
        {
          sampled_at: ago(120),
          used_percent: 35,
          elapsed_percent: 48,
          fresh: true,
        },
        {
          sampled_at: ago(2),
          used_percent: 42,
          elapsed_percent: 50,
          fresh: true,
        },
      ],
    }));
    const endpoints = {
      ...payloads,
      "/slop/factory/data/board": {
        ...payloads["/slop/factory/data/board"],
        active: [],
        quota: {
          available: true,
          sampled_at: NOW,
          stale_after_seconds: 900,
          providers,
          history,
        },
      },
    };
    const data = await load({
      fetch: async (path) => new Response(JSON.stringify(endpoints[path])),
      setHeaders: vi.fn(),
    });
    const target = document.createElement("div");
    document.body.append(target);
    mounted = mount(Page, { target, props: { data } });
    flushSync();
    return target;
  }
  it("leads with pace and meters, keeping charts and evidence in a closed disclosure", async () => {
    const target = await mountOverview();
    const panel = target.querySelector('[aria-label="Subscription pace"]');
    expect(
      [...panel.querySelectorAll("h3")].map((node) => node.textContent),
    ).toEqual(["Codex", "Claude"]);
    expect(panel.textContent).toContain("42% used");
    expect(panel.textContent).toContain(
      "Weekly: 58% left · recommended 16.9 points/day",
    );
    expect(panel.textContent).toContain("5-hour reset in 2h");
    expect(panel.querySelector("details").textContent).toContain(
      "provider-reported times",
    );
    expect(panel.textContent).toContain("Room to use more");
    expect(panel.textContent).toContain("50% target");
    expect(panel.textContent).toContain("Reset in 3d–4d");
    expect(panel.textContent).toContain(
      "Provider report · account coverage unknown",
    );
    expect(panel.textContent).toContain("stale");
    expect(panel.querySelector("details").open).toBe(false);
    expect(panel.querySelectorAll("details svg.quota-history")).toHaveLength(2);
    expect(panel.querySelectorAll(".quota-providers svg")).toHaveLength(0);
    expect(panel.querySelectorAll(".quota-meter")).toHaveLength(2);
    expect(panel.querySelector(".quota-target").style.left).toBe("50%");
  });
  it("uses the response-time clock for a snapshot published during refresh", async () => {
    const target = await mountOverview();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        vi.setSystemTime(new Date(Date.now() + 1000));
        const sampledAt = new Date().toISOString();
        return new Response(
          JSON.stringify({
            quota: {
              available: true,
              sampled_at: sampledAt,
              stale_after_seconds: 900,
              providers: [
                {
                  provider: "codex",
                  source: "accounts",
                  configured_accounts: 2,
                  inventory_complete: true,
                  windows: [
                    {
                      window_seconds: 18000,
                      reported_accounts: 2,
                      used_percent: 43,
                      remaining_seconds: 7200,
                      complete: true,
                      oldest_observed_at: sampledAt,
                      next_reset_at: new Date(
                        Date.now() + 7200000,
                      ).toISOString(),
                      last_reset_at: new Date(
                        Date.now() + 7200000,
                      ).toISOString(),
                    },
                  ],
                },
              ],
            },
          }),
        );
      }),
    );
    await vi.advanceTimersByTimeAsync(60000);
    flushSync();
    const panel = target.querySelector('[aria-label="Subscription pace"]');
    expect(panel.textContent).toContain("43% used");
    expect(panel.textContent).not.toContain("Awaiting quota report");
    expect(panel.querySelector(".quota-provider").textContent).not.toContain(
      "Pace unavailable",
    );
  });
  it("refreshes each minute and preserves the last report when fetching fails", async () => {
    const target = await mountOverview();
    const fetch = vi.fn().mockResolvedValue(new Response("", { status: 503 }));
    vi.stubGlobal("fetch", fetch);
    await vi.advanceTimersByTimeAsync(60000);
    flushSync();
    expect(fetch).toHaveBeenCalledWith(
      "/slop/factory/data/board",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(target.textContent).toContain("Refresh unavailable");
    expect(target.textContent).toContain("42% used");
  });
});
