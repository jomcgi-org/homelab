import { describe, it, expect, vi } from "vitest";
import { actions } from "./+page.server.js";

function event(fields, fetch) {
  const data = new FormData();
  for (const [key, value] of Object.entries(fields)) data.set(key, value);
  return { request: { formData: async () => data }, fetch };
}

describe.each(["decide", "undeleteAction"])("%s action", (name) => {
  it("POSTs to the allowed path with no body", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true });
    const result = await actions[name](
      event({ path: "/api/knowledge/gaps/7/undelete" }, fetch),
    );
    expect(result).toEqual({ ok: true });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toMatch(/\/api\/knowledge\/gaps\/7\/undelete$/);
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
  });

  it("rejects a path outside /api/knowledge/ without fetching", async () => {
    const fetch = vi.fn();
    const result = await actions[name](event({ path: "/api/admin" }, fetch));
    expect(result.status).toBe(400);
    expect(result.data).toEqual({ error: "invalid path" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("relays the upstream status and FastAPI detail", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      text: async () => JSON.stringify({ detail: "row is live" }),
    });
    const result = await actions[name](
      event({ path: "/api/knowledge/notes/n1/undelete" }, fetch),
    );
    expect(result.status).toBe(409);
    expect(result.data).toEqual({ error: "row is live" });
  });
});
