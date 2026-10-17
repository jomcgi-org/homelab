// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import KnowledgeSearch from "./KnowledgeSearch.svelte";
import { settle, typeInto } from "./test-helpers.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const base = `/grimoire/campaigns/${campaignId}`;
const response = (body, status = 200) =>
  new Response(JSON.stringify(body), { status });
let instance;
async function render() {
  instance = mount(KnowledgeSearch, {
    target: document.body,
    props: { campaignId },
  });
  await settle();
}
async function search(q = " clues & +/雪? ") {
  await typeInto(document.querySelector('input[type="search"]'), q);
  document
    .querySelector('form[role="search"]')
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await settle();
}
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

describe("character knowledge search", () => {
  it("renders own and party facts without lore links and disputes only an own fact", async () => {
    const own = {
      type: "fact",
      id: "own-fact",
      preview: "My clue",
      ownership: "you",
      can_dispute: true,
      status: "active",
      source: { fact_id: "own-fact" },
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response([
          own,
          {
            ...own,
            id: "party-fact",
            preview: "Party clue",
            ownership: "party",
            can_dispute: false,
          },
        ]),
      )
      .mockResolvedValueOnce(response({ status: "disputed" }));
    vi.stubGlobal("fetch", fetch);
    await render();
    await search();
    expect(document.body.textContent).toContain("My clue");
    expect(document.body.textContent).toContain("Party clue");
    expect(document.querySelectorAll("li a")).toHaveLength(0);
    const buttons = [...document.querySelectorAll("li button")];
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toContain("Dispute fact");
    buttons[0].click();
    await settle();
    expect(fetch).toHaveBeenLastCalledWith(`${base}/facts/own-fact/dispute`, {
      method: "POST",
      cache: "no-store",
    });
    expect(document.body.textContent).toContain("Disputed");
    expect(document.body.textContent).toContain(
      "Excluded from your assistant's context.",
    );
    expect(document.querySelectorAll("li button")).toHaveLength(0);
  });
  it("keeps an unsuccessful dispute retryable and hides provider errors", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response([
          {
            type: "fact",
            id: "f",
            preview: "Clue",
            ownership: "you",
            can_dispute: true,
            status: "active",
            source: { fact_id: "f" },
          },
        ]),
      )
      .mockResolvedValueOnce(response({ error: "OTHER-PC-SECRET" }, 404));
    vi.stubGlobal("fetch", fetch);
    await render();
    await search();
    document.querySelector("li button").click();
    await settle();
    expect(document.querySelector('[role="alert"]').textContent).toContain(
      "could not be disputed",
    );
    expect(document.body.textContent).not.toContain("OTHER-PC-SECRET");
    expect(document.querySelector("li button").disabled).toBe(false);
  });
  it("labels the input and submits a trimmed query to the campaign BFF without caching", async () => {
    const fetch = vi.fn().mockResolvedValue(response([]));
    vi.stubGlobal("fetch", fetch);
    await render();
    const input = document.querySelector('input[type="search"]');
    expect(document.querySelector(`label[for="${input.id}"]`).textContent).toBe(
      "Search what your character knows",
    );
    expect(input.maxLength).toBe(200);
    expect(input.required).toBe(true);
    expect(document.querySelector('button[type="submit"]').textContent).toBe(
      "Search knowledge",
    );
    expect(fetch).not.toHaveBeenCalled();
    await search();
    expect(fetch).toHaveBeenCalledWith(
      `${base}/knowledge/search?q=clues+%26+%2B%2F%E9%9B%AA%3F`,
      expect.objectContaining({
        cache: "no-store",
        signal: expect.any(AbortSignal),
      }),
    );
  });
  it("renders every typed badge, authorized preview and source href and announces the count", async () => {
    const rows = [
      {
        type: "entity",
        id: "entity-a",
        name: "Hidden door",
        revealed_details: { clue: "A silver hinge" },
        source: { entity_id: "entity-a" },
      },
      {
        type: "note",
        id: "note-a",
        title: "My clue",
        preview: "Note words",
        source: { note_id: "note-a" },
      },
      {
        type: "event",
        id: "event-a",
        preview: "Event words",
        source: { session_id: "session-a", seq: 42 },
      },
      {
        type: "chunk",
        id: "chunk/a",
        display_name: "Lore book",
        preview: "Lore words",
        source: { book_id: "book/a", chunk_id: "chunk/a" },
      },
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(rows)));
    await render();
    await search();
    expect(
      [...document.querySelectorAll(".badge")].map((node) => node.textContent),
    ).toEqual(["Entity", "Note", "Event", "Lore"]);
    expect(
      [...document.querySelectorAll("li a")].map((node) =>
        node.getAttribute("href"),
      ),
    ).toEqual([
      `${base}/entities/entity-a`,
      `${base}/notes?note=note-a#note-note-a`,
      `${base}/session?session=session-a#session-session-a-event-42`,
      "https://jomcgi.dev/app/grimoire/book/book%2Fa/c/chunk%2Fa",
    ]);
    for (const text of [
      "A silver hinge",
      "Note words",
      "Event words",
      "Lore words",
    ])
      expect(document.body.textContent).toContain(text);
    expect(
      document.querySelector('[aria-live="polite"]').textContent,
    ).toContain("4 results found.");
  });
  it("shows loading then an explicit empty state", async () => {
    let resolve;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      ),
    );
    await render();
    await search();
    expect(document.querySelector('[role="status"]').textContent).toContain(
      "Searching knowledge",
    );
    expect(document.querySelector("button").disabled).toBe(true);
    resolve(response([]));
    await settle();
    expect(document.body.textContent).toContain(
      "No knowledge matches your search.",
    );
    expect(
      document.querySelector('[aria-live="polite"]').textContent,
    ).toContain("0 results found.");
    expect(document.querySelector("button").disabled).toBe(false);
  });
  it.each([403, 404, 503])(
    "shows an error for status %s without claiming an empty result",
    async (status) => {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(response({ error: "Search unavailable" }, status)),
      );
      await render();
      await search();
      expect(document.querySelector('[role="alert"]').textContent).toBe(
        "Search unavailable",
      );
      expect(document.body.textContent).not.toContain("No knowledge matches");
      expect(
        document.querySelector('[aria-live="polite"]').textContent.trim(),
      ).toBe("");
    },
  );
  it("clears earlier private results when a later search fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          response([
            {
              type: "note",
              id: "n",
              title: "Private result",
              preview: "Private words",
              source: { note_id: "n" },
            },
          ]),
        )
        .mockRejectedValueOnce(new Error("Offline")),
    );
    await render();
    await search();
    expect(document.body.textContent).toContain("Private words");
    await search("another");
    expect(document.body.textContent).not.toContain("Private words");
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      "Offline",
    );
  });
  it.each([" ", "x".repeat(201)])(
    "rejects an invalid input without fetching",
    async (q) => {
      const fetch = vi.fn();
      vi.stubGlobal("fetch", fetch);
      await render();
      await search(q);
      expect(fetch).not.toHaveBeenCalled();
      expect(document.querySelector('[role="alert"]').textContent).toContain(
        "1 to 200",
      );
    },
  );
  it("fails closed for malformed responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(response({ results: [] })),
    );
    await render();
    await search();
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      "Knowledge search is unavailable.",
    );
  });
  it("aborts an in-flight search when the component is removed", async () => {
    const fetch = vi.fn(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetch);
    await render();
    await search();
    const signal = fetch.mock.calls[0][1].signal;
    await unmount(instance);
    instance = null;
    expect(signal.aborted).toBe(true);
  });
});
