import { act, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { keys } from "../api/lobby";
import type { LobbyView } from "../api/schema";
import { keepDraft, takeDraft } from "../lib/session-expiry.js";
import { openApp } from "../test/app";
import { campaigns, ids, lobby } from "../test/fixtures";
import { note } from "../test/fixtures/notes";
import { sheetsRoutes } from "../test/fixtures/sheets";

const first = lobby({ campaigns: [campaigns.joined] });
const otherUser = {
  id: "88888888-8888-4888-8888-888888888888",
  email: "bram@example.test",
  display_name: "Bram",
};
const privateNote = note({
  id: "77777777-7777-4777-8777-777777777777",
  title: "Elowen's private canary",
  markdown: "Only the first signed-in player can read this.",
  dm_readable: false,
});

function expectReload() {
  const reload = vi.fn();
  vi.stubGlobal("location", {
    origin: "https://friends.example.test",
    reload,
  });
  return reload;
}

afterEach(() => vi.unstubAllGlobals());

async function openNotes(viewer: LobbyView = first) {
  const app = openApp(
    {
      "GET /lobby": viewer,
      "GET /campaigns/{campaign_id}/notes": [privateNote],
    },
    `/campaigns/${ids.joined}/notes`,
  );
  await screen.findByRole("article", { name: privateNote.title });
  return app;
}

async function confirmViewer(
  app: ReturnType<typeof openApp>,
  viewer: LobbyView,
) {
  app.backend.set({ "GET /lobby": viewer });
  await act(async () => {
    await app.client.invalidateQueries({ queryKey: keys.lobby });
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

describe("confirmed viewer changes", () => {
  it("removes private notes, cached drafts and kept messages before reloading another account", async () => {
    const reload = expectReload();
    const app = await openNotes();
    app.client.setQueryData(["feedback", "draft"], "Private feedback draft");
    keepDraft(
      ids.joined,
      { id: first.user.id, role: "player" },
      { text: "Private unsent action", audience: "dm" },
    );

    await confirmViewer(app, { ...first, user: otherUser });

    expect(
      screen.queryByRole("article", { name: privateNote.title }),
    ).toBeNull();
    expect(
      screen.getByText("Your account or campaign access changed"),
    ).toBeTruthy();
    expect(reload).toHaveBeenCalledTimes(1);
    expect(app.client.getQueryCache().getAll()).toHaveLength(0);
    expect(
      takeDraft(
        ids.joined,
        { id: first.user.id, role: "player" },
        { dm: false, characters: [] },
      ),
    ).toBeNull();
  });

  it("removes the previous account's sheet workspace", async () => {
    const reload = expectReload();
    const app = openApp({ ...sheetsRoutes, "GET /lobby": first }, "/sheets");
    await screen.findByRole("region", { name: "Mira Vale" });
    await screen.findByText("We have not reached level 4 yet.");

    await confirmViewer(app, { ...first, user: otherUser });

    expect(screen.queryByRole("region", { name: "Mira Vale" })).toBeNull();
    expect(screen.queryByText("We have not reached level 4 yet.")).toBeNull();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["role", { role: "dm" as const }],
    ["character", { player_character_id: ids.spare }],
    ["ownership", { is_owner: true }],
  ])(
    "fences cached campaign data after a confirmed %s change",
    async (_label, change) => {
      const reload = expectReload();
      const app = await openNotes();
      await confirmViewer(app, {
        ...first,
        campaigns: [{ ...campaigns.joined, ...change }],
      });
      expect(
        screen.queryByRole("article", { name: privateNote.title }),
      ).toBeNull();
      expect(reload).toHaveBeenCalledTimes(1);
    },
  );

  it("fences data after campaign membership is removed", async () => {
    const reload = expectReload();
    const app = await openNotes();
    await confirmViewer(app, { ...first, campaigns: [] });
    expect(
      screen.queryByRole("article", { name: privateNote.title }),
    ).toBeNull();
    expect(app.client.getQueryCache().getAll()).toHaveLength(0);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("fences cached operator data when account administration is withdrawn", async () => {
    const reload = expectReload();
    const app = await openNotes({ ...first, can_administer_accounts: true });
    await confirmViewer(app, first);
    expect(
      screen.queryByRole("article", { name: privateNote.title }),
    ).toBeNull();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("removes private data when the lobby confirms access has been denied", async () => {
    const reload = expectReload();
    const app = await openNotes();
    app.backend.set({
      "GET /lobby": { status: 403, detail: "Grimoire access required." },
    });

    await act(async () => {
      await app.client.invalidateQueries({ queryKey: keys.lobby });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(
      screen.queryByRole("article", { name: privateNote.title }),
    ).toBeNull();
    expect(app.client.getQueryCache().getAll()).toHaveLength(0);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("shows an initial access denial without entering a reload loop", async () => {
    const reload = expectReload();
    openApp(
      { "GET /lobby": { status: 403, detail: "Grimoire access required." } },
      "/sheets",
    );

    await screen.findByText("Grimoire access required.");

    expect(reload).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("preserves the current view and unsaved note through a temporary lobby failure", async () => {
    const reload = expectReload();
    const app = await openNotes();
    const editor = screen.getByRole("region", { name: "Add a note" });
    const title = within(editor).getByRole("textbox", { name: /Note title/ });
    await app.user.type(title, "Still writing");
    app.backend.set({
      "GET /lobby": { status: 503, detail: "Database unavailable." },
    });

    await act(async () => {
      await app.client.invalidateQueries({ queryKey: keys.lobby });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(
      screen.getByRole("article", { name: privateNote.title }),
    ).toBeTruthy();
    expect((title as HTMLInputElement).value).toBe("Still writing");
    expect(reload).not.toHaveBeenCalled();
  });

  it("preserves an unsaved note through unchanged identity and added campaigns", async () => {
    const reload = expectReload();
    const app = await openNotes();
    const editor = screen.getByRole("region", { name: "Add a note" });
    const title = within(editor).getByRole("textbox", { name: /Note title/ });
    await app.user.type(title, "Still writing");

    await confirmViewer(app, {
      ...first,
      user: { ...first.user, display_name: "Updated display name" },
      campaigns: [
        { ...campaigns.joined, name: "Renamed campaign" },
        campaigns.seatless,
      ],
      invitations: [],
    });

    expect((title as HTMLInputElement).value).toBe("Still writing");
    expect(
      screen.getByRole("article", { name: privateNote.title }),
    ).toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
  });

  it("preserves an unsaved sheet through a same-viewer lobby refresh", async () => {
    const reload = expectReload();
    const app = openApp({ ...sheetsRoutes, "GET /lobby": first }, "/sheets");
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await app.user.clear(level);
    await app.user.type(level, "5");

    await confirmViewer(app, { ...first, invitations: [] });

    expect((level as HTMLInputElement).value).toBe("5");
    expect(reload).not.toHaveBeenCalled();
  });
});
