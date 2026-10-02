import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { actions, load } from "./+page.server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const noteId = "22222222-2222-4222-8222-222222222222";
const otherId = "33333333-3333-4333-8333-333333333333";
const membership = {
  id: campaignId,
  name: "Our campaign",
  role: "player",
  player_character_id: otherId,
};
const response = (body, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => body,
});

function event(fetch, fields = {}, overrides = {}) {
  const body = new FormData();
  for (const [key, value] of Object.entries(fields)) body.set(key, value);
  return {
    params: { id: campaignId },
    url: new URL(
      `https://friends.jomcgi.dev/grimoire/campaigns/${campaignId}/notes`,
    ),
    fetch,
    cookies: {
      get: (key) => (key === "grimoire-id-token" ? "signed-token" : undefined),
    },
    setHeaders: vi.fn(),
    request: new Request("https://friends.jomcgi.dev/grimoire", {
      method: "POST",
      body,
    }),
    ...overrides,
  };
}

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
});

describe("notes load", () => {
  it("forwards kind and q, scopes membership, and disables caching", async () => {
    const notes = [{ id: noteId, kind: "party" }];
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ campaigns: [membership] }))
      .mockResolvedValueOnce(response(notes));
    const request = event(
      fetch,
      {},
      {
        url: new URL(
          `https://friends.jomcgi.dev/grimoire/campaigns/${campaignId}/notes?kind=party&q=map%26road`,
        ),
      },
    );
    expect(await load(request)).toEqual({
      campaign: membership,
      notes,
      kind: "party",
      q: "map&road",
    });
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "http://backend.test/api/grimoire/lobby",
      `http://backend.test/api/grimoire/campaigns/${campaignId}/notes?kind=party&q=map%26road`,
    ]);
    expect(request.setHeaders).toHaveBeenCalledWith({
      "cache-control": "private, no-store",
    });
    for (const [, options] of fetch.mock.calls)
      expect(options.headers).toEqual({ "x-grimoire-token": "signed-token" });
  });

  it("defaults to character notes", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ campaigns: [membership] }))
      .mockResolvedValueOnce(response([]));
    expect((await load(event(fetch))).kind).toBe("character");
    expect(fetch.mock.calls[1][0]).toContain("?kind=character&q=");
  });

  it("returns 404 for an invalid campaign UUID before fetching", async () => {
    const fetch = vi.fn();
    await expect(
      load(event(fetch, {}, { params: { id: "bad" } })),
    ).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("never loads another campaign's notes", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response({ campaigns: [{ ...membership, id: otherId }] }),
      );
    await expect(load(event(fetch))).rejects.toMatchObject({ status: 404 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([403, 404, 500])(
    "rejects failed notes reads (%s), never an empty success",
    async (status) => {
      const fetch = vi
        .fn()
        .mockResolvedValueOnce(response({ campaigns: [membership] }))
        .mockResolvedValueOnce(
          response({ detail: "Notes unavailable" }, status),
        );
      await expect(load(event(fetch))).rejects.toThrow("Notes unavailable");
    },
  );

  it("rejects a failed membership read", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(response({ detail: "Not permitted" }, 403));
    await expect(load(event(fetch))).rejects.toThrow("Not permitted");
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("SSR starts on Party and renders action errors", () => {
    const { body } = render(Page, {
      props: {
        data: {
          campaign: membership,
          notes: [
            {
              id: noteId,
              title: "Party plan",
              kind: "party",
              markdown: "Our map",
              links: {},
              can_edit: false,
            },
          ],
          kind: "party",
          q: "",
        },
        form: { error: "Denied" },
      },
    });
    expect(body).toContain("Party plan");
    expect(body).toContain("Denied");
    expect(body).toContain('name="kind" value="party"');
    expect(body).not.toContain("No personal notes");
    expect(body).toContain('action="?kind=party&amp;q=&amp;/create"');
  });

  it("SSR renders a named DM-sharing select on the character tab", () => {
    const { body } = render(Page, {
      props: {
        data: {
          campaign: membership,
          notes: [],
          kind: "character",
          q: "",
        },
      },
    });
    expect(body).toContain('name="dm_readable"');
    const select = body.match(
      /<select[^>]*name="dm_readable"[^>]*>([\s\S]*?)<\/select>/,
    );
    expect(select).not.toBeNull();
    const values = [...select[1].matchAll(/<option[^>]*value="([^"]*)"/g)].map(
      (m) => m[1],
    );
    expect(values).toEqual(["", "false", "true"]);
  });
});

describe("notes actions", () => {
  it.each([undefined, "", "false", "true"])(
    "creates a character note with dm_readable=%s",
    async (choice) => {
      const fetch = vi.fn().mockResolvedValue(response({}));
      const fields = {
        campaign_id: campaignId,
        kind: "character",
        title: "Map",
        markdown: "**North**",
        ...(choice === undefined ? {} : { dm_readable: choice }),
      };
      expect(await actions.create(event(fetch, fields))).toEqual({ ok: true });
      expect(fetch.mock.calls[0][0]).toBe(
        `http://backend.test/api/grimoire/campaigns/${campaignId}/notes`,
      );
      expect(fetch.mock.calls[0][1]).toMatchObject({
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-grimoire-token": "signed-token",
        },
      });
      expect(JSON.parse(fetch.mock.calls[0][1].body).dm_readable).toEqual(
        choice === "true" ? true : choice === "false" ? false : undefined,
      );
      if (choice === undefined || choice === "")
        expect(JSON.parse(fetch.mock.calls[0][1].body)).not.toHaveProperty(
          "dm_readable",
        );
      else
        expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
          kind: "character",
          title: "Map",
          markdown: "**North**",
          dm_readable: choice === "true",
        });
    },
  );

  it("creates party notes without sending a sharing choice", async () => {
    const fetch = vi.fn().mockResolvedValue(response({}));
    await actions.create(
      event(fetch, { campaign_id: campaignId, kind: "party", title: "Plan" }),
    );
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      kind: "party",
      title: "Plan",
      markdown: "",
    });
  });

  it.each([undefined, "true", "false"])(
    "updates text and optional sharing (%s)",
    async (choice) => {
      const fetch = vi.fn().mockResolvedValue(response({}));
      expect(
        await actions.update(
          event(fetch, {
            campaign_id: campaignId,
            note_id: noteId,
            title: "Revised",
            markdown: "New text",
            ...(choice === undefined ? {} : { dm_readable: choice }),
          }),
        ),
      ).toEqual({ ok: true });
      expect(fetch.mock.calls[0][0]).toBe(
        `http://backend.test/api/grimoire/campaigns/${campaignId}/notes/${noteId}`,
      );
      expect(fetch.mock.calls[0][1].method).toBe("PATCH");
      expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
        title: "Revised",
        markdown: "New text",
        ...(choice === undefined ? {} : { dm_readable: choice === "true" }),
      });
    },
  );

  it("soft-deletes through DELETE without a body", async () => {
    const fetch = vi.fn().mockResolvedValue(response(null, 204));
    expect(
      await actions.delete(
        event(fetch, { campaign_id: campaignId, note_id: noteId }),
      ),
    ).toEqual({ ok: true });
    expect(fetch.mock.calls[0]).toEqual([
      `http://backend.test/api/grimoire/campaigns/${campaignId}/notes/${noteId}`,
      {
        method: "DELETE",
        headers: { "x-grimoire-token": "signed-token" },
        signal: expect.any(AbortSignal),
      },
    ]);
  });

  for (const name of ["create", "update", "delete"]) {
    it.each(["bad", "", otherId])(
      `${name} rejects invalid or mismatched campaign selection (%s)`,
      async (selected) => {
        const fetch = vi.fn();
        const result = await actions[name](
          event(fetch, {
            campaign_id: selected,
            note_id: noteId,
            kind: "character",
            title: "Map",
          }),
        );
        expect(result.status).toBe(400);
        expect(fetch).not.toHaveBeenCalled();
      },
    );
    it(`${name} rejects invalid route params`, async () => {
      const fetch = vi.fn();
      await expect(
        actions[name](
          event(fetch, { campaign_id: campaignId }, { params: { id: "bad" } }),
        ),
      ).rejects.toMatchObject({ status: 404 });
      expect(fetch).not.toHaveBeenCalled();
    });
    it(`${name} surfaces backend errors`, async () => {
      const fetch = vi
        .fn()
        .mockResolvedValue(
          response({ detail: "note edit not permitted" }, 403),
        );
      const result = await actions[name](
        event(fetch, {
          campaign_id: campaignId,
          note_id: noteId,
          kind: "character",
          title: "Map",
        }),
      );
      expect(result.status).toBe(400);
      expect(result.data.error).toBe("note edit not permitted");
    });
  }

  for (const name of ["update", "delete"]) {
    it.each(["bad", "", undefined])(
      `${name} rejects invalid or missing note ids (%s)`,
      async (selected) => {
        const fetch = vi.fn();
        const result = await actions[name](
          event(fetch, {
            campaign_id: campaignId,
            ...(selected === undefined ? {} : { note_id: selected }),
          }),
        );
        expect(result.status).toBe(400);
        expect(fetch).not.toHaveBeenCalled();
      },
    );
  }

  it.each(["unknown", "default"])(
    "rejects invalid DM-sharing values (%s)",
    async (choice) => {
      const fetch = vi.fn();
      const result = await actions.create(
        event(fetch, {
          campaign_id: campaignId,
          kind: "character",
          title: "Map",
          dm_readable: choice,
        }),
      );
      expect(result.status).toBe(400);
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("rejects an unknown note kind", async () => {
    const fetch = vi.fn();
    expect(
      (
        await actions.create(
          event(fetch, {
            campaign_id: campaignId,
            kind: "unknown",
            title: "Map",
          }),
        )
      ).status,
    ).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("requires a dedicated Grimoire session", async () => {
    const fetch = vi.fn();
    const result = await actions.create(
      event(
        fetch,
        { campaign_id: campaignId, kind: "party", title: "Plan" },
        { cookies: { get: () => undefined } },
      ),
    );
    expect(result.status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });
});
