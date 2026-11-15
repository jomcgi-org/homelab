import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { openApp } from "../../test/app";
import type { Routes } from "../../test/backend";
import { characters, ids, lobby } from "../../test/fixtures";
import {
  activeSession,
  sessionRoutes,
  story,
} from "../../test/fixtures/session";

const path = `/campaigns/${ids.owned}/session`;
const dm: Routes = {
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/characters": characters,
  ...sessionRoutes,
};
const eventsRoute = "GET /campaigns/{campaign_id}/sessions/{session_id}/events";
const journalRoute =
  "GET /campaigns/{campaign_id}/sessions/{session_id}/journal";

describe("Session read recovery", () => {
  it.each([
    [
      "turn order",
      "GET /campaigns/{campaign_id}/sessions/{session_id}/initiative",
    ],
    ["members", "GET /campaigns/{campaign_id}/members"],
    ["voices", "GET /campaigns/{campaign_id}/voices"],
  ] as const)(
    "retries a failed first %s read before opening the table",
    async (_name, route) => {
      const { user, backend } = openApp(
        {
          ...dm,
          [route]: { status: 503, detail: "The table is unavailable." },
        },
        path,
      );
      await waitFor(
        () => expect(backend.callsTo(route).length).toBeGreaterThan(0),
        { timeout: 5000 },
      );
      expect(
        await screen.findByRole("heading", {
          name: "Could not open the session",
        }),
      ).toBeTruthy();
      expect(
        screen.queryByRole("textbox", { name: "Set the scene" }),
      ).toBeNull();
      expect(screen.queryByRole("list", { name: "Session feed" })).toBeNull();
      backend.set(sessionRoutes);
      await user.click(screen.getByRole("button", { name: "Try again" }));
      expect(
        await screen.findByRole("list", { name: "Session feed" }),
      ).toBeTruthy();
      expect(
        screen.getByRole("textbox", { name: "Set the scene" }),
      ).toBeTruthy();
    },
  );

  it.each(["active", "ended"] as const)(
    "retries a failed first feed read for an %s session",
    async (status) => {
      const { user, backend } = openApp(
        {
          ...dm,
          "GET /campaigns/{campaign_id}/sessions": [
            { ...activeSession, status },
          ],
          [eventsRoute]: { status: 503, detail: "The story is unavailable." },
        },
        path,
      );
      await waitFor(
        () => expect(backend.callsTo(eventsRoute).length).toBeGreaterThan(0),
        {
          timeout: 5000,
        },
      );
      expect(
        await screen.findByRole("heading", {
          name: "Could not open the session",
        }),
      ).toBeTruthy();
      expect(screen.getByText("The story is unavailable.")).toBeTruthy();

      backend.set({ [eventsRoute]: story });
      await user.click(screen.getByRole("button", { name: "Try again" }));
      const feed = await screen.findByRole("list", { name: "Session feed" });
      expect(
        within(feed).getByText("Rain hammers the shutters of the Gilded Eel."),
      ).toBeTruthy();
      expect(backend.callsTo(eventsRoute)).toHaveLength(2);
    },
  );

  it("keeps the story and draft through a failed background feed read", async () => {
    const { user, backend } = openApp(dm, path);
    const feed = await screen.findByRole("list", { name: "Session feed" });
    const box = screen.getByRole("textbox", { name: "Set the scene" });
    await user.type(box, "The door creaks.");
    backend.set({
      [eventsRoute]: { status: 503, detail: "The story is unavailable." },
    });
    await screen.findByText("Reconnecting", {}, { timeout: 4000 });
    expect(
      within(feed).getByText("Rain hammers the shutters of the Gilded Eel."),
    ).toBeTruthy();
    expect((box as HTMLTextAreaElement).value).toBe("The door creaks.");
    expect(
      screen.queryByRole("heading", { name: "Could not open the session" }),
    ).toBeNull();

    backend.set({ [eventsRoute]: [] });
    await screen.findByText("Live", {}, { timeout: 4000 });
    expect(screen.getByRole("textbox", { name: "Set the scene" })).toBe(box);
    expect((box as HTMLTextAreaElement).value).toBe("The door creaks.");
  });

  it("retries a failed initial journal read without treating it as empty", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        [journalRoute]: { status: 503, detail: "The journal is unavailable." },
      },
      path,
    );
    await user.click(await screen.findByRole("tab", { name: "Journal" }));
    expect(
      await screen.findByRole("heading", {
        name: "Could not load the journal",
      }),
    ).toBeTruthy();
    expect(screen.getByText("The journal is unavailable.")).toBeTruthy();
    expect(screen.queryByText("Nothing recorded yet.")).toBeNull();

    backend.set({ [journalRoute]: {} });
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Nothing recorded yet.")).toBeTruthy();
    expect(backend.callsTo(journalRoute)).toHaveLength(2);
  });

  it("lets the reader change journal views after an error and retry the chosen view", async () => {
    const { user, backend } = openApp(
      {
        ...dm,
        [journalRoute]: (call) =>
          call.query.get("view") === "party"
            ? { status: 503, detail: "The party journal is unavailable." }
            : { learned: [] },
      },
      path,
    );
    const box = await screen.findByRole("textbox", { name: "Set the scene" });
    await user.type(box, "The door creaks.");
    await user.click(screen.getByRole("tab", { name: "Journal" }));
    await screen.findByText("Nothing recorded yet.");
    const journal = within(
      screen.getByRole("region", { name: "Session journal" }),
    );
    await user.click(journal.getByRole("radio", { name: "Party" }));
    await screen.findByRole("heading", { name: "Could not load the journal" });
    expect(
      journal
        .getByRole("radio", { name: "Party" })
        .getAttribute("aria-checked"),
    ).toBe("true");

    await user.click(journal.getByRole("radio", { name: "Mine" }));
    expect(await screen.findByText("Nothing recorded yet.")).toBeTruthy();
    await user.click(journal.getByRole("radio", { name: "Party" }));
    await screen.findByRole("heading", { name: "Could not load the journal" });
    backend.set({ [journalRoute]: {} });
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByText("Nothing recorded yet.");
    expect(backend.callsTo(journalRoute).at(-1)?.query.get("view")).toBe(
      "party",
    );

    await user.click(screen.getByRole("tab", { name: "Story" }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Set the scene" })).toBe(box),
    );
    expect((box as HTMLTextAreaElement).value).toBe("The door creaks.");
  });
});
