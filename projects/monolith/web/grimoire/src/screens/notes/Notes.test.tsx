import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Note } from "../../api/notes";
import type { Routes } from "../../test/backend";
import { chooseOption, findToast, openApp } from "../../test/app";
import { ids, lobby } from "../../test/fixtures";
import {
  dmNotes,
  note,
  noteIds,
  notesBackend,
  playerNotes,
} from "../../test/fixtures/notes";

const CREATE = "POST /campaigns/{campaign_id}/notes";
const PATCH = "PATCH /campaigns/{campaign_id}/notes/{note_id}";
const DELETE = "DELETE /campaigns/{campaign_id}/notes/{note_id}";
const LIST = "GET /campaigns/{campaign_id}/notes";

/** `joined` is a player with a character, `owned` the DM, `seatless` neither. */
function openNotes(
  campaignId: string = ids.joined,
  options: { notes?: Note[]; search?: string; routes?: Routes } = {},
) {
  const seed =
    options.notes ?? (campaignId === ids.owned ? dmNotes : playerNotes);
  return openApp(
    { "GET /lobby": lobby(), ...notesBackend(() => seed), ...options.routes },
    `/campaigns/${campaignId}/notes${options.search ?? ""}`,
  );
}

const addForm = () => screen.getByRole("region", { name: "Add a note" });

const selected = (name: string) =>
  screen.getByRole("button", { name }).getAttribute("aria-current");

describe("Notes", () => {
  it("names the campaign and links back to its session", async () => {
    openNotes();
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Salt Marsh Nights notes",
      }),
    ).toBeTruthy();
    expect(
      screen
        .getByRole("link", { name: "Back to session" })
        .getAttribute("href"),
    ).toBe(`/campaigns/${ids.joined}/session`);
  });

  it("switches between Mine and Party and shows only that kind", async () => {
    const { user } = openNotes();
    expect(
      await screen.findByRole("article", { name: "The ferryman's map" }),
    ).toBeTruthy();
    expect(selected("Mine")).toBe("true");
    expect(
      screen.queryByRole("article", { name: "Plan for the cellar" }),
    ).toBeNull();
    expect(
      within(addForm()).getByRole("combobox", { name: "DM sharing" }),
    ).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Party" }));
    expect(
      await screen.findByRole("article", { name: "Plan for the cellar" }),
    ).toBeTruthy();
    expect(selected("Party")).toBe("true");
    expect(
      screen.queryByRole("article", { name: "The ferryman's map" }),
    ).toBeNull();
    expect(
      within(addForm()).queryByRole("combobox", { name: "DM sharing" }),
    ).toBeNull();
    expect(
      screen.getByText(
        "Party notes are shared with the DM and players with a character.",
      ),
    ).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Mine" }));
    expect(
      await screen.findByRole("article", { name: "The ferryman's map" }),
    ).toBeTruthy();
  });

  it("opens on the requested tab and search, and searches within the tab", async () => {
    const { user, backend } = openNotes(ids.joined, {
      search: "?kind=party&q=cellar",
    });
    expect(
      await screen.findByRole("article", { name: "Plan for the cellar" }),
    ).toBeTruthy();
    expect(selected("Party")).toBe("true");
    const box = screen.getByRole("searchbox", {
      name: "Search notes",
    }) as HTMLInputElement;
    expect(box.value).toBe("cellar");
    expect(Object.fromEntries(backend.callsTo(LIST)[0]!.query)).toEqual({
      kind: "party",
      q: "cellar",
    });

    await user.clear(box);
    await user.type(box, "dragon");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(
      await screen.findByText("No party notes match your search."),
    ).toBeTruthy();
    expect(Object.fromEntries(backend.callsTo(LIST).at(-1)!.query)).toEqual({
      kind: "party",
      q: "dragon",
    });
  });

  it("words the empty state for each audience", async () => {
    const { user } = openNotes(ids.joined, { notes: [] });
    expect(await screen.findByText("No personal notes yet.")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Party" }));
    expect(await screen.findByText("No party notes yet.")).toBeTruthy();
  });

  it("adds a note with the campaign default, private or shared with the DM", async () => {
    const { user, backend } = openNotes(ids.joined, { notes: [] });
    await screen.findByText("No personal notes yet.");
    const add = screen.getByRole("button", { name: "Add note" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    const title = screen.getByRole("textbox", { name: /Note title/ });
    const text = screen.getByRole("textbox", { name: /Note text/ });

    await user.type(title, " Found a map ");
    await user.type(text, "## North{enter}Travel tomorrow");
    await user.click(add);
    expect(await findToast("Note added.")).toBeTruthy();
    // Campaign default: the sharing choice is left to the backend.
    expect(backend.callsTo(CREATE)[0]?.body).toEqual({
      kind: "character",
      title: "Found a map",
      markdown: "## North\nTravel tomorrow",
    });
    expect((title as HTMLInputElement).value).toBe("");

    await user.type(title, "Second");
    await chooseOption(addForm(), "DM sharing", "Private");
    await user.click(add);
    await waitFor(() => expect(backend.callsTo(CREATE)).toHaveLength(2));
    expect(backend.callsTo(CREATE)[1]?.body).toMatchObject({
      title: "Second",
      dm_readable: false,
    });

    await user.type(title, "Third");
    await chooseOption(addForm(), "DM sharing", "Share with DM");
    await user.click(add);
    await waitFor(() => expect(backend.callsTo(CREATE)).toHaveLength(3));
    expect(backend.callsTo(CREATE)[2]?.body).toMatchObject({
      title: "Third",
      dm_readable: true,
    });
  });

  it("lets the DM read shared notes and add only party notes", async () => {
    const { user, backend } = openNotes(ids.owned);
    const shared = await screen.findByRole("article", {
      name: "Rumour at the Gilded Eel",
    });
    expect(selected("Shared with you")).toBe("true");
    expect(screen.queryByRole("button", { name: "Add note" })).toBeNull();
    expect(within(shared).queryByText("Edit note")).toBeNull();
    expect(within(shared).queryByText("Private")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Party" }));
    await screen.findByRole("article", { name: "Plan for the cellar" });
    await user.type(
      within(addForm()).getByRole("textbox", { name: /Note title/ }),
      "House rules",
    );
    await user.click(screen.getByRole("button", { name: "Add note" }));
    await waitFor(() => expect(backend.callsTo(CREATE)).toHaveLength(1));
    expect(backend.callsTo(CREATE)[0]?.body).toEqual({
      kind: "party",
      title: "House rules",
      markdown: "",
    });
  });

  it("offers a player without a character nothing to add", async () => {
    const { user } = openNotes(ids.seatless, { notes: [] });
    expect(await screen.findByText("No personal notes yet.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add note" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Party" }));
    expect(await screen.findByText("No party notes yet.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add note" })).toBeNull();
  });

  it("shows text as written and only the entities this viewer may know", async () => {
    const markdown = '<img src=x onerror="alert(1)">\n\n[bad](javascript:x)';
    openNotes(ids.joined, {
      notes: [
        note({
          id: noteIds.map,
          title: "Trap",
          markdown,
          links: {
            entities: [{ id: noteIds.ferryman, name: "Mira", type: "npc" }],
            event_ids: ["opaque-event"],
          },
        }),
      ],
    });
    const article = await screen.findByRole("article", { name: "Trap" });
    expect(article.querySelector("p.note-text")?.textContent).toBe(markdown);
    expect(article.querySelector("img")).toBeNull();
    expect(article.querySelector("a")).toBeNull();
    const chips = within(article).getByRole("list", {
      name: "Linked entities",
    });
    expect(within(chips).getAllByRole("listitem")).toHaveLength(1);
    expect(within(chips).getByText("Mira")).toBeTruthy();
    expect(article.textContent).not.toContain("opaque-event");
  });

  it("offers editing only where allowed and sharing only to the author", async () => {
    const { user, backend } = openNotes(ids.joined, {
      notes: [
        note({ id: noteIds.map, title: "Read only", can_edit: false }),
        note({
          id: noteIds.rumour,
          title: "Shared discovery",
          is_mine: false,
          dm_readable: undefined,
        }),
        note({ id: noteIds.shared, title: "My discovery" }),
      ],
    });
    const readOnly = await screen.findByRole("article", { name: "Read only" });
    expect(within(readOnly).queryByText("Edit note")).toBeNull();
    expect(within(readOnly).getByText("Private")).toBeTruthy();

    const other = screen.getByRole("article", { name: "Shared discovery" });
    expect(within(other).queryByText("Private")).toBeNull();
    await user.click(within(other).getByText("Edit note"));
    expect(
      within(other).queryByRole("combobox", { name: "DM sharing" }),
    ).toBeNull();
    await user.click(
      within(other).getByRole("button", { name: "Save changes" }),
    );
    await waitFor(() => expect(backend.callsTo(PATCH)).toHaveLength(1));
    expect(backend.callsTo(PATCH)[0]?.params.note_id).toBe(noteIds.rumour);
    expect(backend.callsTo(PATCH)[0]?.body).toEqual({
      title: "Shared discovery",
      markdown: "",
    });

    const mine = screen.getByRole("article", { name: "My discovery" });
    await user.click(within(mine).getByText("Edit note"));
    const title = within(mine).getByRole("textbox", { name: /Note title/ });
    await user.clear(title);
    await user.type(title, "My find");
    await chooseOption(mine, "DM sharing", "Share with DM");
    await user.click(
      within(mine).getByRole("button", { name: "Save changes" }),
    );
    await waitFor(() => expect(backend.callsTo(PATCH)).toHaveLength(2));
    expect(backend.callsTo(PATCH)[1]?.body).toEqual({
      title: "My find",
      markdown: "",
      dm_readable: true,
    });
    expect(await findToast("Note saved.")).toBeTruthy();
    const saved = await screen.findByRole("article", { name: "My find" });
    expect(within(saved).getByText("Shared with DM")).toBeTruthy();
  });

  it("deletes a note after confirmation", async () => {
    const { user, backend } = openNotes();
    const article = await screen.findByRole("article", {
      name: "Rumour at the Gilded Eel",
    });
    await user.click(within(article).getByText("Edit note"));
    await user.click(
      within(article).getByRole("button", { name: "Delete note" }),
    );
    expect(backend.callsTo(DELETE)).toHaveLength(0);
    const dialog = await screen.findByRole("alertdialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Delete note" }),
    );
    expect(await findToast("Note deleted.")).toBeTruthy();
    expect(backend.callsTo(DELETE)[0]?.params.note_id).toBe(noteIds.rumour);
    await waitFor(() =>
      expect(
        screen.queryByRole("article", { name: "Rumour at the Gilded Eel" }),
      ).toBeNull(),
    );
  });

  it("shows the refusal the backend gave", async () => {
    const { user } = openNotes(ids.joined, {
      notes: [],
      routes: {
        [CREATE]: { status: 403, detail: "note creation not permitted" },
      },
    });
    await screen.findByText("No personal notes yet.");
    const title = screen.getByRole("textbox", { name: /Note title/ });
    await user.type(title, "Found a map");
    await user.click(screen.getByRole("button", { name: "Add note" }));
    expect(await findToast("note creation not permitted")).toBeTruthy();
    expect((title as HTMLInputElement).value).toBe("Found a map");
  });

  it("reports a failed read instead of an empty list", async () => {
    openNotes(ids.joined, {
      routes: { [LIST]: { status: 500, detail: "notes are down" } },
    });
    expect(await screen.findByText("Could not load these notes")).toBeTruthy();
    expect(screen.queryByText(/^No .* yet\.$/)).toBeNull();
  });

  it("opens the tab a linked note lives on", async () => {
    openNotes(ids.joined, { search: `?note=${noteIds.plan}` });
    expect(
      await screen.findByRole("article", { name: "Plan for the cellar" }),
    ).toBeTruthy();
    expect(selected("Party")).toBe("true");
  });

  it("says so when a linked note cannot be read", async () => {
    openNotes(ids.joined, {
      search: "?note=a9999999-9999-4999-8999-999999999999",
    });
    expect(await screen.findByText("Could not load that note")).toBeTruthy();
    expect(screen.getByText("note not found")).toBeTruthy();
  });

  it("refuses a campaign the viewer is not in", async () => {
    const { backend } = openNotes("77777777-7777-4777-8777-777777777777");
    expect(await screen.findByText("Campaign not found")).toBeTruthy();
    expect(backend.callsTo(LIST)).toHaveLength(0);
  });
});
