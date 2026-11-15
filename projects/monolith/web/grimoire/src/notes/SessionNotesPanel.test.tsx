import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Note } from "../api/notes";
import { createQueryClient, Providers } from "../app/Providers";
import { findToast } from "../test/app";
import { installBackend, type Backend, type Routes } from "../test/backend";
import { ids } from "../test/fixtures";
import {
  dmNotes,
  note,
  noteIds,
  notesBackend,
  playerNotes,
} from "../test/fixtures/notes";
import { SessionNotesPanel } from "./SessionNotesPanel";

let backend: Backend | undefined;
afterEach(() => {
  backend?.restore();
  backend = undefined;
});

function openPanel(
  options: { dm?: boolean; notes?: Note[]; routes?: Routes } = {},
) {
  const dm = options.dm ?? false;
  const seed = options.notes ?? (dm ? dmNotes : playerNotes);
  backend = installBackend({ ...notesBackend(() => seed), ...options.routes });
  const onShowEvent = vi.fn();
  const onOpenKnowledge = vi.fn();
  render(
    <MemoryRouter>
      <Providers client={createQueryClient()}>
        <SessionNotesPanel
          campaignId={ids.joined}
          dm={dm}
          onShowEvent={onShowEvent}
          onOpenKnowledge={onOpenKnowledge}
        />
      </Providers>
    </MemoryRouter>,
  );
  return {
    user: userEvent.setup(),
    backend: backend!,
    onShowEvent,
    onOpenKnowledge,
  };
}

const CREATE = "POST /campaigns/{campaign_id}/notes";
const PATCH = "PATCH /campaigns/{campaign_id}/notes/{note_id}";
const DELETE = "DELETE /campaigns/{campaign_id}/notes/{note_id}";
const LIST = "GET /campaigns/{campaign_id}/notes";

describe("SessionNotesPanel", () => {
  it("starts on private notes with DM sharing off", async () => {
    const { backend } = openPanel();
    expect(
      screen
        .getByRole("tab", { name: "My notes" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(screen.getByRole("tab", { name: "Party notes" })).toBeTruthy();
    expect(
      screen.getByText(
        "Only you can read these unless you share a note with the DM.",
      ),
    ).toBeTruthy();
    const share = screen.getByRole("checkbox", {
      name: "Share this note with the DM",
    }) as HTMLInputElement;
    expect(share.checked).toBe(false);
    expect(
      await screen.findByRole("heading", { name: "The ferryman's map" }),
    ).toBeTruthy();
    expect(backend.callsTo(LIST)[0]?.query.get("kind")).toBe("character");
    expect(screen.queryByText("Plan for the cellar")).toBeNull();
  });

  it("shows what is pinned and what the DM can read", async () => {
    openPanel();
    const list = await screen.findByRole("list", { name: "Character notes" });
    const [map, rumour] = within(list).getAllByRole("listitem");
    expect(within(map!).getByText("Pinned")).toBeTruthy();
    expect(within(map!).queryByText("Shared with the DM")).toBeNull();
    expect(within(rumour!).getByText("Shared with the DM")).toBeTruthy();
    expect(within(rumour!).queryByText("Pinned")).toBeNull();
  });

  it("gives the DM shared notes to read and only party notes to write", async () => {
    const { user } = openPanel({ dm: true });
    expect(screen.getByRole("tab", { name: "Shared with you" })).toBeTruthy();
    expect(
      screen.getByText("Character notes players explicitly shared with you."),
    ).toBeTruthy();
    expect(screen.queryByRole("textbox", { name: /Note title/ })).toBeNull();
    await screen.findByRole("heading", { name: "Rumour at the Gilded Eel" });
    expect(screen.queryByRole("button", { name: /^Edit / })).toBeNull();

    await user.click(screen.getByRole("tab", { name: "Party notes" }));
    expect(
      screen.getByText("Shared with the DM and every player with a character."),
    ).toBeTruthy();
    expect(screen.getByRole("textbox", { name: /Note title/ })).toBeTruthy();
    expect(
      screen.queryByRole("checkbox", { name: "Share this note with the DM" }),
    ).toBeNull();
    expect(
      await screen.findByRole("button", { name: "Edit Plan for the cellar" }),
    ).toBeTruthy();
  });

  it("saves a new character note with its sharing and pin choices", async () => {
    const { user, backend } = openPanel({ notes: [] });
    expect(await screen.findByText("No notes here yet.")).toBeTruthy();
    const save = screen.getByRole("button", { name: "Save note" });
    expect((save as HTMLButtonElement).disabled).toBe(true);

    const title = screen.getByRole("textbox", { name: /Note title/ });
    const text = screen.getByRole("textbox", { name: /Note text/ });
    await user.type(title, "  Found a map ");
    await user.type(text, "North road{enter}Travel tomorrow");
    await user.click(
      screen.getByRole("checkbox", { name: "Share this note with the DM" }),
    );
    await user.click(save);

    expect(backend.callsTo(CREATE)[0]?.body).toEqual({
      kind: "character",
      title: "Found a map",
      markdown: "North road\nTravel tomorrow",
      dm_readable: true,
      pinned: false,
    });
    expect(await findToast("Note saved.")).toBeTruthy();
    expect(
      await screen.findByRole("heading", { name: "Found a map" }),
    ).toBeTruthy();
    expect((title as HTMLInputElement).value).toBe("");
    expect((text as HTMLTextAreaElement).value).toBe("");
  });

  it("creates a party note without a sharing choice", async () => {
    const { user, backend } = openPanel({ notes: [] });
    await user.click(screen.getByRole("tab", { name: "Party notes" }));
    await user.type(
      screen.getByRole("textbox", { name: /Note title/ }),
      "Watch order",
    );
    await user.type(
      screen.getByRole("textbox", { name: /Note text/ }),
      "Mira takes first watch.",
    );
    await user.click(screen.getByRole("checkbox", { name: "Keep pinned" }));
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(backend.callsTo(CREATE)[0]?.body).toEqual({
      kind: "party",
      title: "Watch order",
      markdown: "Mira takes first watch.",
      pinned: true,
    });
  });

  it("edits a note in place and can cancel", async () => {
    const { user, backend } = openPanel();
    await user.click(
      await screen.findByRole("button", { name: "Edit The ferryman's map" }),
    );
    expect(screen.getByRole("heading", { name: "Edit note" })).toBeTruthy();
    const title = screen.getByRole("textbox", {
      name: /Note title/,
    }) as HTMLInputElement;
    expect(title.value).toBe("The ferryman's map");
    expect(
      (
        screen.getByRole("checkbox", {
          name: "Keep pinned",
        }) as HTMLInputElement
      ).checked,
    ).toBe(true);

    await user.click(screen.getByRole("button", { name: "Cancel edit" }));
    expect(title.value).toBe("");
    expect(screen.getByRole("heading", { name: "Add a note" })).toBeTruthy();

    await user.click(
      screen.getByRole("button", { name: "Edit The ferryman's map" }),
    );
    await user.clear(title);
    await user.type(title, "Tolliver's map");
    await user.click(screen.getByRole("button", { name: "Save note" }));
    const [call] = backend.callsTo(PATCH);
    expect(call?.params.note_id).toBe(noteIds.map);
    expect(call?.body).toEqual({
      title: "Tolliver's map",
      markdown: "Three crossings marked.\nThe northern one is circled twice.",
      dm_readable: false,
      pinned: true,
    });
    expect(backend.callsTo(CREATE)).toHaveLength(0);
    expect(
      await screen.findByRole("heading", { name: "Tolliver's map" }),
    ).toBeTruthy();
  });

  it("never sends DM sharing when the DM edits a party note", async () => {
    const { user, backend } = openPanel({ dm: true });
    await user.click(screen.getByRole("tab", { name: "Party notes" }));
    await user.click(
      await screen.findByRole("button", { name: "Edit Plan for the cellar" }),
    );
    await user.click(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(backend.callsTo(PATCH)).toHaveLength(1));
    expect(backend.callsTo(PATCH)[0]?.body).toEqual({
      title: "Plan for the cellar",
      markdown: "Brannoch first. Lanterns hooded until we are past the rats.",
      pinned: false,
    });
  });

  it("drops an unsaved edit when the audience changes", async () => {
    const { user } = openPanel();
    await user.click(
      await screen.findByRole("button", { name: "Edit The ferryman's map" }),
    );
    await user.click(screen.getByRole("tab", { name: "Party notes" }));
    expect(screen.getByRole("heading", { name: "Add a note" })).toBeTruthy();
    expect(
      (screen.getByRole("textbox", { name: /Note title/ }) as HTMLInputElement)
        .value,
    ).toBe("");
  });

  it("deletes a note after confirmation", async () => {
    const { user, backend } = openPanel();
    await user.click(
      await screen.findByRole("button", {
        name: "Delete Rumour at the Gilded Eel",
      }),
    );
    expect(backend.callsTo(DELETE)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Delete note" }));
    expect(await findToast("Note deleted.")).toBeTruthy();
    expect(backend.callsTo(DELETE)[0]?.params.note_id).toBe(noteIds.rumour);
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Rumour at the Gilded Eel" }),
      ).toBeNull(),
    );
  });

  it("jumps to a pinned note's source event and linked knowledge", async () => {
    const { user, onShowEvent, onOpenKnowledge } = openPanel();
    await user.click(
      await screen.findByRole("button", { name: "Source event" }),
    );
    expect(onShowEvent).toHaveBeenCalledWith(noteIds.pinnedEvent);
    await user.click(screen.getByRole("button", { name: "Old Tolliver" }));
    expect(onOpenKnowledge).toHaveBeenCalledWith(noteIds.ferryman);
  });

  it("shows a note's text exactly as written, never as markup", async () => {
    const markdown = '<img src=x onerror="alert(1)">\n\n[bad](javascript:x)';
    openPanel({ notes: [note({ id: noteIds.map, title: "Trap", markdown })] });
    const list = await screen.findByRole("list", { name: "Character notes" });
    const body = within(list).getByText(/onerror/);
    expect(body.textContent).toBe(markdown);
    expect(list.querySelector("img")).toBeNull();
    expect(list.querySelector("a")).toBeNull();
  });

  it("keeps the draft when the backend refuses the save", async () => {
    const { user } = openPanel({
      notes: [],
      routes: {
        [CREATE]: { status: 403, detail: "note creation not permitted" },
      },
    });
    const title = screen.getByRole("textbox", { name: /Note title/ });
    await user.type(title, "Found a map");
    await user.type(
      screen.getByRole("textbox", { name: /Note text/ }),
      "North road",
    );
    await user.click(screen.getByRole("button", { name: "Save note" }));
    expect(await findToast("note creation not permitted")).toBeTruthy();
    expect((title as HTMLInputElement).value).toBe("Found a map");
  });

  it("reports a failed read instead of an empty list", async () => {
    openPanel({
      routes: { [LIST]: { status: 500, detail: "notes are down" } },
    });
    expect(await screen.findByText("Could not load these notes")).toBeTruthy();
    expect(screen.getByText("notes are down")).toBeTruthy();
    expect(screen.queryByText("No notes here yet.")).toBeNull();
  });
});
