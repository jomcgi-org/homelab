import { describe, expect, it, vi } from "vitest";
import { sendDecision, storageKey } from "./decision-client.js";
import {
  chatBody,
  decisionBody,
} from "../../routes/private/factory/escalations/escalations-view.js";

const item = { receipt_id: 31, decision_id: "d31", issue_number: 90031 };
const memory = () => {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, v),
    map,
  };
};
const reply = (status, body) => ({
  ok: status < 300,
  status,
  json: async () => body,
});

describe("sendDecision", () => {
  it("posts one keyed request and records acceptance", async () => {
    const storage = memory();
    const fetchImpl = vi.fn(async () => reply(200, { ok: true }));
    const out = await sendDecision(item, decisionBody("go", "", "d31"), {
      fetchImpl,
      storage,
    });
    expect(out).toEqual({
      status: "accepted",
      message: "Recorded for #90031.",
    });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/factory/escalations/decisions/31");
    const sent = JSON.parse(init.body);
    expect(sent).toMatchObject({ option_key: "go", decision_id: "d31" });
    expect(sent.request_key).toMatch(/^browser-v1:[0-9a-f]{64}$/);
    expect(JSON.parse(storage.getItem(storageKey(item))).status).toBe(
      "accepted",
    );
    // A second tap does not post again.
    await sendDecision(item, decisionBody("go", "", "d31"), {
      fetchImpl,
      storage,
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("keeps an uncertain request and blocks a different answer", async () => {
    const storage = memory();
    const lost = vi.fn(async () => {
      throw new Error("network");
    });
    expect(
      (
        await sendDecision(item, decisionBody("go", "", "d31"), {
          fetchImpl: lost,
          storage,
        })
      ).status,
    ).toBe("uncertain");
    const fetchImpl = vi.fn(async () => reply(200, { ok: true }));
    const other = await sendDecision(item, decisionBody("hold", "", "d31"), {
      fetchImpl,
      storage,
    });
    expect(other.status).toBe("uncertain");
    expect(fetchImpl).not.toHaveBeenCalled();
    const same = await sendDecision(item, decisionBody("go", "", "d31"), {
      fetchImpl,
      storage,
    });
    expect(same.status).toBe("accepted");
    const firstKey = JSON.parse(lost.mock.calls[0][1].body).request_key;
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body).request_key).toBe(
      firstKey,
    );
  });

  it("lets a refused answer be replaced with a new key", async () => {
    const storage = memory();
    const refuse = vi.fn(async () => reply(409, { detail: "stale" }));
    expect(
      await sendDecision(item, decisionBody("go", "", "d31"), {
        fetchImpl: refuse,
        storage,
      }),
    ).toEqual({ status: "refused", message: "stale" });
    const fetchImpl = vi.fn(async () => reply(200, { ok: true }));
    expect(
      (
        await sendDecision(item, decisionBody("hold", "", "d31"), {
          fetchImpl,
          storage,
        })
      ).status,
    ).toBe("accepted");
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body).request_key).not.toBe(
      JSON.parse(refuse.mock.calls[0][1].body).request_key,
    );
  });

  it("treats a 5xx or a non-ok result as uncertain", async () => {
    for (const r of [reply(502, {}), reply(200, { ok: false, reason: "?" })]) {
      const out = await sendDecision(item, decisionBody("go", "", "d31"), {
        fetchImpl: async () => r,
        storage: memory(),
      });
      expect(out.status).toBe("uncertain");
    }
  });

  it("says when a chat was posted but nothing will answer it", async () => {
    const out = await sendDecision(item, chatBody("why?", "d31"), {
      fetchImpl: async () =>
        reply(200, { ok: true, requeued: false, blocked_by: "lane full" }),
      storage: memory(),
    });
    expect(out).toEqual({
      status: "accepted",
      message: "Asked on #90031, but no brief was queued: lane full.",
    });
  });

  it("refuses a card without an exact identity", async () => {
    const fetchImpl = vi.fn();
    const out = await sendDecision(item, decisionBody("go", "", null), {
      fetchImpl,
      storage: memory(),
    });
    expect(out.status).toBe("refused");
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
