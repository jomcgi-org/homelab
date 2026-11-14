import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { findToast, openApp } from "../test/app";
import { characters, ids, lobby } from "../test/fixtures";
import { event, sessionRoutes } from "../test/fixtures/session";

/** Hold the first write after the fixture has accepted it. */
function holdWrite() {
  const fetch = globalThis.fetch;
  let release!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  globalThis.fetch = async (input, init) => {
    const response = await fetch(input, init);
    if (init?.method === "POST") await held;
    return response;
  };
  return release;
}

describe("pending keyboard actions", () => {
  it("creates one campaign when Enter repeats before the response arrives", async () => {
    const { user, backend } = openApp({
      "GET /lobby": lobby({ campaigns: [], invitations: [] }),
      "POST /campaigns": {
        id: ids.owned,
        name: "One table",
        dm_name: null,
        created_at: "2026-11-14T00:00:00Z",
      },
    });
    const release = holdWrite();
    try {
      const name = await screen.findByLabelText("Campaign name");
      await user.type(name, "One table");
      fireEvent.keyDown(name, { key: "Enter" });
      fireEvent.keyDown(name, { key: "Enter" });
      await waitFor(() =>
        expect(backend.callsTo("POST /campaigns").length).toBeGreaterThan(0),
      );
      expect(backend.callsTo("POST /campaigns")).toHaveLength(1);
    } finally {
      await act(async () => {
        release();
      });
    }
  });

  it("rolls once for repeated Enter and allows a later intentional roll", async () => {
    const { user, backend } = openApp(
      {
        "GET /lobby": lobby(),
        "GET /campaigns/{campaign_id}/characters": characters,
        ...sessionRoutes,
        "POST /campaigns/{campaign_id}/sessions/{session_id}/rolls": event({
          kind: "roll",
          body: { formula: "d20", total: 12, rolls: [12], kept: [12] },
        }),
      },
      `/campaigns/${ids.owned}/session`,
    );
    await user.click(await screen.findByRole("button", { name: "Roll dice" }));
    const formula = await screen.findByLabelText("Dice formula");
    const release = holdWrite();
    const operation =
      "POST /campaigns/{campaign_id}/sessions/{session_id}/rolls";
    try {
      fireEvent.keyDown(formula, { key: "Enter" });
      fireEvent.keyDown(formula, { key: "Enter" });
      await waitFor(() =>
        expect(backend.callsTo(operation).length).toBeGreaterThan(0),
      );
      expect(backend.callsTo(operation)).toHaveLength(1);
    } finally {
      await act(async () => {
        release();
      });
    }
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Roll" }).hasAttribute("disabled"),
      ).toBe(false),
    );
    await user.click(screen.getByRole("button", { name: "Roll" }));
    expect(backend.callsTo(operation)).toHaveLength(2);
  });

  it("allows retry after a refused campaign creation", async () => {
    const { user, backend } = openApp({
      "GET /lobby": lobby({ campaigns: [], invitations: [] }),
      "POST /campaigns": { status: 503, detail: "Creation is unavailable." },
    });
    const name = await screen.findByLabelText("Campaign name");
    await user.type(name, "Retry table");
    await user.keyboard("{Enter}");
    await findToast("Creation is unavailable.");
    await user.click(name);
    await user.keyboard("{Enter}");
    expect(backend.callsTo("POST /campaigns")).toHaveLength(2);
  });
});
